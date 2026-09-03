#!/usr/bin/env python3
"""条件单引擎: 一个脚本加载多个条件单, asyncio 并发执行.

条件单 = 轻量状态机 (每单独立盯一个标的):
  WATCH : 盯盘, 最新价相对昨收 gap <= trigger_gap_pct(负=低开) -> 买入 buy_qty, 进 ARMED
  ARMED : 盯盘, 最新价 >= 买入价*(1+sell_rally_pct)            -> 卖出该单数量, 进 DONE
  DONE  : 结束 (同时记录最终盈亏)

通用性: trigger_gap_pct 为负表示"低开/深跌买入", 配 sell_rally_pct>0 表示"反弹卖出";
想改成"低开4% 买 1000 股, 反弹 1% 卖"就配:
  {'symbol':'601899','trigger_gap_pct':-4.0,'buy_qty':1000,'sell_rally_pct':1.0}

交易时段判定 (A股 09:30-11:30 / 13:00-15:00), 非交易时段不判定但保持循环, 开盘自动恢复.
行情: 复用 trader.fetch_intraday_1m -> (df, pre_close), latest = 最后一根 close.

执行: 默认 模拟成交(SimulatedBroker, 不碰同花顺); --live 走 LiveBroker 真实下单 ths_trade.
下单记账统一走 runtime 层 (Portfolio + broker + ctx.submit_order), 与实盘/回测同口径.

Web 管理: backend/main.py 的 /api/conditions 端点在 FastAPI 事件循环上跑同一个
ConditionEngine (动态启停/增删单), 订单配置+运行时状态持久化到
backend/condition_orders.json, CLI 与 Web 共用同一份配置.

用法:
  uv run python trading/condition_orders.py            # 模拟 (安全)
  uv run python trading/condition_orders.py --live      # 真实下单 (同花顺)
  uv run python trading/condition_orders.py --poll 5    # 行情刷新间隔秒
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from strategy.runtime.broker import LiveBroker, SimulatedBroker  # noqa: E402
from strategy.runtime.ctx import Context                          # noqa: E402
from strategy.runtime.portfolio import Portfolio                  # noqa: E402
from strategy import fdata_client                                 # noqa: E402


# ================================================================ 条件单定义 ====
# 在此添加不同条件单; 每个单用一个 symbol (同标的多个单会对同一持仓交叉干扰, 暂不推荐).
CONDITION_ORDERS = [
    {
        "id": "co_601899_gap",
        "symbol": "601899",       # 例: 紫金矿业
        "trigger_gap_pct": -4.0,  # 最新价相对昨收跌幅超过 4% -> 触发买入
        "buy_qty": 1000,          # 买入 1000 股
        "sell_rally_pct": 1.0,    # 反弹达到买入价 +1% -> 卖出
    },
]


# ================================================================ 配置持久化 ====
# Web 端 (backend/conditions.py) 与 CLI 共用: 存在则优先, 否则用上面的硬编码默认单
STORE_PATH = REPO_ROOT / "backend" / "condition_orders.json"


def load_orders_cfg() -> list[dict]:
    """读订单配置 (含运行时状态字段). 文件损坏/缺失时回退到 CONDITION_ORDERS."""
    if STORE_PATH.exists():
        try:
            data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return [dict(c) for c in CONDITION_ORDERS]


def save_orders_cfg(orders: list[dict]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(
        json.dumps(orders, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def is_market_open(t: dt.datetime) -> bool:
    if t.weekday() >= 5:
        return False
    tt = t.time()
    return (dt.time(9, 30) <= tt <= dt.time(11, 30)) or \
           (dt.time(13, 0) <= tt <= dt.time(15, 0))


@dataclass
class ConditionOrder:
    id: str
    symbol: str
    trigger_gap_pct: float
    buy_qty: int
    sell_rally_pct: float
    state: str = "WATCH"            # WATCH / ARMED / DONE
    open_window_min: int = 3        # 仅开盘后前 N 分钟内判定"跌破触发"买入; 超时当日不再买
    day: str = ""                   # 当前判定所属交易日 (用于跨日重置 buy_locked)
    buy_locked: bool = False        # 当日开盘窗口已过, 不能再买入
    buy_order_id: str = ""          # 买入订单 id (查询成交/被拒用)
    buy_price: float = 0.0
    buy_ts: str = ""
    sell_price: float = 0.0
    sell_ts: str = ""
    last_gap_pct: float = 0.0
    last_price: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


class ConditionEngine:
    """加载多个条件单, 各自异步盯盘; 共享一个 Portfolio + Broker (并发安全).

    支持动态管理 (Web 端用): start_all/stop_all/add_order/remove_order,
    订单协程在当前事件循环上以 Task 运行; on_change 回调在订单状态变更时触发
    (由调用方落盘, 实现重启恢复).
    """

    def __init__(self, orders_cfg: list[dict], *, live: bool, cash: float,
                 poll_seconds: float, on_change: Optional[Callable[[], None]] = None):
        self.poll_seconds = poll_seconds
        # 共享账户: 一个 Portfolio 管理所有标的持仓, 一个 broker (模拟或实盘)
        # load: 复用 strategy/state/condition_orders.state.json, 重启恢复资金/持仓
        self.pf = Portfolio.load("condition_orders", cash)
        self.broker = LiveBroker(self.pf) if live else SimulatedBroker(self.pf)
        # 允许从配置恢复运行时状态 (state/buy_price/...), CLI 默认单无这些字段
        self.orders = [ConditionOrder(**{k: v for k, v in c.items()
                                         if k in ConditionOrder.__dataclass_fields__})
                       for c in orders_cfg]  # type: ignore[arg-type]
        # 每个条件单独立 ctx (symbol/price 私有, 避免并发协程互踩); pf/broker 共享
        self.ctxs = {o.id: Context(symbol=o.symbol, params={}, portfolio=self.pf,
                                   broker=self.broker, dry_run=not live)
                     for o in self.orders}
        self._live = live
        self.on_change = on_change
        self.logs: deque[str] = deque(maxlen=300)   # 环形日志, Web 端展示用
        self._tasks: dict[str, asyncio.Task] = {}

    # ---------- 日志 / 状态变更通知 ----------
    def _log(self, tag: str, msg: str) -> None:
        line = f"{dt.datetime.now():%H:%M:%S} {tag} {msg}"
        self.logs.append(line)
        print(line, flush=True)

    def _touch(self) -> None:
        """订单状态变更 -> 通知调用方持久化 (on_change 异常不影响盯盘)."""
        if self.on_change is None:
            return
        try:
            self.on_change()
        except Exception as e:  # noqa: BLE001
            self._log("[engine]", f"on_change 持久化失败: {e!r}")

    # ---------- 动态任务管理 ----------
    def _spawn(self, co: ConditionOrder) -> None:
        t = self._tasks.get(co.id)
        if t is not None and not t.done():
            return
        self._tasks[co.id] = asyncio.ensure_future(self._run_one(co))

    def start_all(self) -> None:
        """为每个订单启动独立盯盘协程 (DONE 状态的单也启动, 协程内立即退出)."""
        for co in self.orders:
            self._spawn(co)

    def stop_all(self) -> None:
        for t in self._tasks.values():
            if not t.done():
                t.cancel()
        self._tasks.clear()

    @property
    def is_running(self) -> bool:
        return any(not t.done() for t in self._tasks.values())

    def add_order(self, cfg: dict) -> str:
        """运行中动态加单: 建单 + ctx + 启动协程. 返回订单 id."""
        clean = {k: v for k, v in cfg.items()
                 if k in ConditionOrder.__dataclass_fields__}
        co = ConditionOrder(**clean)  # type: ignore[arg-type]
        self.orders.append(co)
        self.ctxs[co.id] = Context(symbol=co.symbol, params={},
                                   portfolio=self.pf, broker=self.broker,
                                   dry_run=not self._live)
        self._spawn(co)
        self._log("[engine]", f"动态加单 {co.id} ({co.symbol})")
        self._touch()
        return co.id

    def remove_order(self, co_id: str) -> bool:
        co = next((o for o in self.orders if o.id == co_id), None)
        if co is None:
            return False
        t = self._tasks.pop(co_id, None)
        if t is not None and not t.done():
            t.cancel()
        self.orders.remove(co)
        self.ctxs.pop(co_id, None)
        self._log("[engine]", f"动态删单 {co_id}")
        self._touch()
        return True

    # ---------- 快照 ----------
    def export_configs(self) -> list[dict]:
        """全量订单状态 -> 可持久化 dict 列表 (重启恢复用)."""
        return [co.to_dict() for co in self.orders]

    def snapshot(self) -> dict:
        return {
            "live": self._live,
            "poll_seconds": self.poll_seconds,
            "orders": self.export_configs(),
            "portfolio": self.pf.snapshot(),
        }

    async def _fetch_quote(self, symbol: str):
        """拉最新价 + 昨收 (统一走 fdata serve 长连接, 失败 CLI 回退)."""
        def _q() -> tuple:
            q = fdata_client.quote(symbol)
            if q and q.get("last") and q.get("pre_close"):
                return float(q["last"]), float(q["pre_close"])
            return None, None

        return await asyncio.to_thread(_q)

    async def _run_one(self, co: ConditionOrder) -> None:
        tag = f"[{co.id}]"
        ctx = self.ctxs[co.id]
        while True:
            try:
                latest, pre_close = await self._fetch_quote(co.symbol)
                if latest is None or pre_close is None or pre_close <= 0:
                    await asyncio.sleep(self.poll_seconds)
                    continue
                co.last_price = latest
                co.last_gap_pct = (latest - pre_close) / pre_close * 100.0
                ctx.set_price(latest)

                if not is_market_open(dt.datetime.now()):
                    await asyncio.sleep(self.poll_seconds)
                    continue

                if co.state == "WATCH":
                    now = dt.datetime.now()
                    today = now.date().isoformat()
                    if co.day != today:
                        co.day = today          # 新交易日: 重置当日锁
                        co.buy_locked = False
                    # 买入窗口: 仅开盘后前 open_window_min 分钟内判定"跌破触发"
                    opened = dt.datetime.combine(now.date(), dt.time(9, 30))
                    elapsed_min = (now - opened).total_seconds() / 60.0
                    if elapsed_min > co.open_window_min:
                        if not co.buy_locked:
                            co.buy_locked = True
                            self._log(tag, f"开盘窗口 {co.open_window_min} 分钟已过, "
                                      f"本单今日不再买入")
                            self._touch()
                        # 窗口已过 -> 当日不判定买入 (等待下一交易日回落位)
                        await asyncio.sleep(self.poll_seconds)
                        continue
                    if co.buy_locked:
                        await asyncio.sleep(self.poll_seconds)
                        continue
                    if co.last_gap_pct <= co.trigger_gap_pct:
                        # 用 ctx 下单
                        oid = await ctx.submit_order(
                            side="buy", qty=co.buy_qty, price=latest)
                        co.state = "ARMED"
                        co.buy_price = latest
                        co.buy_order_id = oid
                        co.buy_ts = dt.datetime.now().isoformat(timespec="seconds")
                        self.pf.save()
                        self._log(tag, f"触发: gap={co.last_gap_pct:.2f}% "
                                  f"<= {co.trigger_gap_pct}% 买入 {co.buy_qty}@{latest:.3f} "
                                  f"(order={oid})")
                        self._touch()
                    else:
                        self._log(tag, f"开盘窗口盯盘 "
                                  f"gap={co.last_gap_pct:+.2f}% ...")

                elif co.state == "ARMED":
                    # 买入尚未成交(持仓未到位, 如 live 异步 poll) -> 先等待, 不判卖出
                    if ctx.position <= 0:
                        o = self.pf.orders.get(co.buy_order_id) if co.buy_order_id else None
                        if o and o.status in ("rejected", "cancelled"):
                            co.state = "DONE"
                            co.error = o.error or o.status
                            self._log(tag, f"买入{('被拒' if o.status=='rejected' else '撤销')}: "
                                  f"{o.error} | 订单状态 {o.status}, 该单结束")
                            self._touch()
                            return
                        self._log(tag, f"等待买入成交... 持仓={ctx.position}")
                        await asyncio.sleep(max(self.poll_seconds, 3))
                        continue
                    target = co.buy_price * (1 + co.sell_rally_pct / 100.0)
                    if latest >= target:
                        sell_qty = min(co.buy_qty, ctx.position)
                        if sell_qty > 0:
                            oid = await ctx.submit_order(
                                side="sell", qty=sell_qty, price=latest)
                        co.state = "DONE"
                        co.sell_price = latest
                        co.sell_ts = dt.datetime.now().isoformat(timespec="seconds")
                        pnl = (latest - co.buy_price) * sell_qty
                        self.pf.save()
                        self._log(tag, f"反弹达成: {latest:.3f} >= "
                                  f"{target:.3f} 卖出 {sell_qty}@{latest:.3f} "
                                  f"(order={oid}, 盈亏≈{pnl:+.2f})")
                        self._touch()
                    else:
                        self._log(tag, f"等反弹: {latest:.3f} / 目标 {target:.3f}")

                elif co.state == "DONE":
                    return  # 该单完成, 退出其盯盘协程

            except Exception as e:  # noqa: BLE001
                co.error = repr(e)
                self._log(tag, f"error: {e!r}")
                self._touch()
            await asyncio.sleep(self.poll_seconds)

    async def run(self) -> None:
        """CLI 入口: 启动全部订单协程并等待完成 (Web 端用 start_all, 不走这里)."""
        self._log("[engine]", f"条件单引擎启动: live={self._live} "
                  f"poll={self.poll_seconds}s 订单数={len(self.orders)}")
        self.start_all()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        for co in self.orders:
            print(f"{co.id}: state={co.state} buy={co.buy_price} "
                  f"sell={co.sell_price}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="条件单引擎")
    ap.add_argument("--live", action="store_true", help="真实下单(同花顺), 默认模拟")
    ap.add_argument("--cash", type=float, default=100_000.0, help="初始资金")
    ap.add_argument("--poll", type=float, default=5.0, help="行情刷新间隔(秒)")
    args = ap.parse_args()

    # 与 Web 端共用 backend/condition_orders.json (存在则用), 否则回退硬编码默认单
    orders_cfg = load_orders_cfg()
    src = "backend/condition_orders.json" if STORE_PATH.exists() else "内置 CONDITION_ORDERS"
    print(f"订单配置来源: {src} ({len(orders_cfg)} 单)", flush=True)
    eng = ConditionEngine(orders_cfg, live=args.live,
                          cash=args.cash, poll_seconds=args.poll)
    try:
        asyncio.run(eng.run())
    except KeyboardInterrupt:
        print("\n已停止。快照:", flush=True)
        print(eng.pf.snapshot(), flush=True)


if __name__ == "__main__":
    main()
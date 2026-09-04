#!/usr/bin/env python3
"""选股自动交易引擎: 多策略组并行, 选股 -> 买入入库(买入组) -> 卖出扫描 -> 卖出指令.

与条件单/网格引擎同构 (asyncio 每组一个任务, 可跑在 FastAPI 事件循环上):
- 策略组 (PickerGroup): 策略ID + 选股插件 + 股票池 + 参数 + 仓位约束, 配置存 SQLite
  (backend/stockpicker.db, backend/picker_db.py).
- 买入: 周期性跑插件 select() 扫描股票池 -> 候选去重(已持有)/限仓(max_positions)
  -> broker 下单 -> 成交后写 picker_positions (strategy_id 挂钩), 即该策略的"买入组".
- 卖出: 每轮对买入组持仓跑插件 sell_reason() -> 命中即发卖出指令 -> 成交后平仓记录.
- 独立性: 每个策略组独立 Portfolio (strategy/state/stockpicker_<id>.state.json)
  + 独立协程, 互不影响, 可同时运行多个策略组; 同一插件可挂多个组 (参数按组覆盖).

执行: 默认 SimulatedBroker 模拟成交 (不碰同花顺); live=True 走 LiveBroker 真实下单
ths_trade (成交回报异步轮询, 引擎按订单状态推进 holding/selling, 买卖均支持).
实盘默认 T+1 保护: 当日买入批次当日不卖 (t1_protect 可关).

Web 管理: backend/stockpicker.py 在 FastAPI 事件循环上跑本引擎 (动态增删组/启停),
CLI (本文件 main) 与 Web 共用同一份 SQLite 配置.

用法:
  uv run python trading/stock_picker.py                  # 模拟 (安全), 常驻扫描
  uv run python trading/stock_picker.py --live           # 真实下单 (同花顺)
  uv run python trading/stock_picker.py --once           # 手动跑一轮扫描 (无视盘中时段)
  uv run python trading/stock_picker.py --once --id sp_x # 只跑指定策略组一轮
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend import picker_db                                   # noqa: E402
from strategy.runtime.broker import Broker, LiveBroker, SimulatedBroker  # noqa: E402
from strategy.runtime.ctx import Context                        # noqa: E402
from strategy.runtime.portfolio import Portfolio                # noqa: E402
from strategy import fdata_client                               # noqa: E402
from trading.condition_orders import is_market_open             # noqa: E402
from trading.picker_strategies import registry                  # noqa: E402
from trading.picker_strategies.base import PickStrategy         # noqa: E402

LOT = 100
BARS_TTL = 60.0          # 日 K 缓存秒数 (卖出扫描每轮都要指标, 不必每次重拉)


@dataclass
class PickerGroup:
    """策略组: 配置 (落 SQLite) + 运行时状态 (内存)."""
    strategy_id: str
    picker: str                    # 选股插件 ID (picker_strategies registry)
    title: str = ""
    universe: list[str] = field(default_factory=list)
    params: dict = field(default_factory=dict)
    per_qty: int = 0               # 每只买入股数 (0=按 cash_per_symbol 自动整手)
    cash_per_symbol: float = 10_000.0
    max_positions: int = 0         # 买入组最大持仓只数 (0=不限)
    buy_scan_every: int = 60       # 每 N 轮 poll 跑一次买入选股 (卖出每轮都扫)
    t1_protect: bool = True
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""
    # ---- 运行时 (不落库) ----
    rounds: int = 0
    last_buy_scan: str = ""
    last_sell_scan: str = ""
    last_error: str = ""

    @classmethod
    def from_cfg(cls, cfg: dict) -> "PickerGroup":
        known = {k for k in cls.__dataclass_fields__ if not k.startswith("_")}
        clean = {k: v for k, v in cfg.items() if k in known}
        # SQLite 出来的布尔列是 0/1
        for k in ("t1_protect", "enabled"):
            if k in clean:
                clean[k] = bool(clean[k])
        return cls(**clean)

    def config_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__
                if k in ("strategy_id", "picker", "title", "universe", "params",
                         "per_qty", "cash_per_symbol", "max_positions",
                         "buy_scan_every", "t1_protect", "enabled",
                         "created_at", "updated_at")}


class StockPickerEngine:
    """加载多个策略组, 各自异步"选股-买入-卖出"循环; 组间完全独立.

    支持动态管理 (Web 端用): start_all/stop_all/add_group/update_group/
    remove_group/run_once. 持仓与指令流水实时写 SQLite (买入组即
    picker_positions 里同 strategy_id 的 holding 行), 无需额外落盘回调.
    """

    def __init__(self, groups_cfg: list[dict], *, live: bool = False,
                 cash: float = 100_000.0, poll_seconds: float = 5.0,
                 pickers: Optional[dict[str, PickStrategy]] = None,
                 db=picker_db):
        self.poll_seconds = poll_seconds
        self._live = live
        self.cash = cash
        self.db = db
        # 允许注入插件表 (测试用); 默认零注册自动发现
        self.pickers: dict[str, PickStrategy] = (
            dict(pickers) if pickers is not None else registry.discover())
        self.logs: deque[str] = deque(maxlen=300)   # 环形日志, Web 端展示用
        self.groups: dict[str, PickerGroup] = {}
        self.pfs: dict[str, Portfolio] = {}
        self.brokers: dict[str, Broker] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._started = False            # start_all 后为 True; 未启动时 add/update 不建协程
        self._group_locks: dict[str, asyncio.Lock] = {}
        # gid -> [{code,name,qty,price,reason,order_id}] 实盘买入已提交未回报
        self._pending_buys: dict[str, list[dict]] = {}
        self._quotes: dict[str, dict[str, float]] = {}          # gid -> {code: last}
        self._bars_cache: dict[str, tuple[float, list[dict]]] = {}  # code:limit -> (ts, bars)
        # gid -> {code}: 本轮已卖出/在卖的标的, 同轮买入扫描跳过 (防卖出后立即回购换手)
        self._sold_this_round: dict[str, set[str]] = {}
        for cfg in groups_cfg:
            self._register_group(PickerGroup.from_cfg(cfg))

    # ---------------------------------------------------------------- 日志 ----
    def _log(self, gid: str, msg: str) -> None:
        line = f"{dt.datetime.now():%H:%M:%S} [{gid}] {msg}"
        self.logs.append(line)
        print(line, flush=True)

    def _merged_params(self, g: PickerGroup) -> dict:
        """插件参数默认值 <- 策略组 params 覆盖."""
        strat = self.pickers.get(g.picker)
        base = ({k: v.get("default") for k, v in strat.PARAMS.items()}
                if strat else {})
        base.update(g.params or {})
        return base

    # ------------------------------------------------------- 组生命周期 ----
    def _register_group(self, g: PickerGroup) -> None:
        """登记组: 独立 Portfolio + Broker (组间隔离的关键). 插件缺失只标记错误,
        不阻断登记 (资金/持仓照常可见, 补上插件 update 后即可恢复扫描)."""
        self.groups[g.strategy_id] = g
        # load: 复用 strategy/state/stockpicker_<id>.state.json, 重启恢复资金/持仓
        pf = Portfolio.load(f"stockpicker_{g.strategy_id}", self.cash)
        self.pfs[g.strategy_id] = pf
        self.brokers[g.strategy_id] = LiveBroker(pf) if self._live else SimulatedBroker(pf)
        self._group_locks[g.strategy_id] = asyncio.Lock()
        self._pending_buys.setdefault(g.strategy_id, [])
        if g.picker not in self.pickers:
            g.last_error = f"选股插件不存在: {g.picker} (可选: {list(self.pickers)})"
            self._log(g.strategy_id, g.last_error)

    def _spawn(self, g: PickerGroup) -> None:
        t = self._tasks.get(g.strategy_id)
        if t is not None and not t.done():
            return
        if g.picker not in self.pickers:
            return                                  # 插件缺失, 不起协程
        self._tasks[g.strategy_id] = asyncio.ensure_future(self._run_group(g))
    def start_all(self) -> None:
        self._started = True
        for g in self.groups.values():
            if g.enabled:
                self._spawn(g)

    def stop_all(self) -> None:
        self._started = False
        for t in self._tasks.values():
            if not t.done():
                t.cancel()
        self._tasks.clear()

    @property
    def is_running(self) -> bool:
        return any(not t.done() for t in self._tasks.values())

    def add_group(self, cfg: dict) -> str:
        """运行中动态加组: 登记 + (enabled 则) 起协程. 返回策略 ID."""
        g = PickerGroup.from_cfg(cfg)
        if g.strategy_id in self.groups:
            raise ValueError(f"策略 ID {g.strategy_id} 已存在")
        self._register_group(g)
        if self._started and g.enabled:
            self._spawn(g)
        self._log(g.strategy_id, f"动态加组 picker={g.picker} "
                  f"universe={len(g.universe)}只 max_positions={g.max_positions or '不限'}")
        return g.strategy_id

    def update_group(self, gid: str, patch: dict) -> Optional[PickerGroup]:
        """更新组配置 (参数/启停/仓位约束), 运行中即时生效 (启停对应协程)."""
        g = self.groups.get(gid)
        if g is None:
            return None
        for k, v in patch.items():
            if hasattr(g, k) and not k.startswith("_"):
                setattr(g, k, v)
        if gid in self._tasks and (not g.enabled or g.picker not in self.pickers):
            t = self._tasks.pop(gid)
            if not t.done():
                t.cancel()
        elif self._started and gid not in self._tasks and g.enabled \
                and g.picker in self.pickers:
            self._spawn(g)
        return g

    def remove_group(self, gid: str) -> bool:
        """停止并摘除组 (持仓/流水保留在 SQLite 供审计)."""
        t = self._tasks.pop(gid, None)
        if t is not None and not t.done():
            t.cancel()
        removed = self.groups.pop(gid, None) is not None
        self.pfs.pop(gid, None)
        self.brokers.pop(gid, None)
        self._pending_buys.pop(gid, None)
        self._quotes.pop(gid, None)
        self._group_locks.pop(gid, None)
        if removed:
            self._log(gid, "动态删组 (历史持仓与流水已保留)")
        return removed

    # ---------------------------------------------------------------- 快照 ----
    def snapshot(self) -> dict:
        groups_out = []
        for g in self.groups.values():
            gid = g.strategy_id
            quotes = self._quotes.get(gid, {})
            holdings = self.db.list_positions(strategy_id=gid, status="holding")
            selling = self.db.list_positions(strategy_id=gid, status="selling")
            for p in holdings + selling:
                last = quotes.get(p["code"]) or 0.0
                p["last_price"] = last
                p["pnl_pct"] = (round((last - p["buy_price"]) / p["buy_price"] * 100, 2)
                                if last > 0 and p["buy_price"] > 0 else None)
            strat = self.pickers.get(g.picker)
            groups_out.append({
                **g.config_dict(),
                "picker_title": getattr(strat, "TITLE", g.picker),
                "running": gid in self._tasks and not self._tasks[gid].done(),
                "rounds": g.rounds,
                "last_buy_scan": g.last_buy_scan,
                "last_sell_scan": g.last_sell_scan,
                "last_error": g.last_error,
                "holdings": holdings,       # 买入组 (含浮动盈亏)
                "selling": selling,         # 实盘卖出已提交未回报
                "pending_buys": list(self._pending_buys.get(gid, [])),
            })
        return {
            "live": self._live,
            "poll_seconds": self.poll_seconds,
            "groups": groups_out,
            "portfolios": {gid: pf.snapshot() for gid, pf in self.pfs.items()},
            "events": self.db.list_events(limit=100),
            "logs": list(self.logs)[-80:],
        }

    # ---------------------------------------------------------------- 数据 ----
    async def _fetch_quote(self, code: str) -> Optional[dict]:
        """实时快照 (统一走 fdata serve 长连接, 失败返回 None 本轮跳过)."""
        def _q() -> Optional[dict]:
            try:
                return fdata_client.quote(code)
            except Exception:  # noqa: BLE001
                return None
        return await asyncio.to_thread(_q)

    async def _fetch_bars(self, code: str, limit: int) -> list[dict]:
        """日 K (带 TTL 缓存): 卖出判定每轮要指标, 不必每轮重拉."""
        key = f"{code}:{limit}"
        ts, bars = self._bars_cache.get(key, (0.0, []))
        if time.time() - ts > BARS_TTL:
            def _k() -> list[dict]:
                try:
                    return fdata_client.kline(code, "day", "stock", None, limit)
                except Exception:  # noqa: BLE001
                    return []
            bars = await asyncio.to_thread(_k)
            self._bars_cache[key] = (time.time(), bars)
        return bars

    def _make_ctx(self, g: PickerGroup, code: str, price: float) -> Context:
        """临时 Context: symbol/price 私有, Portfolio/Broker 组内共享."""
        ctx = Context(symbol=code, params={}, portfolio=self.pfs[g.strategy_id],
                      broker=self.brokers[g.strategy_id], dry_run=not self._live)
        ctx.set_price(price)
        return ctx

    # ---------------------------------------------------------------- 主循环 ----
    async def _run_group(self, g: PickerGroup) -> None:
        while True:
            try:
                if g.enabled:
                    await self._round(g, force=False)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                g.last_error = repr(e)
                self._log(g.strategy_id, f"error: {e!r}")
            await asyncio.sleep(self.poll_seconds)

    async def _round(self, g: PickerGroup, force: bool = False) -> None:
        """一轮扫描: 先卖 (保护持仓) 后买. force=True 无视盘中时段 (手动 run-once).

        组内串行锁: 常驻扫描协程与 Web run-once 并发触发同一组时排队执行,
        防止两路扫描交叉下单/重复入库.
        """
        lock = self._group_locks.setdefault(g.strategy_id, asyncio.Lock())
        async with lock:
            now = dt.datetime.now()
            if not force and not is_market_open(now):
                return
            g.rounds += 1
            g.last_sell_scan = now.isoformat(timespec="seconds")
            self._sold_this_round[g.strategy_id] = set()
            await self._scan_sell(g)
            if force or g.rounds == 1 or (g.buy_scan_every > 0
                                          and g.rounds % g.buy_scan_every == 0):
                g.last_buy_scan = dt.datetime.now().isoformat(timespec="seconds")
                await self._scan_buy(g)

    # ------------------------------------------------------------- 卖出扫描 ----
    async def _scan_sell(self, g: PickerGroup) -> None:
        gid = g.strategy_id
        # 1) 推进实盘已提交未回报的卖单 (selling 状态)
        for pos in self.db.list_positions(strategy_id=gid, status="selling"):
            o = self.pfs[gid].orders.get(pos.get("sell_order_id") or "")
            if o is None:
                self.db.revert_selling(pos["id"])       # 异常兜底: 回 holding
                continue
            if o.status == "filled":
                self._close_sell(g, pos, o.avg_fill_price or pos["buy_price"],
                                 o.order_id, pos.get("sell_reason") or "实盘回报成交")
                self.pfs[gid].save()
            elif o.status in ("rejected", "cancelled"):
                self.db.revert_selling(pos["id"])
                self.db.add_event(gid, pos["code"], "sell", pos["qty"], 0.0,
                                  o.order_id, dry_run=not self._live,
                                  status=o.status, detail=o.error)
                self._log(gid, f"卖出{o.status}: {pos['code']} {o.error} -> 恢复持有")
        # 2) 逐持仓判卖出条件
        strat = self.pickers.get(g.picker)
        if strat is None:
            return
        params = self._merged_params(g)
        today = dt.date.today().isoformat()
        for pos in self.db.list_positions(strategy_id=gid, status="holding"):
            code = pos["code"]
            if g.t1_protect and str(pos.get("buy_ts", ""))[:10] == today:
                self._log(gid, f"T+1 保护: {code} 今日买入, 当日不卖")
                continue
            quote = await self._fetch_quote(code)
            last = float((quote or {}).get("last") or 0)
            if last <= 0:
                continue
            self._quotes.setdefault(gid, {})[code] = last
            bars = await self._fetch_bars(code, int(params.get("kline_limit") or 60))
            try:
                reason = strat.sell_reason(pos, quote, bars, params) or ""
            except Exception as e:  # noqa: BLE001 插件异常不中断扫描
                self._log(gid, f"sell_reason 异常 {code}: {e!r}")
                continue
            if not reason:
                continue
            qty = int(pos["qty"])
            if qty <= 0:
                continue
            ctx = self._make_ctx(g, code, last)
            oid = await ctx.submit_order("sell", qty, price=last)
            self.pfs[gid].save()
            o = self.pfs[gid].orders.get(oid)
            status = o.status if o else ""
            if status == "filled":                      # 模拟/dry-run: 立即成交
                self._close_sell(g, pos, o.avg_fill_price or last, oid, reason)
            elif status == "rejected":
                self.db.add_event(gid, code, "sell", qty, last, oid,
                                  dry_run=not self._live, status="rejected",
                                  detail=o.error)
                self._log(gid, f"卖出被拒 {code}: {o.error} ({reason})")
            else:                                       # 实盘已提交, 等异步回报
                self.db.mark_selling(pos["id"], oid, reason)
                self.db.add_event(gid, code, "sell", qty, last, oid,
                                  dry_run=not self._live, status="submitted",
                                  detail=reason)
                self._log(gid, f"卖出指令已提交 {code} {qty}@{last:.3f} "
                          f"(order={oid}, {reason})")

    def _close_sell(self, g: PickerGroup, pos: dict, price: float,
                    oid: str, reason: str) -> None:
        """卖出成交 -> 移出买入组 (sold 留痕) + 流水 + 日志."""
        pnl = (price - float(pos["buy_price"])) * int(pos["qty"])
        self.db.close_position(pos["id"], price,
                               dt.datetime.now().isoformat(timespec="seconds"),
                               oid, reason)
        self.db.add_event(g.strategy_id, pos["code"], "sell", int(pos["qty"]),
                          price, oid, dry_run=not self._live, status="filled",
                          detail=reason)
        self._sold_this_round.setdefault(g.strategy_id, set()).add(pos["code"])
        self._log(g.strategy_id, f"卖出成交 {pos['code']} {pos['qty']}@{price:.3f} "
                  f"(order={oid}, {reason}, 盈亏≈{pnl:+.2f})")

    # ------------------------------------------------------------- 买入扫描 ----
    async def _scan_buy(self, g: PickerGroup) -> None:
        gid = g.strategy_id
        pf = self.pfs[gid]
        # 1) 推进实盘已提交未回报的买单 (LiveBroker 轮询超时会转 rejected, 自然回收)
        pending = self._pending_buys.setdefault(gid, [])
        for pb in list(pending):
            o = pf.orders.get(pb["order_id"])
            if o is None:
                pending.remove(pb)
            elif o.status == "filled":
                pending.remove(pb)
                self._record_buy(g, pb, o.avg_fill_price or pb["price"],
                                 o.filled_qty or pb["qty"])
            elif o.status in ("rejected", "cancelled"):
                pending.remove(pb)
                self.db.add_event(gid, pb["code"], "buy", pb["qty"], pb["price"],
                                  pb["order_id"], dry_run=not self._live,
                                  status=o.status, detail=o.error)
                self._log(gid, f"买入{o.status}: {pb['code']} {o.error}")
        # 2) 选股
        strat = self.pickers.get(g.picker)
        if strat is None:
            return
        params = self._merged_params(g)
        try:
            candidates = await strat.select(g.universe, params)
        except Exception as e:  # noqa: BLE001
            g.last_error = f"选股异常: {e!r}"
            self._log(gid, g.last_error)
            return
        if not candidates:
            self._log(gid, "选股: 本轮无满足买入条件的标的")
            return
        # 3) 过滤 (已持有/在途/限仓) -> 下单 -> 成交入库
        holding = self.db.list_positions(strategy_id=gid, status="holding")
        selling = self.db.list_positions(strategy_id=gid, status="selling")
        held = ({p["code"] for p in holding} | {p["code"] for p in selling}
                | {pb["code"] for pb in pending}
                | self._sold_this_round.get(gid, set()))   # 当轮已卖出不回补
        n_active = len(holding) + len(selling) + len(pending)
        slots = (g.max_positions - n_active) if g.max_positions > 0 else len(candidates)
        for cand in candidates:
            if slots <= 0:
                self._log(gid, f"买入组已满 ({n_active}/{g.max_positions or '∞'}), "
                          f"跳过 {cand.code} 等候选")
                break
            if cand.code in held:
                continue
            # 实时快照补全: 用最新价下单 (K线收盘价可能陈旧) + 补股票名称
            # (fdata kline 无 name 字段, 名称只能来自 quote)
            quote = await self._fetch_quote(cand.code)
            last = float((quote or {}).get("last") or 0)
            px = last if last > 0 else float(cand.price)
            if px <= 0:
                self._log(gid, f"无有效报价, 跳过 {cand.code}")
                continue
            name = str((quote or {}).get("name") or cand.name or "")
            qty = g.per_qty or self._auto_qty(g, px)
            # 现金约束: 固定股数/自动整手都不得超过组内可用资金 (防现金负数/实盘废单)
            affordable = int(max(pf.cash, 0.0) // px // LOT * LOT)
            if qty > affordable:
                if affordable <= 0:
                    self._log(gid, f"组内现金不足, 跳过 {cand.code} @{px:.3f}")
                    continue
                self._log(gid, f"组内现金不足, {cand.code} 数量 {qty} 下调为 {affordable}")
                qty = affordable
            if qty <= 0:
                self._log(gid, f"现金不足/整手为0, 跳过 {cand.code} @{px:.3f}")
                continue
            ctx = self._make_ctx(g, cand.code, px)
            oid = await ctx.submit_order("buy", qty, price=px)
            self.pfs[gid].save()
            o = pf.orders.get(oid)
            status = o.status if o else ""
            pb = {"code": cand.code, "name": name, "qty": qty,
                  "price": px, "reason": cand.reason, "order_id": oid,
                  "ts": dt.datetime.now().isoformat(timespec="seconds")}
            if status == "filled":                      # 模拟/dry-run: 立即成交
                self._record_buy(g, pb, o.avg_fill_price or px, qty)
            elif status == "rejected":
                self.db.add_event(gid, cand.code, "buy", qty, px, oid,
                                  dry_run=not self._live, status="rejected",
                                  detail=o.error)
                self._log(gid, f"买入被拒 {cand.code}: {o.error}")
            else:                                       # 实盘已提交, 等异步回报
                pending.append(pb)
                self.db.add_event(gid, cand.code, "buy", qty, px, oid,
                                  dry_run=not self._live, status="submitted",
                                  detail=cand.reason)
                self._log(gid, f"买入指令已提交 {cand.code} {qty}@{px:.3f} "
                          f"(order={oid}, {cand.reason})")
            slots -= 1

    def _auto_qty(self, g: PickerGroup, price: float) -> int:
        """per_qty=0 时按组内现金预算自动整手 (不超组内剩余现金)."""
        if price <= 0:
            return 0
        pf = self.pfs[g.strategy_id]
        budget = min(g.cash_per_symbol, max(pf.cash, 0.0))
        return int(budget // price // LOT * LOT)

    def _record_buy(self, g: PickerGroup, pb: dict, price: float, qty: int) -> None:
        """买入成交 -> 计入该策略 ID 的买入组 (SQLite) + 流水 + 日志."""
        try:
            self.db.insert_position(
                strategy_id=g.strategy_id, code=pb["code"], name=pb.get("name", ""),
                qty=qty, buy_price=price, buy_ts=dt.datetime.now().isoformat(
                    timespec="seconds"),
                buy_order_id=pb.get("order_id", ""), buy_reason=pb.get("reason", ""))
        except ValueError as e:                         # 同组同码已有活动持仓
            self._log(g.strategy_id, f"入库失败 {pb['code']}: {e}")
            return
        self.db.add_event(g.strategy_id, pb["code"], "buy", qty, price,
                          pb.get("order_id", ""), dry_run=not self._live,
                          status="filled", detail=pb.get("reason", ""))
        self._log(g.strategy_id, f"买入成交 {pb['code']} {qty}@{price:.3f} "
                  f"计入买入组 (order={pb.get('order_id')}, {pb.get('reason')})")

    # ---------------------------------------------------------------- 入口 ----
    async def run_once(self, gid: str) -> dict:
        """手动跑指定组一轮 (force, 无视盘中时段). Web run-once / CLI --once 用."""
        g = self.groups.get(gid)
        if g is None:
            raise KeyError(f"策略组不存在: {gid}")
        await self._round(g, force=True)
        return self.snapshot()

    async def run(self) -> None:
        """CLI 入口: 启动全部组协程并常驻 (Web 端用 start_all, 不走这里)."""
        self._log("engine", f"选股引擎启动: live={self._live} poll={self.poll_seconds}s "
                  f"组数={len(self.groups)} 插件={list(self.pickers)}")
        self.start_all()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="选股自动交易引擎")
    ap.add_argument("--live", action="store_true", help="真实下单(同花顺), 默认模拟")
    ap.add_argument("--cash", type=float, default=100_000.0, help="每组初始资金")
    ap.add_argument("--poll", type=float, default=5.0, help="扫描间隔(秒)")
    ap.add_argument("--once", action="store_true",
                    help="手动跑一轮扫描后退出 (无视盘中时段, 验证链路用)")
    ap.add_argument("--id", type=str, default="", help="只跑指定策略 ID (--once 配合)")
    args = ap.parse_args()

    all_groups = picker_db.list_groups()
    groups = ([g for g in all_groups if g["strategy_id"] == args.id] if args.id
              else [g for g in all_groups if g.get("enabled")])
    src = f"backend/stockpicker.db ({len(all_groups)} 组)"
    print(f"策略组配置来源: {src}, 本次运行 {len(groups)} 组", flush=True)
    eng = StockPickerEngine(groups, live=args.live, cash=args.cash,
                            poll_seconds=args.poll)
    try:
        if args.once:
            async def _once():
                for g in groups:
                    await eng.run_once(g["strategy_id"])
                return eng.snapshot()
            snap = asyncio.run(_once())
            print(__import__("json").dumps(snap, ensure_ascii=False, indent=2),
                  flush=True)
        else:
            asyncio.run(eng.run())
    except KeyboardInterrupt:
        print("\n已停止。快照:", flush=True)
        print(eng.snapshot(), flush=True)


if __name__ == "__main__":
    main()

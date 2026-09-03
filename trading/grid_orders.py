#!/usr/bin/env python3
"""网格交易引擎: 区间内自动低吸高抛, 复刻券商 APP 云网格 (asyncio 并发, 每单独立协程).

核心模型 (同花顺网格口径):
  base_price 基准价 -> 买触发 = base*(1-step%) / base-step (等比/等差)
                   -> 卖触发 = base*(1+step%) / base+step
  触发成交后 base 滚动更新为触发价, 重算双触发价 -> 持续向下买/向上卖.

状态机 (每网格单):
  RUNNING   : 盯盘判定买卖触发
  PAUSED    : 用户手动暂停 (单边行情规避)
  EXHAUSTED : 买卖触发价均超出区间 -> 网格失效 (THS: 双方触发价都出界)
  EXPIRED   : 有效期到自动停

复刻的券商功能:
  - 等比(百分比)/等差(价格)网格间距; 每格按股数/按金额委托
  - 价格区间: 触发价出界该方向停, 双向出界失效; 回到区间内自动恢复
  - 最大持仓上限 (到上限只卖不买) / 最小底仓 (到下限只买不卖)
  - 有效期 (到期自动暂停, 空=长期)
增强功能:
  - 回落卖出 / 反弹买入确认 (THS): 触发后跟踪极值, 反向回撤确认才成交, 过滤假突破
  - 梯度倍量 (广发/中信建投): 每深一档买入量 x multiplier, 金字塔摊薄
  - 价格浮动 pad% (买加价/卖降价, 保成交)
  - 成交驱动 (银河): 上一笔委托未成交前不判定新触发 (内置默认)
  - 启动底仓: 建单时可选立即买入 base_qty 底仓, 使上行方向有货可卖
  - T+1 保护: 当日买入批次当日不卖 (模拟口径贴近 A 股实盘)

行情/执行: 复用条件单引擎同款链路 — fdata_client.quote 取最新价;
下单走 runtime 层 (Portfolio + broker + ctx.submit_order), 模拟/实盘(LiveBroker)同口径.
账户: strategy/state/grid_orders.state.json; 配置: backend/grid_orders.json (CLI 与 Web 共用).

用法:
  uv run python trading/grid_orders.py            # 模拟 (安全)
  uv run python trading/grid_orders.py --live      # 真实下单 (同花顺)
  uv run python trading/grid_orders.py --poll 5    # 行情刷新间隔秒
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import time
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
from trading.condition_orders import is_market_open               # noqa: E402


# ================================================================ 配置持久化 ====
# Web 端 (backend/grids.py) 与 CLI 共用: 存在则优先, 否则用下面的硬编码默认网格
STORE_PATH = REPO_ROOT / "backend" / "grid_orders.json"

# 在此添加默认网格 (为空则全部由 Web 端创建); 每个网格用一个 symbol (同标的多网格会共享持仓交叉干扰)
DEFAULT_GRIDS: list[dict] = [
    # {
    #     "id": "gr_510300", "symbol": "510300",
    #     "upper": 4.6, "lower": 3.6, "grid_unit": "pct", "step": 2.0,
    #     "base_price": 4.0, "qty_mode": "cash", "per_cash": 5000.0,
    #     "max_position": 60000, "min_position": 10000,
    # },
]


def load_grids_cfg() -> list[dict]:
    """读网格配置 (含运行时状态字段). 文件损坏/缺失时回退到 DEFAULT_GRIDS."""
    if STORE_PATH.exists():
        try:
            data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return [dict(c) for c in DEFAULT_GRIDS]


def save_grids_cfg(grids: list[dict]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(
        json.dumps(grids, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def calc_triggers(base_price: float, grid_unit: str, step: float) -> tuple[float, float]:
    """按基准价算首档买卖触发价 (等比/等差), 创建网格时预填用."""
    if grid_unit == "price":
        return base_price - step, base_price + step
    return base_price * (1 - step / 100.0), base_price * (1 + step / 100.0)


@dataclass
class GridOrder:
    id: str
    symbol: str
    upper: float                    # 网格上限 (卖方向出界停)
    lower: float                    # 网格下限 (买方向出界停)
    grid_unit: str = "pct"          # pct=等比(百分比) / price=等差(价格)
    step: float = 2.0               # 网格间距 (pct 模式=%; price 模式=元)
    base_price: float = 0.0         # 基准价 (每次成交滚动更新为触发价)
    # ---- 每格数量 ----
    qty_mode: str = "qty"           # qty=固定股数 / cash=固定金额
    per_qty: int = 1000             # 每格股数 (qty 模式)
    per_cash: float = 5000.0        # 每格金额 (cash 模式)
    multiplier: float = 1.0         # 梯度倍量: 第 n 档买入量 = 每格量 * m^n (1=固定)
    # ---- 持仓风控 ----
    max_position: int = 0           # 最大持仓 (0=不限): 到上限只卖不买
    min_position: int = 0           # 最小底仓 (0=不限): 到下限只买不卖
    # ---- 增强 ----
    sell_retrace_pct: float = 0.0   # 卖出回落确认 % (0=到价即卖; >0 从触发后高点回落该幅度才卖)
    buy_rebound_pct: float = 0.0    # 买入反弹确认 % (0=到价即买; >0 从触发后低点反弹该幅度才买)
    pad_pct: float = 0.0            # 下单价格浮动 % (买加价/卖降价, 保成交)
    t1_protect: bool = True         # A股 T+1: 当日买入批次当日不卖
    expire_date: str = ""           # 有效期 YYYY-MM-DD (空=长期)
    base_qty: int = 0               # 启动底仓 (建单后首次运行立即买入, 0=不买)
    # ---- 运行时状态 (持久化) ----
    state: str = "RUNNING"          # RUNNING / PAUSED / EXHAUSTED / EXPIRED
    bootstrapped: bool = False      # 底仓是否已处理
    buy_trigger: float = 0.0        # 下一档买触发价
    sell_trigger: float = 0.0       # 下一档卖触发价
    depth: int = 0                  # 净加仓深度 (买入+1/卖出-1, 相对底仓)
    pending_order_id: str = ""      # 待成交订单 (未成交不判新触发 = 成交驱动)
    pending_side: str = ""
    pending_qty: int = 0
    pending_price: float = 0.0      # 触发价 (成交后基准滚动到这里)
    pending_cost: float = 0.0       # 卖出发起时的批次成本 (盈亏口径)
    pending_kind: str = ""          # grid / bootstrap (底仓买入不推 depth)
    extreme_price: float = 0.0      # 回落/反弹确认期间跟踪的极值
    extreme_mode: str = ""          # "" / wait_rebound / wait_retrace
    last_buy_price: float = 0.0
    last_buy_qty: int = 0
    last_buy_day: str = ""          # T+1 检查用
    last_sell_price: float = 0.0
    grid_rounds: int = 0            # 完成卖出次数 (约等于买卖对数)
    realized_pnl: float = 0.0       # 网格累计已实现盈亏 (相对批次成本)
    trades: int = 0                 # 成交总笔数
    last_price: float = 0.0
    error: str = ""
    created_at: str = ""

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


class GridEngine:
    """加载多个网格单, 各自异步盯盘; 共享一个 Portfolio + Broker (并发安全).

    与 ConditionEngine 同款生命周期: start_all/stop_all/add_grid/remove_grid/
    pause/resume, 订单协程在当前事件循环上以 Task 运行; on_change 回调在状态
    变更时触发 (由调用方落盘, 重启恢复).
    """

    RETRY_COOLDOWN = 60.0  # 委托被拒后的重试冷却 (秒), 防止资金不足时每 tick 重下

    def __init__(self, grids_cfg: list[dict], *, live: bool, cash: float,
                 poll_seconds: float, on_change: Optional[Callable[[], None]] = None):
        self.poll_seconds = poll_seconds
        # 共享账户: 一个 Portfolio 管所有标的持仓; load 复用
        # strategy/state/grid_orders.state.json, 重启恢复资金/持仓
        self.pf = Portfolio.load("grid_orders", cash)
        self.broker = LiveBroker(self.pf) if live else SimulatedBroker(self.pf)
        self.grids = [GridOrder(**{k: v for k, v in c.items()
                                   if k in GridOrder.__dataclass_fields__})
                      for c in grids_cfg]  # type: ignore[arg-type]
        # 每个网格单独立 ctx (symbol/price 私有); pf/broker 共享
        self.ctxs = {g.id: Context(symbol=g.symbol, params={}, portfolio=self.pf,
                                   broker=self.broker, dry_run=not live)
                     for g in self.grids}
        self._live = live
        self.on_change = on_change
        self.logs: deque[str] = deque(maxlen=300)
        self._tasks: dict[str, asyncio.Task] = {}
        self._retry_after: dict[str, float] = {}   # grid_id -> monotonic 可重试时间
        self._skip_logged: dict[str, str] = {}     # grid_id -> 上次跳过原因 (防刷屏)

    # ---------- 日志 / 状态变更通知 ----------
    def _log(self, tag: str, msg: str) -> None:
        line = f"{dt.datetime.now():%H:%M:%S} {tag} {msg}"
        self.logs.append(line)
        print(line, flush=True)

    def _log_once(self, g: GridOrder, reason: str, msg: str) -> None:
        """同类跳过原因只 log 一次, 行情持续满足时避免刷屏."""
        if self._skip_logged.get(g.id) != reason:
            self._skip_logged[g.id] = reason
            self._log(f"[{g.id}]", msg)

    def _touch(self) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change()
        except Exception as e:  # noqa: BLE001
            self._log("[engine]", f"on_change 持久化失败: {e!r}")

    # ---------- 动态任务管理 ----------
    def _spawn(self, g: GridOrder) -> None:
        t = self._tasks.get(g.id)
        if t is not None and not t.done():
            return
        self._tasks[g.id] = asyncio.ensure_future(self._run_one(g))

    def start_all(self) -> None:
        for g in self.grids:
            self._spawn(g)

    def stop_all(self) -> None:
        for t in self._tasks.values():
            if not t.done():
                t.cancel()
        self._tasks.clear()

    @property
    def is_running(self) -> bool:
        return any(not t.done() for t in self._tasks.values())

    def add_grid(self, cfg: dict) -> str:
        """运行中动态加网格: 建单 + ctx + 启动协程. 返回 id."""
        clean = {k: v for k, v in cfg.items()
                 if k in GridOrder.__dataclass_fields__}
        g = GridOrder(**clean)  # type: ignore[arg-type]
        self.grids.append(g)
        self.ctxs[g.id] = Context(symbol=g.symbol, params={},
                                  portfolio=self.pf, broker=self.broker,
                                  dry_run=not self._live)
        self._spawn(g)
        self._log("[engine]", f"动态加网格 {g.id} ({g.symbol})")
        self._touch()
        return g.id

    def remove_grid(self, gid: str) -> bool:
        g = next((x for x in self.grids if x.id == gid), None)
        if g is None:
            return False
        t = self._tasks.pop(gid, None)
        if t is not None and not t.done():
            t.cancel()
        self.grids.remove(g)
        self.ctxs.pop(gid, None)
        self._retry_after.pop(gid, None)
        self._skip_logged.pop(gid, None)
        self._log("[engine]", f"动态删网格 {gid}")
        self._touch()
        return True

    def pause(self, gid: str) -> bool:
        g = next((x for x in self.grids if x.id == gid), None)
        if g is None or g.state != "RUNNING":
            return False
        g.state = "PAUSED"
        self._log(f"[{gid}]", "已手动暂停 (触发价/深度/持仓保留)")
        self._touch()
        return True

    def resume(self, gid: str) -> bool:
        g = next((x for x in self.grids if x.id == gid), None)
        if g is None or g.state != "PAUSED":
            return False
        g.state = "RUNNING"
        self._skip_logged.pop(gid, None)
        self._log(f"[{gid}]", "已恢复运行")
        self._touch()
        return True

    # ---------- 快照 ----------
    def export_configs(self) -> list[dict]:
        return [g.to_dict() for g in self.grids]

    def snapshot(self) -> dict:
        return {
            "live": self._live,
            "poll_seconds": self.poll_seconds,
            "grids": self.export_configs(),
            "portfolio": self.pf.snapshot(),
        }

    # ---------- 行情 ----------
    async def _fetch_quote(self, symbol: str):
        """拉最新价 + 昨收 (统一走 fdata serve 长连接, 失败 CLI 回退)."""
        def _q() -> tuple:
            q = fdata_client.quote(symbol)
            if q and q.get("last") and q.get("pre_close"):
                return float(q["last"]), float(q["pre_close"])
            return None, None

        return await asyncio.to_thread(_q)

    # ---------- 网格核心计算 ----------
    def _recalc_triggers(self, g: GridOrder) -> None:
        """由基准价重算双触发价 (等比/等差)."""
        g.buy_trigger, g.sell_trigger = calc_triggers(
            g.base_price, g.grid_unit, g.step)

    def _grid_qty(self, g: GridOrder, ctx: Context, level: int) -> int:
        """第 level 档的每格数量 (梯度倍量, 向下取整到 100 整手)."""
        m = max(1.0, g.multiplier) ** max(0, level)
        if g.qty_mode == "cash":
            return ctx.qty_for(g.per_cash * m)
        return int(g.per_qty * m // 100 * 100)

    # ---------- 决策 (每 tick 一次; 行情抓取在外层, 便于单测) ----------
    async def _decide(self, g: GridOrder, ctx: Context, latest: float) -> None:
        tag = f"[{g.id}]"
        today = dt.date.today().isoformat()

        # 有效期: 到期自动暂停 (券商"有效期"功能)
        if g.expire_date and today > g.expire_date:
            g.state = "EXPIRED"
            self._log(tag, f"有效期至 {g.expire_date} 已到, 自动暂停")
            self._touch()
            return

        # 成交驱动: 上一笔委托未确认前不判新触发
        if g.pending_order_id:
            self._check_pending(g, ctx)
            return

        if g.base_price <= 0:
            g.base_price = latest
            self._touch()
        if g.buy_trigger <= 0 or g.sell_trigger <= 0:
            self._recalc_triggers(g)
            self._touch()

        # 区间判定 (THS 口径): 触发价出界 -> 该方向停; 双向出界 -> 失效
        buy_ok = g.buy_trigger >= g.lower
        sell_ok = g.sell_trigger <= g.upper
        if not buy_ok and not sell_ok:
            g.state = "EXHAUSTED"
            self._log(tag, f"买卖触发价均超出区间 [{g.lower}, {g.upper}], 网格失效 "
                           f"(可删除后重建, 参数/持仓保留)")
            self._touch()
            return

        # 回落/反弹确认子状态 (增强: 过滤假突破)
        if g.extreme_mode == "wait_rebound":
            g.extreme_price = min(g.extreme_price, latest)
            if latest >= g.extreme_price * (1 + g.buy_rebound_pct / 100.0):
                self._log(tag, f"反弹确认: {latest:.3f} 自低点 {g.extreme_price:.3f} "
                               f"回升 {g.buy_rebound_pct}% -> 买入")
                g.extreme_mode = ""
                await self._do_buy(g, ctx, latest)
            return
        if g.extreme_mode == "wait_retrace":
            g.extreme_price = max(g.extreme_price, latest)
            if latest <= g.extreme_price * (1 - g.sell_retrace_pct / 100.0):
                self._log(tag, f"回落确认: {latest:.3f} 自高点 {g.extreme_price:.3f} "
                               f"回落 {g.sell_retrace_pct}% -> 卖出")
                g.extreme_mode = ""
                await self._do_sell(g, ctx, latest)
            return

        # 触发判定 (卖优先: 先止盈)
        if sell_ok and latest >= g.sell_trigger:
            if g.sell_retrace_pct > 0:
                g.extreme_mode = "wait_retrace"
                g.extreme_price = latest
                self._log(tag, f"到卖触发 {g.sell_trigger:.3f}, 等回落 "
                               f"{g.sell_retrace_pct}% 确认")
                self._touch()
            else:
                await self._do_sell(g, ctx, latest)
            return
        if buy_ok and latest <= g.buy_trigger:
            if g.buy_rebound_pct > 0:
                g.extreme_mode = "wait_rebound"
                g.extreme_price = latest
                self._log(tag, f"到买触发 {g.buy_trigger:.3f}, 等反弹 "
                               f"{g.buy_rebound_pct}% 确认")
                self._touch()
            else:
                await self._do_buy(g, ctx, latest)
            return
        self._log(tag, f"盯盘 {latest:.3f} 买触发 {g.buy_trigger:.3f} / "
                       f"卖触发 {g.sell_trigger:.3f} depth={g.depth} "
                       f"持仓={ctx.position}")

    # ---------- 执行 ----------
    def _cooldown(self, g: GridOrder) -> bool:
        """委托被拒冷却中 -> True."""
        until = self._retry_after.get(g.id)
        return until is not None and time.monotonic() < until

    async def _do_buy(self, g: GridOrder, ctx: Context, latest: float) -> None:
        tag = f"[{g.id}]"
        if self._cooldown(g):
            return
        qty = self._grid_qty(g, ctx, g.depth)
        if qty < 100:
            self._log_once(g, "qty<100", f"每格数量不足一手 ({qty}), 跳过买入")
            return
        # 对价委托 + 价格浮动: 按最新价加价挂限价, 保成交 (回落/反弹确认后
        # 最新价已偏离触发价, 用触发价会挂出过期价格)
        price = round(latest * (1 + g.pad_pct / 100.0), 3)
        # 资金预检 (SimulatedBroker 不校验现金, 这里统一挡)
        if qty * price > ctx.cash:
            self._log_once(g, "cash", f"资金不足 (需 {qty * price:.0f} > "
                                      f"可用 {ctx.cash:.0f}), 跳过买入")
            return
        pos = ctx.position
        if g.max_position > 0 and pos + qty > g.max_position:
            self._log_once(g, "max_pos", f"持仓 {pos}+{qty} 将超最大持仓 "
                                         f"{g.max_position}, 只卖不买")
            return
        oid = await ctx.submit_order("buy", qty, price=price)
        if not oid:
            return
        g.pending_order_id = oid
        g.pending_side = "buy"
        g.pending_qty = qty
        g.pending_price = g.buy_trigger
        g.pending_kind = "grid"
        self._skip_logged.pop(g.id, None)
        self._log(tag, f"触发买入 {qty}@{price:.3f} (触发 {g.buy_trigger:.3f}, "
                       f"order={oid})")
        self._touch()

    async def _do_sell(self, g: GridOrder, ctx: Context, latest: float) -> None:
        tag = f"[{g.id}]"
        if self._cooldown(g):
            return
        pos = ctx.position
        # 卖出量: depth>0 卖最近一批买入量; depth==0 做底仓/外部仓高抛, 卖标准每格量
        if g.depth > 0:
            qty = int(g.last_buy_qty)
            cost = g.last_buy_price
        else:
            qty = self._grid_qty(g, ctx, 0)
            cost = ctx.pf.position(g.symbol).avg_price if pos > 0 else 0.0
        # T+1 保护: 当日买入批次当日不卖
        if g.t1_protect and g.last_buy_day == dt.date.today().isoformat():
            self._log_once(g, "t1", "T+1 保护: 今日有买入, 当日不卖")
            return
        # 最小底仓保护: 缩量到 (pos - min_position), 不足一手则跳过
        floor_qty = max(g.min_position, 0)
        if pos - qty < floor_qty:
            qty = (pos - floor_qty) // 100 * 100
        if qty < 100:
            self._log_once(g, "min_pos", f"持仓 {pos} 触及最小底仓 {floor_qty}, "
                                         f"只买不卖")
            return
        # 对价委托 - 价格浮动: 按最新价降价挂限价, 保成交
        price = round(latest * (1 - g.pad_pct / 100.0), 3)
        oid = await ctx.submit_order("sell", qty, price=price)
        if not oid:
            return
        g.pending_order_id = oid
        g.pending_side = "sell"
        g.pending_qty = qty
        g.pending_price = g.sell_trigger
        g.pending_cost = cost
        g.pending_kind = "grid"
        self._skip_logged.pop(g.id, None)
        self._log(tag, f"触发卖出 {qty}@{price:.3f} (触发 {g.sell_trigger:.3f}, "
                       f"order={oid})")
        self._touch()

    async def _maybe_bootstrap(self, g: GridOrder, ctx: Context,
                               latest: float) -> None:
        """启动底仓: 首次运行时一次性买入 base_qty, 让上行方向有货可卖."""
        if g.bootstrapped or g.base_qty <= 0:
            g.bootstrapped = True
            return
        need = (g.base_qty - ctx.position) // 100 * 100
        g.bootstrapped = True
        if need < 100:
            return
        price = round(latest * (1 + g.pad_pct / 100.0), 3)
        if need * price > ctx.cash:
            self._log(f"[{g.id}]", f"底仓资金不足 (需 {need * price:.0f} > "
                                   f"可用 {ctx.cash:.0f}), 未建底仓")
            self._touch()
            return
        oid = await ctx.submit_order("buy", need, price=price)
        g.pending_order_id = oid
        g.pending_side = "buy"
        g.pending_qty = need
        g.pending_price = g.base_price  # 底仓不滚动基准
        g.pending_kind = "bootstrap"
        self._log(f"[{g.id}]", f"启动底仓买入 {need}@{price:.3f} (order={oid})")
        self._touch()

    # ---------- 成交确认 ----------
    def _check_pending(self, g: GridOrder, ctx: Context) -> None:
        o = self.pf.orders.get(g.pending_order_id)
        if o is None:
            g.pending_order_id = ""
            return
        if o.status in ("pending_submit", "submitted", "partial_filled"):
            return  # 仍在途 (LiveBroker 异步轮询中)
        side = g.pending_side or o.side
        filled_qty = o.filled_qty or g.pending_qty
        fill_price = o.avg_fill_price or o.price
        if o.status == "filled":
            if side == "buy":
                if g.pending_kind == "bootstrap":
                    g.last_buy_day = dt.date.today().isoformat()
                    self._log(f"[{g.id}]", f"底仓成交 {filled_qty}@{fill_price:.3f}")
                else:
                    g.base_price = g.pending_price   # 基准滚动到触发价 (THS 口径)
                    g.depth += 1
                    g.last_buy_price = fill_price
                    g.last_buy_qty = filled_qty
                    g.last_buy_day = dt.date.today().isoformat()
                    g.trades += 1
                    self._log(f"[{g.id}]", f"买入成交 {filled_qty}@{fill_price:.3f} "
                                           f"基准滚动 {g.base_price:.3f} "
                                           f"depth={g.depth}")
            else:
                g.base_price = g.pending_price   # 基准滚动到触发价 (THS 口径)
                pnl = 0.0
                if g.pending_cost > 0:
                    pnl = (fill_price - g.pending_cost) * filled_qty
                g.realized_pnl += pnl
                g.grid_rounds += 1
                g.last_sell_price = fill_price
                g.depth = max(0, g.depth - 1)
                g.trades += 1
                self._log(f"[{g.id}]", f"卖出成交 {filled_qty}@{fill_price:.3f} "
                                       f"盈亏≈{pnl:+.2f} 累计 {g.realized_pnl:+.2f} "
                                       f"depth={g.depth}")
            self._recalc_triggers(g)
            g.pending_order_id = ""
            g.pending_side = ""
            g.pending_kind = ""
            self.pf.save()
            self._touch()
        elif o.status in ("rejected", "cancelled"):
            g.error = o.error or o.status
            self._log(f"[{g.id}]", f"{'买入' if side == 'buy' else '卖出'}委托"
                                   f"{o.status}: {o.error or ''} "
                                   f"{self.RETRY_COOLDOWN:.0f}s 后重试判定")
            self._retry_after[g.id] = time.monotonic() + self.RETRY_COOLDOWN
            g.pending_order_id = ""
            g.pending_side = ""
            g.pending_kind = ""
            self._touch()

    # ---------- 单协程主循环 ----------
    async def _run_one(self, g: GridOrder) -> None:
        tag = f"[{g.id}]"
        ctx = self.ctxs[g.id]
        while True:
            try:
                latest, _pre_close = await self._fetch_quote(g.symbol)
                if latest is not None:
                    g.last_price = latest
                    ctx.set_price(latest)
                if (g.state != "RUNNING" or latest is None
                        or not is_market_open(dt.datetime.now())):
                    await asyncio.sleep(self.poll_seconds)
                    continue
                await self._maybe_bootstrap(g, ctx, latest)
                await self._decide(g, ctx, latest)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                g.error = repr(e)
                self._log(tag, f"error: {e!r}")
                self._touch()
            await asyncio.sleep(self.poll_seconds)

    async def run(self) -> None:
        """CLI 入口: 启动全部网格协程并等待."""
        self._log("[engine]", f"网格引擎启动: live={self._live} "
                              f"poll={self.poll_seconds}s 网格数={len(self.grids)}")
        self.start_all()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="网格交易引擎")
    ap.add_argument("--live", action="store_true", help="真实下单(同花顺), 默认模拟")
    ap.add_argument("--cash", type=float, default=100_000.0, help="初始资金")
    ap.add_argument("--poll", type=float, default=5.0, help="行情刷新间隔(秒)")
    args = ap.parse_args()

    grids_cfg = load_grids_cfg()
    src = "backend/grid_orders.json" if STORE_PATH.exists() else "内置 DEFAULT_GRIDS"
    print(f"网格配置来源: {src} ({len(grids_cfg)} 个)", flush=True)
    eng = GridEngine(grids_cfg, live=args.live,
                     cash=args.cash, poll_seconds=args.poll)
    try:
        asyncio.run(eng.run())
    except KeyboardInterrupt:
        print("\n已停止。快照:", flush=True)
        print(eng.pf.snapshot(), flush=True)


if __name__ == "__main__":
    main()

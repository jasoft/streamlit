"""Broker: 订单意图 -> 成交事件. 回测与实盘共用同一接口.

订单状态机: pending_submit -> submitted -> (partial_filled)* -> filled | cancelled | rejected

- BacktestBroker: submit 入 pending 队列, runner 在下一根 bar 开盘时调
  flush_pending(symbol, open_price) 用开盘价成交. 对齐 engine.py 口径:
  收盘出信号 -> 次日开盘成交 (无未来函数).

- LiveBroker: submit 调 scripts/ths_trade.py 下单. dry_run=True 同步记账;
  dry_run=False 订单入 _pending 池, _poll_loop 异步轮询 query_order,
  成交 -> apply_fill (真实成交均价), 拒单/撤单 -> apply_reject.
  阻塞 subprocess 必须用 asyncio.to_thread 包装 (避免卡事件循环).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .portfolio import Fill, Order, Portfolio

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
THS_TRADE = REPO_ROOT / "trading" / "ths_trade.py"
COMMISSION = 0.0001  # ETF 佣金万1, 无印花税


def six_digit(symbol: str) -> str:
    """sz159915 -> 159915 (ths_trade 用 6 位代码)."""
    return symbol[-6:]


class Broker:
    async def submit(self, order: Order, dry_run: bool = False) -> Optional[Fill]:
        raise NotImplementedError


class BacktestBroker(Broker):
    """回测执行器: 次日开盘成交. submit 入队, flush_pending 才 fill."""

    def __init__(self, portfolio: Portfolio):
        self.pf = portfolio
        # pending: symbol -> [Order] (等待下一根 open 成交)
        self._pending: dict[str, list[Order]] = {}

    async def submit(self, order: Order, dry_run: bool = False) -> Optional[Fill]:
        """回测: submit 不立即成交, 入 pending 队列等下一根 bar 开盘."""
        order.status = "submitted"
        self._pending.setdefault(order.symbol, []).append(order)
        return None  # 回测不立即返回 fill, 由 flush_pending 处理

    def flush_pending(self, symbol: str, open_price: float) -> list[Fill]:
        """下一根 bar 开盘时调用: 用 open_price 成交所有 pending, 返回 fills.

        runner 流程: bar_t 到来 -> flush_pending(bar_t.open) 成交昨日信号
        -> on_bar(bar_t) 产生今日信号 -> 入 pending -> bar_t+1 开盘成交.
        """
        fills = []
        for o in self._pending.get(symbol, []):
            commission = o.qty * open_price * COMMISSION
            fills.append(Fill(
                order_id=o.order_id, symbol=symbol, side=o.side,
                qty=o.qty, price=open_price,
                ts=dt.datetime.now().isoformat(timespec="seconds"),
                commission=commission,
            ))
        self._pending[symbol] = []
        return fills

    def has_pending(self, symbol: str) -> bool:
        return bool(self._pending.get(symbol))


class SimulatedBroker(Broker):
    """模拟执行器 (paper 模式): submit 立即按信号价 fill, 不调 ths_trade.

    用于纯模拟回测/演练, 不产生真实订单. 成交价用 order.price (信号价近似).
    """

    def __init__(self, portfolio: Portfolio):
        self.pf = portfolio

    async def submit(self, order: Order, dry_run: bool = False) -> Optional[Fill]:
        order.status = "submitted"
        commission = order.qty * order.price * COMMISSION
        return Fill(
            order_id=order.order_id, symbol=order.symbol, side=order.side,
            qty=order.qty, price=order.price,
            ts=dt.datetime.now().isoformat(timespec="seconds"),
            commission=commission,
        )


class LiveBroker(Broker):
    """实盘执行器: 调 ths_trade.py 下单 + 异步轮询成交状态.

    dry_run=True: 调 ths_trade --dry-run, 无论 ok 都按 fill 记账 (链路测试用).
    dry_run=False: ok=True 订单入 pending 池, _poll_loop 轮询 query_order,
                  成交 -> apply_fill (真实成交均价), 拒单/撤单 -> apply_reject.
    """

    def __init__(self, portfolio: Portfolio, timeout: float = 120.0,
                 poll_interval: float = 5.0, max_polls: int = 60):
        self.pf = portfolio
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_polls = max_polls
        self._poll_task: Optional[asyncio.Task] = None
        self._pending: dict[str, Order] = {}  # order_id -> Order (等待成交回报)

    async def submit(self, order: Order, dry_run: bool = False) -> Optional[Fill]:
        order.status = "submitted"
        ts = dt.datetime.now().isoformat(timespec="seconds")
        if dry_run:
            # 测试模式: 调 ths --dry-run, 无论反馈都按 fill 记账 (commission=0)
            await asyncio.to_thread(self._call_ths, order, dry_run)
            return Fill(order_id=order.order_id, symbol=order.symbol, side=order.side,
                        qty=order.qty, price=order.price, ts=ts, commission=0.0)
        res = await asyncio.to_thread(self._call_ths, order, dry_run)
        ok = bool(res.get("ok", False))
        if not ok:
            order.status = "rejected"
            order.error = (res.get("stderr", "") or res.get("stdout", ""))[-200:]
            return None
        # 真实订单已提交, 入 pending 池等轮询
        self._pending[order.order_id] = order
        self._ensure_poll()
        return None  # 异步: 成交回报由 _poll_loop 驱动

    def _ensure_poll(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())

    def _call_ths(self, order: Order, dry_run: bool) -> dict:
        """同步调 ths_trade.py 下单 (在线程池跑). 返回 {ok, stdout, stderr}."""
        cmd = [sys.executable, str(THS_TRADE), order.side,
               six_digit(order.symbol), str(order.qty)]
        # 显式传委托价, 不依赖同花顺报价窗联动 (联动价未就绪会下单失败)
        if order.price > 0:
            cmd += ["--price", f"{order.price:.3f}"]
        if dry_run:
            cmd.append("--dry-run")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as e:
            return {"ok": False, "stdout": "", "stderr": f"timeout: {e}"}
        out, err = r.stdout, r.stderr
        ok = r.returncode == 0
        if ok and out.strip():
            try:
                data = json.loads(out.strip().splitlines()[-1])
                ok = bool(data.get("ok", False))
                rt = str(data.get("result_text", "") or "")
                if ok and any(kw in rt for kw in
                              ("警告", "错误", "失败", "关闭", "非交易", "禁止",
                               "无效", "不足", "超过", "不允许", "拒绝")):
                    ok = False
            except (json.JSONDecodeError, IndexError):
                pass
        return {"ok": ok, "stdout": out[-2000:], "stderr": err[-500:]}

    def _query_orders(self, code: str) -> dict:
        """同步调 ths_trade.py query_order --code (在线程池跑). 返回完整 JSON."""
        cmd = [sys.executable, str(THS_TRADE), "query_order", "--code", code]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired:
            return {"ok": False, "status_map": {}, "orders": []}
        out = r.stdout.strip()
        if not out:
            return {"ok": False, "status_map": {}, "orders": []}
        try:
            return json.loads(out.splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            return {"ok": False, "status_map": {}, "orders": []}

    def _match_order(self, order: Order, rows: list,
                     status_map: dict) -> Optional[dict]:
        """在委托表行中匹配我们的订单. 返回 {status,filled_qty,avg_price,contract_no,raw}."""
        # 已知合同号: 精确匹配
        if order.ths_contract_no and order.ths_contract_no in status_map:
            m = dict(status_map[order.ths_contract_no])
            m["contract_no"] = order.ths_contract_no
            m["raw"] = ""
            return m
        # 未知合同号: 按 操作 + 委托数量 匹配 (取第一个匹配)
        side_text = "买入" if order.side == "buy" else "卖出"

        def to_int(v):
            try:
                return int(float(str(v).replace(",", "")))
            except (ValueError, TypeError):
                return -1

        for r in rows:
            if r.get("操作") != side_text:
                continue
            if to_int(r.get("委托数量")) != order.qty:
                continue
            cno = r.get("合同编号", "")
            if cno and cno in status_map:
                m = dict(status_map[cno])
                m["contract_no"] = cno
                m["raw"] = r.get("备注", "") or r.get("委托属性", "")
                order.ths_contract_no = cno  # 记下来, 下次精确匹配
                return m
        return None

    async def _poll_loop(self) -> None:
        """异步轮询 pending 订单. 成交 -> apply_fill, 拒单/撤单 -> apply_reject."""
        poll_count: dict[str, int] = {}
        while self._pending:
            await asyncio.sleep(self.poll_interval)
            # 按 symbol 分组查询 (减少 ths 调用)
            by_sym: dict[str, list[Order]] = {}
            for o in self._pending.values():
                by_sym.setdefault(o.symbol, []).append(o)
            for sym, orders in by_sym.items():
                try:
                    res = await asyncio.to_thread(self._query_orders, six_digit(sym))
                except Exception as e:
                    print(f"[LiveBroker] query {sym} error: {e!r}", flush=True)
                    continue
                if not res.get("ok"):
                    continue
                rows = res.get("orders", [])
                smap = res.get("status_map", {})
                for o in orders:
                    poll_count[o.order_id] = poll_count.get(o.order_id, 0) + 1
                    info = self._match_order(o, rows, smap)
                    if info is None:
                        if poll_count[o.order_id] >= self.max_polls:
                            print(f"[LiveBroker] {o.order_id} 轮询超时放弃",
                                  flush=True)
                            self.pf.apply_reject(o.order_id, reason="poll timeout")
                            self._pending.pop(o.order_id, None)
                        continue
                    st = info.get("status", "pending")
                    if st == "filled":
                        px = float(info.get("avg_price") or o.price)
                        fill = Fill(
                            order_id=o.order_id, symbol=o.symbol, side=o.side,
                            qty=int(info.get("filled_qty") or o.qty), price=px,
                            ts=dt.datetime.now().isoformat(timespec="seconds"),
                            commission=o.qty * px * COMMISSION,
                        )
                        self.pf.apply_fill(fill)
                        self._pending.pop(o.order_id, None)
                        print(f"[LiveBroker] {o.order_id} 已成交 "
                              f"{fill.qty}@{fill.price}", flush=True)
                    elif st in ("rejected", "cancelled"):
                        self.pf.apply_reject(
                            o.order_id, reason=f"ths: {info.get('raw', st)}")
                        self._pending.pop(o.order_id, None)
                        print(f"[LiveBroker] {o.order_id} {st}", flush=True)
                    elif st == "partial":
                        o.status = "partial_filled"
                        o.filled_qty = int(info.get("filled_qty") or 0)
                        o.avg_fill_price = float(info.get("avg_price") or 0)
        self._poll_task = None

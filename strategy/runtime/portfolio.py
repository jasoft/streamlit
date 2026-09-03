"""Portfolio: 内存持仓 + state.json 落盘. 跟踪现金/持仓目标/订单状态.

核心区分两个概念:
- target (0/1): 策略意图, submit_order 时立即更新 (不等待成交)
- qty (份数): 实际持仓, apply_fill 后更新 (成交后才变)

这样 on_bar 里 ctx.target 反映"策略应有仓位", ctx.position 反映"实际成交持仓",
策略基于 target 决策不会重复下单 (即使 BacktestBroker 次日才 fill).

state.json 格式 (向后兼容 trader.py 旧 state):
  {
    "cash": float,
    "positions": {symbol: {qty, avg_price, target, date, dry}},
    "orders": {order_id: {...}},          # 新增, 可选
  }
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

LOT = 100
STATE_DIR = Path(__file__).resolve().parent.parent / "state"


@dataclass
class Position:
    symbol: str
    qty: int = 0               # 实际持仓份数 (成交后更新)
    avg_price: float = 0.0
    target: int = 0            # 策略目标仓位 0/1 (submit 即更新)
    date: str = ""             # 最近一次目标变更日 (YYYY-MM-DD)
    dry: bool = False          # 是否 dry-run 记录


@dataclass
class Order:
    order_id: str
    symbol: str
    side: str                  # "buy" / "sell"
    qty: int
    price: float              # 信号价 (期望成交参考)
    status: str = "pending_submit"  # pending_submit/submitted/partial_filled/filled/cancelled/rejected
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    ts: str = ""
    error: str = ""
    ths_contract_no: str = ""   # 同花顺合同号 (query_order 匹配用, 可选)


@dataclass
class Fill:
    """成交事件, broker -> portfolio."""
    order_id: str
    symbol: str
    side: str
    qty: int
    price: float
    ts: str = ""
    commission: float = 0.0


class Portfolio:
    def __init__(self, name: str, cash: float = 100_000.0):
        self.name = name
        self.cash = cash
        self.initial_cash = cash
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, Order] = {}
        self._seq = 0

    # ---------- 加载/落盘 ----------
    @classmethod
    def load(cls, name: str, cash: float = 100_000.0) -> "Portfolio":
        """从 state.json 加载 (不存在则新建). 兼容旧 trader.py 写入的扁平格式."""
        p = cls(name, cash)
        path = STATE_DIR / f"{name}.state.json"
        if not path.exists():
            return p
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return p
        p.cash = float(data.get("cash", cash))
        p.initial_cash = float(data.get("initial_cash", p.cash))
        for sym, pos in (data.get("positions") or {}).items():
            # 旧 trader.py 格式: {symbol: {target, qty, date, dry, price}}
            p.positions[sym] = Position(
                symbol=sym,
                qty=int(pos.get("qty", 0)),
                avg_price=float(pos.get("avg_price") or pos.get("price") or 0.0),
                target=int(pos.get("target", 0)),
                date=str(pos.get("date", "")),
                dry=bool(pos.get("dry", False)),
            )
        return p

    def save(self) -> None:
        STATE_DIR.mkdir(exist_ok=True)
        path = STATE_DIR / f"{self.name}.state.json"
        data = {
            "cash": self.cash,
            "initial_cash": self.initial_cash,
            "positions": {
                sym: {"qty": p.qty, "avg_price": p.avg_price,
                      "target": p.target, "date": p.date, "dry": p.dry}
                for sym, p in self.positions.items()
            },
        }
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # ---------- 查询 ----------
    def position(self, symbol: str) -> Position:
        return self.positions.get(symbol, Position(symbol=symbol))

    def qty(self, symbol: str) -> int:
        return self.position(symbol).qty

    def target(self, symbol: str) -> int:
        return self.position(symbol).target

    def total_value(self, prices: dict[str, float]) -> float:
        """总市值 = cash + Σ qty*price. prices: symbol -> 当前价."""
        v = self.cash
        for sym, p in self.positions.items():
            if p.qty and sym in prices:
                v += p.qty * prices[sym]
        return v

    # ---------- 订单意图 (submit 即更新 target, 不等 fill) ----------
    def new_order_id(self) -> str:
        self._seq += 1
        return f"{self.name}-{dt.datetime.now():%Y%m%d%H%M%S}-{self._seq}"

    def register_order(self, symbol: str, side: str, qty: int,
                       price: float, dry: bool = False) -> Order:
        """登记订单意图, 立即更新 target (策略意图先落地, 不等成交)."""
        oid = self.new_order_id()
        o = Order(order_id=oid, symbol=symbol, side=side, qty=qty, price=price,
                  ts=dt.datetime.now().isoformat(timespec="seconds"))
        self.orders[oid] = o
        pos = self.positions.setdefault(symbol, Position(symbol=symbol))
        pos.target = 1 if side == "buy" else 0
        pos.date = dt.date.today().isoformat()
        pos.dry = dry
        return o

    # ---------- 成交回报 ----------
    def apply_fill(self, fill: Fill) -> None:
        """成交事件 -> 更新持仓/现金. 佣金从 cash 扣."""
        pos = self.positions.setdefault(fill.symbol, Position(symbol=fill.symbol))
        o = self.orders.get(fill.order_id)
        if o:
            o.status = "filled"
            o.filled_qty = fill.qty
            o.avg_fill_price = fill.price
        if fill.side == "buy":
            if pos.qty + fill.qty > 0:
                pos.avg_price = (pos.avg_price * pos.qty + fill.price * fill.qty) / (pos.qty + fill.qty)
            pos.qty += fill.qty
            self.cash -= fill.qty * fill.price + fill.commission
        else:  # sell
            pos.qty -= fill.qty
            self.cash += fill.qty * fill.price - fill.commission
            if pos.qty == 0:
                pos.avg_price = 0.0

    def apply_reject(self, order_id: str, reason: str = "") -> None:
        """订单被拒: 回滚 target 到实际 qty 对应的应有状态."""
        o = self.orders.get(order_id)
        if not o:
            return
        o.status = "rejected"
        o.error = reason
        pos = self.positions.setdefault(o.symbol, Position(symbol=o.symbol))
        # target 回滚: buy 被拒 -> target 应反映当前实际持仓 (有则1无则0)
        pos.target = 1 if pos.qty > 0 else 0

    # ---------- 快照 ----------
    def snapshot(self) -> dict:
        return {
            "cash": round(self.cash, 2),
            "initial_cash": round(self.initial_cash, 2),
            "positions": {
                sym: {"qty": p.qty, "avg_price": round(p.avg_price, 3),
                      "target": p.target, "date": p.date, "dry": p.dry}
                for sym, p in self.positions.items()
            },
            "open_orders": sum(1 for o in self.orders.values()
                               if o.status in ("pending_submit", "submitted", "partial_filled")),
        }

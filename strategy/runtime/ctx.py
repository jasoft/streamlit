"""Context: 策略 on_bar 看到的"世界". 由 runner 构造, 委托 Portfolio + Broker.

接口 (策略作者只关心这些):
- history(n=None) -> DataFrame   取历史K线 (含当前bar, 由 runner 预喂)
- position -> int                实际持仓份数 (成交后更新)
- target -> int                   策略目标仓位 0/1 (submit 即更新, 用于决策对比)
- cash -> float                   可用现金
- params -> dict                  策略参数 (已 validate)
- account() -> dict               账户快照
- set_price(p)                    runner 喂当前价 (qty_for 用)
- qty_for(cash=None) -> int       按 cash 算整手份数 (向下取整 LOT=100)
- submit_order(side, qty, ...)    订单意图层, 不直接碰 ths_trade

submit_order 内部: portfolio.register_order (立即更新 target) -> broker.submit
  -> fill 则 apply_fill (更新 qty/cash)
  -> None 且 rejected 则 apply_reject (回滚 target)
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from .portfolio import Portfolio
from .broker import Broker

LOT = 100


class Context:
    def __init__(self, *, symbol: str, params: dict, portfolio: Portfolio,
                 broker: Broker, cash_per_symbol: float = 10_000.0,
                 last_price: float = 0.0, dry_run: bool = False):
        self.symbol = symbol
        self.params = params
        self.pf = portfolio
        self.broker = broker
        self.cash_per_symbol = cash_per_symbol
        self._last_price = last_price
        self._dry_run = dry_run
        self._df = pd.DataFrame()  # 历史 K 线 (runner 预喂)

    # ---------- 数据 ----------
    def set_history(self, df: pd.DataFrame) -> None:
        self._df = df

    def history(self, n: Optional[int] = None) -> pd.DataFrame:
        """取历史 K 线 (含当前 bar). n=None 返回全部, 否则取末尾 n 根."""
        if n is None or n >= len(self._df):
            return self._df
        return self._df.tail(n).reset_index(drop=True)

    # ---------- 持仓/账户 ----------
    @property
    def position(self) -> int:
        return self.pf.qty(self.symbol)

    @property
    def target(self) -> int:
        return self.pf.target(self.symbol)

    @property
    def cash(self) -> float:
        return self.pf.cash

    def account(self) -> dict:
        return self.pf.snapshot()

    def set_price(self, price: float) -> None:
        self._last_price = float(price)

    def qty_for(self, cash: Optional[float] = None) -> int:
        """按 cash (默认 cash_per_symbol) 算整手份数, 向下取整到 LOT=100."""
        c = self.cash_per_symbol if cash is None else cash
        px = self._last_price
        if px <= 0:
            return 0
        return int(c // px // LOT * LOT)

    # ---------- 下单意图 ----------
    async def submit_order(self, side: str, qty: int,
                          price: Optional[float] = None,
                          dry_run: Optional[bool] = None) -> str:
        """提交订单意图 -> order_id. 不直接调 ths_trade (broker 隔离).

        portfolio.register_order 立即更新 target (策略意图先落地),
        broker.submit 返回 fill 则 apply_fill (更新 qty/cash),
        返回 None 且 status=rejected 则 apply_reject (回滚 target).
        dry_run 默认用 Context 配置 (由 Runner 传入).
        """
        if qty <= 0:
            return ""
        dr = self._dry_run if dry_run is None else dry_run
        px = float(price) if price is not None else self._last_price
        o = self.pf.register_order(self.symbol, side, qty, px, dry=dr)
        fill = await self.broker.submit(o, dry_run=dr)
        if fill is not None:
            self.pf.apply_fill(fill)
        elif o.status == "rejected":
            self.pf.apply_reject(o.order_id, reason="broker rejected")
        return o.order_id

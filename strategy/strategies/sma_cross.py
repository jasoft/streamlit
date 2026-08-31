"""SMA 金叉死叉: 快线上穿慢线持有, 下穿空仓."""
from __future__ import annotations

import pandas as pd

from strategy.base import Strategy, INT


class SmaCross(Strategy):
    NAME = "sma_cross"
    TITLE = "SMA 金叉死叉"
    PARAMS = {
        "fast": {"type": INT, "default": 10, "min": 2, "max": 60},
        "slow": {"type": INT, "default": 30, "min": 5, "max": 250},
    }
    SYMBOLS = ["sh510300"]

    def target_position(self, df: pd.DataFrame, params: dict) -> pd.Series:
        fast = int(min(params["fast"], params["slow"]) )
        slow = int(max(params["fast"], params["slow"]))
        ma_f = df["close"].rolling(fast).mean()
        ma_s = df["close"].rolling(slow).mean()
        return (ma_f > ma_s).astype(int).where(ma_s.notna(), 0)

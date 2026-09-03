"""MA20 趋势跟踪: 收盘 > MA20 持有, 收盘 < MA20 空仓."""
from __future__ import annotations

import pandas as pd

from strategy.base import Strategy, INT


class Ma20Trend(Strategy):
    NAME = "ma20_trend"
    TITLE = "MA20 趋势跟踪"
    PARAMS = {"window": {"type": INT, "default": 20, "min": 5, "max": 120}}
    SYMBOLS = ["sz159915"]

    def signal(self, df: pd.DataFrame, params: dict) -> pd.Series:
        ma = df["close"].rolling(int(params["window"])).mean()
        return (df["close"] > ma).astype(int).where(ma.notna(), 0)

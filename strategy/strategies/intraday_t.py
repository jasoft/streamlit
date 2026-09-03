"""日内做T策略 (量能自适应先买后卖/先卖后买).

数据: 5分钟K线 (TIMEFRAME="5m").

每日 10:00 后用量能预估定方向 (复用项目已有成交量预估):
  - 预估全天成交额 > 近N日均值 × vol_expand (放量) -> 先买后卖
  - 缩量 / 量能变化不大 / 预估 < min_amount_yi (2万亿) -> 先卖后卖 (需底仓, 框架中默认持仓)

买卖点 (5m 级灵敏指标):
  买入(恐慌放量杀跌): RSI(6) <= oversold 且 5m量 >= vol_burst × 前20根均量 且 价在日内均价(VWAP)下方
  先买后卖卖出: 回升到 VWAP±vwap_band 止盈; RSI(6) 死叉 RSI(12) 保护性离场
  先卖后买卖出: 早盘冲高 >= surge 后 20 分钟未创新高, 且 RSI(6) 死叉 RSI(12) -> 卖出
  先卖后买回补: 恐慌杀跌同买入条件, 或价回到 VWAP 下方
  (已取消开盘/收盘时间限制, 全天任一分钟级 bar 均可交易)
"""
from __future__ import annotations

import datetime as dt
import time as _time
from functools import lru_cache, wraps

import pandas as pd

from strategy.base import Strategy, INT, FLOAT

_WARMUP = 30  # 恐慌放量对比窗口 (bar 数)


# ---------- 运行时 TTL 缓存 (替换 @st.cache_data, 不依赖 Streamlit runtime) ----------
def _ttl_cache(ttl_seconds: int = 300, maxsize: int = 128):
    """和 @st.cache_data(ttl=300) 等价的运行时 TTL 缓存, 但无 Streamlit 依赖.

    - 纯函数参数必须可哈希 (count:int, 符合)
    - 首次调用 + ttl 秒内: 命中缓存
    - 超过 ttl: 重新计算
    """
    def deco(fn):
        fn_inner = lru_cache(maxsize=maxsize)(fn)
        expire_at = [0.0]
        @wraps(fn)
        def wrapper(*args, **kwargs):
            now = _time.monotonic()
            if now > expire_at[0]:
                fn_inner.cache_clear()
                expire_at[0] = now + ttl_seconds
            return fn_inner(*args, **kwargs)
        wrapper.cache_clear = lambda: (fn_inner.cache_clear(), expire_at.__setitem__(0, 0.0))
        return wrapper
    return deco


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = (gain / loss).astype(float)
    return (100 - 100 / (1 + rs)).fillna(50.0)


@_ttl_cache(ttl_seconds=300)
def _market_day_amounts(count: int = 60) -> pd.Series:
    """沪深两市每日总成交额 (元), 通达信指数日K, 与预估同口径."""
    from data_analysis.stockview.tdx_source import fetch_index_daily
    sh = fetch_index_daily("sh000001", count=count)
    sz = fetch_index_daily("sz399001", count=count)
    sh = sh.assign(date=pd.to_datetime(sh["date"]).dt.date).set_index("date")
    sz = sz.assign(date=pd.to_datetime(sz["date"]).dt.date).set_index("date")
    return (sh["amount"].astype(float) + sz["amount"].astype(float)).sort_index()


def _live_estimate() -> float:
    """盘中实时全天成交额预估 (元), 复用项目已有代码."""
    from data_analysis.stockview.helpers import during_market_time, minutes_since_market_open
    from data_analysis.stockview.main import get_estimate_amount
    now = dt.datetime.now()
    if not during_market_time(now):
        return 0.0
    return float(get_estimate_amount(minutes_since_market_open(now)))


class IntradayT(Strategy):
    NAME = "intraday_t"
    TITLE = "日内做T (量能自适应)"
    TIMEFRAME = "5m"
    SYMBOLS = ["sz159915"]
    PARAMS = {
        "avg_days": {"type": INT, "default": 5, "min": 2, "max": 20},
        "vol_expand": {"type": FLOAT, "default": 1.05, "min": 1.0, "max": 2.0},
        "vol_shrink": {"type": FLOAT, "default": 0.95, "min": 0.5, "max": 1.0},
        "min_amount_yi": {"type": FLOAT, "default": 20000, "min": 5000, "max": 50000},
        "rsi_fast": {"type": INT, "default": 6, "min": 3, "max": 14},
        "rsi_slow": {"type": INT, "default": 12, "min": 6, "max": 30},
        "oversold": {"type": FLOAT, "default": 25, "min": 5, "max": 45},
        "vol_burst": {"type": FLOAT, "default": 1.8, "min": 1.0, "max": 5.0},
        "vwap_band": {"type": FLOAT, "default": 0.003, "min": 0.0005, "max": 0.02},
        "surge": {"type": FLOAT, "default": 0.015, "min": 0.003, "max": 0.06},
        "stale_min": {"type": INT, "default": 20, "min": 5, "max": 60},
    }

    # ---- 量能方向判定 ----
    def _regime(self, day: dt.date, amounts: pd.Series, p: dict) -> str:
        today = dt.date.today()
        if day == today:
            est = _live_estimate()
        else:
            est = float(amounts.get(day, 0.0))
        past = amounts[amounts.index < day].tail(int(p["avg_days"]))
        avg = float(past.mean()) if len(past) else 0.0
        if avg <= 0 or est <= 0:
            return "sell_first"  # 数据不足时保守: 先卖后买
        if est > avg * p["vol_expand"] and est >= p["min_amount_yi"] * 1e8:
            return "buy_first"
        return "sell_first"  # 缩量 / 变化不大 / 低于阈值

    def signal(self, df: pd.DataFrame, params: dict) -> pd.Series:
        p = self.validate_params(params)
        # 兼容两种契约: date 在列上 (默认) 或 date 已作为 index
        if "date" not in df.columns:
            df = df.copy()
            df["date"] = pd.to_datetime(df.index)
        df = df.sort_values("date").reset_index(drop=True).copy()
        df["date"] = pd.to_datetime(df["date"])
        day = df["date"].dt.date
        # 日内累计 VWAP (向量化, 防 O(n²))
        df["_cum_share"] = df["volume"].groupby(day).cumsum() * 100
        df["_cum_amt"] = (df["volume"] * 100 * df["close"]).groupby(day).cumsum()
        vwap_series = df["_cum_amt"] / df["_cum_share"].replace(0, pd.NA)
        rsi_f = _rsi(df["close"], int(p["rsi_fast"]))
        rsi_s = _rsi(df["close"], int(p["rsi_slow"]))
        stale = dt.timedelta(minutes=int(p["stale_min"]))
        burst_vol = df["volume"].rolling(_WARMUP).mean()

        amounts = _market_day_amounts(60)
        target = pd.Series(0, index=df.index)
        last = 0  # 隔日携带的仓位
        prev_crossed = False

        for day, g in df.groupby(df["date"].dt.date, sort=True):
            regime = self._regime(day, amounts, p)
            day_open_px = g["open"].iloc[0]
            day_high, day_high_time = 0.0, None
            pos = last  # 日内状态: sell_first 默认持有底仓, buy_first 默认空仓
            sold_px = None

            for idx, row in g.iterrows():
                px = row["close"]
                if row["high"] > day_high:
                    day_high, day_high_time = row["high"], row["date"]
                vwap = vwap_series.iloc[idx]
                if pd.isna(vwap):
                    vwap = px

                cross_down = (rsi_f.iloc[idx] < rsi_s.iloc[idx]
                              and rsi_f.iloc[idx - 1] >= rsi_s.iloc[idx - 1])
                panic = (rsi_f.iloc[idx] <= p["oversold"]
                         and row["volume"] >= p["vol_burst"] * (burst_vol.iloc[idx] or 0)
                         and px <= vwap)

                if regime == "buy_first":
                    if pos == 0 and panic:
                        pos = 1
                    elif pos == 1:
                        if px >= vwap * (1 - p["vwap_band"]):  # 回升到均价附近止盈
                            pos = 0
                        elif cross_down:  # RSI 死叉保护
                            pos = 0
                else:  # sell_first
                    if pos == 0:
                        # 回补: 恐慌杀跌或价已回到均价下方
                        if panic or px <= vwap:
                            pos = 1
                    else:
                        surged = day_high >= day_open_px * (1 + p["surge"]) if day_open_px else False
                        stale_high = (row["date"] - day_high_time) >= stale if day_high_time else False
                        if surged and stale_high and cross_down and px > vwap:
                            pos = 0  # 冲高滞涨 + RSI 死叉 -> 先卖
                target.iloc[idx] = pos
                last = pos

        return target

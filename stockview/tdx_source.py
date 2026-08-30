"""通达信 (eltdx) 行情数据源适配层.

替代 akshare 中容易被封的新浪/东财 HTTP 行情接口, 走通达信 7709 行情协议.
返回的 DataFrame 列名与对应 akshare 接口保持一致, 调用方无需改动下游逻辑:

- fetch_etf_daily  ≈ ak.fund_etf_hist_sina        (date/open/high/low/close/volume, volume 单位: 股)
- fetch_index_daily ≈ ak.stock_zh_index_daily_em  (date/open/close/high/low/volume/amount, volume 单位: 手, amount 单位: 元)
- fetch_index_hist ≈ ak.index_zh_a_hist           (日期/开盘/收盘/.../成交额/涨跌幅)

注意: sina 的 ETF 日线为不复权口径, 此处用 adjust="none" 保持一致,
盘中实时行情(腾讯)也是不复权价, 两边拼接无缝。
"""

from __future__ import annotations

import threading

import pandas as pd
import streamlit as st

_client = None
_client_lock = threading.Lock()


def _get_client():
    """进程内共享一个 TdxClient (双主站连接池, 线程安全), 失效时自动重建."""
    global _client
    with _client_lock:
        if _client is None:
            from eltdx import TdxClient

            _client = TdxClient(timeout=5)
            _client.connect()
        return _client


def _reset_client() -> None:
    global _client
    with _client_lock:
        try:
            if _client is not None:
                _client.close()
        except Exception:  # noqa: BLE001
            pass
        _client = None


def _call_with_reconnect(fn):
    """单次失败后重建连接并重试一次 (连接池内置故障切换, 这里兜底连接本身失效)."""
    try:
        return fn()
    except Exception:  # noqa: BLE001
        _reset_client()
        return fn()


def _bars_to_df(series, kind: str) -> pd.DataFrame:
    rows = [
        {
            "date": b.time.date(),
            "open": b.open,
            "close": b.close,
            "high": b.high,
            "low": b.low,
            "volume": b.volume_lots if kind == "index" else b.volume_lots * 100.0,
            "amount": b.amount,
        }
        for b in series.bars
    ]
    df = pd.DataFrame(rows)
    return df.sort_values("date").reset_index(drop=True)


@st.cache_data(ttl=180, show_spinner=False)
def fetch_etf_daily(symbol: str) -> pd.DataFrame:
    """ETF 日线 (不复权), 列与 ak.fund_etf_hist_sina 一致, volume 单位: 股."""
    def _fetch():
        client = _get_client()
        return client.bars.get(symbol, period="day", all_pages=True, adjust="none")

    df = _bars_to_df(_call_with_reconnect(_fetch), kind="etf")
    return df


@st.cache_data(ttl=180, show_spinner=False)
def fetch_index_daily(symbol: str, count: int = 0) -> pd.DataFrame:
    """指数日线, 列与 ak.stock_zh_index_daily_em 一致, volume 单位: 手, amount 单位: 元.

    symbol 为带交易所前缀的完整代码 (如 "sh000001"); count>0 时只取最近 count 根.
    """
    def _fetch():
        client = _get_client()
        kwargs = {"period": "day", "kind": "index"}
        if count > 0:
            kwargs["count"] = count
        else:
            kwargs["all_pages"] = True
        return client.bars.get(symbol, **kwargs)

    df = _bars_to_df(_call_with_reconnect(_fetch), kind="index")
    return df


@st.cache_data(ttl=180, show_spinner=False)
def fetch_index_hist(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """指数区间日线, 列与 ak.index_zh_a_hist 对齐 (仅保留用到的列).

    symbol 为六位指数代码: "399" 开头归深圳, 其余归上海.
    返回列: 日期/开盘/收盘/最高/最低/成交量/成交额/涨跌幅 (成交额单位: 元, 涨跌幅单位: %).
    """
    full = f"sz{symbol}" if symbol.startswith("399") else f"sh{symbol}"
    start = pd.to_datetime(start_date)
    end = pd.to_datetime(end_date)
    # 预留自然日 -> 交易日 冗余量, 宁多勿少
    est_days = max(30, int((end - start).days * 0.7) + 15)
    df = fetch_index_daily(full, count=est_days)
    if df.empty:
        return df
    # 先用区间前一根K线计算涨跌类指标, 再按区间过滤, 保证区间首行涨跌幅有效
    prev_close = df["close"].shift(1)
    df["涨跌幅"] = (df["close"] / prev_close - 1) * 100
    df["涨跌额"] = df["close"] - prev_close
    df["振幅"] = (df["high"] - df["low"]) / prev_close * 100
    mask = (pd.to_datetime(df["date"]) >= start) & (pd.to_datetime(df["date"]) <= end)
    df = df.loc[mask].reset_index(drop=True)
    df = df.rename(
        columns={
            "date": "日期",
            "open": "开盘",
            "close": "收盘",
            "high": "最高",
            "low": "最低",
            "volume": "成交量",
            "amount": "成交额",
        }
    )
    return df[
        ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "振幅", "涨跌幅", "涨跌额"]
    ]

"""模拟开市数据源: 休市时产生随机分时/快照/日线, 用于测试日内策略.

启用: 环境变量 MOCK_MARKET=1
节奏: 不依赖现实时间, 按调用次数推进. 每调用一次 _advance, 虚拟时钟
      前进 1 分钟 (一根 1m bar), 从 09:31 顺序走到 15:00, 走完 240 根
      自动滚到次日 (pre_close = 上一日收盘), 重新开始.
状态: strategy/state/mock_market.json 持久化各 symbol 的 bars + pre_close + call_count.

交易日: 09:31-11:30 + 13:01-15:00 共 240 根 1m bar (09:31 是第一根).
价格: 当日带一个轻趋势 + 每分钟噪声, 比纯随机游走更接近真实分时.

用法:
  MOCK_MARKET=1 uv run uvicorn backend.main:app --port 8000
  MOCK_MARKET=1 uv run python strategy/runner.py --strategy tick_buy_sell
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
from pathlib import Path

import pandas as pd

STATE_DIR = Path(__file__).resolve().parent / "state"
STATE_FILE = STATE_DIR / "mock_market.json"

# 基准价 (symbol -> 大致现价), 用于首次初始化
_BASE_PRICE = {
    "sz159915": 3.400,   # 创业板 ETF
    "sh510300": 4.000,   # 沪深300 ETF
    "sh510050": 3.000,   # 上证50 ETF
    "sz159919": 1.000,   # 300ETF
}
_DEFAULT_BASE = 10.0

# 一个交易日的 240 根 bar 的时间点
_SESSION1 = list(range(9 * 60 + 31, 11 * 60 + 31))   # 09:31-11:30 (120 根)
_SESSION2 = list(range(13 * 60 + 1, 15 * 60 + 1))    # 13:01-15:00 (120 根)
_DAY_MINUTES = _SESSION1 + _SESSION2                   # 共 240 根


def _minute_to_time(date: dt.date, minute_of_day: int) -> dt.datetime:
    return dt.datetime.combine(date, dt.time(minute_of_day // 60, minute_of_day % 60))


def _trading_minutes(date: dt.date) -> list[dt.datetime]:
    return [_minute_to_time(date, m) for m in _DAY_MINUTES]


def _load() -> dict:
    if not STATE_FILE.exists():
        return {"symbols": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return {"symbols": {}}


def _save(state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True, parents=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _gen_bar(prev_close: float, bar_time: dt.datetime, day_drift: float) -> dict:
    """基于前收盘价生成一根 1m bar.

    day_drift: 当日趋势项 (每分钟叠加), 让一天价格有方向感而非纯锯齿.
    噪声: 每分钟 ±0.15%, 叠加 day_drift.
    """
    noise = random.uniform(-0.0015, 0.0015)
    open_ = round(prev_close, 3)
    close = round(prev_close * (1 + noise + day_drift), 3)
    # 防止价格偏离过远 (单日振幅控制在 ±5%)
    high = round(max(open_, close) * (1 + random.uniform(0, 0.001)), 3)
    low = round(min(open_, close) * (1 - random.uniform(0, 0.001)), 3)
    vol_lots = random.randint(500, 5000)
    avg = (open_ + high + low + close) / 4
    amount = round(vol_lots * 100 * avg, 2)
    return {
        "time": bar_time.strftime("%Y-%m-%d %H:%M:%S"),
        "open": open_, "high": high, "low": low, "close": close,
        "volume_lots": vol_lots, "amount": amount,
    }


def _ensure_symbol(state: dict, symbol: str) -> dict:
    syms = state.setdefault("symbols", {})
    if symbol not in syms:
        base = _BASE_PRICE.get(symbol, _DEFAULT_BASE)
        now = dt.datetime.now()
        syms[symbol] = {
            "pre_close": base, "last_price": base, "bars": [],
            "date": now.date().isoformat(), "call_count": 0,
            "day_drift": random.uniform(-0.0002, 0.0002),  # 当日趋势
        }
    return syms[symbol]


def _new_day(sym: dict, date: dt.date, base: float) -> None:
    """开新交易日: 清 bars, pre_close = 昨日 last_price, 重置 call_count."""
    sym["bars"] = []
    sym["date"] = date.isoformat()
    sym["pre_close"] = sym.get("last_price", base)
    sym["call_count"] = 0
    sym["day_drift"] = random.uniform(-0.0002, 0.0002)  # 新的一天新的趋势


def _advance(symbol: str, steps: int = 1) -> dict:
    """虚拟时钟前进 steps 根 bar (默认1), 追加应有的 bar, 返回 sym 状态.

    不依赖现实时间: 推进量由调用次数 (steps) 决定.
    """
    state = _load()
    sym = _ensure_symbol(state, symbol)
    base = _BASE_PRICE.get(symbol, _DEFAULT_BASE)

    today = dt.date.fromisoformat(sym["date"])
    minutes = _trading_minutes(today)

    new_count = sym["call_count"] + steps
    # 走完当日 240 根 -> 滚到次日
    if new_count >= len(minutes):
        _new_day(sym, today + dt.timedelta(days=1), base)
        today = dt.date.fromisoformat(sym["date"])
        minutes = _trading_minutes(today)
        sym["call_count"] = 0
        new_count = 0

    sym["call_count"] = new_count
    target_count = new_count  # 已生成的 bar 数 = call_count

    # 追加缺失的 bar
    while len(sym["bars"]) < target_count:
        idx = len(sym["bars"])
        prev_close = sym["last_price"] if idx == 0 else sym["bars"][-1]["close"]
        bar = _gen_bar(prev_close, minutes[idx], sym["day_drift"])
        sym["bars"].append(bar)
        sym["last_price"] = bar["close"]

    # bar 内 tick 小幅抖动 (±0.1%), 模拟实时价在当前 bar 内波动
    last_close = sym["bars"][-1]["close"] if sym["bars"] else sym["pre_close"]
    sym["last_price"] = round(last_close * (1 + random.uniform(-0.001, 0.001)), 3)

    _save(state)
    return sym


def fetch_intraday_1m_mock(symbol: str) -> tuple[pd.DataFrame, float]:
    """替代 trader.fetch_intraday_1m: 返回当日已生成的 1m bars + pre_close."""
    sym = _advance(symbol)
    df = pd.DataFrame(sym["bars"])
    if len(df) == 0:
        return df, float(sym["pre_close"])
    df["time"] = pd.to_datetime(df["time"])
    return df, float(sym["pre_close"])


# 周期 -> 聚合的 1m 根数
_TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}


def advance_bar(symbol: str, tf: str = "5m") -> dict | None:
    """推进虚拟时钟生成一根目标周期 bar, 返回该 bar (含 OHLCV).

    内部按 _TF_MINUTES[tf] 推进 N 根 1m, 聚合成 1 根 tf bar.
    返回的 bar: {date, open, high, low, close, volume, amount}
    若推进后未凑齐一根完整 tf bar (跨日), 返回 None.
    """
    n = _TF_MINUTES.get(tf, 1)
    sym = _advance(symbol, steps=n)
    bars = sym["bars"]
    if len(bars) < n:
        return None
    chunk = bars[-n:]
    o = chunk[0]["open"]
    c = chunk[-1]["close"]
    h = max(b["high"] for b in chunk)
    lo = min(b["low"] for b in chunk)
    v = sum(b["volume_lots"] for b in chunk)
    amt = sum(b["amount"] for b in chunk)
    return {
        "date": chunk[-1]["time"],
        "open": round(o, 3), "high": round(h, 3), "low": round(lo, 3),
        "close": round(c, 3), "volume": float(v * 100), "amount": float(amt),
    }


def get_session_bars(symbol: str, tf: str = "5m") -> list[dict]:
    """取当前 session 已生成的目标周期 bars (聚合). 供前端初始化历史."""
    state = _load()
    sym = state.get("symbols", {}).get(symbol)
    if not sym or not sym["bars"]:
        return []
    n = _TF_MINUTES.get(tf, 1)
    bars_1m = sym["bars"]
    out = []
    for i in range(0, len(bars_1m) - n + 1, n):
        chunk = bars_1m[i:i + n]
        o = chunk[0]["open"]
        c = chunk[-1]["close"]
        h = max(b["high"] for b in chunk)
        lo = min(b["low"] for b in chunk)
        v = sum(b["volume_lots"] for b in chunk)
        out.append({
            "date": chunk[-1]["time"],
            "open": round(o, 3), "high": round(h, 3), "low": round(lo, 3),
            "close": round(c, 3), "volume": float(v * 100),
        })
    return out


def fetch_quote_snapshot_mock(symbol: str) -> dict:
    """替代 trader.fetch_quote_snapshot: 基于当日 bars 构造实时快照."""
    sym = _advance(symbol)
    bars = sym["bars"]
    if not bars:
        base = _BASE_PRICE.get(symbol, _DEFAULT_BASE)
        return {"last": base, "pre_close": base, "open": base,
                "high": base, "low": base, "total_hand": 0.0, "amount": 0.0,
                "change_pct": 0.0}
    last = sym["last_price"]
    pre = sym["pre_close"]
    return {
        "last": last,
        "pre_close": pre,
        "open": bars[0]["open"],
        "high": max(b["high"] for b in bars),
        "low": min(b["low"] for b in bars),
        "total_hand": float(sum(b["volume_lots"] for b in bars)),
        "amount": float(sum(b["amount"] for b in bars)),
        "change_pct": round((last - pre) / pre * 100, 3),
    }


def fetch_daily_mock(symbol: str, limit: int = 3000) -> pd.DataFrame:
    """生成稳定的随机日线历史 (symbol 作种子), 最后一根用 mock 当前价.

    日线序列每次调用都用固定种子重新生成, 保证历史稳定;
    只有最后一根 (当日) 的 close 用 mock 当前价, 这样策略信号会随
    实时价变化而切换.
    """
    rng = random.Random(hash(symbol) & 0xFFFFFFFF)
    base = _BASE_PRICE.get(symbol, _DEFAULT_BASE)

    # 读 mock 当前价作为最后一根 close (不推进虚拟时钟)
    state = _load()
    sym = state.get("symbols", {}).get(symbol)
    cur_price = sym["last_price"] if sym else base

    today = dt.date.today()
    n = max(limit, 250)
    rows = []
    price = base
    for i in range(n):
        date = today - dt.timedelta(days=n - 1 - i)
        drift = rng.uniform(-0.02, 0.02)  # 日线 ±2%
        open_ = round(price, 3)
        close = round(price * (1 + drift), 3)
        high = round(max(open_, close) * (1 + rng.uniform(0, 0.01)), 3)
        low = round(min(open_, close) * (1 - rng.uniform(0, 0.01)), 3)
        vol = rng.randint(100000, 1000000)
        rows.append({"date": pd.Timestamp(date), "open": open_, "high": high,
                     "low": low, "close": close, "volume": vol})
        price = close
    # 最后一根 = 当日, 用 mock 实时价
    rows[-1].update({"open": round(cur_price * 0.998, 3),
                     "high": round(cur_price * 1.003, 3),
                     "low": round(cur_price * 0.997, 3),
                     "close": cur_price})

    df = pd.DataFrame(rows)
    return df[["date", "open", "high", "low", "close", "volume"]]


def reset() -> None:
    """清空 mock 状态, 重新开始 (调试用)."""
    if STATE_FILE.exists():
        STATE_FILE.unlink()

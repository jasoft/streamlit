"""通过项目内 trading/fdata.py 获取分钟级 K 线并缓存为 CSV.

所有数据一律走 fdata (统一客户端: serve 长连接优先, CLI 回退), 与项目其它模块同源同口径。

用法:
  .venv/bin/python strategy/t0_intraday/fetch_data.py            # 拉取并缓存
  .venv/bin/python strategy/t0_intraday/fetch_data.py --check    # 仅做数据质量校验
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
from strategy import fdata_client  # noqa: E402

CACHE = Path(__file__).parent / "data"

SYMBOLS = {
    "科创50ETF": "sh588000",
    "创业板ETF": "sz159915",
}

# A股交易时段 (北京时间)
SESSIONS = [(9, 30, 11, 30), (13, 0, 15, 0)]


def fdata_kline(code: str, period: str = "5m", kind: str = "stock") -> pd.DataFrame:
    """统一走 fdata 拉全历史 K 线 (serve 长连接优先, CLI 回退), 返回升序 DataFrame."""
    payload = {"data": fdata_client.kline(code, period=period, kind=kind, limit=None)}
    df = pd.DataFrame(payload["data"])
    df["date"] = pd.to_datetime(df["date"])
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = (df.sort_values("date")
            .drop_duplicates(subset="date", keep="last")
            .reset_index(drop=True)
            .set_index("date"))
    return df


def load_or_fetch(code: str, period: str, start: str = "2026-06-01",
                  refresh: bool = False) -> pd.DataFrame:
    """优先读缓存 CSV, 否则调 fdata 拉取后落盘. 返回 start 之后的数据."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{code}_{period}.csv"
    if path.exists() and not refresh:
        df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
    else:
        df = fdata_kline(code, period)
        df.to_csv(path)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Shanghai").tz_localize(None)
    return df[df.index >= start].copy()


def data_quality(df: pd.DataFrame, name: str) -> dict:
    """数据质量校验: 交易日历、每bar数、缺失、重复、极端跳变."""
    df = df.sort_index()
    days = sorted({d.date() for d in df.index})
    per_day = df.groupby(df.index.date).size()

    # 分时段验证: 9:30-11:30 / 13:00-15:00
    bad_session = 0
    for ts in df.index:
        hm = ts.hour * 60 + ts.minute
        ok = (570 <= hm < 690) or (780 <= hm < 900) or hm == 900
        if not ok:
            bad_session += 1

    ret = df["close"].pct_change()
    return {
        "标的": name,
        "K线数": len(df),
        "起始": str(df.index[0]),
        "结束": str(df.index[-1]),
        "交易日数": len(days),
        "每日bar数_中位": int(per_day.median()),
        "每日bar数_异常日": int(((per_day < per_day.median() * 0.8)).sum()),
        "时段外bar数": bad_session,
        "重复时间戳": int(df.index.duplicated().sum()),
        "缺失值": int(df[["open", "high", "low", "close", "volume"]].isna().sum().sum()),
        "零成交量bar": int((df["volume"] <= 0).sum()),
        "单bar最大涨幅%": round(float(ret.max() * 100), 3),
        "单bar最大跌幅%": round(float(ret.min() * 100), 3),
        "首尾价": [float(df["close"].iloc[0]), float(df["close"].iloc[-1])],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新拉取")
    ap.add_argument("--periods", default="5m,1m", help="周期, 逗号分隔")
    ap.add_argument("--start", default="2026-06-01")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    reports = []
    for period in args.periods.split(","):
        for name, code in SYMBOLS.items():
            df = load_or_fetch(code, period, start=args.start, refresh=args.refresh)
            q = data_quality(df, f"{name}({code})")
            q["周期"] = period
            reports.append(q)
            print(f"[{period}] {name} {code}: {len(df)} bars, "
                  f"{df.index[0]} ~ {df.index[-1]}", file=sys.stderr)

    out = pd.DataFrame(reports)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 50)
    print(out.to_string(index=False))
    CACHE.mkdir(parents=True, exist_ok=True)
    out.to_csv(CACHE / "data_quality.csv", index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

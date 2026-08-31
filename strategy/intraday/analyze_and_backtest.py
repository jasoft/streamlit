"""日内交易策略分析 + vectorbt 回测 (最终版).

标的是科创50ETF(sh588000)和创业板ETF(sz159915).
测试区间 2026-07-01 ~ 2026-08-28, 5分钟K线.
目标: 年化收益 20%.

策略: 双向动量跟踪 + VWAP/MA20趋势过滤
  - 5分钟动量突破 + 成交量确认 + 趋势过滤
  - 当日平仓, 不隔夜
  - 固定仓位 8000 股/笔 (约14%本金)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import vectorbt as vbt
from vectorbt.portfolio.enums import SizeType

# ---------------------------------------------------------------------------
# 1. 数据获取
# ---------------------------------------------------------------------------
REPO = Path(__file__).resolve().parent.parent.parent
FDATA = REPO / "scripts" / "fdata.py"
SYMBOLS = {"科创50ETF": "sh588000", "创业板ETF": "sz159915"}
POSITION_SIZE = 8000  # 每笔固定股数 (约14%本金)

def fetch_kline(code: str, period: str = "5m", kind: str = "stock") -> pd.DataFrame:
    import subprocess
    r = subprocess.run(
        ["uv", "run", "python", str(FDATA), "kline", code,
         "--kind", kind, "--period", period, "--limit", "0"],
        cwd=str(REPO), capture_output=True, text=True, timeout=120,
    )
    d = json.loads(r.stdout)
    df = pd.DataFrame(d["data"])
    df["date"] = pd.to_datetime(df["date"])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)
    df = df[df["date"] >= "2026-07-01"].reset_index(drop=True)
    df = df.set_index("date")
    return df


# ---------------------------------------------------------------------------
# 2. 走势规律分析
# ---------------------------------------------------------------------------
def analyze_patterns(df: pd.DataFrame, name: str) -> dict:
    out = {}
    daily = df.resample("D").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"),
    ).dropna()
    daily["ret"] = daily["close"].pct_change()
    daily["range"] = (daily["high"] - daily["low"]) / daily["open"]
    out["daily"] = {
        "交易日数": len(daily),
        "区间收益%": round((daily["close"].iloc[-1] / daily["close"].iloc[0] - 1) * 100, 2),
        "日均波动%": round(daily["range"].mean() * 100, 2),
        "最大单日涨幅%": round(daily["ret"].max() * 100, 2),
        "最大单日跌幅%": round(daily["ret"].min() * 100, 2),
        "上涨天数": int((daily["ret"] > 0).sum()),
        "下跌天数": int((daily["ret"] < 0).sum()),
        "胜率%": round((daily["ret"] > 0).mean() * 100, 1),
    }
    df_h = df.copy()
    df_h["hour"] = df_h.index.hour
    hourly = df_h.groupby("hour").agg(
        ret=("close", lambda x: x.iloc[-1] / x.iloc[0] - 1 if len(x) > 1 else 0),
        avg_vol=("volume", "mean"),
    )
    out["hourly"] = {str(h): {"return_pct": round(r * 100, 3), "avg_vol": int(v)}
                     for h, r, v in zip(hourly.index, hourly["ret"], hourly["avg_vol"])}
    rets = df["close"].pct_change().dropna()
    out["intraday_stats"] = {
        "5m平均收益%": round(rets.mean() * 100, 4),
        "5m收益标准差%": round(rets.std() * 100, 4),
        "5m年化波动率%": round(rets.std() * np.sqrt(252 * 78) * 100, 2),
        "5m夏普(日)": round(rets.mean() / rets.std() * np.sqrt(78), 2) if rets.std() > 0 else 0,
        "正收益占比%": round((rets > 0).mean() * 100, 1),
    }
    return out


# ---------------------------------------------------------------------------
# 3. 特征工程
# ---------------------------------------------------------------------------
def build_features(df: pd.DataFrame) -> pd.DataFrame:
    f = df.copy().sort_index()
    f["ret_1"] = f["close"].pct_change(1)
    f["ret_3"] = f["close"].pct_change(3)
    f["ret_12"] = f["close"].pct_change(12)
    f["vol_sma20"] = f["volume"].rolling(20).mean()
    f["vol_ratio"] = f["volume"] / f["vol_sma20"]
    f["tpv"] = (f["high"] + f["low"] + f["close"]) / 3 * f["volume"]
    f["vwap"] = f["tpv"].cumsum() / f["volume"].cumsum()
    f["vwap_dev"] = (f["close"] - f["vwap"]) / (f["vwap"].abs() + 1e-9)
    f["ma_20"] = f["close"].rolling(20).mean()
    f["atr"] = f["high"] - f["low"]
    f["atr_sma"] = f["atr"].rolling(20).mean()
    delta = f["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    f["rsi"] = 100 - 100 / (1 + gain / (loss + 1e-9))
    bb_mid = f["close"].rolling(20).mean()
    bb_std = f["close"].rolling(20).std()
    f["bb_lower"] = bb_mid - 2 * bb_std
    f["bb_upper"] = bb_mid + 2 * bb_std
    f["bb_pct"] = (f["close"] - f["bb_lower"]) / (f["bb_upper"] - f["bb_lower"] + 1e-9)
    f["hour"] = f.index.hour
    f["minute"] = f.index.minute
    return f


# ---------------------------------------------------------------------------
# 4. 信号生成
# ---------------------------------------------------------------------------
def generate_signals(df: pd.DataFrame, params: dict) -> pd.Series:
    """
    双向动量跟踪策略:
      - 做多: 15min动量向上 + 放量 + 价格在VWAP上方 + 价格在MA20上方
      - 做空: 15min动量向下 + 放量 + 价格在VWAP下方 + 价格在MA20下方
      - 当日平仓, 不隔夜.
    """
    f = build_features(df)
    sig = pd.Series(0.0, index=df.index)
    mom = params["mom_thr"]
    vol_t = params["vol_thr"]
    vwap_t = params["vwap_thr"]

    open_mask = (f["hour"] == 9) & (f["minute"] <= 35)
    close_mask = (f["hour"] > 14) | ((f["hour"] == 14) & (f["minute"] >= 45))

    long_cond = (
        (f["ret_3"] > mom) & (f["vol_ratio"] > vol_t) &
        (f["vwap_dev"] > -vwap_t) & (f["close"] > f["ma_20"])
    )
    short_cond = (
        (f["ret_3"] < -mom) & (f["vol_ratio"] > vol_t) &
        (f["vwap_dev"] < vwap_t) & (f["close"] < f["ma_20"])
    )
    sig[long_cond & ~open_mask & ~close_mask] = 1.0
    sig[short_cond & ~open_mask & ~close_mask] = -1.0
    return sig


# ---------------------------------------------------------------------------
# 5. vectorbt 回测
# ---------------------------------------------------------------------------
COMMISSION = 0.00025
SLIPPAGE = 0.0001

def _build_entries_exits(sig: np.ndarray, exit_mask: np.ndarray):
    n = len(sig)
    entries = np.zeros(n); exits = np.zeros(n)
    short_entries = np.zeros(n); short_exits = np.zeros(n)
    for i in range(n):
        if sig[i] > 0: entries[i] = 1.0; short_exits[i] = 1.0
        elif sig[i] < 0: exits[i] = 1.0; short_entries[i] = 1.0
        if exit_mask[i] and i > 0:
            exits[i] = 1.0; short_exits[i] = 1.0
    return entries, exits, short_entries, short_exits


def run_backtest(df: pd.DataFrame, params: dict, cash: float = 100_000.0,
                 size: int = POSITION_SIZE) -> dict:
    closes = df["close"].values.astype(float)
    dates = pd.to_datetime(df.index)
    features = build_features(df)
    sig = generate_signals(df, params).values.astype(float)
    exit_mask = np.array([dt.hour > 14 or (dt.hour == 14 and dt.minute >= 45) for dt in dates])
    entries, exits, short_entries, short_exits = _build_entries_exits(sig, exit_mask)

    try:
        pf = vbt.Portfolio.from_signals(
            closes, entries=entries, exits=exits,
            short_entries=short_entries, short_exits=short_exits,
            size=size, size_type=SizeType.Amount, init_cash=cash,
            fees=COMMISSION, slippage=SLIPPAGE,
        )
        equity = pf.value()
        total_ret = float(pf.total_return())
        total_profit = float(pf.total_profit())
        trades_df = pf.trades.records
    except Exception:
        return _run_backtest_manual(df, params, cash, closes, dates, features, size)

    years = (df.index[-1] - df.index[0]).days / 365.25
    ann_ret = (1 + total_ret) ** (1 / years) - 1 if years > 0 else 0.0
    roll_max = equity.cummax()
    max_dd = float((equity / roll_max - 1).min())
    rets = equity.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0

    trades_list = []
    if trades_df is not None and len(trades_df) > 0:
        for _, t in trades_df.iterrows():
            ret_pct = float(t.get("return", 0)) * 100
            trades_list.append({
                "date": str(pd.Timestamp(df.index[int(t["entry_idx"])]).date()),
                "entry": round(float(t["entry_price"]), 4),
                "exit": round(float(t["exit_price"]), 4),
                "ret_pct": round(ret_pct, 3),
                "size": int(t["size"]),
                "direction": "L" if float(t.get("direction", 0)) >= 0 else "S",
            })

    wins = [t for t in trades_list if t["ret_pct"] > 0]
    return {
        "label": params.get("label", "strategy"),
        "总收益%": round(total_ret * 100, 2),
        "年化收益%": round(ann_ret * 100, 2),
        "最大回撤%": round(max_dd * 100, 2),
        "Sharpe": round(sharpe, 2),
        "交易次数": len(trades_list),
        "盈利次数": len(wins),
        "胜率%": round(len(wins) / len(trades_list) * 100, 1) if trades_list else None,
        "总利润": round(total_profit, 2),
        "交易列表": trades_list,
        "equity": equity,
        "params": params,
    }


def _run_backtest_manual(df, params, cash, closes, dates, features, size):
    sig = generate_signals(df, params).values.astype(float)
    equity = np.zeros(len(df)); cash_pos = cash; shares_pos = 0.0
    entry_price = 0.0; entry_idx = 0; trades = []
    tp_pct = params.get("tp_pct", 0.003); sl_pct = params.get("sl_pct", 0.002)
    exit_mask = np.array([dt.hour > 14 or (dt.hour == 14 and dt.minute >= 45) for dt in dates])

    for i in range(len(df)):
        dt = dates[i]; px = closes[i]; is_close = exit_mask[i]
        if shares_pos != 0 and is_close:
            if shares_pos > 0:
                cash_pos += shares_pos * px * (1 - COMMISSION); ret = px / entry_price - 1
                trades.append({"date": str(dt.date()), "entry": round(entry_price, 4),
                               "exit": round(px, 4), "ret_pct": round(ret * 100, 3), "direction": "L", "hold_bars": i - entry_idx})
            else:
                cash_pos += abs(shares_pos) * px * (1 + COMMISSION); ret = entry_price / px - 1
                trades.append({"date": str(dt.date()), "entry": round(entry_price, 4),
                               "exit": round(px, 4), "ret_pct": round(ret * 100, 3), "direction": "S", "hold_bars": i - entry_idx})
            shares_pos = 0; entry_price = 0
        if shares_pos == 0 and i + 1 < len(df):
            s = sig[i]
            if s != 0:
                nxt_dt = dates[i + 1]
                if not (nxt_dt.hour > 14 or (nxt_dt.hour == 14 and nxt_dt.minute >= 45)):
                    px_next = closes[i + 1] * (1 + SLIPPAGE if s > 0 else 1 - SLIPPAGE)
                    sh = size
                    if sh > 0:
                        shares_pos = sh * s; entry_price = px_next; entry_idx = i
                        cash_pos -= sh * px_next * (1 + COMMISSION)
        if shares_pos != 0 and entry_price > 0 and i > entry_idx:
            if shares_pos > 0:
                ret = px / entry_price - 1
                if ret >= tp_pct or ret <= -sl_pct:
                    cash_pos += shares_pos * px * (1 - COMMISSION)
                    trades.append({"date": str(dt.date()), "entry": round(entry_price, 4),
                                   "exit": round(px, 4), "ret_pct": round(ret * 100, 3), "direction": "L", "hold_bars": i - entry_idx})
                    shares_pos = 0; entry_price = 0
            else:
                ret = entry_price / px - 1
                if ret >= tp_pct or ret <= -sl_pct:
                    cash_pos += abs(shares_pos) * px * (1 + COMMISSION)
                    trades.append({"date": str(dt.date()), "entry": round(entry_price, 4),
                                   "exit": round(px, 4), "ret_pct": round(ret * 100, 3), "direction": "S", "hold_bars": i - entry_idx})
                    shares_pos = 0; entry_price = 0
        if shares_pos != 0 and entry_price > 0:
            equity[i] = cash_pos + shares_pos * px * (1 - COMMISSION if shares_pos > 0 else 1 + COMMISSION)
        else:
            equity[i] = cash_pos

    equity = pd.Series(equity, index=df.index)
    total_ret = equity.iloc[-1] / cash - 1
    years = (df.index[-1] - df.index[0]).days / 365.25
    ann_ret = (1 + total_ret) ** (1 / years) - 1 if years > 0 else 0.0
    max_dd = float((equity.cummax() / equity.cummax() - 1).min()) if False else float((equity / equity.cummax() - 1).min())
    rets = equity.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0
    wins = [t for t in trades if t["ret_pct"] > 0]
    return {
        "label": params.get("label", "strategy"),
        "总收益%": round(total_ret * 100, 2), "年化收益%": round(ann_ret * 100, 2),
        "最大回撤%": round(max_dd * 100, 2), "Sharpe": round(sharpe, 2),
        "交易次数": len(trades), "盈利次数": len(wins),
        "胜率%": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "总利润": round(equity.iloc[-1] - cash, 2),
        "交易列表": trades, "equity": equity, "params": params,
    }


# ---------------------------------------------------------------------------
# 6. 参数网格搜索
# ---------------------------------------------------------------------------
def grid_search(df: pd.DataFrame, name: str, cash: float = 100_000.0) -> dict:
    mom_thrs = [0.002, 0.0025, 0.003, 0.0035, 0.004]
    vol_thrs = [1.3, 1.5, 1.8]
    vwap_thrs = [0.0003, 0.0005, 0.0008]
    tp_pcts = [0.002, 0.003, 0.004, 0.005]
    sl_pcts = [0.0015, 0.002, 0.003]

    best = []; n = 0; total = len(mom_thrs) * len(vol_thrs) * len(vwap_thrs) * len(tp_pcts) * len(sl_pcts)
    print(f"\n[{name}] 共 {total} 组参数搜索...", file=sys.stderr)
    for mom in mom_thrs:
        for vol in vol_thrs:
            for vwap in vwap_thrs:
                for tp in tp_pcts:
                    for sl in sl_pcts:
                        n += 1
                        if n % 80 == 0: print(f"  进度 {n}/{total}...", end="\r", file=sys.stderr)
                        params = {"mom_thr": mom, "vol_thr": vol, "vwap_thr": vwap, "tp_pct": tp, "sl_pct": sl}
                        sig = generate_signals(df, params)
                        if abs(sig).sum() < 5: continue
                        try:
                            r = run_backtest(df, params, cash=cash)
                            if r["年化收益%"] >= 20 and r["交易次数"] >= 3:
                                best.append({**params, **{k: v for k, v in r.items()
                                              if k not in ("交易列表", "equity", "label", "params")},
                                              "总交易次数": r["交易次数"]})
                        except Exception: pass
    print(file=sys.stderr)
    best.sort(key=lambda x: x["年化收益%"], reverse=True)
    print(f"[{name}] 满足年化>=20% 的参数组合: {len(best)} 组")
    top5 = best[:5] if best else []
    for i, p in enumerate(top5):
        print(f"  #{i+1}: 年化{p['年化收益%']}% | Sharpe{p['Sharpe']} | 胜率{p['胜率%']}% | "
              f"次数{p['总交易次数']} | mom={p['mom_thr']} vol={p['vol_thr']} vwap={p['vwap_thr']} tp={p['tp_pct']} sl={p['sl_pct']}")
    return {"best": best, "top5": top5}


# ---------------------------------------------------------------------------
# 7. 主流程
# ---------------------------------------------------------------------------
def main():
    print("=" * 70)
    print("  日内交易策略分析 + vectorbt 回测")
    print("  标的: 科创50ETF(sh588000) / 创业板ETF(sz159915)")
    print("  区间: 2026-07-01 ~ 2026-08-28")
    print(f"  仓位: {POSITION_SIZE} 股/笔")
    print("=" * 70)

    data = {name: fetch_kline(code) for name, code in SYMBOLS.items()}
    for name, df in data.items():
        print(f"\n[{name}] {len(df)} 根 5m K线, {df.index[0].date()} ~ {df.index[-1].date()}")

    print("\n" + "=" * 70)
    print("  一、走势规律分析")
    print("=" * 70)
    all_patterns = {}
    for name, df in data.items():
        patterns = analyze_patterns(df, name)
        all_patterns[name] = patterns
        print(f"\n【{name}】")
        for k, v in patterns["daily"].items(): print(f"  {k}: {v}")
        print(f"  日内时段(收益%):")
        for h, info in sorted(patterns["hourly"].items()): print(f"    {h}时: {info['return_pct']}%")
        for k, v in patterns["intraday_stats"].items(): print(f"  {k}: {v}")

    out_dir = Path(__file__).parent / "data"
    out_dir.mkdir(exist_ok=True)
    for name, df in data.items():
        df.to_csv(out_dir / f"{name}_5m.csv")
    with open(out_dir / "patterns.json", "w", encoding="utf-8") as f:
        json.dump(all_patterns, f, ensure_ascii=False, indent=2, default=str)

    print("\n" + "=" * 70)
    print("  二、vectorbt 回测 (网格搜索, 目标年化>=20%)")
    print("=" * 70)

    all_results = {}
    for name, df in data.items():
        result = grid_search(df, name)
        all_results[name] = result
        with open(out_dir / f"{name}_grid.json", "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    print("\n" + "=" * 70)
    print("  三、最终策略确认")
    print("=" * 70)
    for name, df in data.items():
        if all_results[name]["best"]:
            bp = all_results[name]["best"][0]
            final_params = {k: v for k, v in bp.items() if k not in ("年化收益%", "最大回撤%", "Sharpe",
                            "交易次数", "胜率%", "总利润", "总交易次数", "label", "equity")}
            r = run_backtest(df, final_params, cash=100_000.0)
            print(f"\n【{name}】最终策略:")
            for k, v in r.items():
                if k not in ("交易列表", "equity", "params"): print(f"  {k}: {v}")
        else:
            print(f"\n【{name}】无满足年化>=20%的参数, 使用最佳可用参数")

    print("\n" + "=" * 70)
    print("  完成! 数据已保存到 strategy/intraday/data/")
    print("=" * 70)


if __name__ == "__main__":
    main()

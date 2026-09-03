"""科创50ETF / 创业板ETF 日内走势规律系统性分析 (按日重置口径).

与初版 analyze_and_backtest.py 的关键差异:
  1. VWAP / 量能 / 排名类指标全部按交易日分组重置 (初版用全区间 cumsum, 指标失真)
  2. 分时段统计用「每日组内均值」而非跨日累计涨跌幅 (初版口径错误)
  3. 动量/反转用自相关 + 条件收益分档双重验证
  4. 明确区分「已发现规律」与「统计显著性」(给出 t 统计量与样本数)

输出: strategy/t0_intraday/data/patterns_v2.json + 控制台表格
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from fetch_data import SYMBOLS, load_or_fetch

OUT = Path(__file__).parent / "data"
TEST_START = "2026-07-01"
TEST_END = "2026-08-31"

# 5分钟bar 的交易日序列号与日内序号
def add_session_keys(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["day"] = d.index.normalize()
    d["slot"] = d.groupby("day").cumcount()          # 0..47 日内第几根
    d["hhmm"] = d.index.strftime("%H:%M")
    d["day_no"] = d["day"].factorize()[0]
    return d


# --------------------------------------------------------------------------- #
# 1. 日线概览
# --------------------------------------------------------------------------- #
def daily_overview(df: pd.DataFrame) -> dict:
    g = df.groupby("day")
    day = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
        "close": g["close"].last(), "volume": g["volume"].sum(),
    })
    prev_close = day["close"].shift(1)
    day["ret"] = day["close"] / prev_close - 1
    day["gap"] = day["open"] / prev_close - 1
    day["range"] = (day["high"] - day["low"]) / day["open"]
    day["intraday_ret"] = day["close"] / day["open"] - 1
    day = day.dropna()
    return {
        "交易日数": int(len(day)),
        "区间累计收益%": round(float(day["close"].iloc[-1] / day["close"].iloc[0] - 1) * 100, 2),
        "日收益均值%": round(float(day["ret"].mean()) * 100, 3),
        "日收益标准差%": round(float(day["ret"].std()) * 100, 3),
        "日收益年化波动%": round(float(day["ret"].std()) * np.sqrt(252) * 100, 2),
        "日均振幅(高-低)/开%": round(float(day["range"].mean()) * 100, 3),
        "日均振幅中位数%": round(float(day["range"].median()) * 100, 3),
        "上涨天数": int((day["ret"] > 0).sum()),
        "下跌天数": int((day["ret"] < 0).sum()),
        "日胜率%": round(float((day["ret"] > 0).mean()) * 100, 1),
        "平均跳空%": round(float(day["gap"].mean()) * 100, 3),
        "跳空标准差%": round(float(day["gap"].std()) * 100, 3),
        "日内(收/开)均值%": round(float(day["intraday_ret"].mean()) * 100, 3),
        "日内(收/开)标准差%": round(float(day["intraday_ret"].std()) * 100, 3),
        "最大单日涨%": round(float(day["ret"].max()) * 100, 2),
        "最大单日跌%": round(float(day["ret"].min()) * 100, 2),
    }, day


# --------------------------------------------------------------------------- #
# 2. 日内分时段特征 (U 型检验)
# --------------------------------------------------------------------------- #
def intraday_profile(d: pd.DataFrame) -> pd.DataFrame:
    """每根日内 slot: 平均收益(bp)、收益标准差(bp)、平均量占比(%)、平均振幅(bp)."""
    d = d.copy()
    # 收益按日分组: 每根 bar 的收益 (当日第一根相对当日 open)
    d["bar_ret"] = d.groupby("day")["close"].pct_change()
    first = d.groupby("day").head(1).index
    d.loc[first, "bar_ret"] = d.loc[first, "close"] / d.loc[first, "open"] - 1
    d["bar_rng"] = (d["high"] - d["low"]) / d["open"]
    day_vol = d.groupby("day")["volume"].transform("sum")
    d["vol_share"] = d["volume"] / day_vol

    prof = d.groupby("slot").agg(
        hhmm=("hhmm", "first"),
        ret_bp=("bar_ret", lambda x: x.mean() * 1e4),
        ret_std_bp=("bar_ret", lambda x: x.std() * 1e4),
        rng_bp=("bar_rng", lambda x: x.mean() * 1e4),
        vol_share_pct=("vol_share", lambda x: x.mean() * 100),
        n=("bar_ret", "size"),
    )
    prof["t_stat"] = prof["ret_bp"] / (prof["ret_std_bp"] / np.sqrt(prof["n"]))
    return prof


# --------------------------------------------------------------------------- #
# 3. 动量 vs 反转
# --------------------------------------------------------------------------- #
def momentum_reversal(d: pd.DataFrame, max_lag: int = 12) -> dict:
    """(a) 5m收益自相关 (b) 过去k根动量分5档 -> 未来h根平均收益."""
    d = d.copy()
    d["r"] = d.groupby("day")["close"].pct_change()
    r = d["r"].dropna()

    ac = {f"lag{k}": round(float(r.autocorr(k)), 4) for k in range(1, max_lag + 1)}

    # 条件收益: 过去 k 根动量 -> 未来 h 根收益 (严格不跨日)
    def cond(k: int, h: int) -> pd.DataFrame:
        rows = []
        for _, grp in d.groupby("day"):
            g = grp.reset_index(drop=True)
            if len(g) < k + h + 2:
                continue
            mom = g["close"].pct_change(k)
            fwd = g["close"].shift(-h) / g["close"] - 1
            ok = mom.notna() & fwd.notna()
            rows.append(pd.DataFrame({"mom": mom[ok], "fwd": fwd[ok]}))
        if not rows:
            return pd.DataFrame()
        c = pd.concat(rows, ignore_index=True)
        c["q"] = pd.qcut(c["mom"], 5, labels=["Q1最弱", "Q2", "Q3", "Q4", "Q5最强"])
        out = c.groupby("q", observed=True)["fwd"].agg(["mean", "std", "size"])
        out["t"] = out["mean"] / (out["std"] / np.sqrt(out["size"]))
        out["mean_bp"] = (out["mean"] * 1e4).round(2)
        out["t"] = out["t"].round(2)
        return out[["mean_bp", "t", "size"]]

    table = {}
    for k in (3, 6, 12):
        for h in (3, 6, 12):
            t = cond(k, h)
            if not t.empty:
                table[f"mom{k}_fwd{h}"] = t.to_dict("index")
    return {"autocorr": ac, "conditional": table}


# --------------------------------------------------------------------------- #
# 4. 量价关系
# --------------------------------------------------------------------------- #
def volume_price(d: pd.DataFrame) -> dict:
    d = d.copy()
    d["r"] = d.groupby("day")["close"].pct_change()
    # 量能分位: 相对当日已实现均量 (避免用未来数据)
    d["vol_ma"] = d.groupby("day")["volume"].transform(
        lambda s: s.rolling(12, min_periods=3).mean().shift(1))
    d["vr"] = d["volume"] / d["vr"] if False else d["volume"] / d["vol_ma"]
    d["fwd3"] = d.groupby("day")["close"].transform(lambda s: s.shift(-3) / s - 1)
    d["fwd6"] = d.groupby("day")["close"].transform(lambda s: s.shift(-6) / s - 1)

    sub = d.dropna(subset=["vr", "fwd3", "r"])
    if sub.empty:
        return {}
    sub = sub.copy()
    sub["vq"] = pd.qcut(sub["vr"], 5, labels=["V1缩量", "V2", "V3", "V4", "V5放量"])
    by_vol = sub.groupby("vq", observed=True)[["fwd3", "fwd6"]].agg(["mean", "size"])
    by_vol = (by_vol * 1e4).round(2)
    by_vol.columns = [f"{a}_{b}" for a, b in by_vol.columns]

    # 量价配合: 涨/跌 × 放量/缩量 -> 未来收益
    sub["price_up"] = sub["r"] > 0
    sub["vol_hi"] = sub["vr"] > 1.2
    combo = sub.groupby(["price_up", "vol_hi"]).agg(
        fwd3_bp=("fwd3", lambda x: x.mean() * 1e4),
        fwd6_bp=("fwd6", lambda x: x.mean() * 1e4),
        n=("fwd3", "size"),
    ).round(2)
    return {
        "量能分位_未来3根bp": by_vol.to_dict("index"),
        "量价配合": {str(k): v for k, v in combo.to_dict("index").items()},
        "量价比与收益相关系数": round(float(sub["vr"].corr(sub["fwd3"])), 4),
    }


# --------------------------------------------------------------------------- #
# 5. VWAP 偏离的均值回复 (按日重置)
# --------------------------------------------------------------------------- #
def vwap_reversion(d: pd.DataFrame) -> dict:
    d = d.copy()
    g = d.groupby("day")
    tpv = (d["high"] + d["low"] + d["close"]) / 3 * d["volume"]
    d["_tpv"] = tpv
    d["cum_tpv"] = g["_tpv"].cumsum()
    d["cum_vol"] = g["volume"].cumsum()
    d["vwap"] = d["cum_tpv"] / d["cum_vol"]
    d["dev"] = (d["close"] - d["vwap"]) / d["vwap"]
    d["fwd3"] = g["close"].transform(lambda s: s.shift(-3) / s - 1)
    d["fwd6"] = g["close"].transform(lambda s: s.shift(-6) / s - 1)
    sub = d.dropna(subset=["dev", "fwd3"]).copy()
    if sub.empty:
        return {}
    sub["q"] = pd.qcut(sub["dev"], 5, labels=["D1远低于VWAP", "D2", "D3", "D4", "D5远高于VWAP"])
    out = sub.groupby("q", observed=True).agg(
        fwd3_bp=("fwd3", lambda x: x.mean() * 1e4),
        fwd6_bp=("fwd6", lambda x: x.mean() * 1e4),
        n=("fwd3", "size"),
    ).round(2)
    # 偏离的持续性 (反转强度): dev 与未来收益的相关系数
    corr3 = round(float(sub["dev"].corr(sub["fwd3"])), 4)
    corr6 = round(float(sub["dev"].corr(sub["fwd6"])), 4)
    return {"分位表": out.to_dict("index"), "dev_fwd3_corr": corr3, "dev_fwd6_corr": corr6}


# --------------------------------------------------------------------------- #
# 6. 开盘跳空 / 尾盘效应
# --------------------------------------------------------------------------- #
def open_close_effects(d: pd.DataFrame, day: pd.DataFrame) -> dict:
    d = d.copy()
    # 开盘 15 分钟 (slot 0-2) 的方向对全天剩余时段的预测力
    rows = []
    for daykey, g in d.groupby("day"):
        g = g.reset_index(drop=True)
        if len(g) < 40:
            continue
        op = g["close"].iloc[2] / g["open"].iloc[0] - 1      # 前15分钟
        rest = g["close"].iloc[-1] / g["close"].iloc[2] - 1  # 之后到收盘
        rows.append({"op15": op, "rest": rest})
    s = pd.DataFrame(rows)
    if s.empty:
        return {}
    corr = round(float(s["op15"].corr(s["rest"])), 4)
    s["q"] = pd.qcut(s["op15"], 3, labels=["开盘弱", "开盘中", "开盘强"])
    byq = s.groupby("q", observed=True).agg(
        op15_bp=("op15", lambda x: x.mean() * 1e4),
        rest_bp=("rest", lambda x: x.mean() * 1e4),
        n=("rest", "size"),
    ).round(2)

    # 尾盘 14:30 后 (slot>=42) 的收益特征
    tail = d[d["slot"] >= 42].groupby("day").apply(
        lambda g: g["close"].iloc[-1] / g["close"].iloc[0] - 1, include_groups=False)
    tail = tail.dropna()

    return {
        "开盘15min与剩余时段相关": corr,
        "开盘强度分档": byq.to_dict("index"),
        "尾盘30min平均收益bp": round(float(tail.mean()) * 1e4, 2),
        "尾盘30min标准差bp": round(float(tail.std()) * 1e4, 2),
        "尾盘30min为正占比%": round(float((tail > 0).mean()) * 100, 1),
        "跳空与日内收益相关": round(float(day["gap"].corr(day["intraday_ret"])), 4),
    }


# --------------------------------------------------------------------------- #
def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    all_out = {}
    for name, code in SYMBOLS.items():
        df = load_or_fetch(code, "5m")
        df = df[(df.index >= TEST_START) & (df.index <= TEST_END)]
        d = add_session_keys(df)
        print(f"\n{'='*78}\n  {name} ({code})  测试区间 {TEST_START} ~ {df.index[-1].date()}\n{'='*78}")

        ov, day = daily_overview(d)
        print("\n【一、日线概览】")
        for k, v in ov.items():
            print(f"  {k:24s}: {v}")

        prof = intraday_profile(d)
        print("\n【二、日内分时段特征】(收益单位 bp = 0.01%; |t|>2 视为显著)")
        print(prof.round(2).to_string())
        print(f"  -> 波动 U 型: 开盘段(slot0-5)均波动 {prof['ret_std_bp'].iloc[:6].mean():.1f}bp, "
              f"中段(12-30) {prof['ret_std_bp'].iloc[12:31].mean():.1f}bp, "
              f"尾盘(42-47) {prof['ret_std_bp'].iloc[42:].mean():.1f}bp")
        print(f"  -> 量能 U 型: 开盘段 {prof['vol_share_pct'].iloc[:6].mean():.2f}%, "
              f"中段 {prof['vol_share_pct'].iloc[12:31].mean():.2f}%, "
              f"尾盘 {prof['vol_share_pct'].iloc[42:].mean():.2f}%")

        mr = momentum_reversal(d)
        print("\n【三、动量 / 反转】")
        print("  5m收益自相关:", mr["autocorr"])
        for key in ("mom3_fwd3", "mom6_fwd6", "mom12_fwd6", "mom12_fwd12"):
            if key in mr["conditional"]:
                print(f"  {key}:")
                for q, v in mr["conditional"][key].items():
                    print(f"      {q:8s} 未来收益 {v['mean_bp']:+8.2f}bp  t={v['t']:+6.2f}  n={v['size']}")

        vp = volume_price(d)
        print("\n【四、量价关系】")
        print(f"  量价比 vs 未来3根收益 相关系数: {vp.get('量价比与收益相关系数')}")
        for q, v in vp.get("量能分位_未来3根bp", {}).items():
            print(f"      {q:8s} fwd3 {v.get('fwd3_mean', float('nan')):+8.2f}bp  n={v.get('fwd3_size')}")
        print("  量价配合 (price_up, vol_hi):")
        for q, v in vp.get("量价配合", {}).items():
            print(f"      涨={q.split(',')[0].strip('(')} 放量={q.split(',')[1].strip().strip(')')} "
                  f"fwd3 {v['fwd3_bp']:+8.2f}bp  n={v['n']}")

        vw = vwap_reversion(d)
        print("\n【五、VWAP 偏离均值回复 (按日重置)】")
        print(f"  dev vs fwd3 corr = {vw.get('dev_fwd3_corr')}, vs fwd6 = {vw.get('dev_fwd6_corr')}")
        for q, v in vw.get("分位表", {}).items():
            print(f"      {q:14s} fwd3 {v['fwd3_bp']:+8.2f}bp  fwd6 {v['fwd6_bp']:+8.2f}bp  n={v['n']}")

        oc = open_close_effects(d, day)
        print("\n【六、开盘 / 尾盘效应】")
        for k, v in oc.items():
            if isinstance(v, dict):
                print(f"  {k}:")
                for k2, v2 in v.items():
                    print(f"      {k2:8s} {v2}")
            else:
                print(f"  {k:24s}: {v}")

        all_out[name] = {
            "daily": ov,
            "intraday_profile": prof.round(3).to_dict("index"),
            "momentum_reversal": mr,
            "volume_price": vp,
            "vwap_reversion": vw,
            "open_close": oc,
        }

    with open(OUT / "patterns_v2.json", "w", encoding="utf-8") as f:
        json.dump(all_out, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n已保存 -> {OUT / 'patterns_v2.json'}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    raise SystemExit(main())

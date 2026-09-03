"""日内信号挖掘 (漂移调整版).

核心修正: 测试区间 ETF 下跌 -17%, 所有"未来收益"都被负漂移污染。
本脚本统一改用 **日内去漂移收益**: 对每一根 bar 的未来收益, 减去当日全体 bar 的平均收益,
从而剥离"日内持有 beta", 只留下"择时 alpha"。日内 T+0 策略隔夜空仓, 但日内任意时刻
仍有方向敞口, 因此必须区分:
    - 漂移收益 (drift)  : 只要日内一直持有就该拿到的收益 (脆弱, 依赖区间行情)
    - 择时收益 (timing) : 靠信号切换方向多出来的收益 (这才是策略 Alpha)

只统计 timing 部分, 才能避免把"这波正好跌"误当成策略能力。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from fetch_data import SYMBOLS, load_or_fetch

OUT = Path(__file__).parent / "data"
TEST_START, TEST_END = "2026-07-01", "2026-08-31"


def prep(code: str) -> pd.DataFrame:
    df = load_or_fetch(code, "5m")
    df = df[(df.index >= TEST_START) & (df.index <= TEST_END)].copy()
    df["day"] = df.index.normalize()
    df["slot"] = df.groupby("day").cumcount()
    df["hhmm"] = df.index.strftime("%H:%M")
    return df


# --------------------------------------------------------------------------- #
# 1. 日内漂移曲线 (日内累计收益路径)
# --------------------------------------------------------------------------- #
def drift_curve(d: pd.DataFrame) -> pd.DataFrame:
    """等权平均的日内累计收益路径 (bp), 以及去漂移后的分时段收益."""
    g = d.groupby("day")
    d = d.copy()
    d["bar_ret"] = g["close"].pct_change()
    first_idx = g.head(1).index
    d.loc[first_idx, "bar_ret"] = d.loc[first_idx, "close"] / d.loc[first_idx, "open"] - 1
    g2 = d.groupby("day")
    d["cum"] = g2["bar_ret"].cumsum()
    # 去漂移: 每根 bar 收益减去当日均值
    d["day_mean"] = g2["bar_ret"].transform("mean")
    d["adj"] = d["bar_ret"] - d["day_mean"]
    d["cum_adj"] = g2["adj"].cumsum()

    cur = d.groupby("slot").agg(
        hhmm=("hhmm", "first"),
        cum_bp=("cum", lambda x: x.mean() * 1e4),
        cum_adj_bp=("cum_adj", lambda x: x.mean() * 1e4),
        bar_bp=("bar_ret", lambda x: x.mean() * 1e4),
        adj_bp=("adj", lambda x: x.mean() * 1e4),
        std_bp=("bar_ret", lambda x: x.std() * 1e4),
        n=("bar_ret", "size"),
    )
    cur["adj_t"] = cur["adj_bp"] / (cur["std_bp"] / np.sqrt(cur["n"]))
    return cur.round(2)


# --------------------------------------------------------------------------- #
# 2. 去漂移后的因子有效性检验
# --------------------------------------------------------------------------- #
def factor_test(d: pd.DataFrame, fact: pd.Series, name: str,
                horizons=(3, 6, 12), nq: int = 5) -> dict:
    """单因子分档检验: 因子分 nq 档 -> 去漂移后未来 h 根 bar 的平均收益.

    返回每档收益(bp)、t 值, 以及 多空价差 (Qn - Q1) 与其 t 值。
    """
    d = d.copy()
    d["_f"] = fact
    g = d.groupby("day")
    res = {}
    for h in horizons:
        d["_fwd"] = g["close"].transform(lambda s: s.shift(-h) / s - 1)
        d["_dm"] = g["_fwd"].transform("mean")          # 当日该 horizon 的平均收益 = 漂移
        d["_ex"] = d["_fwd"] - d["_dm"]
        sub = d.dropna(subset=["_f", "_ex"]).copy()
        if len(sub) < 50:
            continue
        sub["_q"] = pd.qcut(sub["_f"], nq, labels=False, duplicates="drop")
        agg = sub.groupby("_q")["_ex"].agg(["mean", "std", "size"])
        agg["t"] = agg["mean"] / (agg["std"] / np.sqrt(agg["size"]))
        hi, lo = agg.index.max(), agg.index.min()
        a, b = agg.loc[hi], agg.loc[lo]
        se = np.sqrt(a["std"] ** 2 / a["size"] + b["std"] ** 2 / b["size"])
        spread_bp = (a["mean"] - b["mean"]) * 1e4
        res[f"fwd{h}"] = {
            "分档收益bp": {f"Q{i+1}": round(float(v * 1e4), 2) for i, v in enumerate(agg["mean"])},
            "分档t值": {f"Q{i+1}": round(float(v), 2) for i, v in enumerate(agg["t"])},
            "多空价差bp": round(float(spread_bp), 2),
            "价差t值": round(float(spread_bp / (se * 1e4)), 2),
            "样本数": int(agg["size"].sum()),
        }
    return {"因子": name, "结果": res}


def build_factors(d: pd.DataFrame) -> dict[str, pd.Series]:
    g = d.groupby("day")
    f = {}
    close = d["close"]
    # 动量 (不同窗口)
    for k in (3, 6, 12, 24):
        f[f"动量{k}根"] = g["close"].transform(lambda s, k=k: s / s.shift(k) - 1)
    # 量能比 (相对当日已实现滚动均量, 严格只用过去数据)
    vol_ma = g["volume"].transform(lambda s: s.rolling(12, min_periods=3).mean().shift(1))
    f["量能比"] = d["volume"] / vol_ma
    # 量能比(短)
    vol_ma6 = g["volume"].transform(lambda s: s.rolling(6, min_periods=2).mean().shift(1))
    f["量能比6"] = d["volume"] / vol_ma6
    # VWAP 偏离 (按日重置)
    tpv = (d["high"] + d["low"] + d["close"]) / 3 * d["volume"]
    cum_tpv = tpv.groupby(d["day"]).cumsum()
    cum_vol = d["volume"].groupby(d["day"]).cumsum()
    vwap = cum_tpv / cum_vol
    f["VWAP偏离"] = (close - vwap) / vwap
    # 当日位置 (价格处在当日高低区间的分位)
    hi = g["high"].cummax()
    lo = g["low"].cummin()
    f["日内位置"] = (close - lo) / (hi - lo + 1e-9)
    # 相对开盘的收益
    op = g["open"].transform("first")
    f["相对开盘"] = close / op - 1
    # 短期反转 (上一根收益)
    f["上一根收益"] = g["close"].pct_change()
    # 振幅/波动 (过去12根已实现波动)
    r = g["close"].pct_change()
    f["已实现波动"] = r.groupby(d["day"]).transform(
        lambda s: s.rolling(12, min_periods=4).std())
    # 主动买卖压力近似: (close-open)/(high-low) 位置
    f["K线实体位置"] = (close - d["open"]) / (d["high"] - d["low"] + 1e-9)
    return f


# --------------------------------------------------------------------------- #
# 3. 二维联合检验 (量能 × 动量)
# --------------------------------------------------------------------------- #
def joint_test(d: pd.DataFrame, fa: pd.Series, fb: pd.Series,
               na: str, nb: str, h: int = 6) -> dict:
    d = d.copy()
    d["_a"], d["_b"] = fa, fb
    g = d.groupby("day")
    d["_fwd"] = g["close"].transform(lambda s: s.shift(-h) / s - 1)
    d["_dm"] = g["_fwd"].transform("mean")
    d["_ex"] = d["_fwd"] - d["_dm"]
    sub = d.dropna(subset=["_a", "_b", "_ex"]).copy()
    if len(sub) < 100:
        return {}
    sub["_qa"] = pd.qcut(sub["_a"], 3, labels=["低", "中", "高"])
    sub["_qb"] = pd.qcut(sub["_b"], 3, labels=["低", "中", "高"])
    piv = sub.pivot_table(index="_qa", columns="_qb", values="_ex",
                          aggfunc="mean", observed=True) * 1e4
    cnt = sub.pivot_table(index="_qa", columns="_qb", values="_ex",
                          aggfunc="size", observed=True)
    return {"行": na, "列": nb, "收益bp": piv.round(2).to_dict(),
            "样本数": cnt.to_dict(), "horizon": h}


# --------------------------------------------------------------------------- #
def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    store = {}
    for name, code in SYMBOLS.items():
        d = prep(code)
        print(f"\n{'='*78}\n  {name} ({code}) — 漂移调整后的信号挖掘\n{'='*78}")

        cur = drift_curve(d)
        print("\n【一、日内漂移曲线】(cum_bp=持有到该时点的累计收益; cum_adj_bp=去漂移后)")
        show = cur.iloc[::4][["hhmm", "cum_bp", "cum_adj_bp", "bar_bp", "adj_bp", "std_bp", "adj_t"]]
        print(show.to_string())
        print(f"  日内漂移总量: {cur['cum_bp'].iloc[-1]:+.1f}bp;  "
              f"最大回撤段: {cur['cum_bp'].min():+.1f}bp @ {cur.loc[cur['cum_bp'].idxmin(),'hhmm']}")
        print(f"  去漂移后显著时段(|t|>2): "
              f"{list(cur.loc[cur['adj_t'].abs() > 2, ['hhmm','adj_bp','adj_t']].itertuples(index=False, name=None))}")

        fac = build_factors(d)
        print("\n【二、单因子有效性】(去漂移; 价差 = 最高档 - 最低档)")
        rows = []
        fres = {}
        for fname, series in fac.items():
            r = factor_test(d, series, fname)
            fres[fname] = r
            for hk, hv in r["结果"].items():
                rows.append({"因子": fname, "horizon": hk,
                             "多空价差bp": hv["多空价差bp"], "价差t值": hv["价差t值"],
                             "样本数": hv["样本数"]})
        ft = pd.DataFrame(rows).sort_values("价差t值", key=abs, ascending=False)
        print(ft.to_string(index=False))

        print("\n【三、二维联合检验】(行=量能比, 列=动量12根, h=6)")
        jt = joint_test(d, fac["量能比"], fac["动量12根"], "量能比", "动量12根", h=6)
        if jt:
            print(pd.DataFrame(jt["收益bp"]).to_string())
            print("样本数:"); print(pd.DataFrame(jt["样本数"]).to_string())
        jt2 = joint_test(d, fac["量能比"], fac["日内位置"], "量能比", "日内位置", h=6)
        if jt2:
            print("\n(行=量能比, 列=日内位置, h=6)")
            print(pd.DataFrame(jt2["收益bp"]).to_string())

        store[name] = {"drift_curve": cur.to_dict("index"), "factors": fres,
                       "joint_vol_mom12": jt, "joint_vol_pos": jt2}

    with open(OUT / "signal_mining.json", "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n已保存 -> {OUT / 'signal_mining.json'}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    raise SystemExit(main())

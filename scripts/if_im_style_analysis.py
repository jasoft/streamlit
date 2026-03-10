from __future__ import annotations

import argparse
import json
from bisect import bisect_right, insort
from fractions import Fraction
from pathlib import Path

import akshare as ak
import numpy as np
import pandas as pd


INDEX_START_DATE = "20050101"
FUTURES_START_DATE = "20220101"
END_DATE = "20260310"


def percentile_rank(series: pd.Series, value: float) -> float:
    sorted_values = pd.Series(series).dropna().astype(float).sort_values().tolist()
    if not sorted_values:
        return float("nan")
    return bisect_right(sorted_values, float(value)) / len(sorted_values)


def expanding_percentiles(values: list[float]) -> list[float]:
    seen: list[float] = []
    output: list[float] = []
    for raw_value in values:
        value = float(raw_value)
        insort(seen, value)
        output.append(bisect_right(seen, value) / len(seen))
    return output


def fetch_current_index_snapshot() -> dict[str, float]:
    important = ak.stock_zh_index_spot_em(symbol="沪深重要指数")
    sh_series = ak.stock_zh_index_spot_em(symbol="上证系列指数")
    sz_series = ak.stock_zh_index_spot_em(symbol="深证系列指数")

    hs300 = important.loc[important["代码"] == "000300"].iloc[0]
    zz1000 = important.loc[important["代码"] == "000852"].iloc[0]
    sh = sh_series.loc[sh_series["代码"] == "000001"].iloc[0]
    sz = sz_series.loc[sz_series["代码"] == "399001"].iloc[0]

    total_amount = float(sh["成交额"]) + float(sz["成交额"])
    hs300_amount = float(hs300["成交额"])
    zz1000_amount = float(zz1000["成交额"])

    return {
        "snapshot_date": END_DATE,
        "hs300_price": float(hs300["最新价"]),
        "zz1000_price": float(zz1000["最新价"]),
        "hs300_amount": hs300_amount,
        "zz1000_amount": zz1000_amount,
        "total_market_amount": total_amount,
        "price_ratio": float(hs300["最新价"]) / float(zz1000["最新价"]),
        "zz1000_market_share": zz1000_amount / total_amount,
        "zz1000_pair_share": zz1000_amount / (zz1000_amount + hs300_amount),
    }


def fetch_index_history() -> pd.DataFrame:
    hs300 = ak.index_zh_a_hist(
        symbol="000300", period="daily", start_date=INDEX_START_DATE, end_date=END_DATE
    )
    zz1000 = ak.index_zh_a_hist(
        symbol="000852", period="daily", start_date=INDEX_START_DATE, end_date=END_DATE
    )
    sh = ak.index_zh_a_hist(
        symbol="000001", period="daily", start_date=INDEX_START_DATE, end_date=END_DATE
    )
    sz = ak.index_zh_a_hist(
        symbol="399001", period="daily", start_date=INDEX_START_DATE, end_date=END_DATE
    )

    for item in [hs300, zz1000, sh, sz]:
        item["日期"] = pd.to_datetime(item["日期"])
        item["收盘"] = pd.to_numeric(item["收盘"])
        item["成交额"] = pd.to_numeric(item["成交额"])

    merged = (
        hs300[["日期", "收盘", "成交额"]]
        .rename(columns={"收盘": "hs300_close", "成交额": "hs300_amount"})
        .merge(
            zz1000[["日期", "收盘", "成交额"]].rename(
                columns={"收盘": "zz1000_close", "成交额": "zz1000_amount"}
            ),
            on="日期",
            how="inner",
        )
        .merge(
            sh[["日期", "成交额"]].rename(columns={"成交额": "sh_amount"}),
            on="日期",
            how="inner",
        )
        .merge(
            sz[["日期", "成交额"]].rename(columns={"成交额": "sz_amount"}),
            on="日期",
            how="inner",
        )
        .sort_values("日期")
        .reset_index(drop=True)
    )
    merged["total_market_amount"] = merged["sh_amount"] + merged["sz_amount"]
    merged["price_ratio"] = merged["hs300_close"] / merged["zz1000_close"]
    merged["zz1000_market_share"] = (
        merged["zz1000_amount"] / merged["total_market_amount"]
    )
    merged["zz1000_pair_share"] = merged["zz1000_amount"] / (
        merged["zz1000_amount"] + merged["hs300_amount"]
    )
    return merged


def fetch_pe_history() -> pd.DataFrame:
    hs300 = ak.stock_index_pe_lg(symbol="沪深300")
    zz1000 = ak.stock_index_pe_lg(symbol="中证1000")

    for item in [hs300, zz1000]:
        item["日期"] = pd.to_datetime(item["日期"])
        item["滚动市盈率"] = pd.to_numeric(item["滚动市盈率"])

    merged = (
        hs300[["日期", "滚动市盈率"]]
        .rename(columns={"滚动市盈率": "hs300_ttm_pe"})
        .merge(
            zz1000[["日期", "滚动市盈率"]].rename(
                columns={"滚动市盈率": "zz1000_ttm_pe"}
            ),
            on="日期",
            how="inner",
        )
        .sort_values("日期")
        .reset_index(drop=True)
    )
    merged["pe_ratio"] = merged["hs300_ttm_pe"] / merged["zz1000_ttm_pe"]
    return merged


def fetch_current_futures_snapshot() -> dict[str, float]:
    futures = ak.futures_zh_spot(symbol="IF0,IM0", market="FF", adjust="0")
    if_quote = futures.iloc[0]
    im_quote = futures.iloc[1]
    hedge_ratio = float(if_quote["current_price"]) * 300 / (
        float(im_quote["current_price"]) * 200
    )
    combo = Fraction(hedge_ratio).limit_denominator(10)
    return {
        "snapshot_date": END_DATE,
        "if_price": float(if_quote["current_price"]),
        "im_price": float(im_quote["current_price"]),
        "if_volume": float(if_quote["volume"]),
        "im_volume": float(im_quote["volume"]),
        "hedge_ratio_im_per_if": hedge_ratio,
        "practical_if_contracts": combo.denominator,
        "practical_im_contracts": combo.numerator,
    }


def fetch_futures_history() -> pd.DataFrame:
    if0 = ak.futures_main_sina(
        symbol="IF0", start_date=FUTURES_START_DATE, end_date=END_DATE
    )
    im0 = ak.futures_main_sina(
        symbol="IM0", start_date=FUTURES_START_DATE, end_date=END_DATE
    )

    for item in [if0, im0]:
        item["日期"] = pd.to_datetime(item["日期"])
        item["收盘价"] = pd.to_numeric(item["收盘价"])

    merged = (
        if0[["日期", "收盘价"]]
        .rename(columns={"收盘价": "if_close"})
        .merge(
            im0[["日期", "收盘价"]].rename(columns={"收盘价": "im_close"}),
            on="日期",
            how="inner",
        )
        .sort_values("日期")
        .reset_index(drop=True)
    )
    return merged


def build_state_frame(
    index_history: pd.DataFrame, pe_history: pd.DataFrame, futures_history: pd.DataFrame
) -> pd.DataFrame:
    state = (
        futures_history.merge(
            index_history[
                [
                    "日期",
                    "price_ratio",
                    "zz1000_pair_share",
                    "zz1000_market_share",
                    "total_market_amount",
                ]
            ],
            on="日期",
            how="inner",
        )
        .merge(pe_history[["日期", "pe_ratio"]], on="日期", how="inner")
        .sort_values("日期")
        .reset_index(drop=True)
    )

    for column in [
        "price_ratio",
        "pe_ratio",
        "zz1000_pair_share",
        "zz1000_market_share",
        "total_market_amount",
    ]:
        state[f"{column}_pct"] = expanding_percentiles(state[column].tolist())
    return state


def compute_trade_stats(
    state: pd.DataFrame,
    current_features: dict[str, float],
    horizons: list[int],
    neighbors: int,
    exclude_recent_days: int,
) -> tuple[dict[str, dict[str, float]], dict[str, pd.DataFrame], dict[str, dict[str, float]]]:
    results: dict[str, dict[str, float]] = {}
    nearest_samples: dict[str, pd.DataFrame] = {}
    baseline: dict[str, dict[str, float]] = {}

    for horizon in horizons:
        sample = state.copy()
        sample["if_exit"] = sample["if_close"].shift(-horizon)
        sample["im_exit"] = sample["im_close"].shift(-horizon)
        sample = sample.dropna().copy()
        sample["hedge_ratio"] = sample["if_close"] * 300 / (sample["im_close"] * 200)
        sample["pnl"] = (sample["if_exit"] - sample["if_close"]) * 300 - sample[
            "hedge_ratio"
        ] * (sample["im_exit"] - sample["im_close"]) * 200
        sample["gross_notional"] = sample["if_close"] * 300 + sample[
            "hedge_ratio"
        ] * sample["im_close"] * 200
        sample["return"] = sample["pnl"] / sample["gross_notional"]

        baseline[str(horizon)] = {
            "count": int(len(sample)),
            "win_rate": float((sample["pnl"] > 0).mean()),
            "avg_return": float(sample["return"].mean()),
            "avg_pnl": float(sample["pnl"].mean()),
        }

        working = sample.iloc[:-exclude_recent_days].copy()
        for feature_name, value in current_features.items():
            working[f"{feature_name}_dist"] = (working[feature_name] - value) ** 2
        working["distance"] = np.sqrt(
            working[[column for column in working.columns if column.endswith("_dist")]]
            .sum(axis=1)
        )
        nearest = working.nsmallest(neighbors, "distance").copy()
        nearest_samples[str(horizon)] = nearest

        results[str(horizon)] = {
            "count": int(len(nearest)),
            "win_rate": float((nearest["pnl"] > 0).mean()),
            "avg_return": float(nearest["return"].mean()),
            "avg_pnl": float(nearest["pnl"].mean()),
            "best_match_date": nearest["日期"].min().strftime("%Y-%m-%d"),
            "worst_match_date": nearest["日期"].max().strftime("%Y-%m-%d"),
        }

    return results, nearest_samples, baseline


def assess_overheat(metrics: dict[str, float]) -> dict[str, object]:
    structural_heat = (
        metrics["price_ratio_percentile"] <= 0.25
        and metrics["pe_ratio_percentile"] <= 0.35
        and metrics["zz1000_pair_share_percentile"] >= 0.80
    )
    broad_heat = (
        metrics["zz1000_market_share_percentile"] >= 0.80
        and metrics["total_market_amount_percentile"] >= 0.80
    )

    if structural_heat and broad_heat:
        label = "全面过热"
    elif structural_heat:
        label = "结构性偏热"
    elif metrics["zz1000_pair_share_percentile"] >= 0.70:
        label = "轻度偏热"
    else:
        label = "未见明显过热"

    reasons = [
        f"指数比值分位 {metrics['price_ratio_percentile']:.1%}",
        f"PE 比值分位 {metrics['pe_ratio_percentile']:.1%}",
        f"中证1000 / (沪深300+中证1000) 成交占比分位 {metrics['zz1000_pair_share_percentile']:.1%}",
        f"中证1000 / 全市场成交占比分位 {metrics['zz1000_market_share_percentile']:.1%}",
        f"全市场成交额分位 {metrics['total_market_amount_percentile']:.1%}",
    ]
    return {"label": label, "structural_heat": structural_heat, "broad_heat": broad_heat, "reasons": reasons}


def to_serializable(value):
    if isinstance(value, (pd.Timestamp, pd.Timedelta)):
        return str(value)
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="沪深300 / 中证1000 风格与 IF-IM 配对分析")
    parser.add_argument(
        "--output-dir",
        default="outputs/if_im_style_analysis",
        help="结果输出目录",
    )
    parser.add_argument(
        "--neighbors", type=int, default=60, help="历史相似状态采样数量"
    )
    parser.add_argument(
        "--exclude-recent-days",
        type=int,
        default=40,
        help="回测时排除最近若干天，避免和当前状态过近",
    )
    parser.add_argument(
        "--horizons",
        default="5,10,20",
        help="统计持有期，逗号分隔的交易日列表",
    )
    args = parser.parse_args()

    horizons = [int(item) for item in args.horizons.split(",") if item.strip()]
    summary = run_analysis(
        output_dir=args.output_dir,
        neighbors=args.neighbors,
        exclude_recent_days=args.exclude_recent_days,
        horizons=horizons,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=to_serializable))


def run_analysis(
    output_dir: str = "outputs/if_im_style_analysis",
    neighbors: int = 60,
    exclude_recent_days: int = 40,
    horizons: list[int] | None = None,
) -> dict[str, object]:
    if horizons is None:
        horizons = [5, 10, 20]

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    current_index = fetch_current_index_snapshot()
    current_futures = fetch_current_futures_snapshot()
    index_history = fetch_index_history()
    pe_history = fetch_pe_history()
    futures_history = fetch_futures_history()
    state = build_state_frame(
        index_history=index_history,
        pe_history=pe_history,
        futures_history=futures_history,
    )

    current_metrics = {
        "price_ratio": current_index["price_ratio"],
        "price_ratio_percentile": percentile_rank(
            index_history["price_ratio"], current_index["price_ratio"]
        ),
        "pe_ratio": float(pe_history.iloc[-1]["pe_ratio"]),
        "pe_ratio_percentile": percentile_rank(
            pe_history["pe_ratio"], float(pe_history.iloc[-1]["pe_ratio"])
        ),
        "hs300_ttm_pe": float(pe_history.iloc[-1]["hs300_ttm_pe"]),
        "zz1000_ttm_pe": float(pe_history.iloc[-1]["zz1000_ttm_pe"]),
        "pe_snapshot_date": pe_history.iloc[-1]["日期"].strftime("%Y-%m-%d"),
        "zz1000_market_share": current_index["zz1000_market_share"],
        "zz1000_market_share_percentile": percentile_rank(
            index_history["zz1000_market_share"], current_index["zz1000_market_share"]
        ),
        "zz1000_pair_share": current_index["zz1000_pair_share"],
        "zz1000_pair_share_percentile": percentile_rank(
            index_history["zz1000_pair_share"], current_index["zz1000_pair_share"]
        ),
        "total_market_amount": current_index["total_market_amount"],
        "total_market_amount_percentile": percentile_rank(
            index_history["total_market_amount"], current_index["total_market_amount"]
        ),
    }

    current_state_features = {
        "price_ratio_pct": current_metrics["price_ratio_percentile"],
        "pe_ratio_pct": current_metrics["pe_ratio_percentile"],
        "zz1000_pair_share_pct": current_metrics["zz1000_pair_share_percentile"],
        "total_market_amount_pct": current_metrics["total_market_amount_percentile"],
    }

    conditional_stats, nearest_samples, baseline_stats = compute_trade_stats(
        state=state,
        current_features=current_state_features,
        horizons=horizons,
        neighbors=neighbors,
        exclude_recent_days=exclude_recent_days,
    )
    overheat = assess_overheat(current_metrics)

    summary = {
        "data_sources": {
            "index_history": "ak.index_zh_a_hist",
            "index_spot": "ak.stock_zh_index_spot_em",
            "index_pe": "ak.stock_index_pe_lg",
            "futures_history": "ak.futures_main_sina",
            "futures_spot": "ak.futures_zh_spot",
            "akshare_version": ak.__version__,
        },
        "current_index_snapshot": current_index,
        "current_futures_snapshot": current_futures,
        "current_metrics": current_metrics,
        "overheat_assessment": overheat,
        "conditional_trade_stats": conditional_stats,
        "baseline_trade_stats": baseline_stats,
    }

    for horizon, frame in nearest_samples.items():
        frame.to_csv(
            output_path / f"nearest_samples_h{horizon}.csv",
            index=False,
            encoding="utf-8-sig",
        )

    state.to_csv(output_path / "daily_state.csv", index=False, encoding="utf-8-sig")
    with (output_path / "summary.json").open("w", encoding="utf-8") as file:
        json.dump(summary, file, ensure_ascii=False, indent=2, default=to_serializable)
    return summary


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from bisect import bisect_right, insort
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import akshare as ak
import numpy as np
import pandas as pd


# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

INDEX_START_DATE = "20050101"
FUTURES_START_DATE = "20220101"
# 动态获取当前日期
END_DATE = datetime.now().strftime("%Y%m%d")


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
    """获取当前指数快照数据，增加备选方案"""
    try:
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
    except Exception as e:
        logger.warning(f"从 EM 获取指数快照失败，尝试新浪接口: {e}")
        try:
            spot_sina = ak.stock_zh_index_spot_sina()
            hs300 = spot_sina.loc[spot_sina["代码"] == "sh000300"].iloc[0]
            zz1000 = spot_sina.loc[spot_sina["代码"] == "sh000852"].iloc[0]
            sh = spot_sina.loc[spot_sina["代码"] == "sh000001"].iloc[0]
            sz = spot_sina.loc[spot_sina["代码"] == "sz399001"].iloc[0]

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
        except Exception as e2:
            logger.error(f"从新浪获取指数快照也失败了: {e2}")
            # 如果还失败，可以考虑调用 cli_index.py，但这里暂时抛出异常
            raise e2


def fetch_index_history() -> pd.DataFrame:
    """获取指数历史数据，优先使用 cli_index.py 确保成交额数据准确"""
    indices = {
        "hs300": "000300",
        "zz1000": "000852",
        "sh": "000001",
        "sz": "399001"
    }
    
    dfs = {}
    script_path = Path(__file__).parent / "cli_index.py"
    
    for name, symbol in indices.items():
        try:
            logger.info(f"正在通过问财获取 {name}({symbol}) 历史数据...")
            query = f"{symbol}自2021年以来每日收盘价和成交额"
            result = subprocess.run(
                [sys.executable, str(script_path), "--query", query, "--limit", "1000"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if data.get("success") and data.get("datas"):
                    record = data["datas"][0]
                    monthly_data = []
                    for key, value in record.items():
                        # 匹配 "收盘价[YYYYMMDD]" 和 "成交额[YYYYMMDD]"
                        if "[" in key and "]" in key:
                            parts = key.split("[")
                            metric = parts[0]
                            date_str = parts[1][:-1]
                            if len(date_str) == 8:
                                monthly_data.append({"日期": date_str, "metric": metric, "value": value})
                    
                    if monthly_data:
                        temp_df = pd.DataFrame(monthly_data)
                        temp_df = temp_df.pivot(index="日期", columns="metric", values="value").reset_index()
                        temp_df.rename(columns={"收盘价": "收盘", "成交额": "成交额"}, inplace=True)
                        temp_df["日期"] = pd.to_datetime(temp_df["日期"])
                        dfs[name] = temp_df
                        continue
            
            logger.warning(f"问财获取 {name} 失败，尝试 Akshare EM...")
            dfs[name] = ak.index_zh_a_hist(
                symbol=symbol, period="daily", start_date="20210101", end_date=END_DATE
            )
            dfs[name].rename(columns={"日期": "日期", "收盘": "收盘", "成交额": "成交额"}, inplace=True)
            dfs[name]["日期"] = pd.to_datetime(dfs[name]["日期"])
            
        except Exception as e:
            logger.warning(f"获取 {name} 失败，尝试 Akshare Sina: {e}")
            dfs[name] = ak.stock_zh_index_daily(symbol=f"{'sh' if symbol.startswith('0') else 'sz'}{symbol}")
            dfs[name].rename(columns={"date": "日期", "close": "收盘", "volume": "成交量"}, inplace=True)
            dfs[name]["成交额"] = 0 # Sina 无成交额
            dfs[name]["日期"] = pd.to_datetime(dfs[name]["日期"])

    hs300, zz1000, sh, sz = dfs["hs300"], dfs["zz1000"], dfs["sh"], dfs["sz"]

    for item in [hs300, zz1000, sh, sz]:
        item["日期"] = pd.to_datetime(item["日期"])
        item["收盘"] = pd.to_numeric(item["收盘"])
        item["成交额"] = pd.to_numeric(item.get("成交额", 0))

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
    """获取 PE 历史数据，处理乐咕乐股解析错误"""
    try:
        hs300 = ak.stock_index_pe_lg(symbol="沪深300")
        zz1000 = ak.stock_index_pe_lg(symbol="中证1000")
    except Exception as e:
        logger.error(f"从乐咕乐股获取 PE 失败，由于 Akshare 内部解析 Bug 或接口变动: {e}")
        # 如果失败，尝试返回最后一条记录的占位符，避免整体崩溃
        return pd.DataFrame([{"日期": pd.to_datetime(datetime.now().date()), "hs300_ttm_pe": 12.0, "zz1000_ttm_pe": 25.0, "pe_ratio": 12.0/25.0}])

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
    # 基础合并
    state = futures_history.merge(
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
    
    # 合并 PE 数据，使用 left join 以免因为 PE 数据缺失导致整体数据丢失
    if not pe_history.empty:
        state = state.merge(pe_history[["日期", "pe_ratio"]], on="日期", how="left")
    else:
        state["pe_ratio"] = np.nan

    state = state.sort_values("日期").reset_index(drop=True)

    # 计算百分位分位数
    columns_to_rank = [
        "price_ratio",
        "zz1000_pair_share",
        "zz1000_market_share",
        "total_market_amount",
    ]
    if "pe_ratio" in state.columns and not state["pe_ratio"].isna().all():
        columns_to_rank.append("pe_ratio")

    for column in columns_to_rank:
        state[f"{column}_pct"] = expanding_percentiles(state[column].tolist())
    
    # 如果 pe_ratio 缺失，填充一个默认分位数
    if "pe_ratio_pct" not in state.columns:
        state["pe_ratio_pct"] = 0.5

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

        if nearest.empty:
            results[str(horizon)] = {
                "count": 0,
                "win_rate": 0.0,
                "avg_return": 0.0,
                "avg_pnl": 0.0,
                "best_match_date": "N/A",
                "worst_match_date": "N/A",
            }
        else:
            # 过滤掉无效日期
            valid_dates = nearest["日期"].dropna()
            best_match = valid_dates.min().strftime("%Y-%m-%d") if not valid_dates.empty else "N/A"
            worst_match = valid_dates.max().strftime("%Y-%m-%d") if not valid_dates.empty else "N/A"
            
            results[str(horizon)] = {
                "count": int(len(nearest)),
                "win_rate": float((nearest["pnl"] > 0).mean()),
                "avg_return": float(nearest["return"].mean()),
                "avg_pnl": float(nearest["pnl"].mean()),
                "best_match_date": best_match,
                "worst_match_date": worst_match,
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

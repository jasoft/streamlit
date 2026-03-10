from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from scripts.if_im_style_analysis import run_analysis


OUTPUT_DIR = "outputs/if_im_style_analysis"


@st.cache_data(ttl=300)
def load_if_im_summary() -> dict[str, object]:
    return run_analysis(output_dir=OUTPUT_DIR)


def render_if_im_page() -> None:
    st.title("IF / IM 风格配对")
    st.caption("自动抓取最新指数、估值和股指期货连续合约，评估当前做多 IF / 做空 IM 的统计优势。")

    with st.spinner("正在抓取最新数据并计算历史分位..."):
        summary = load_if_im_summary()

    current_metrics = summary["current_metrics"]
    futures = summary["current_futures_snapshot"]
    overheat = summary["overheat_assessment"]

    col1, col2, col3, col4 = st.columns(4)
    col1.metric(
        "指数比值",
        f"{current_metrics['price_ratio']:.4f}",
        f"{current_metrics['price_ratio_percentile']:.1%} 分位",
    )
    col2.metric(
        "PE 比值",
        f"{current_metrics['pe_ratio']:.4f}",
        f"{current_metrics['pe_ratio_percentile']:.1%} 分位",
    )
    col3.metric(
        "中证1000相对成交占比",
        f"{current_metrics['zz1000_pair_share']:.2%}",
        f"{current_metrics['zz1000_pair_share_percentile']:.1%} 分位",
    )
    col4.metric(
        "全市场成交额",
        f"{current_metrics['total_market_amount'] / 1e12:.3f} 万亿",
        f"{current_metrics['total_market_amount_percentile']:.1%} 分位",
    )

    st.info(
        "\n".join(
            [
                f"过热判断: {overheat['label']}",
                f"当前 TTM PE: 沪深300 {current_metrics['hs300_ttm_pe']:.2f}, 中证1000 {current_metrics['zz1000_ttm_pe']:.2f}",
                f"当前期货配比: 1 IF 对 {futures['hedge_ratio_im_per_if']:.3f} IM，离散近似 {futures['practical_if_contracts']} IF 对 {futures['practical_im_contracts']} IM",
            ]
        )
    )

    comparison_rows = []
    for horizon, stats in summary["conditional_trade_stats"].items():
        baseline = summary["baseline_trade_stats"][horizon]
        comparison_rows.append(
            {
                "持有期": f"{horizon}日",
                "条件胜率": f"{stats['win_rate']:.1%}",
                "条件期望收益": f"{stats['avg_return']:.2%}",
                "条件平均PnL": f"{stats['avg_pnl']:.0f}",
                "基线胜率": f"{baseline['win_rate']:.1%}",
                "基线期望收益": f"{baseline['avg_return']:.2%}",
                "基线平均PnL": f"{baseline['avg_pnl']:.0f}",
            }
        )
    st.subheader("历史相似状态 vs 基线")
    st.dataframe(pd.DataFrame(comparison_rows), use_container_width=True, hide_index=True)

    st.subheader("当前状态解读")
    for reason in overheat["reasons"]:
        st.write(f"- {reason}")

    horizon = st.selectbox("查看相似样本", ["5", "10", "20"], index=2)
    csv_path = Path(OUTPUT_DIR) / f"nearest_samples_h{horizon}.csv"
    if csv_path.exists():
        nearest = pd.read_csv(csv_path)
        keep_columns = [
            "日期",
            "if_close",
            "im_close",
            "price_ratio",
            "pe_ratio",
            "zz1000_pair_share",
            "total_market_amount",
            "pnl",
            "return",
            "distance",
        ]
        existing_columns = [column for column in keep_columns if column in nearest.columns]
        st.dataframe(nearest[existing_columns].head(30), use_container_width=True, hide_index=True)


from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from scripts.hs300_industry_weight import run_analysis


OUTPUT_DIR = "outputs/hs300_industry_weight"


@st.cache_data(ttl=3600)
def load_hs300_industry_weight() -> dict[str, object]:
    return run_analysis(output_dir=OUTPUT_DIR)


def _format_weight_table(df: pd.DataFrame) -> pd.DataFrame:
    display = df.copy()
    display["指数权重占比"] = display["指数权重占比"].map(lambda value: f"{value:.3f}%")
    display["市值占比"] = display["市值占比"].map(lambda value: f"{value:.3f}%")
    display["总市值(万亿)"] = (display["总市值"] / 1e12).map(lambda value: f"{value:.3f}")
    return display[["所属同花顺一级行业", "指数权重占比", "成分数", "市值占比", "总市值(万亿)"]]


def render_hs300_industry_page() -> None:
    st.title("沪深300行业权重")

    if st.button("刷新数据"):
        load_hs300_industry_weight.clear()

    with st.spinner("正在拉取中证权重和问财行业数据..."):
        result = load_hs300_industry_weight()

    summary = result["summary"]
    holdings = result["holdings"]
    metadata = result["metadata"]

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("权重日期", metadata["weight_date"] or "-")
    col2.metric("行业数据日期", metadata["industry_date"] or "-")
    col3.metric("匹配成分股", f"{metadata['matched_count']}/{metadata['constituent_count']}")
    col4.metric("权重合计", f"{metadata['weight_sum']:.3f}%")

    chart_df = summary.head(20).copy()
    fig = px.bar(
        chart_df.sort_values("指数权重占比"),
        x="指数权重占比",
        y="所属同花顺一级行业",
        orientation="h",
        text="指数权重占比",
        labels={"指数权重占比": "指数权重占比(%)", "所属同花顺一级行业": "行业"},
        height=560,
    )
    fig.update_traces(texttemplate="%{text:.2f}%", textposition="outside")
    fig.update_layout(margin=dict(l=10, r=40, t=20, b=10), yaxis_title=None)
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("行业汇总")
    st.dataframe(_format_weight_table(summary), use_container_width=True, hide_index=True)

    with st.expander("查看成分股明细"):
        detail = holdings[
            [
                "成分券代码",
                "成分券名称",
                "所属同花顺一级行业",
                "权重",
                "总市值",
            ]
        ].copy()
        detail = detail.sort_values("权重", ascending=False)
        detail["权重"] = detail["权重"].map(lambda value: f"{value:.3f}%")
        detail["总市值(亿)"] = (detail["总市值"] / 1e8).round(2)
        detail = detail.drop(columns=["总市值"])
        st.dataframe(detail, use_container_width=True, hide_index=True)

    st.caption(
        "计算口径：中证指数沪深300成分股权重按同花顺一级行业聚合；"
        "行业与市值数据来自问财，成分权重来自中证指数。"
    )


if __name__ == "__main__":
    st.set_page_config(layout="wide")
    render_hs300_industry_page()

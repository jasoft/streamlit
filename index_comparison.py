#!/usr/bin/env python3
"""
沪深300 vs 中证1000 月度相对表现分析
统计最近5年每个月的相对涨幅，分析月度规律
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots


def fetch_index_data(index_name: str) -> Dict:
    """调用 cli_index.py 获取指数月度涨跌幅数据"""
    script_path = Path(__file__).parent / "scripts" / "cli_index.py"
    query = f"{index_name}近5年月度涨跌幅"

    result = subprocess.run(
        [sys.executable, str(script_path), "--query", query, "--limit", "1"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        st.error(f"获取 {index_name} 数据失败: {result.stderr}")
        return {}

    return json.loads(result.stdout)


def parse_monthly_data(data: Dict) -> pd.DataFrame:
    """解析月度涨跌幅数据"""
    if not data.get("success") or not data.get("datas"):
        return pd.DataFrame()

    record = data["datas"][0]
    monthly_data = []

    for key, value in record.items():
        if key.startswith("月涨跌幅[") and key.endswith("]"):
            date_str = key[5:-1]  # 提取 YYYYMMDD
            date = pd.to_datetime(date_str, format="%Y%m%d")
            monthly_data.append(
                {
                    "date": date,
                    "year": date.year,
                    "month": date.month,
                    "change_pct": value,
                }
            )

    df = pd.DataFrame(monthly_data)
    df = df.sort_values("date").reset_index(drop=True)
    return df


def calculate_relative_performance(
    df_hs300: pd.DataFrame, df_csi1000: pd.DataFrame
) -> pd.DataFrame:
    """计算相对表现 (中证1000 - 沪深300)"""
    # 合并数据，只基于年月匹配
    df_merged = pd.merge(
        df_hs300[["year", "month", "change_pct"]].rename(columns={"change_pct": "change_pct_hs300"}),
        df_csi1000[["year", "month", "change_pct"]].rename(columns={"change_pct": "change_pct_csi1000"}),
        on=["year", "month"],
    )

    # 添加日期列（用于趋势图）
    df_merged["date"] = pd.to_datetime(df_merged["year"].astype(str) + "-" + df_merged["month"].astype(str) + "-01")

    # 计算相对涨幅
    df_merged["relative"] = df_merged["change_pct_csi1000"] - df_merged["change_pct_hs300"]

    # 正值表示中证1000更强，负值表示沪深300更强
    df_merged["stronger"] = df_merged["relative"].apply(
        lambda x: "中证1000" if x > 0 else "沪深300"
    )

    return df_merged


def calculate_monthly_pattern(df: pd.DataFrame) -> pd.DataFrame:
    """计算月度规律：每个月的平均相对表现"""
    pattern = (
        df.groupby("month")
        .agg(
            avg_relative=("relative", "mean"),
            std_relative=("relative", "std"),
            count=("relative", "count"),
            csi1000_wins=("stronger", lambda x: (x == "中证1000").sum()),
            hs300_wins=("stronger", lambda x: (x == "沪深300").sum()),
        )
        .reset_index()
    )

    pattern["month_name"] = pattern["month"].apply(lambda x: f"{x}月")
    pattern["dominant"] = pattern["avg_relative"].apply(
        lambda x: "中证1000" if x > 0 else "沪深300"
    )
    pattern["win_rate_csi1000"] = (pattern["csi1000_wins"] / pattern["count"] * 100).round(1)

    return pattern


def create_heatmap(df: pd.DataFrame) -> go.Figure:
    """创建年度-月份热力图"""
    # 准备数据矩阵 - 完整的1-12月
    years = sorted(df["year"].unique())
    months = list(range(1, 13))

    z_data = []
    text_data = []
    for year in years:
        row = []
        text_row = []
        for month in months:
            match = df[(df["year"] == year) & (df["month"] == month)]
            if not match.empty:
                val = match.iloc[0]["relative"]
                row.append(val)
                stronger = "中证1000" if val > 0 else "沪深300"
                text_row.append(f"{val:+.2f}%<br>{stronger}更强")
            else:
                row.append(None)
                text_row.append("")
        z_data.append(row)
        text_data.append(text_row)

    fig = go.Figure(
        data=go.Heatmap(
            z=z_data,
            x=[f"{m}月" for m in months],
            y=[str(y) for y in years],
            text=text_data,
            texttemplate="%{text}",
            textfont={"size": 10},
            colorscale=[
                [0, "rgb(44, 127, 184)"],  # 蓝色 - 沪深300更强
                [0.5, "rgb(255, 255, 255)"],  # 白色 - 持平
                [1, "rgb(215, 48, 39)"],  # 红色 - 中证1000更强
            ],
            zmid=0,
            hovertemplate="年份: %{y}<br>月份: %{x}<br>相对涨幅: %{z:+.2f}%<extra></extra>",
        )
    )

    fig.update_layout(
        title="沪深300 vs 中证1000 月度相对表现热力图<br><sub>红色=中证1000更强, 蓝色=沪深300更强 | 空白=无数据</sub>",
        xaxis_title="月份",
        yaxis_title="年份",
        height=400,
    )

    return fig


def create_monthly_pattern_chart(pattern: pd.DataFrame) -> go.Figure:
    """创建月度规律柱状图"""
    colors = ["rgb(215, 48, 39)" if x > 0 else "rgb(44, 127, 184)" for x in pattern["avg_relative"]]

    fig = go.Figure()

    fig.add_trace(
        go.Bar(
            x=pattern["month_name"],
            y=pattern["avg_relative"],
            marker_color=colors,
            text=[f"{v:+.2f}%" for v in pattern["avg_relative"]],
            textposition="outside",
            hovertemplate="月份: %{x}<br>平均相对涨幅: %{y:+.2f}%<extra></extra>",
        )
    )

    fig.add_hline(y=0, line_dash="dash", line_color="gray")

    fig.update_layout(
        title="月度平均相对表现<br><sub>正值=中证1000更强, 负值=沪深300更强</sub>",
        xaxis_title="月份",
        yaxis_title="平均相对涨幅 (%)",
        height=400,
        yaxis=dict(zeroline=False),
    )

    return fig


def create_win_rate_chart(pattern: pd.DataFrame) -> go.Figure:
    """创建胜率图表"""
    fig = go.Figure()

    fig.add_trace(
        go.Bar(
            x=pattern["month_name"],
            y=pattern["win_rate_csi1000"],
            marker_color="rgb(215, 48, 39)",
            name="中证1000胜率",
            text=[f"{v}%" for v in pattern["win_rate_csi1000"]],
            textposition="outside",
        )
    )

    fig.add_hline(y=50, line_dash="dash", line_color="gray")

    fig.update_layout(
        title="中证1000月度胜率<br><sub>高于50%=中证1000更常跑赢</sub>",
        xaxis_title="月份",
        yaxis_title="胜率 (%)",
        height=350,
        yaxis=dict(range=[0, 100]),
    )

    return fig


def create_trend_chart(df: pd.DataFrame) -> go.Figure:
    """创建趋势线图"""
    fig = go.Figure()

    # 沪深300
    fig.add_trace(
        go.Scatter(
            x=df["date"],
            y=df["change_pct_hs300"],
            name="沪深300",
            line=dict(color="rgb(44, 127, 184)", width=2),
            hovertemplate="日期: %{x}<br>沪深300: %{y:+.2f}%<extra></extra>",
        )
    )

    # 中证1000
    fig.add_trace(
        go.Scatter(
            x=df["date"],
            y=df["change_pct_csi1000"],
            name="中证1000",
            line=dict(color="rgb(215, 48, 39)", width=2),
            hovertemplate="日期: %{x}<br>中证1000: %{y:+.2f}%<extra></extra>",
        )
    )

    # 相对表现
    fig.add_trace(
        go.Scatter(
            x=df["date"],
            y=df["relative"],
            name="相对表现",
            line=dict(color="rgb(100, 100, 100)", width=1, dash="dot"),
            yaxis="y2",
            hovertemplate="日期: %{x}<br>相对涨幅: %{y:+.2f}%<extra></extra>",
        )
    )

    fig.add_hline(y=0, line_dash="dash", line_color="gray", yaxis="y2")

    fig.update_layout(
        title="近5年月度涨跌幅趋势",
        xaxis_title="日期",
        yaxis_title="月度涨跌幅 (%)",
        yaxis2=dict(title="相对表现 (%)", overlaying="y", side="right"),
        height=450,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
    )

    return fig


def main():
    st.set_page_config(
        page_title="沪深300 vs 中证1000 月度分析",
        page_icon="📊",
        layout="wide",
    )

    st.title("沪深300 vs 中证1000 月度相对表现分析")
    st.markdown("统计最近5年每个月的相对涨幅，分析哪些月份哪个指数更强")

    # 获取数据
    with st.spinner("正在获取沪深300数据..."):
        data_hs300 = fetch_index_data("沪深300")
        df_hs300 = parse_monthly_data(data_hs300)

    with st.spinner("正在获取中证1000数据..."):
        data_csi1000 = fetch_index_data("中证1000")
        df_csi1000 = parse_monthly_data(data_csi1000)

    if df_hs300.empty or df_csi1000.empty:
        st.error("获取数据失败，请检查网络或 API Key")
        return

    # 计算相对表现
    df_merged = calculate_relative_performance(df_hs300, df_csi1000)
    pattern = calculate_monthly_pattern(df_merged)

    # 核心指标
    st.markdown("---")
    col1, col2, col3, col4 = st.columns(4)

    total_months = len(df_merged)
    csi1000_wins = (df_merged["stronger"] == "中证1000").sum()
    hs300_wins = (df_merged["stronger"] == "沪深300").sum()
    avg_relative = df_merged["relative"].mean()

    with col1:
        st.metric("统计月数", f"{total_months}个月")
    with col2:
        st.metric("中证1000跑赢", f"{csi1000_wins}次 ({csi1000_wins/total_months*100:.1f}%)")
    with col3:
        st.metric("沪深300跑赢", f"{hs300_wins}次 ({hs300_wins/total_months*100:.1f}%)")
    with col4:
        dominant = "中证1000" if avg_relative > 0 else "沪深300"
        st.metric("整体占优", dominant, f"平均 {avg_relative:+.2f}%")

    # 热力图
    st.markdown("---")
    st.plotly_chart(create_heatmap(df_merged), use_container_width=True)

    # 月度规律分析
    st.markdown("---")
    st.subheader("月度规律分析")

    col1, col2 = st.columns(2)

    with col1:
        st.plotly_chart(create_monthly_pattern_chart(pattern), use_container_width=True)

    with col2:
        st.plotly_chart(create_win_rate_chart(pattern), use_container_width=True)

    # 月度规律总结
    st.markdown("---")
    st.subheader("月度规律总结")

    # 找出中证1000最强和最弱的月份
    best_months_csi = pattern.nlargest(3, "avg_relative")
    worst_months_csi = pattern.nsmallest(3, "avg_relative")

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("**中证1000最强的月份（相对沪深300）**")
        for _, row in best_months_csi.iterrows():
            st.markdown(f"- **{row['month_name']}**: 平均跑赢 {row['avg_relative']:+.2f}%，胜率 {row['win_rate_csi1000']}%")

    with col2:
        st.markdown("**沪深300最强的月份（相对中证1000）**")
        for _, row in worst_months_csi.iterrows():
            win_rate_hs300 = 100 - row["win_rate_csi1000"]
            st.markdown(f"- **{row['month_name']}**: 平均跑赢 {row['avg_relative']:.2f}%，胜率 {win_rate_hs300}%")

    # 趋势图
    st.markdown("---")
    st.plotly_chart(create_trend_chart(df_merged), use_container_width=True)

    # 详细数据表格
    st.markdown("---")
    with st.expander("查看详细数据"):
        st.subheader("月度相对表现数据")

        # 格式化显示
        display_df = df_merged[["date", "change_pct_hs300", "change_pct_csi1000", "relative", "stronger"]].copy()
        display_df.columns = ["日期", "沪深300涨跌幅(%)", "中证1000涨跌幅(%)", "相对涨幅(%)", "更强指数"]
        display_df["日期"] = display_df["日期"].dt.strftime("%Y-%m")
        display_df = display_df.sort_values("日期", ascending=False)

        st.dataframe(
            display_df.style.applymap(
                lambda x: "color: red" if isinstance(x, float) and x > 0 else "color: blue" if isinstance(x, float) and x < 0 else "",
                subset=["相对涨幅(%)"],
            ),
            use_container_width=True,
        )

        st.subheader("月度规律统计")
        display_pattern = pattern[["month_name", "avg_relative", "count", "win_rate_csi1000", "dominant"]].copy()
        display_pattern.columns = ["月份", "平均相对涨幅(%)", "统计月数", "中证1000胜率(%)", "占优指数"]
        st.dataframe(display_pattern, use_container_width=True)

    # 数据来源说明
    st.markdown("---")
    st.caption("数据来源：同花顺问财 | 相对涨幅 = 中证1000涨跌幅 - 沪深300涨跌幅 | 正值表示中证1000更强")


if __name__ == "__main__":
    main()

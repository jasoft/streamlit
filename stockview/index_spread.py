import streamlit as st
import akshare as ak
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta


def get_index_data(index_code, start_date, end_date):
    """获取指数数据"""
    df = ak.index_zh_a_hist(
        symbol=index_code, period="daily", start_date=start_date, end_date=end_date
    )

    df["日期"] = pd.to_datetime(df["日期"])
    df.set_index("日期", inplace=True)
    return df


def calculate_return_spread(df1, df2, window=40):
    """计算收益差"""
    returns1 = df1["收盘"].pct_change(window)
    returns2 = df2["收盘"].pct_change(window)
    return_spread = returns1 - returns2
    return return_spread


def create_spread_chart():
    """创建指数与40日收益差对比图"""
    # 计算日期范围
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=365 * 5)).strftime("%Y%m%d")

    # 获取指数数据
    hs300_df = get_index_data("000300", start_date, end_date)
    zz1000_df = get_index_data("000852", start_date, end_date)
    dividend_df = get_index_data("000015", start_date, end_date)

    # 计算收益差
    hs300_zz1000_spread = calculate_return_spread(hs300_df, zz1000_df)
    dividend_zz1000_spread = calculate_return_spread(dividend_df, zz1000_df)
    hs300_zz1000_spread = hs300_zz1000_spread.dropna()
    dividend_zz1000_spread = dividend_zz1000_spread.dropna()

    # 创建图表
    fig = go.Figure()

    # 添加中证1000指数走势
    fig.add_trace(
        go.Scatter(
            x=zz1000_df.index,
            y=zz1000_df["收盘"],
            mode="lines",
            name="中证1000",
            line=dict(color="orange"),
            yaxis="y",
        )
    )

    # 添加沪深300与中证1000的收益差
    fig.add_trace(
        go.Scatter(
            x=hs300_zz1000_spread.index,
            y=hs300_zz1000_spread * 100,  # 转换为百分比
            mode="lines",
            name="沪深300-中证1000收益差",
            line=dict(color="blue"),
            yaxis="y2",
        )
    )

    # 添加中证1000与红利指数的收益差
    fig.add_trace(
        go.Scatter(
            x=dividend_zz1000_spread.index,
            y=dividend_zz1000_spread * 100,  # 转换为百分比
            mode="lines",
            name="红利指数-中证1000收益差",
            line=dict(color="green"),
            yaxis="y2",
        )
    )

    # 添加零线到收益差
    fig.add_hline(y=0, line_dash="dash", line_color="red", opacity=0.5)

    # 更新布局
    fig.update_layout(
        title="指数走势与40日收益差对比",
        xaxis=dict(title="日期"),
        yaxis=dict(
            title="中证1000指数",
            side="left",
            showgrid=True,
            domain=[0.6, 0.95],  # 调整上半部分位置，使中证1000指数居中显示
        ),
        yaxis2=dict(
            title="40日收益差(%)",
            side="right",
            showgrid=False,
            domain=[0, 0.45],  # 下半部分显示收益差
        ),
        hovermode="x unified",
        height=800,  # 增加图表高度
        legend=dict(
            yanchor="top",
            y=0.99,
            xanchor="right",  # 将图例移到右上角
            x=0.99,
        ),
    )

    return (
        fig,
        hs300_zz1000_spread.iloc[-1] * 100,
        dividend_zz1000_spread.iloc[-1] * 100,
    )


def main():
    st.markdown("### 📈 指数40日收益差分析")

    # 创建图表并获取当前收益差
    fig, hs300_zz1000_spread, dividend_zz1000_spread = create_spread_chart()

    # 显示图表
    st.plotly_chart(fig, use_container_width=True)

    # 显示当前收益差
    col1, col2 = st.columns(2)
    with col1:
        st.write(f"沪深300-中证1000收益差: {hs300_zz1000_spread:.2f}%")
    with col2:
        st.write(f"红利指数-中证1000收益差: {dividend_zz1000_spread:.2f}%")


if __name__ == "__main__":
    main()

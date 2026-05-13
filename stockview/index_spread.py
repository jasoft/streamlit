import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import requests
import json

# 腾讯日线 API
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


@st.cache_data(ttl=3600)
def fetch_tencent_kline(symbol: str, days: int = 1800) -> pd.DataFrame:
    """从腾讯获取日K线数据"""
    try:
        params = {"param": f"{symbol},day,,,{days},qfq"}
        response = requests.get(TENCENT_KLINE_URL, params=params, timeout=15)
        data = response.json()
        if "data" not in data:
            return pd.DataFrame()
        for code, info in data["data"].items():
            kline = info.get("qfqday") or info.get("day", [])
            if not kline:
                continue
            df = pd.DataFrame(kline, columns=["date", "open", "close", "high", "low", "volume"])
            df["date"] = pd.to_datetime(df["date"])
            for col in ["open", "close", "high", "low", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df.set_index("date", inplace=True)
            return df
        return pd.DataFrame()
    except Exception as e:
        print(f"腾讯K线获取失败 {symbol}: {e}")
        return pd.DataFrame()


def get_index_data(index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """获取指数数据"""
    df = fetch_tencent_kline(index_code, days=1800)
    if df.empty:
        return df
    # 过滤日期范围
    start = pd.to_datetime(start_date)
    end = pd.to_datetime(end_date)
    return df[(df.index >= start) & (df.index <= end)]


def calculate_return_spread(df1: pd.DataFrame, df2: pd.DataFrame, window: int = 40) -> pd.Series:
    """计算收益差"""
    returns1 = df1["close"].pct_change(window)
    returns2 = df2["close"].pct_change(window)
    return returns1 - returns2


def create_spread_chart():
    """创建指数与40日收益差对比图"""
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=365 * 5)).strftime("%Y%m%d")

    hs300_df = get_index_data("sh000300", start_date, end_date)
    zz1000_df = get_index_data("sh000852", start_date, end_date)
    dividend_df = get_index_data("sh000015", start_date, end_date)

    hs300_zz1000_spread = calculate_return_spread(hs300_df, zz1000_df)
    dividend_zz1000_spread = calculate_return_spread(dividend_df, zz1000_df)
    hs300_zz1000_spread = hs300_zz1000_spread.dropna()
    dividend_zz1000_spread = dividend_zz1000_spread.dropna()

    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=zz1000_df.index, y=zz1000_df["close"],
        mode="lines", name="中证1000",
        line=dict(color="orange"), yaxis="y"
    ))

    fig.add_trace(go.Scatter(
        x=hs300_zz1000_spread.index, y=hs300_zz1000_spread * 100,
        mode="lines", name="沪深300-中证1000收益差",
        line=dict(color="blue"), yaxis="y2"
    ))

    fig.add_trace(go.Scatter(
        x=dividend_zz1000_spread.index, y=dividend_zz1000_spread * 100,
        mode="lines", name="红利指数-中证1000收益差",
        line=dict(color="green"), yaxis="y2"
    ))

    fig.add_hline(y=0, line_dash="dash", line_color="red", opacity=0.5)

    fig.update_layout(
        title="指数走势与40日收益差对比",
        xaxis=dict(title="日期"),
        yaxis=dict(title="中证1000指数", side="left", showgrid=True, domain=[0.6, 0.95]),
        yaxis2=dict(title="40日收益差(%)", side="right", showgrid=False, domain=[0, 0.45]),
        hovermode="x unified",
        height=800,
        legend=dict(yanchor="top", y=0.99, xanchor="right", x=0.99),
    )

    return (
        fig,
        hs300_zz1000_spread.iloc[-1] * 100,
        dividend_zz1000_spread.iloc[-1] * 100,
    )


def main():
    st.markdown("### 📈 指数40日收益差分析")
    fig, hs300_zz1000_spread, dividend_zz1000_spread = create_spread_chart()
    st.plotly_chart(fig, use_container_width=True)
    col1, col2 = st.columns(2)
    with col1:
        st.write(f"沪深300-中证1000收益差: {hs300_zz1000_spread:.2f}%")
    with col2:
        st.write(f"红利指数-中证1000收益差: {dividend_zz1000_spread:.2f}%")


if __name__ == "__main__":
    main()

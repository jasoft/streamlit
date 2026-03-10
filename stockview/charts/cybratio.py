import altair as alt
import akshare
import pandas as pd
import streamlit as st

from stockview.akcache import CacheWrapper

ak = CacheWrapper(akshare, cache_time=180)

SYMBOL_SH = "sh000001"
SYMBOL_SZ = "sz399001"
SYMBOL_CYB = "sz399006"


# Fetch data for the indices, data format:

#   date	    open	close	high	low	    amount	    amount
# 0	1991-04-03	988.05	988.05	988.05	988.05	1	        1.000000e+04

@st.cache_data(ttl=180)
def build_cyb_ratio_dataframe(lookback_days: int = 250) -> pd.DataFrame:
    df_sh = ak.stock_zh_index_daily_em(SYMBOL_SH).tail(lookback_days).copy()
    df_sz = ak.stock_zh_index_daily_em(SYMBOL_SZ).tail(lookback_days).copy()
    df_cyb = ak.stock_zh_index_daily_em(SYMBOL_CYB).tail(lookback_days).copy()

    df_sh["date"] = pd.to_datetime(df_sh["date"]).dt.date
    df_sz["date"] = pd.to_datetime(df_sz["date"]).dt.date
    df_cyb["date"] = pd.to_datetime(df_cyb["date"]).dt.date

    df_sh_sz = pd.merge(
        df_sh[["date", "amount"]],
        df_sz[["date", "amount"]],
        on="date",
        suffixes=("_sh", "_sz"),
    )
    df_sh_sz["total_amount"] = df_sh_sz["amount_sh"] + df_sh_sz["amount_sz"]

    df_merged = pd.merge(
        df_sh_sz,
        df_cyb[["date", "amount"]].rename(columns={"amount": "cyb_amount"}),
        on="date",
    )
    df_merged["创业板成交占比"] = (df_merged["cyb_amount"] * 100) / df_merged["total_amount"]

    df_result = df_merged[["date", "创业板成交占比"]]
    df_result = pd.merge(
        df_result,
        df_sh[["date", "close"]].rename(columns={"close": "上证指数"}),
        on="date",
    )
    return df_result


def render_cyb_ratio_page() -> None:
    st.title("创业板成交占比")
    st.caption("用创业板成交额占沪深总成交额的比重，观察小盘成长情绪与大盘位置。")

    lookback_days = st.slider("回看交易日", min_value=60, max_value=500, value=250, step=10)
    df_result = build_cyb_ratio_dataframe(lookback_days)

    base = alt.Chart(df_result).encode(x="date:T")
    bars = base.mark_bar(color="steelblue").encode(
        y=alt.Y("创业板成交占比:Q", scale=alt.Scale(domain=[0, 100])),
        tooltip=["date:T", "创业板成交占比:Q"],
    )
    line = base.mark_line(color="crimson").encode(
        y=alt.Y("上证指数:Q", scale=alt.Scale(domain=[2000, df_result["上证指数"].max()]))
    )
    chart = alt.layer(bars, line).resolve_scale(y="independent")

    latest = df_result.iloc[-1]
    col1, col2 = st.columns(2)
    col1.metric("最新创业板成交占比", f"{latest['创业板成交占比']:.2f}%")
    col2.metric("最新上证指数", f"{latest['上证指数']:.2f}")

    st.altair_chart(chart, use_container_width=True)
    st.dataframe(df_result.sort_values("date", ascending=False), use_container_width=True)


if __name__ == "__main__":
    st.set_page_config(layout="wide")
    render_cyb_ratio_page()

from datetime import datetime, timedelta

import akshare
import altair as alt
import pandas as pd
import streamlit as st

from stockview.akcache.akcache import CacheWrapper

ak = CacheWrapper(akshare, cache_time=180)


@st.cache_data(ttl=180)
def build_index_amount_dataframe(days: int = 365) -> pd.DataFrame:
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

    index_300 = ak.index_zh_a_hist(
        symbol="000300", period="daily", start_date=start_date, end_date=end_date
    )
    index_1000 = ak.index_zh_a_hist(
        symbol="399852", period="daily", start_date=start_date, end_date=end_date
    )
    index_2000 = ak.index_zh_a_hist(
        symbol="932000", period="daily", start_date=start_date, end_date=end_date
    )
    sh_index = ak.index_zh_a_hist(
        symbol="000001", period="daily", start_date=start_date, end_date=end_date
    )
    sz_index = ak.index_zh_a_hist(
        symbol="399001", period="daily", start_date=start_date, end_date=end_date
    )

    df = pd.DataFrame(
        {
            "date": pd.to_datetime(index_300["日期"]).dt.date,
            "沪深300成交额": index_300["成交额"].astype(float),
            "中证1000成交额": index_1000["成交额"].astype(float),
            "中证2000成交额": index_2000["成交额"].astype(float),
            "上证指数成交额": sh_index["成交额"].astype(float),
            "深证指数成交额": sz_index["成交额"].astype(float),
            "沪深300涨幅": index_300["涨跌幅"].astype(float),
            "沪深300收盘价": index_300["收盘"].astype(float),
            "中证1000收盘价": index_1000["收盘"].astype(float),
            "中证1000涨幅": index_1000["涨跌幅"].astype(float),
            "中证2000涨幅": index_2000["涨跌幅"].astype(float),
        }
    )
    df["总成交额"] = df["上证指数成交额"] + df["深证指数成交额"]
    df["沪深300占比"] = (df["沪深300成交额"] / df["总成交额"]).round(4)
    df["中证1000占比"] = (df["中证1000成交额"] / df["总成交额"]).round(4)
    df["中证2000占比"] = (df["中证2000成交额"] / df["总成交额"]).round(4)
    df["中证1000相对沪深300涨幅"] = df["中证1000涨幅"] - df["沪深300涨幅"]
    df["总成交额(亿)"] = (df["总成交额"] / 1e8).astype(int)
    return df


def render_index_amount_compare_page() -> None:
    st.title("指数成交额风格对比")
    st.caption("观察中证1000/2000在总成交额中的占比，以及相对沪深300的日收益表现。")

    lookback_days = st.slider("回看天数", min_value=120, max_value=730, value=365, step=10)

    df = build_index_amount_dataframe(lookback_days)

    st.dataframe(
        df[
            [
                "date",
                "总成交额(亿)",
                "沪深300占比",
                "中证1000占比",
                "中证2000占比",
                "沪深300涨幅",
                "中证1000相对沪深300涨幅",
            ]
        ],
        use_container_width=True,
    )

    chart = (
        alt.Chart(df)
        .mark_circle(size=60)
        .encode(
            x=alt.X("总成交额(亿):Q", title="总成交额(亿)"),
            y=alt.Y("中证1000相对沪深300涨幅:Q", title="中证1000相对沪深300涨幅"),
            tooltip=["date", "总成交额(亿)", "中证1000占比", "中证1000相对沪深300涨幅"],
        )
        .properties(height=420)
        .interactive()
    )
    st.altair_chart(chart, use_container_width=True)

    bins = [i / 100 for i in range(16, 30, 1)]
    analyzed = df.copy()
    analyzed["中证1000占比分组"] = pd.cut(analyzed["中证1000占比"], bins)
    grouped = analyzed.groupby("中证1000占比分组", observed=False)[
        "中证1000相对沪深300涨幅"
    ].agg(比例=lambda x: (x > 0.3).mean(), 总天数="count")

    valid_grouped = grouped[grouped["总天数"] > 0]
    if valid_grouped.empty:
        st.warning("当前样本区间内没有足够数据来识别有效分组。")
        return

    best_group = valid_grouped["比例"].idxmax()
    best_ratio = valid_grouped["比例"].max()

    col1, col2 = st.columns(2)
    col1.metric("最佳中证1000成交占比分组", str(best_group))
    col2.metric("相对涨幅超过 0.3% 的比例", f"{best_ratio:.2%}")

    st.subheader("各分组命中率")
    st.dataframe(
        valid_grouped.reset_index().rename(
            columns={"比例": "相对涨幅超过0.3的比例", "总天数": "交易日数量"}
        ),
        use_container_width=True,
    )

    best_group_days = analyzed[analyzed["中证1000占比分组"] == best_group]
    st.subheader("最佳分组对应交易日")
    st.dataframe(
        best_group_days[
            ["date", "总成交额(亿)", "中证1000占比", "中证1000相对沪深300涨幅"]
        ],
        use_container_width=True,
    )


if __name__ == "__main__":
    st.set_page_config(layout="wide")
    render_index_amount_compare_page()

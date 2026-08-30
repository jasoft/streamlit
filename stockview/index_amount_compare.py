from datetime import datetime, timedelta

import akshare
import altair as alt
import pandas as pd
import streamlit as st

from stockview.tdx_source import fetch_index_hist


@st.cache_data(ttl=180)
def build_index_amount_dataframe(days: int = 365) -> pd.DataFrame:
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

    index_300 = fetch_index_hist(
        symbol="000300", start_date=start_date, end_date=end_date
    )
    index_1000 = fetch_index_hist(
        symbol="399852", start_date=start_date, end_date=end_date
    )
    # 中证2000(932000) 通达信协议无此代码, 走中证官网源, 成交金额单位为亿元
    index_2000 = akshare.stock_zh_index_hist_csindex(
        symbol="932000", start_date=start_date, end_date=end_date
    )
    sh_index = fetch_index_hist(
        symbol="000001", start_date=start_date, end_date=end_date
    )
    sz_index = fetch_index_hist(
        symbol="399001", start_date=start_date, end_date=end_date
    )

    def _keyed(df_src, date_col, value_col):
        """按日期键取序列, 不同源(盘中数据覆盖不一致)必须按键对齐而非按位置."""
        s = pd.to_datetime(df_src[date_col]).dt.date
        return pd.Series(df_src[value_col].astype(float).values, index=s)

    df = pd.DataFrame(
        {
            "沪深300成交额": _keyed(index_300, "日期", "成交额"),
            "中证1000成交额": _keyed(index_1000, "日期", "成交额"),
            "中证2000成交额": _keyed(index_2000, "日期", "成交金额") * 1e8,
            "上证指数成交额": _keyed(sh_index, "日期", "成交额"),
            "深证指数成交额": _keyed(sz_index, "日期", "成交额"),
            "沪深300涨幅": _keyed(index_300, "日期", "涨跌幅"),
            "沪深300收盘价": _keyed(index_300, "日期", "收盘"),
            "中证1000收盘价": _keyed(index_1000, "日期", "收盘"),
            "中证1000涨幅": _keyed(index_1000, "日期", "涨跌幅"),
            "中证2000涨幅": _keyed(index_2000, "日期", "涨跌幅"),
        }
    ).dropna(how="any")
    df["date"] = df.index
    df = df.reset_index(drop=True)
    df["总成交额"] = df["上证指数成交额"] + df["深证指数成交额"]
    df["沪深300占比"] = (df["沪深300成交额"] / df["总成交额"]).round(4)
    df["中证1000占比"] = (df["中证1000成交额"] / df["总成交额"]).round(4)
    df["中证2000占比"] = (df["中证2000成交额"] / df["总成交额"]).round(4)
    df["中证1000相对沪深300涨幅"] = df["中证1000涨幅"] - df["沪深300涨幅"]
    df["总成交额(亿)"] = (df["总成交额"] / 1e8).astype(int)
    return df


from stockview.state import init_slider_state, on_slider_change


def render_index_amount_compare_page() -> None:
    st.title("指数成交额风格对比")
    st.caption("观察中证1000/2000在总成交额中的占比，以及相对沪深300的日收益表现。")

    slider_key = "index_compare_lookback_days"
    init_val = init_slider_state(slider_key, default_value=365, min_value=120, max_value=730)
    lookback_days = st.slider(
        "回看天数",
        min_value=120,
        max_value=730,
        value=init_val,
        step=10,
        key=slider_key,
        on_change=on_slider_change,
        args=(slider_key,),
    )

    try:
        df = build_index_amount_dataframe(lookback_days)
    except Exception as exc:
        st.error(f"指数数据获取失败: {exc}")
        return

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

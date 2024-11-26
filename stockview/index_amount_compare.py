import streamlit as st
import akshare
from akcache import CacheWrapper
import pandas as pd
from datetime import datetime, timedelta
import altair as alt

ak = CacheWrapper(akshare, cache_time=180)

# 获取最近一年的日期范围
end_date = datetime.now().strftime("%Y%m%d")
start_date = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")

# 获取指数数据
index_300 = ak.index_zh_a_hist(
    symbol="000300", period="daily", start_date=start_date, end_date=end_date
)
index_1000 = ak.index_zh_a_hist(
    symbol="399852", period="daily", start_date=start_date, end_date=end_date
)
index_2000 = ak.index_zh_a_hist(
    symbol="932000", period="daily", start_date=start_date, end_date=end_date
)
# 获取上证和深证指数的成交额信息
sh_index = ak.index_zh_a_hist(
    symbol="000001", period="daily", start_date=start_date, end_date=end_date
)
sz_index = ak.index_zh_a_hist(
    symbol="399001", period="daily", start_date=start_date, end_date=end_date
)

# 整合数据到一个DataFrame中
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

# 计算每天的总成交额
df["总成交额"] = df["上证指数成交额"] + df["深证指数成交额"]

# 计算沪深300, 中证1000, 中证2000在每天的成交额占总成交额的比例
df["沪深300占比"] = (df["沪深300成交额"] / df["总成交额"]).round(2)
df["中证1000占比"] = (df["中证1000成交额"] / df["总成交额"]).round(2)
df["中证2000占比"] = (df["中证2000成交额"] / df["总成交额"]).round(2)
df["中证1000相对沪深300涨幅"] = df["中证1000涨幅"] - df["沪深300涨幅"]

# 显示数据表格
st.set_page_config(layout="wide")
df["总成交额(亿)"] = (df["总成交额"] / 1e8).astype(int)

st.write(
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
    ]
)
# 绘制成交额和相对涨幅的关系图
chart = (
    alt.Chart(df)
    .mark_circle(size=60)
    .encode(
        x=alt.X("总成交额(亿)", title="总成交额(亿)"),
        y=alt.Y("中证1000相对沪深300涨幅", title="中证1000相对沪深300涨幅"),
        tooltip=["date", "总成交额(亿)", "中证1000相对沪深300涨幅"],
    )
    .properties(width=800, height=400)
    .interactive()
)

st.altair_chart(chart, use_container_width=True)

# 从0.15循环到0.4, 0.01步长, 找出超过哪个占比, 中证1000相对涨幅为正的日子最多, 给出详细数据

bins = [i / 100 for i in range(16, 30, 1)]  # 从0.15到0.4，步长为0.01
df["中证1000占比分组"] = pd.cut(df["中证1000占比"], bins)

# 计算每个组别中中证1000相对沪深300涨幅为正的比例
grouped = df.groupby("中证1000占比分组")["中证1000相对沪深300涨幅"].agg(
    比例=lambda x: (x > 0.3).mean(), 总天数="count"
)

# 找出最佳比例
best_group = grouped["比例"].idxmax()
best_ratio = grouped["比例"].max()

# 显示结果
st.write(f"最佳比例区间: {best_group}")
st.write(f"在该区间内中证1000相对沪深300涨幅为正的比例: {best_ratio:.2%}")

# 显示详细数据
st.write("各组别中证1000相对沪深300涨幅为正的比例")

st.dataframe(
    grouped.reset_index().rename(
        columns={"比例": "相对涨幅超过0.3的比例", "总天数": "交易日数量"}
    )
)
# 列出符合最佳组别条件的所有交易日
best_group_days = df[df["中证1000占比分组"] == best_group]

# 显示符合条件的交易日
st.write("符合最佳比例区间的交易日:")
st.dataframe(
    best_group_days[["date", "总成交额(亿)", "中证1000占比", "中证1000相对沪深300涨幅"]]
)

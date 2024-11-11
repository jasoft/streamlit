import streamlit as st
import akshare
from akcache import CacheWrapper
import pandas as pd
import altair as alt
from datetime import datetime, timedelta

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

# 确保日期列为 datetime 格式
index_300["日期"] = pd.to_datetime(index_300["日期"])
index_1000["日期"] = pd.to_datetime(index_1000["日期"])
index_2000["日期"] = pd.to_datetime(index_2000["日期"])

# 创建数据框
df_300 = index_300[["日期", "成交额", "收盘"]].rename(
    columns={"成交额": "成交额_300", "收盘": "收盘_300"}
)
df_1000 = index_1000[["日期", "成交额"]].rename(columns={"成交额": "成交额_1000"})
df_2000 = index_2000[["日期", "成交额"]].rename(columns={"成交额": "成交额_2000"})

# 合并数据框
df = pd.merge(df_300, df_1000, on="日期")
df = pd.merge(df, df_2000, on="日期")
# 将成交额单位改为亿
df["成交额_300"] = df["成交额_300"] / 1e8
df["成交额_1000"] = df["成交额_1000"] / 1e8
df["成交额_2000"] = df["成交额_2000"] / 1e8

# 使用 Streamlit 画图
st.title("指数每日成交额对比")
st.write("数据来源：akshare")

# 创建成交额对比图
base = alt.Chart(df).encode(x="日期:T")

line_300 = (
    base.mark_bar(color="blue").encode(y="成交额_300:Q").properties(title="300成交额")
)
line_1000 = (
    base.mark_bar(color="green")
    .encode(y="成交额_1000:Q")
    .properties(title="1000成交额")
)
line_2000 = (
    base.mark_bar(color="red").encode(y="成交额_2000:Q").properties(title="2000成交额")
)

volume_chart = alt.layer(
    line_300,
    line_1000,
    line_2000,
    base.mark_line(color="orange").encode(y="收盘_300:Q"),
).resolve_scale(y="independent")


# 显示图表
st.altair_chart(volume_chart, use_container_width=True)

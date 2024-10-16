import sys
import os

# 获取上级目录路径
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, parent_dir)
from akcache import akshare as ak
import pandas as pd
import streamlit as st
import altair as alt

SYMBOL_SH = "sh000001"
SYMBOL_SZ = "sz399001"
SYMBOL_CYB = "sz399006"


# Fetch data for the indices, data format:

#   date	    open	close	high	low	    amount	    amount
# 0	1991-04-03	988.05	988.05	988.05	988.05	1	        1.000000e+04

df_sh = ak.stock_zh_index_daily_em(SYMBOL_SH).tail(250)
df_sz = ak.stock_zh_index_daily_em(SYMBOL_SZ).tail(250)
df_cyb = ak.stock_zh_index_daily_em(SYMBOL_CYB).tail(250)

# Ensure the date columns are in datetime format
df_sh["date"] = pd.to_datetime(df_sh["date"]).dt.date
df_sz["date"] = pd.to_datetime(df_sz["date"]).dt.date
df_cyb["date"] = pd.to_datetime(df_cyb["date"]).dt.date

# Merge the amounts of SH and SZ
df_sh_sz = pd.merge(
    df_sh[["date", "amount"]],
    df_sz[["date", "amount"]],
    on="date",
    suffixes=("_sh", "_sz"),
)
df_sh_sz["total_amount"] = df_sh_sz["amount_sh"] + df_sh_sz["amount_sz"]

# Use CYB to divide SH+SZ
df_merged = pd.merge(
    df_sh_sz,
    df_cyb[["date", "amount"]].rename(columns={"amount": "cyb_amount"}),
    on="date",
)
scaling_factor = 100  # Adjust this factor as needed
df_merged["创业板成交占比"] = (df_merged["cyb_amount"] * scaling_factor) / df_merged[
    "total_amount"
]


# Create a new DataFrame with the required columns
df_result = df_merged[["date", "创业板成交占比"]]
# Add the close prices from df_sh to the result DataFrame
df_result = pd.merge(
    df_result, df_sh[["date", "close"]].rename(columns={"close": "上证指数"}), on="date"
)
# Use Streamlit to draw a chart
# Create a base chart with the date as the x-axis
base = alt.Chart(df_result).mark_line().encode(x="date:T", y="创业板成交占比:Q")

# Create a bar chart for 创业板成交占比
bars = base.mark_bar(color="blue").encode(
    y=alt.Y("创业板成交占比:Q", scale=alt.Scale(domain=[0, 100])),
    tooltip=["date:T", "创业板成交占比:Q"],
)

# Create a line chart for 上证指数
line = base.mark_line(color="red").encode(
    y=alt.Y("上证指数:Q", scale=alt.Scale(domain=[2000, df_result["上证指数"].max()]))
)

# Combine the bar and line charts
chart = alt.layer(bars, line).resolve_scale(
    y="independent"  # Ensure the y-axes are independent
)

# Display the chart using Streamlit
st.altair_chart(chart, use_container_width=True)

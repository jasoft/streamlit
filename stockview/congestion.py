import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from bs4 import BeautifulSoup
import requests


# 数据获取函数
def get_html_content(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.text
    except requests.RequestException as e:
        st.error(f"获取数据时发生错误: {e}")
        return None


# 数据处理函数
def process_html_data(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    table = soup.find("table", class_="table table-striped table-condensed")

    if not table:
        st.error("无法在页面中找到所需的表格。")
        return None

    data = []
    for row in table.find_all("tr")[1:]:  # Skip the header row
        cols = row.find_all("td")
        if len(cols) == 5:
            data.append(
                {
                    "日期": cols[0].text.strip(),
                    "收盘价": float(cols[1].text.strip()),
                    "前5%的个股的成交额(亿元)": float(cols[2].text.strip()),
                    "全部A股成交额(亿元)": float(cols[3].text.strip()),
                    "拥挤度(%)": float(cols[4].text.strip()),
                }
            )

    if not data:
        st.error("未能从表格中提取到数据。")
        return None

    df = pd.DataFrame(data)
    df["日期"] = pd.to_datetime(df["日期"])
    return df


# Streamlit 应用
def main():
    st.title("A股市场拥挤度和上证指数走势图")
    # 设置页面布局为宽屏模式
    # 用户输入 URL
    url = st.text_input(
        "输入数据源 URL", "https://legulegu.com/stockdata/ashares-congestion"
    )

    with st.spinner("正在获取和处理数据..."):
        # 获取 HTML 内容
        html_content = get_html_content(url)

        if html_content:
            # 处理数据
            df = process_html_data(html_content)

            if df is not None and not df.empty:
                # 创建双轴图表
                fig = make_subplots(specs=[[{"secondary_y": True}]])

                # 添加拥挤度折线
                fig.add_trace(
                    go.Scatter(
                        x=df["日期"],
                        y=df["拥挤度(%)"],
                        name="拥挤度(%)",
                        line=dict(color="blue"),
                    ),
                    secondary_y=False,
                )

                # 添加上证指数折线
                fig.add_trace(
                    go.Scatter(
                        x=df["日期"],
                        y=df["收盘价"],
                        name="上证指数",
                        line=dict(color="red"),
                    ),
                    secondary_y=True,
                )

                # 更新布局
                fig.update_layout(
                    title_text="A股市场拥挤度和上证指数走势图",
                    xaxis_title="日期",
                    legend=dict(
                        orientation="h",
                        yanchor="bottom",
                        y=1.02,
                        xanchor="right",
                        x=1,
                    ),
                )

                # 更新y轴标题
                fig.update_yaxes(
                    title_text="拥挤度(%)", secondary_y=False, color="blue"
                )
                fig.update_yaxes(title_text="上证指数", secondary_y=True, color="red")

                # 显示图表
                st.plotly_chart(fig, use_container_width=True)

                # 显示数据表格
                st.subheader("原始数据")
                st.dataframe(df)
            else:
                st.error("无法处理获取到的数据。请检查 URL 是否正确。")


if __name__ == "__main__":
    main()

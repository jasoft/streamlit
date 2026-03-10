from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> None:
    st.set_page_config(
        "Stock Analysis Dashboard",
        "📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    from stockview.charts.cybratio import render_cyb_ratio_page
    from stockview.congestion import render_congestion_page
    from stockview.if_im_page import render_if_im_page
    from stockview.index_amount_compare import render_index_amount_compare_page
    from stockview.main import streamlit_app as render_market_dashboard

    st.sidebar.title("Stock Analysis")
    page = st.sidebar.radio(
        "选择功能",
        [
            "综合面板",
            "IF-IM 风格配对",
            "指数成交额风格对比",
            "市场拥挤度",
            "创业板成交占比",
            "使用说明",
        ],
        index=0,
    )

    if page == "综合面板":
        render_market_dashboard()
    elif page == "IF-IM 风格配对":
        render_if_im_page()
    elif page == "指数成交额风格对比":
        render_index_amount_compare_page()
    elif page == "市场拥挤度":
        render_congestion_page()
    elif page == "创业板成交占比":
        render_cyb_ratio_page()
    else:
        st.title("使用说明")
        st.markdown(
            """
            - 统一入口: `streamlit run stockview/app.py`
            - 综合面板: 成交量情绪、龙头股、指数收益差
            - IF-IM 风格配对: 自动计算指数比值、PE 比值、历史分位、成交占比和条件胜率
            - 指数成交额风格对比: 观察沪深300/中证1000/中证2000占总成交额比例与风格超额
            - 市场拥挤度: 抓取乐咕乐股拥挤度页面并画双轴图
            - 创业板成交占比: 观察创业板占沪深总成交额比例
            - 脚本入口: `./.venv/bin/python scripts/if_im_style_analysis.py`
            """
        )


if __name__ == "__main__":
    main()

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> None:
    st.set_page_config(
        "Stock Analysis Dashboard",
        "📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.sidebar.title("Stock Analysis")
    page = st.sidebar.radio(
        "选择功能",
        [
            "综合面板",
            "指数成交额风格对比",
            "沪深300行业权重",
            "市场拥挤度",
            "创业板成交占比",
            "实时板块资金流向",
            "创业板ETF买卖信号",
            "使用说明",
        ],
        index=0,
    )

    if page == "综合面板":
        from data_analysis.stockview.main import streamlit_app as render_market_dashboard

        render_market_dashboard()
    elif page == "指数成交额风格对比":
        from data_analysis.stockview.index_amount_compare import render_index_amount_compare_page

        render_index_amount_compare_page()
    elif page == "沪深300行业权重":
        from data_analysis.stockview.hs300_industry import render_hs300_industry_page

        render_hs300_industry_page()
    elif page == "实时板块资金流向":
        import streamlit.components.v1 as components
        components.html(
            """
            <iframe id="nextjs-iframe" src="/fundflow" width="100%" height="900px" style="border:none; margin:0; padding:0;"></iframe>
            """,
            height=900
        )
    elif page == "创业板ETF买卖信号":
        from data_analysis.stockview.etf_signal import render_etf_signal_page

        render_etf_signal_page()
    elif page == "市场拥挤度":
        from data_analysis.stockview.congestion import render_congestion_page

        render_congestion_page()
    elif page == "创业板成交占比":
        from data_analysis.stockview.charts.cybratio import render_cyb_ratio_page

        render_cyb_ratio_page()
    else:
        st.title("使用说明")
        st.markdown(
            """
            - 统一入口: `streamlit run stockview/app.py`
            - 综合面板: 成交量情绪、龙头股、指数收益差
            - 指数成交额风格对比: 观察沪深300/中证1000/中证2000占总成交额比例与风格超额
            - 沪深300行业权重: 按中证成分权重和问财行业归属聚合行业占比
            - 市场拥挤度: 抓取乐咕乐股拥挤度页面并画双轴图
            - 创业板成交占比: 观察创业板占沪深总成交额比例
            - 实时板块资金流向: 行业/概念/地域资金流分时曲线、排行看板、自动刷新
            - 创业板ETF买卖信号: 趋势/动量/量能/成分股宽度/消息情绪五因子合成 -100~+100 评分与七档评级, 盘中60秒自动刷新
            """
        )


if __name__ == "__main__":
    main()

"""
IF/IM 组合策略分析页面
多因子评分系统：季节性、成交量、牛熊氛围、估值、市场偏好、宏观政策
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime
import requests
import json

from stockview.akcache.rate_limiter import rate_limiter
from stockview.log import logger

# 腾讯行情 API
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="

# 指数代码
INDEX_CODES = {
    "sh000300": "沪深300",
    "sh000852": "中证1000",
    "sh000905": "中证500",
}


@rate_limiter
def _fetch_index_quotes_raw() -> dict:
    """获取指数实时行情"""
    try:
        symbols = ",".join(INDEX_CODES.keys())
        url = f"{TENCENT_QUOTE_URL}{symbols}"
        response = requests.get(url, timeout=10)
        result = {}
        for line in response.text.strip().split(";"):
            if "=" not in line:
                continue
            data = line.split("=")[1].strip('"').split("~")
            if len(data) < 40:
                continue
            code = data[2]
            result[code] = {
                "name": data[1],
                "price": float(data[3]) if data[3] else 0,
                "change_pct": float(data[32]) if data[32] else 0,
                "amount": float(data[37]) if data[37] else 0,
            }
        return result
    except Exception as e:
        logger.error(f"获取指数行情失败: {e}")
        return {}


@st.cache_data(ttl=180)
def fetch_index_quotes() -> dict:
    return _fetch_index_quotes_raw()


def get_seasonal_score() -> tuple:
    """季节性评分：基于历史月度表现数据"""
    month = datetime.now().month

    # 基于历史统计的月度胜率 (中证1000相对沪深300)
    # 数据来自 index_comparison.py 的分析结果
    seasonal_data = {
        1: {"im_win_rate": 50, "avg_spread": -2.75, "bias": "IF"},
        2: {"im_win_rate": 100, "avg_spread": 3.71, "bias": "IM"},
        3: {"im_win_rate": 60, "avg_spread": 1.2, "bias": "IM"},
        4: {"im_win_rate": 17, "avg_spread": -2.09, "bias": "IF"},
        5: {"im_win_rate": 60, "avg_spread": 0.8, "bias": "IM"},
        6: {"im_win_rate": 60, "avg_spread": 1.5, "bias": "IM"},
        7: {"im_win_rate": 80, "avg_spread": 3.02, "bias": "IM"},
        8: {"im_win_rate": 60, "avg_spread": 1.0, "bias": "IM"},
        9: {"im_win_rate": 60, "avg_spread": 0.5, "bias": "IM"},
        10: {"im_win_rate": 60, "avg_spread": 4.03, "bias": "IM"},
        11: {"im_win_rate": 60, "avg_spread": 1.8, "bias": "IM"},
        12: {"im_win_rate": 80, "avg_spread": -2.44, "bias": "IF"},
    }

    data = seasonal_data[month]
    im_score = data["im_win_rate"] / 10
    if_score = 10 - im_score

    return im_score, if_score, f"{month}月历史胜率: IM {data['im_win_rate']}%, 平均收益差 {data['avg_spread']:+.2f}%"


def get_volume_score() -> tuple:
    """成交量评分：基于总成交额（从market_heat获取）"""
    try:
        from stockview.main import get_market_heat
        data = get_market_heat()
        if data is None:
            return 5, 5, "无数据"

        # data["数值"][8] = 预计今日总成交额（亿）
        total_amount = data["数值"][8] if data["数值"][8] else data["数值"][9]

        # 成交额判断（单位：亿）
        # 2.5万亿以上为高成交，利多IM
        if total_amount > 25000:
            im_score, if_score = 9, 1
            reason = f"成交额 {total_amount/10000:.1f}万亿 (高成交，强利多IM)"
        elif total_amount > 20000:
            im_score, if_score = 7, 3
            reason = f"成交额 {total_amount/10000:.1f}万亿 (放量，利多IM)"
        elif total_amount > 15000:
            im_score, if_score = 5, 5
            reason = f"成交额 {total_amount/10000:.1f}万亿 (温和)"
        elif total_amount > 10000:
            im_score, if_score = 3, 7
            reason = f"成交额 {total_amount/10000:.1f}万亿 (缩量，利多IF)"
        else:
            im_score, if_score = 1, 9
            reason = f"成交额 {total_amount/10000:.1f}万亿 (极度缩量，强利多IF)"

        return im_score, if_score, reason
    except Exception as e:
        logger.error(f"获取成交量数据失败: {e}")
        return 5, 5, f"获取失败: {e}"


def get_sentiment_score() -> tuple:
    """牛熊氛围评分：基于涨跌数据"""
    try:
        from stockview.main import get_market_heat
        data = get_market_heat()
        if data is None:
            return 5, 5, "无数据"

        up_ratio = data["数值"][14]  # 上涨占比
        limit_up = data["数值"][15]   # 涨停
        limit_down = data["数值"][16] # 跌停

        # 评分逻辑
        if up_ratio > 65 and limit_up > 80:
            im_score, if_score = 8, 2
            reason = f"强势市场: 上涨{up_ratio:.1f}%, 涨停{limit_up}只"
        elif up_ratio > 55:
            im_score, if_score = 6, 4
            reason = f"偏强市场: 上涨{up_ratio:.1f}%"
        elif up_ratio > 45:
            im_score, if_score = 5, 5
            reason = f"震荡市场: 上涨{up_ratio:.1f}%"
        elif up_ratio > 35:
            im_score, if_score = 4, 6
            reason = f"偏弱市场: 上涨{up_ratio:.1f}%"
        else:
            im_score, if_score = 2, 8
            reason = f"弱势市场: 上涨{up_ratio:.1f}%, 跌停{limit_down}只"

        return im_score, if_score, reason
    except Exception as e:
        logger.error(f"获取情绪数据失败: {e}")
        return 5, 5, f"获取失败: {e}"


def get_valuation_score() -> tuple:
    """估值评分：基于PE分位数（使用近似值）"""
    # 简化处理：使用市场普遍认知的估值区间
    # 实际应用可接入更精确的估值数据

    # 沪深300 PE约11-12倍 (历史分位约40%)
    # 中证1000 PE约40-50倍 (历史分位约30%)
    # 基于当前市场环境的近似判断

    im_score, if_score = 5, 5
    reason = "估值数据需接入专业数据源"

    # 这里可以扩展接入真实的PE数据
    # 暂时返回中性评分
    return im_score, if_score, reason


def get_preference_score(quotes: dict) -> tuple:
    """市场偏好评分：基于大小盘相对表现"""
    if not quotes:
        return 5, 5, "无数据"

    hs300_change = quotes.get("000300", {}).get("change_pct", 0)
    csi1000_change = quotes.get("000852", {}).get("change_pct", 0)

    spread = csi1000_change - hs300_change

    if spread > 1.5:
        im_score, if_score = 8, 2
        reason = f"小盘强势: 中证1000 {csi1000_change:+.2f}%, 沪深300 {hs300_change:+.2f}%"
    elif spread > 0.5:
        im_score, if_score = 6, 4
        reason = f"小盘偏强: 中证1000 {csi1000_change:+.2f}%, 沪深300 {hs300_change:+.2f}%"
    elif spread > -0.5:
        im_score, if_score = 5, 5
        reason = f"大小盘均衡: 中证1000 {csi1000_change:+.2f}%, 沪深300 {hs300_change:+.2f}%"
    elif spread > -1.5:
        im_score, if_score = 4, 6
        reason = f"大盘偏强: 沪深300 {hs300_change:+.2f}%, 中证1000 {csi1000_change:+.2f}%"
    else:
        im_score, if_score = 2, 8
        reason = f"大盘强势: 沪深300 {hs300_change:+.2f}%, 中证1000 {csi1000_change:+.2f}%"

    return im_score, if_score, reason


def create_score_gauge(im_score: float, if_score: float) -> go.Figure:
    """创建评分仪表盘"""
    fig = go.Figure()

    # IM评分
    fig.add_trace(go.Indicator(
        mode="gauge+number+delta",
        value=im_score,
        title={"text": "IM (中证1000)", "font": {"size": 16}},
        domain={"x": [0, 0.45], "y": [0, 1]},
        gauge={
            "axis": {"range": [0, 10], "tickwidth": 1},
            "bar": {"color": "rgb(215, 48, 39)"},
            "steps": [
                {"range": [0, 3], "color": "lightblue"},
                {"range": [3, 7], "color": "lightyellow"},
                {"range": [7, 10], "color": "lightpink"},
            ],
            "threshold": {
                "line": {"color": "red", "width": 4},
                "thickness": 0.75,
                "value": 7
            }
        }
    ))

    # IF评分
    fig.add_trace(go.Indicator(
        mode="gauge+number+delta",
        value=if_score,
        title={"text": "IF (沪深300)", "font": {"size": 16}},
        domain={"x": [0.55, 1], "y": [0, 1]},
        gauge={
            "axis": {"range": [0, 10], "tickwidth": 1},
            "bar": {"color": "rgb(44, 127, 184)"},
            "steps": [
                {"range": [0, 3], "color": "lightpink"},
                {"range": [3, 7], "color": "lightyellow"},
                {"range": [7, 10], "color": "lightblue"},
            ],
            "threshold": {
                "line": {"color": "blue", "width": 4},
                "thickness": 0.75,
                "value": 7
            }
        }
    ))

    fig.update_layout(
        height=250,
        margin=dict(l=30, r=30, t=80, b=30),
    )

    return fig


def create_radar_chart(scores: dict) -> go.Figure:
    """创建雷达图"""
    categories = list(scores.keys())
    im_values = [scores[k]["im"] for k in categories]
    if_values = [scores[k]["if"] for k in categories]

    # 闭合雷达图
    categories.append(categories[0])
    im_values.append(im_values[0])
    if_values.append(if_values[0])

    fig = go.Figure()

    fig.add_trace(go.Scatterpolar(
        r=im_values,
        theta=categories,
        fill='toself',
        name='IM (中证1000)',
        line_color='rgb(215, 48, 39)',
        fillcolor='rgba(215, 48, 39, 0.2)',
    ))

    fig.add_trace(go.Scatterpolar(
        r=if_values,
        theta=categories,
        fill='toself',
        name='IF (沪深300)',
        line_color='rgb(44, 127, 184)',
        fillcolor='rgba(44, 127, 184, 0.2)',
    ))

    fig.update_layout(
        polar=dict(
            radialaxis=dict(
                visible=True,
                range=[0, 10]
            )),
        showlegend=True,
        height=400,
        margin=dict(l=80, r=80, t=40, b=40),
    )

    return fig


def create_signal_history_chart() -> go.Figure:
    """创建信号历史图表（示例数据）"""
    # 生成示例数据
    dates = pd.date_range(end=datetime.now(), periods=30, freq='D')
    im_scores = [5 + 2 * (i % 7 - 3) / 3 for i in range(30)]
    if_scores = [10 - s for s in im_scores]

    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=dates,
        y=im_scores,
        mode='lines+markers',
        name='IM评分',
        line=dict(color='rgb(215, 48, 39)', width=2),
    ))

    fig.add_trace(go.Scatter(
        x=dates,
        y=if_scores,
        mode='lines+markers',
        name='IF评分',
        line=dict(color='rgb(44, 127, 184)', width=2),
    ))

    # 添加决策区域
    fig.add_hline(y=7, line_dash="dash", line_color="red", opacity=0.5, annotation_text="强IM信号")
    fig.add_hline(y=3, line_dash="dash", line_color="blue", opacity=0.5, annotation_text="强IF信号")

    fig.update_layout(
        title="策略评分趋势（示例）",
        xaxis_title="日期",
        yaxis_title="评分",
        yaxis=dict(range=[0, 10]),
        height=350,
        hovermode="x unified",
    )

    return fig


def render_if_im_strategy_page():
    """渲染IF/IM策略分析页面"""
    st.title("🎯 IF/IM 组合策略分析")
    st.markdown("""
    **多因子评分系统** - 综合判断何时做多IM/做空IF，或做多IF/做空IM

    - **IM (中证1000)**：小盘成长股代表
    - **IF (沪深300)**：大盘蓝筹股代表
    """)

    # 获取实时数据
    quotes = fetch_index_quotes()

    # ========== 多因子评分 ==========
    st.markdown("---")
    st.subheader("📊 多因子评分系统")

    # 计算各维度评分
    scores = {}

    # 1. 季节性
    im_s, if_s, reason = get_seasonal_score()
    scores["季节性"] = {"im": im_s, "if": if_s, "reason": reason, "weight": 15}

    # 2. 成交量
    im_s, if_s, reason = get_volume_score()
    scores["成交量"] = {"im": im_s, "if": if_s, "reason": reason, "weight": 20}

    # 3. 牛熊氛围
    im_s, if_s, reason = get_sentiment_score()
    scores["牛熊氛围"] = {"im": im_s, "if": if_s, "reason": reason, "weight": 25}

    # 4. 估值
    im_s, if_s, reason = get_valuation_score()
    scores["估值"] = {"im": im_s, "if": if_s, "reason": reason, "weight": 20}

    # 5. 市场偏好
    im_s, if_s, reason = get_preference_score(quotes)
    scores["市场偏好"] = {"im": im_s, "if": if_s, "reason": reason, "weight": 15}

    # 6. 宏观政策（简化）
    scores["宏观政策"] = {"im": 5, "if": 5, "reason": "需人工判断", "weight": 5}

    # 计算加权总分
    im_total = sum(scores[k]["im"] * scores[k]["weight"] / 100 for k in scores)
    if_total = sum(scores[k]["if"] * scores[k]["weight"] / 100 for k in scores)

    # 显示仪表盘
    col1, col2 = st.columns([2, 1])

    with col1:
        fig = create_score_gauge(im_total * 10, if_total * 10)
        st.plotly_chart(fig, use_container_width=True)

    with col2:
        st.markdown("#### 📋 综合评分")
        diff = abs(im_total - if_total)

        if diff > 2:
            if im_total > if_total:
                st.success(f"**强烈看多IM**\n\n差值: {diff:.2f}")
                st.markdown("🔴 建议: **做多IM / 做空IF**")
            else:
                st.success(f"**强烈看多IF**\n\n差值: {diff:.2f}")
                st.markdown("🔵 建议: **做多IF / 做空IM**")
        elif diff > 1:
            if im_total > if_total:
                st.info(f"**偏多IM**\n\n差值: {diff:.2f}")
                st.markdown("🟠 建议: **轻仓多IM / 空IF**")
            else:
                st.info(f"**偏多IF**\n\n差值: {diff:.2f}")
                st.markdown("🟠 建议: **轻仓多IF / 空IM**")
        else:
            st.warning(f"**观望**\n\n差值: {diff:.2f}")
            st.markdown("⚪ 建议: **暂不建仓或对冲持仓**")

    # ========== 雷达图 ==========
    st.markdown("---")
    st.subheader("🕸️ 多维度对比")

    fig = create_radar_chart(scores)
    st.plotly_chart(fig, use_container_width=True)

    # ========== 各维度详情 ==========
    st.markdown("---")
    st.subheader("📝 各维度评分明细")

    for dim, data in scores.items():
        with st.expander(f"**{dim}** (权重{data['weight']}%) - {data['reason']}"):
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("IM评分", f"{data['im']}/10")
            with col2:
                st.metric("IF评分", f"{data['if']}/10")
            with col3:
                bias = "IM" if data['im'] > data['if'] else "IF" if data['if'] > data['im'] else "中性"
                st.metric("偏向", bias)

    # ========== 策略建议 ==========
    st.markdown("---")
    st.subheader("💡 策略建议")

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("""
        **做多IM / 做空IF 时机：**
        - 🟢 季节性：2月、7月、10月
        - 🟢 成交放量 > 1.5万亿
        - 🟢 市场情绪高涨，涨停 > 80只
        - 🟢 小盘股相对强势
        - 🟢 流动性宽松周期
        """)

    with col2:
        st.markdown("""
        **做多IF / 做空IM 时机：**
        - 🔵 季节性：1月、4月、12月
        - 🔵 成交缩量 < 8000亿
        - 🔵 市场避险情绪升温
        - 🔵 大盘蓝筹相对强势
        - 🔵 外资持续流入
        """)

    # ========== 实时行情 ==========
    st.markdown("---")
    st.subheader("📈 实时行情")

    if quotes:
        col1, col2, col3 = st.columns(3)

        with col1:
            hs300 = quotes.get("000300", {})
            st.metric(
                "沪深300 (IF)",
                f"{hs300.get('price', 0):.2f}",
                f"{hs300.get('change_pct', 0):+.2f}%"
            )

        with col2:
            csi1000 = quotes.get("000852", {})
            st.metric(
                "中证1000 (IM)",
                f"{csi1000.get('price', 0):.2f}",
                f"{csi1000.get('change_pct', 0):+.2f}%"
            )

        with col3:
            spread = csi1000.get('change_pct', 0) - hs300.get('change_pct', 0)
            st.metric(
                "日内价差",
                f"{spread:+.2f}%",
                "IM相对强弱"
            )

    # ========== 风险提示 ==========
    st.markdown("---")
    st.warning("""
    ⚠️ **风险提示**
    - 本策略评分仅供参考，不构成投资建议
    - 股指期货交易需开通相应权限，注意保证金管理
    - 极端行情下价差可能持续偏离，注意止损
    - 小盘股流动性风险：IM在极端行情可能大幅贴水
    """)

    # 数据来源
    st.caption("数据来源：腾讯行情、同花问财 | 评分模型基于历史统计，不代表未来收益")


if __name__ == "__main__":
    st.set_page_config(page_title="IF/IM策略分析", page_icon="🎯", layout="wide")
    render_if_im_strategy_page()

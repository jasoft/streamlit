"""创业板 ETF 多因子买卖信号面板.

数据源:
- 日线历史: 通达信协议 (stockview.tdx_source, 替代原新浪源, 稳定不被封)
- 盘中实时: 腾讯行情 qt.gtimg.cn (批量、稳定)
- 财经快讯: 新浪全球财经快讯 (akshare.stock_info_global_sina)
- 成分股权重: 内置快照, 面板中可编辑校准

因子体系 (总分 [-100, +100]):
- 趋势 trend      25%: 均线排列 / 价格偏离 MA20 / MACD 状态
- 动量 momentum   20%: 近5日收益动能 + RSI14 超买超卖修正
- 量能 volume     20%: 量比方向配合 / 量价背离检测
- 宽度 breadth    25%: 前十大成分股权重加权涨跌 / 家数比 / 龙头贡献
- 情绪 news       10%: 快讯利好利空关键词净命中

评级映射:
>=55 强烈买入 | >=25 买入 | >=10 偏多 | >-10 中性观望
>-25 偏空 | >-55 卖出 | <=-55 强烈卖出
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

try:
    import akshare as ak
except Exception:  # pragma: no cover
    ak = None

import streamlit as st

from data_analysis.stockview.tdx_source import fetch_etf_daily as _fetch_etf_daily_raw
from data_analysis.stockview.tdx_source import fetch_index_daily as _fetch_index_daily_raw

SH_TZ = dt.timezone(dt.timedelta(hours=8))

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

ETF_CODE = "sz159915"
ETF_NAME = "创业板ETF易方达 (159915)"
INDEX_CODE = "sz399006"

# 创业板指前十大权重股 (内置快照 2026-08, 可在面板中编辑校准)
DEFAULT_CONSTITUENTS: List[Dict[str, object]] = [
    {"code": "sz300750", "name": "宁德时代", "weight": 19.0},
    {"code": "sz300059", "name": "东方财富", "weight": 8.0},
    {"code": "sz300760", "name": "迈瑞医疗", "weight": 6.0},
    {"code": "sz300308", "name": "中际旭创", "weight": 4.8},
    {"code": "sz300502", "name": "新易盛", "weight": 3.8},
    {"code": "sz300274", "name": "阳光电源", "weight": 3.2},
    {"code": "sz300124", "name": "汇川技术", "weight": 2.8},
    {"code": "sz300014", "name": "亿纬锂能", "weight": 2.6},
    {"code": "sz300498", "name": "温氏股份", "weight": 2.2},
    {"code": "sz300015", "name": "爱尔眼科", "weight": 1.8},
]

FACTOR_WEIGHTS: Dict[str, float] = {
    "trend": 0.25,
    "momentum": 0.20,
    "volume": 0.20,
    "breadth": 0.25,
    "news": 0.10,
}

POSITIVE_WORDS = [
    "利好", "增长", "上涨", "突破", "获批", "中标", "回购", "增持",
    "盈利", "超预期", "政策支持", "降准", "降息", "刺激", "回暖",
    "复苏", "扩张", "创新高", "涨停",
]
NEGATIVE_WORDS = [
    "利空", "下跌", "亏损", "减持", "处罚", "调查", "退市", "违约",
    "下滑", "暴跌", "制裁", "关税", "警告", "风险", "崩盘", "熔断",
    "跌停", "立案",
]


# ---------------------------------------------------------------------------
# 数据获取
# ---------------------------------------------------------------------------


@dataclass
class RealtimeQuote:
    code: str
    name: str
    price: float
    prev_close: float
    open_: float
    high: float
    low: float
    volume: float          # 手
    amount: float          # 万元
    change_pct: float      # %
    time_text: str = ""

    @property
    def change_pct_safe(self) -> float:
        return self.change_pct if self.change_pct == self.change_pct else 0.0


def _parse_tencent_payload(payload: str) -> Dict[str, RealtimeQuote]:
    """解析腾讯行情响应.

    腾讯返回形如 ``v_sz159915="51~名称~159915~..."``；必须用左侧 ``v_sz159915``
    作为带交易所前缀的 key，不能只用字段里的六位数字代码。
    """
    result: Dict[str, RealtimeQuote] = {}
    for line in payload.strip().split(";"):
        if "=" not in line or '"' not in line:
            continue
        symbol = line[: line.index("=")].strip().removeprefix("v_")
        fields = line[line.index('"') + 1 : line.rindex('"')].split("~")
        if not symbol or len(fields) < 38:
            continue
        try:
            quote = RealtimeQuote(
                code=symbol,
                name=fields[1],
                price=float(fields[3] or 0),
                prev_close=float(fields[4] or 0),
                open_=float(fields[5] or 0),
                high=float(fields[33] or 0),
                low=float(fields[34] or 0),
                volume=float(fields[36] or 0),
                amount=float(fields[37] or 0),
                change_pct=float(fields[32] or 0),
                time_text=fields[30],
            )
        except (ValueError, IndexError):
            continue
        result[symbol] = quote
    return result


def fetch_tencent_realtime(codes: List[str]) -> Dict[str, RealtimeQuote]:
    """批量拉取腾讯实时行情."""
    url = "https://qt.gtimg.cn/q=" + ",".join(codes)
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    resp.encoding = "gbk"
    return _parse_tencent_payload(resp.text)


@st.cache_data(ttl=120, show_spinner=False)
def fetch_etf_daily_cached(symbol: str = ETF_CODE) -> pd.DataFrame:
    df = _fetch_etf_daily_raw(symbol)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


@st.cache_data(ttl=120, show_spinner=False)
def fetch_index_daily_cached(symbol: str = INDEX_CODE) -> pd.DataFrame:
    df = _fetch_index_daily_raw(symbol)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


@st.cache_data(ttl=60, show_spinner=False)
def fetch_news_cached() -> pd.DataFrame:
    return ak.stock_info_global_sina()


def merge_realtime_bar(hist: pd.DataFrame, quote: Optional[RealtimeQuote]) -> Tuple[pd.DataFrame, bool]:
    """把盘中实时行情合成到历史日线上, 返回 (df, merged).

    - 若实时日期晚于日线最后日期: 新增一行
    - 若相同: 替换最后一行 (盘中新浪日线有时已带当日部分数据)
    """
    if quote is None or quote.price <= 0:
        return hist.copy(), False
    today = pd.Timestamp(pd.Timestamp.now(SH_TZ).date())
    out = hist.copy()
    bar = {
        "date": today,
        "open": quote.open_ or quote.prev_close,
        "high": max(quote.high, quote.price),
        "low": min(quote.low or quote.open_ or quote.prev_close, quote.price),
        "close": quote.price,
        "volume": quote.volume * 100.0,  # 腾讯手 -> 股, 与新浪股口径一致
        "amount": quote.amount,
    }
    if len(out) and out["date"].iloc[-1] == today:
        for k, v in bar.items():
            out.loc[out.index[-1], k] = v
        return out, True
    if len(out) and today <= out["date"].iloc[-1]:
        return out, False
    return pd.concat([out, pd.DataFrame([bar])], ignore_index=True), True


# ---------------------------------------------------------------------------
# 技术指标与因子计算 (纯函数)
# ---------------------------------------------------------------------------


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    out = out.where(~((loss == 0) & (gain > 0)), 100.0)  # 无亏损 -> RSI=100
    return out


def _clip(v: float, lo: float = -100.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def compute_ma_alignment_score(close: pd.Series) -> Tuple[float, str]:
    """均线排列与偏离: MA5/MA10/MA20 全多头 +40, 全空头 -40; 收盘相对MA20每偏离1%记8分(±30封顶)."""
    ma5, ma10, ma20 = close.rolling(5).mean(), close.rolling(10).mean(), close.rolling(20).mean()
    c, m5, m10, m20 = close.iloc[-1], ma5.iloc[-1], ma10.iloc[-1], ma20.iloc[-1]
    if any(pd.isna(x) for x in (m5, m10, m20)):
        return 0.0, "均线数据不足"
    bull = m5 > m10 > m20
    bear = m5 < m10 < m20
    score = 0.0
    if bull:
        score += 40
    elif bear:
        score -= 40
    else:
        score += (13 if m5 > m10 else -13) + (13 if m10 > m20 else -13)
    dev_pct = (c / m20 - 1) * 100
    score += _clip(dev_pct * 8, -30, 30)
    desc = f"MA5={m5:.3f} MA10={m10:.3f} MA20={m20:.3f} 收盘偏离MA20 {dev_pct:+.2f}%"
    return _clip(score), desc


def compute_macd_score(close: pd.Series) -> Tuple[float, str]:
    dif = ema(close, 12) - ema(close, 26)
    dea = ema(dif, 9)
    hist = dif - dea
    h_now, h_prev = hist.iloc[-1], hist.iloc[-2]
    expanding = abs(h_now) >= abs(h_prev)
    if h_now > 0:
        score = 30 if expanding else 15
    elif h_now < 0:
        score = -30 if expanding else -15
    else:
        score = 0
    color_txt = "红" if h_now > 0 else ("绿" if h_now < 0 else "平")
    expand_txt = "放大" if expanding else "收敛"
    desc = f"DIF={dif.iloc[-1]:.3f} DEA={dea.iloc[-1]:.3f} 柱{expand_txt}({color_txt})"
    return _clip(score), desc


def compute_trend_factor(daily: pd.DataFrame) -> Tuple[float, List[str]]:
    close = daily["close"].astype(float)
    s1, d1 = compute_ma_alignment_score(close)
    s2, d2 = compute_macd_score(close)
    return _clip(0.6 * s1 + 0.4 * s2), [f"均线排列得分 {s1:+.0f}: {d1}", f"MACD 得分 {s2:+.0f}: {d2}"]


def compute_momentum_factor(daily: pd.DataFrame) -> Tuple[float, List[str]]:
    close = daily["close"].astype(float)
    ret5 = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) >= 6 else 0.0
    ret20 = (close.iloc[-1] / close.iloc[-21] - 1) * 100 if len(close) >= 21 else 0.0
    rsi_val = rsi_wilder(close).iloc[-1]
    momentum_score = _clip(ret5 * 12, -40, 40)
    rsi_adj = 0.0
    if not pd.isna(rsi_val):
        if rsi_val < 30:
            rsi_adj = 25
        elif rsi_val > 75:
            rsi_adj = -25
        elif rsi_val < 45:
            rsi_adj = (rsi_val - 30) / 15 * 10
        elif rsi_val > 60:
            rsi_adj = -(rsi_val - 60) / 15 * 10
    details = [
        f"近5日收益 {ret5:+.2f}% 动能分 {momentum_score:+.0f}",
        f"近20日收益 {ret20:+.2f}%",
        f"RSI14={rsi_val:.1f} 修正分 {rsi_adj:+.0f}",
    ]
    return _clip(momentum_score + rsi_adj), details


def compute_volume_factor(daily: pd.DataFrame) -> Tuple[float, List[str]]:
    vol = daily["volume"].astype(float)
    close = daily["close"].astype(float)
    chg_today = close.iloc[-1] / close.iloc[-2] - 1 if len(close) >= 2 else 0.0
    base_vol = vol.iloc[-6:-1].mean() if len(vol) >= 6 else 0.0
    vol_ratio = vol.iloc[-1] / base_vol if base_vol > 0 else 1.0
    up = chg_today > 0
    heavy = vol_ratio >= 1.5
    light = vol_ratio <= 0.7
    if up:
        base = 40 if heavy else (15 if light else 25)
    elif chg_today < 0:
        base = -40 if heavy else (-15 if light else -25)
    else:
        base = 0.0
    diverge = 0.0
    if len(vol) >= 21:
        v_short, v_long = vol.iloc[-5:].mean(), vol.iloc[-20:].mean()
        new_high = close.iloc[-1] >= close.iloc[-10:].max() * 0.999
        if new_high and v_long > 0 and v_short < v_long * 0.8:
            diverge = -20
    ratio_txt = "放量" if heavy else ("缩量" if light else "常量")
    details = [
        f"今日量比 {vol_ratio:.2f} ({ratio_txt}) 涨跌幅 {chg_today*100:+.2f}%",
        f"方向配合分 {base:+.0f}",
    ]
    if diverge:
        details.append(f"⚠️ 价创新高但量能萎缩, 顶背离预警分 {diverge}")
    return _clip(base + diverge), details


def compute_breadth_factor(
    constituents: pd.DataFrame,
    quotes: Dict[str, RealtimeQuote],
) -> Tuple[float, List[str]]:
    """成分股宽度: 权重加权涨跌 + 家数比 + 宁王单独贡献."""
    rows = constituents.to_dict("records")
    total_w = sum(float(r.get("weight", 0)) for r in rows) or 1.0
    wchg = 0.0
    adv = 0
    catl_chg = 0.0
    contributions: List[Tuple[str, float]] = []
    for r in rows:
        q = quotes.get(str(r["code"]))
        w = float(r.get("weight", 0))
        chg = q.change_pct_safe if q else 0.0
        wchg += w * chg
        adv += 1 if chg > 0 else 0
        contributions.append((str(r["name"]), chg))
        if str(r["code"]) == "sz300750":
            catl_chg = chg
    wavg = wchg / total_w
    adv_ratio = adv / len(rows) if rows else 0.5
    part_wavg = _clip(wavg * 10, -60, 60)
    part_adv = _clip((adv_ratio - 0.5) * 100, -30, 30)
    part_catl = _clip(catl_chg * 4, -10, 10)
    weakest = min(contributions, key=lambda x: x[1])
    strongest = max(contributions, key=lambda x: x[1])
    details = [
        f"权重加权平均涨跌 {wavg:+.2f}% -> 分 {part_wavg:+.0f}",
        f"上涨家数 {adv}/{len(rows)} 家数比分 {part_adv:+.0f}",
        f"宁德时代 {catl_chg:+.2f}% 龙头分 {part_catl:+.0f}",
        f"最弱/最强: {weakest[0]}({weakest[1]:+.2f}%) / {strongest[0]}({strongest[1]:+.2f}%)",
    ]
    return _clip(part_wavg + part_adv + part_catl), details


def news_sentiment_score(texts: List[str]) -> Tuple[float, List[Tuple[str, str]]]:
    """关键词情绪打分, 返回 (score, [(text, tag)])."""
    good_hits = bad_hits = 0
    tagged: List[Tuple[str, str]] = []
    for t in texts:
        g = sum(1 for w in POSITIVE_WORDS if w in t)
        b = sum(1 for w in NEGATIVE_WORDS if w in t)
        good_hits += g
        bad_hits += b
        tag = "✅利好" if g > b else ("❌利空" if b > g else "⚪中性")
        tagged.append((t, tag))
    denom = max(1, good_hits + bad_hits)
    return _clip((good_hits - bad_hits) / denom * 100), tagged


# ---------------------------------------------------------------------------
# 合成与评级
# ---------------------------------------------------------------------------

RATING_TABLE = [
    (55.0, "强烈买入", "🚀🚀🚀", "#16a34a"),
    (25.0, "买入", "🚀", "#22c55e"),
    (10.0, "偏多", "📈", "#84cc16"),
    (-10.0, "中性观望", "⏸️", "#a3a3a3"),
    (-25.0, "偏空", "📉", "#fb923c"),
    (-55.0, "卖出", "⚠️", "#ef4444"),
    (-101.0, "强烈卖出", "🛑🛑🛑", "#dc2626"),
]


def map_rating(total: float) -> Tuple[str, str, str]:
    for threshold, label, emoji, color in RATING_TABLE:
        if total >= threshold:
            return label, emoji, color
    return "强烈卖出", "🛑🛑🛑", "#dc2626"


@dataclass
class SignalResult:
    total: float
    rating: str
    emoji: str
    color: str
    factor_scores: Dict[str, float]
    factor_details: Dict[str, List[str]]
    generated_at: str = field(default_factory=lambda: pd.Timestamp.now(SH_TZ).strftime("%Y-%m-%d %H:%M:%S"))


FACTOR_NAME_MAP = {"趋势": "trend", "动量": "momentum", "量能": "volume", "宽度": "breadth", "情绪": "news"}


def composite_signal(
    daily: pd.DataFrame,
    constituents: pd.DataFrame,
    quotes: Dict[str, RealtimeQuote],
    news_texts: List[str],
) -> SignalResult:
    t_score, t_detail = compute_trend_factor(daily)
    m_score, m_detail = compute_momentum_factor(daily)
    v_score, v_detail = compute_volume_factor(daily)
    b_score, b_detail = compute_breadth_factor(constituents, quotes)
    n_score, _n_tagged = news_sentiment_score(news_texts)
    scores = {"趋势": t_score, "动量": m_score, "量能": v_score, "宽度": b_score, "情绪": n_score}
    details = {"趋势": t_detail, "动量": m_detail, "量能": v_detail, "宽度": b_detail}
    total = sum(FACTOR_WEIGHTS[FACTOR_NAME_MAP[k]] * v for k, v in scores.items())
    rating, emoji, color = map_rating(total)
    return SignalResult(
        total=_clip(total),
        rating=rating,
        emoji=emoji,
        color=color,
        factor_scores=scores,
        factor_details=details,
    )


def is_market_open(now: Optional[dt.datetime] = None) -> bool:
    now = now or pd.Timestamp.now(SH_TZ).to_pydatetime()
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return (915 <= t <= 1135) or (1255 <= t <= 1505)


# ---------------------------------------------------------------------------
# Streamlit 页面
# ---------------------------------------------------------------------------


def render_etf_signal_page() -> None:
    st.title("🎯 创业板ETF 多因子买卖信号")
    st.caption("因子: 趋势25% + 动量20% + 量能20% + 成分股宽度25% + 消息情绪10% → 总分 [-100, +100]")
    live = is_market_open()

    col_status, col_refresh, col_spacer = st.columns([1, 1, 4])
    with col_status:
        bg = "#22c55e" if live else "#94a3b8"
        label = "🟢 盘中实时" if live else "⏸ 已收盘 · 展示最新快照"
        st.markdown(
            f'<span style="background:{bg};color:white;padding:2px 12px;'
            f'border-radius:99px;font-size:0.85em;">{label}</span>',
            unsafe_allow_html=True,
        )
    with col_refresh:
        auto = st.toggle("自动刷新(60s)", value=live)
    if auto:
        from streamlit_autorefresh import st_autorefresh

        st_autorefresh(interval=60_000, key="etf_signal_refresh")

    err_holder = st.empty()
    try:
        etf_hist = fetch_etf_daily_cached()
        codes_all = [ETF_CODE, INDEX_CODE] + [str(c["code"]) for c in DEFAULT_CONSTITUENTS]
        quotes = fetch_tencent_realtime(codes_all)
        etf_rt = quotes.get(ETF_CODE)
        daily, merged = merge_realtime_bar(etf_hist, etf_rt)
        news_df = fetch_news_cached()
    except Exception as exc:  # noqa: BLE001
        err_holder.error(f"数据获取失败: {type(exc).__name__}: {exc}")
        return

    with st.sidebar.expander("⚖️ 成分股权重校准 (内置快照 2026-08)", expanded=False):
        edited = st.data_editor(
            pd.DataFrame(DEFAULT_CONSTITUENTS),
            use_container_width=True,
            num_rows="fixed",
            column_config={
                "weight": st.column_config.NumberColumn("权重%", min_value=0.0, max_value=100.0, step=0.1),
                "code": None,
                "name": None,
            },
            key="constituent_weight_editor",
        )

    head_cols = st.columns([2, 1, 1, 1.4])
    if etf_rt:
        head_cols[0].metric(ETF_NAME, f"{etf_rt.price:.3f}", f"{etf_rt.change_pct_safe:+.2f}%")
        head_cols[1].metric("今开", f"{etf_rt.open_:.3f}" if etf_rt.open_ else "-")
        head_cols[2].metric("成交额", f"{etf_rt.amount:,.0f} 万")
        head_cols[3].metric("数据时间", etf_rt.time_text or "—")

    news_texts = news_df["内容"].astype(str).tolist() if news_df is not None and len(news_df) else []
    sig = composite_signal(daily, edited, quotes, news_texts)

    st.divider()
    gauge_col, radar_col, detail_col = st.columns([1.2, 1.4, 1.6])

    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    with gauge_col:
        fig_gauge = go.Figure(
            go.Indicator(
                mode="gauge+number",
                value=sig.total,
                gauge={
                    "axis": {"range": [-100, 100]},
                    "bar": {"color": sig.color},
                    "steps": [
                        {"range": [-100, -55], "color": "#fecaca"},
                        {"range": [-55, -25], "color": "#fed7aa"},
                        {"range": [-25, -10], "color": "#fef3c7"},
                        {"range": [-10, 10], "color": "#f1f5f9"},
                        {"range": [10, 25], "color": "#ecfccb"},
                        {"range": [25, 55], "color": "#bbf7d0"},
                        {"range": [55, 100], "color": "#86efac"},
                    ],
                },
            )
        )
        fig_gauge.update_layout(height=280, margin=dict(l=20, r=20, t=10, b=10))
        st.plotly_chart(fig_gauge, use_container_width=True)
        st.markdown(
            f"<div style='text-align:center;font-size:1.9em;color:{sig.color};font-weight:800;'>"
            f"{sig.emoji} {sig.rating}</div>",
            unsafe_allow_html=True,
        )
        st.caption(f"生成时间: {sig.generated_at} · {'已并入盘中实时K线' if merged else '基于最近收盘K线'}")

    with radar_col:
        labels = list(sig.factor_scores.keys())
        values = [sig.factor_scores[k] for k in labels]
        fig_radar = go.Figure(
            go.Scatterpolar(
                r=values + values[:1],
                theta=labels + labels[:1],
                fill="toself",
                line_color="#2563eb",
            )
        )
        fig_radar.update_layout(
            polar={"radialaxis": {"range": [-100, 100]}},
            height=320,
            margin=dict(l=40, r=40, t=10, b=10),
        )
        st.plotly_chart(fig_radar, use_container_width=True)

    with detail_col:
        st.subheader("因子明细")
        weight_labels = {"趋势": "趋势25%", "动量": "动量20%", "量能": "量能20%", "宽度": "宽度25%"}
        for fname in ["趋势", "动量", "量能", "宽度"]:
            sc = sig.factor_scores[fname]
            st.markdown(f"**{fname} `{sc:+.0f}`** × {weight_labels[fname]}")
            for line in sig.factor_details.get(fname, []):
                st.markdown(f"- {line}")
        nscore = sig.factor_scores["情绪"]
        st.markdown(f"**情绪 `{nscore:+.0f}`** × 情绪10% — 快讯净命中打分")

    st.divider()
    st.subheader("ETF 日 K + 均线 + 成交量")
    plot_df = daily.tail(120).copy().reset_index(drop=True)
    close_s = daily["close"].astype(float)
    fig_full = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.72, 0.28], vertical_spacing=0.03)
    fig_full.add_trace(
        go.Candlestick(
            x=plot_df["date"], open=plot_df["open"], high=plot_df["high"],
            low=plot_df["low"], close=plot_df["close"],
            increasing_line_color="#ef4444", decreasing_line_color="#22c55e",
            showlegend=False,
        ),
        row=1, col=1,
    )
    for n, color in [(5, "#f59e0b"), (10, "#8b5cf6"), (20, "#06b6d4")]:
        ma = close_s.rolling(n).mean().tail(120).reset_index(drop=True)
        fig_full.add_trace(go.Scatter(x=plot_df["date"], y=ma, mode="lines", name=f"MA{n}", line=dict(width=1.4, color=color)), row=1, col=1)
    fig_full.add_trace(
        go.Bar(
            x=plot_df["date"], y=plot_df["volume"],
            marker_color=["#ef4444" if c >= o else "#22c55e" for c, o in zip(plot_df["close"], plot_df["open"])],
            opacity=0.5, showlegend=False,
        ),
        row=2, col=1,
    )
    fig_full.update_layout(height=520, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h", yanchor="bottom", y=1.02))
    st.plotly_chart(fig_full, use_container_width=True)

    left_c, right_c = st.columns([1.4, 1])
    with left_c:
        st.subheader("前十大权重股实时")
        table_rows = []
        for r in edited.to_dict("records"):
            q = quotes.get(str(r["code"]))
            table_rows.append({
                "名称": r["name"],
                "代码": str(r["code"]).replace("sz", ""),
                "权重%": float(r["weight"]),
                "现价": q.price if q else None,
                "涨跌%": round(q.change_pct_safe, 2) if q else None,
                "贡献": round(float(r["weight"]) * (q.change_pct_safe if q else 0), 2),
            })
        contrib_sum = sum(t["贡献"] for t in table_rows)
        st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)
        st.caption(f"加权贡献合计: {contrib_sum:+.2f} (越大越支撑指数)")
    with right_c:
        st.subheader("最新快讯情绪标注")
        _n, n_tagged = news_sentiment_score(news_texts)
        for t, tag in n_tagged[:10]:
            txt = re.sub(r"\s+", " ", t)[:80]
            st.markdown(f"{tag} · {txt}")

    st.divider()
    st.caption("⚠️ 本面板为量化参考工具, 不构成投资建议。评级由规则化因子合成, 参数可在 stockview/etf_signal.py 中调整。")


if __name__ == "__main__":  # pragma: no cover
    render_etf_signal_page()

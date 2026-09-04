"""规则化选股策略引擎: 用户在 Web 上用"条件原语"组装买入/卖出规则, 存 SQLite.

语义:
- buy_rules  全部命中 -> 买入候选 (reasons 用 " 且 " 连接作为买入理由)
- sell_rules 任一命中 -> 卖出 (返回原因串)
- 规则 dict: {"type": "rsi_below", "n": 6, "threshold": 25} — 参数平铺, 便于前端渲染
- 所有判定只依赖"截至当日"的日K (bars 升序且含当日), 无未来函数; 停牌(量/收盘为0)不命中

原语清单即本文件的 BUY_RULE_TYPES / SELL_RULE_TYPES (Web 端据此渲染规则编辑器,
GET /api/picker/rule-types 直接透传).
"""
from __future__ import annotations

import datetime as dt

BUY_RULE_TYPES: dict[str, dict] = {
    "rsi_below":        {"label": "RSI 低于阈值", "params": {"n": 6, "threshold": 25.0},
                         "desc": "收盘 RSI(n) ≤ threshold (超卖)"},
    "vol_ratio_above":  {"label": "量比 ≥ 倍数", "params": {"days": 5, "ratio": 1.5},
                         "desc": "当日量 / 前 days 日均量 ≥ ratio (放量)"},
    "breakout_high":    {"label": "突破近N日最高收盘", "params": {"days": 20},
                         "desc": "当日收盘 > 前 days 日最高收盘 (不含当日)"},
    "ma_above":         {"label": "收盘 ≥ N日均线", "params": {"n": 20}},
    "ma_below":         {"label": "收盘 ≤ N日均线", "params": {"n": 20}},
    "pct_change_below": {"label": "当日涨幅 ≤ %", "params": {"pct": 7.0},
                         "desc": "防追高/防一字板"},
    "pct_change_above": {"label": "当日涨幅 ≥ %", "params": {"pct": -5.0}},
}

SELL_RULE_TYPES: dict[str, dict] = {
    "take_profit":   {"label": "止盈: 相对买价涨幅 ≥ %", "params": {"pct": 10.0}},
    "stop_loss":     {"label": "止损: 相对买价跌幅 ≤ %", "params": {"pct": -5.0}},
    "rsi_above":     {"label": "RSI 高于阈值", "params": {"n": 6, "threshold": 55.0},
                      "desc": "超卖修复兑现"},
    "breakdown_low": {"label": "跌破近N日最低收盘", "params": {"days": 5}},
    "ma_below":      {"label": "收盘跌破N日均线", "params": {"n": 20}},
    "hold_days":     {"label": "持仓超过N天(自然日)", "params": {"days": 5}},
}


# ---------------------------------------------------------------- 数据 ----
def clean_day_bars(bars: list[dict]) -> list[dict]:
    """丢弃非真实交易的日K bar: 量与额都为 0 (eltdx 盘前会补当日占位 bar,
    OHLC 恒等于昨收; 停牌日同样是空槽). 不丢弃会让全部标的被判停牌、指标被平价 bar 污染."""
    return [b for b in bars or []
            if float(b.get("volume") or 0) > 0 or float(b.get("amount") or 0) > 0]


# ---------------------------------------------------------------- 指标 ----
def _closes(bars: list[dict]) -> list[float]:
    return [float(b.get("close") or 0) for b in bars]


def _rsi(closes: list[float], n: int) -> float:
    if len(closes) < n + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        chg = closes[i] - closes[i - 1]
        gains.append(max(chg, 0.0))
        losses.append(max(-chg, 0.0))
    avg_gain = sum(gains[-n:]) / n
    avg_loss = sum(losses[-n:]) / n
    if avg_loss <= 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _ma(values: list[float], n: int) -> float:
    seg = [v for v in values[-n:] if v > 0]
    return sum(seg) / len(seg) if seg else 0.0


def _pct(a: float, b: float) -> float:
    return (a - b) / b * 100.0 if b > 0 else 0.0


def _days_between(d1: str, d2: str) -> int:
    try:
        return (dt.date.fromisoformat(d2) - dt.date.fromisoformat(d1)).days
    except ValueError:
        return 0


# ---------------------------------------------------------------- 买入 ----
def eval_buy(bars: list[dict], rules: list[dict]) -> str:
    """全部命中返回 " 且 " 连接的原因串; 否则 ''. bars 升序含当日."""
    if len(bars) < 2:
        return ""
    last = bars[-1]
    px = float(last.get("close") or 0)
    vol = float(last.get("volume") or 0)
    if px <= 0 or vol <= 0:                      # 停牌/无成交
        return ""
    reasons: list[str] = []
    for r in rules or []:
        ok, why = _eval_buy_one(str(r.get("type") or ""), r, bars, px)
        if not ok:
            return ""
        if why:
            reasons.append(why)
    return " 且 ".join(reasons)


def _eval_buy_one(t: str, r: dict, bars: list[dict], px: float) -> tuple[bool, str]:
    closes = _closes(bars)
    if t == "rsi_below":
        n = max(int(r.get("n") or 6), 2)
        th = float(r.get("threshold") or 25)
        v = _rsi(closes, n)
        return (v <= th, f"RSI{n}={v:.1f}≤{th:g}")
    if t == "vol_ratio_above":
        days = max(int(r.get("days") or 5), 1)
        if len(bars) < days + 1:
            return False, ""
        prev = [float(b.get("volume") or 0) for b in bars[-days - 1:-1]]
        avg = sum(prev) / days
        if avg <= 0:
            return False, ""
        vr = float(bars[-1].get("volume") or 0) / avg
        return (vr >= float(r.get("ratio") or 1.5), f"量比{vr:.2f}")
    if t == "breakout_high":
        days = max(int(r.get("days") or 20), 1)
        if len(closes) < days + 1:
            return False, ""
        prev_high = max(closes[-days - 1:-1])
        return (prev_high > 0 and px > prev_high, f"破{days}日高{prev_high:.2f}")
    if t == "ma_above":
        n = max(int(r.get("n") or 20), 2)
        m = _ma(closes, n)
        return (m > 0 and px >= m, f"≥MA{n}{m:.2f}")
    if t == "ma_below":
        n = max(int(r.get("n") or 20), 2)
        m = _ma(closes, n)
        return (m > 0 and px <= m, f"≤MA{n}{m:.2f}")
    if t == "pct_change_below":
        th = float(r.get("pct") or 7)
        pc = _pct(px, closes[-2]) if len(closes) >= 2 else 0.0
        return (pc <= th, f"涨幅{pc:.2f}%≤{th:g}%")
    if t == "pct_change_above":
        th = float(r.get("pct") or -5)
        pc = _pct(px, closes[-2]) if len(closes) >= 2 else 0.0
        return (pc >= th, f"涨幅{pc:.2f}%≥{th:g}%")
    return False, ""                             # 未知类型不命中 (校验层已挡)


# ---------------------------------------------------------------- 卖出 ----
def eval_sell(pos: dict, bars: list[dict], rules: list[dict],
              today: str | None = None, px: float | None = None) -> str:
    """任一命中返回原因串. pos 需 buy_price / buy_ts(ISO).

    today: 判定基准日 (回测传当日 bar 日期; 实盘默认今天), hold_days 用.
    px:    现价 (实盘传实时 last; 缺省用 bars 最后一根收盘).
    """
    if not bars:
        return ""
    price = float(px or 0) or float(bars[-1].get("close") or 0)
    if price <= 0:
        return ""
    buy_px = float(pos.get("buy_price") or 0)
    today = today or dt.date.today().isoformat()
    closes = _closes(bars)
    for r in rules or []:
        t = str(r.get("type") or "")
        if t == "take_profit":
            th = float(r.get("pct") or 10)
            pnl = _pct(price, buy_px)
            if buy_px > 0 and pnl >= th:
                return f"止盈 {pnl:.2f}%≥{th:g}%"
        elif t == "stop_loss":
            th = float(r.get("pct") or -5)
            pnl = _pct(price, buy_px)
            if buy_px > 0 and pnl <= th:
                return f"止损 {pnl:.2f}%≤{th:g}%"
        elif t == "rsi_above":
            n = max(int(r.get("n") or 6), 2)
            th = float(r.get("threshold") or 55)
            v = _rsi(closes, n)
            if v >= th:
                return f"RSI{n}={v:.1f}≥{th:g}"
        elif t == "breakdown_low":
            days = max(int(r.get("days") or 5), 1)
            if len(closes) >= days + 1:
                lows = closes[-days - 1:-1]
                prev_low = min((v for v in lows if v > 0), default=0)
                if prev_low > 0 and price < prev_low:
                    return f"破{days}日低{prev_low:.2f}"
        elif t == "ma_below":
            n = max(int(r.get("n") or 20), 2)
            m = _ma(closes, n)
            if m > 0 and price < m:
                return f"跌破MA{n}{m:.2f}"
        elif t == "hold_days":
            days = max(int(r.get("days") or 5), 1)
            held = _days_between(str(pos.get("buy_ts") or "")[:10], today)
            if str(pos.get("buy_ts") or "")[:10] and held >= days:
                return f"持仓{held}天≥{days}天"
    return ""


# ---------------------------------------------------------------- 校验 ----
def validate_rules(kind: str, rules) -> tuple[list[dict], str]:
    """校验规则列表: 返回 (清洗后的规则, 错误信息). kind: buy|sell.

    只保留已知 type + 该 type 声明的数值参数; 未知类型/空规则列表报错.
    """
    table = BUY_RULE_TYPES if kind == "buy" else SELL_RULE_TYPES
    clean: list[dict] = []
    if not isinstance(rules, list):
        return [], f"{kind}_rules 必须是列表"
    for i, r in enumerate(rules):
        if not isinstance(r, dict) or not str(r.get("type") or "").strip():
            return [], f"{kind}_rules[{i}] 缺少 type"
        t = str(r["type"]).strip()
        if t not in table:
            return [], f"{kind}_rules[{i}] 未知条件类型: {t}"
        item = {"type": t}
        for pname in table[t].get("params", {}):
            try:
                item[pname] = float(r.get(pname) if r.get(pname) is not None
                                    else table[t]["params"][pname])
            except (TypeError, ValueError):
                return [], f"{kind}_rules[{i}] ({t}) 参数 {pname} 必须是数字"
        clean.append(item)
    if not clean:
        return [], f"{kind}_rules 不能为空 (至少一个条件)"
    return clean, ""

"""选股策略历史回测: 规则策略在历史日K上的模拟 (纯计算, 不下单).

口径 (与实盘引擎一致, 无未来函数):
- 数据: 每只股票的日K (升序), 判定只用截至当日的 bars 切片
- 信号: 当日收盘出信号, 按当日收盘价成交 (EOD 回测口径)
- 买入: 买入前先卖出 (先卖后买); 槽位均分预算 (等权), 整手
- 卖出: T+1 当日买不卖 (t1_protect); 佣金双边 0.0001 (与 runtime 口径一致)
- 净值: 每日 cash + 持仓按最近收盘市值; 未平仓浮动盈亏计入净值不计入胜率

run_backtest() 拉真实数据; _run_core() 接受现成 bars (单测用合成数据).
"""
from __future__ import annotations

from strategy import fdata_client
from trading import picker_rules

COMMISSION = 0.0001
LOT = 100


def run_backtest(buy_rules: list[dict], sell_rules: list[dict], universe: list[str],
                 *, days: int = 250, cash: float = 100_000.0,
                 max_positions: int = 3, t1_protect: bool = True,
                 kline_extra: int = 120) -> dict:
    """拉取股票池日K并回测. 数据不足 (fdata 未运行/代码无效) 抛 ValueError."""
    bars_by_code: dict[str, list[dict]] = {}
    for code in universe:
        try:
            bars = picker_rules.clean_day_bars(
                fdata_client.kline(code, "day", "stock", None,
                                   int(days) + int(kline_extra)))
        except Exception:  # noqa: BLE001 单只失败跳过
            bars = []
        if len(bars) >= 30:
            bars_by_code[code] = bars
    if not bars_by_code:
        raise ValueError("股票池无可用日K数据 (fdata serve 未运行或代码无效?)")
    return _run_core(buy_rules, sell_rules, bars_by_code, days=days, cash=cash,
                     max_positions=max_positions, t1_protect=t1_protect)


def _run_core(buy_rules: list[dict], sell_rules: list[dict],
              bars_by_code: dict[str, list[dict]], *, days: int = 250,
              cash: float = 100_000.0, max_positions: int = 3,
              t1_protect: bool = True) -> dict:
    """核心回测循环 (数据无关, 可用合成 bars 单测)."""
    # 交易日历: 各代码日期并集升序, 取末尾 days 个
    dates = sorted({str(b.get("date") or "")[:10]
                    for bars in bars_by_code.values() for b in bars})
    dates = [d for d in dates if d][-int(days):] if days > 0 else dates
    bar_index = {code: {str(b.get("date") or "")[:10]: i
                        for i, b in enumerate(bars)}
                 for code, bars in bars_by_code.items()}

    cash_f = float(cash)
    positions: dict[str, dict] = {}          # code -> {qty, buy_price, buy_date, reason}
    trades: list[dict] = []
    equity: list[dict] = []
    universe = list(bars_by_code)            # 用实际有数据的代码

    for d in dates:
        # ---- 先卖 (T+1: 当日买入不卖) ----
        for code in list(positions):
            pos = positions[code]
            if t1_protect and pos["buy_date"] == d:
                continue
            i = bar_index.get(code, {}).get(d)
            if i is None:
                continue
            bars = bars_by_code[code][:i + 1]
            reason = picker_rules.eval_sell(pos, bars, sell_rules, today=d)
            if not reason:
                continue
            px = float(bars[-1].get("close") or 0)
            cash_f += pos["qty"] * px * (1 - COMMISSION)
            trades.append({
                "code": code, "qty": pos["qty"],
                "buy_date": pos["buy_date"], "buy_price": round(pos["buy_price"], 3),
                "sell_date": d, "sell_price": round(px, 3),
                "pnl": round((px - pos["buy_price"]) * pos["qty"], 2),
                "pnl_pct": round(picker_rules._pct(px, pos["buy_price"]), 2),
                "sell_reason": reason,
            })
            del positions[code]

        # ---- 后买 (槽位均分预算, 整手) ----
        slots = (max_positions - len(positions)) if max_positions > 0 else 1
        for code in universe:
            if slots <= 0:
                break
            if code in positions:
                continue
            i = bar_index.get(code, {}).get(d)
            if i is None:
                continue
            bars = bars_by_code[code][:i + 1]
            reason = picker_rules.eval_buy(bars, buy_rules)
            if not reason:
                continue
            px = float(bars[-1].get("close") or 0)
            if px <= 0:
                continue
            budget = cash_f / max(slots, 1)
            qty = int(budget // px // LOT * LOT)
            if qty <= 0:
                continue
            cash_f -= qty * px * (1 + COMMISSION)
            positions[code] = {"qty": qty, "buy_price": px, "buy_date": d,
                               "buy_ts": d, "reason": reason}   # buy_ts 供 hold_days 判定
            slots -= 1

        # ---- 净值 ----
        mv = cash_f
        for code, pos in positions.items():
            i = bar_index.get(code, {}).get(d)
            px = float(bars_by_code[code][i].get("close") or 0) if i is not None \
                else pos["buy_price"]
            mv += pos["qty"] * (px or pos["buy_price"])
        equity.append({"date": d, "value": round(mv, 2)})

    # ---- 指标 ----
    values = [e["value"] for e in equity]
    peak = values[0] if values else cash
    mdd = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak * 100)
    final = values[-1] if values else cash
    closed = [t for t in trades]
    wins = [t for t in closed if t["pnl"] > 0]
    open_pos = [{
        "code": code, "qty": p["qty"], "buy_date": p["buy_date"],
        "buy_price": round(p["buy_price"], 3), "reason": p["reason"],
    } for code, p in positions.items()]
    return {
        "days": len(dates),
        "metrics": {
            "initial_cash": round(float(cash), 2),
            "final_value": round(final, 2),
            "total_return_pct": round((final / cash - 1) * 100, 2) if cash > 0 else 0,
            "max_drawdown_pct": round(mdd, 2),
            "trades": len(closed),
            "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
            "avg_pnl_pct": (round(sum(t["pnl_pct"] for t in closed) / len(closed), 2)
                            if closed else None),
        },
        "equity": equity,
        "trades": trades,
        "open_positions": open_pos,
    }

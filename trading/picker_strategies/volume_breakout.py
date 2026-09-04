"""放量突破选股插件: 收盘突破近 N 日高点 + 显著放量 买入, 跌破/止盈止损 卖出.

买入条件 (全部满足):
  - 收盘价 > 前 breakout_days 日最高收盘 (不含当日, 突破确认)
  - 当日量 >= vol_ratio × 前 vol_days 日均量 (放量)
  - 当日涨幅 <= max_gain_pct (防追一字/过热)

卖出条件 (命中其一):
  - 收盘 < 前 breakdown_days 日最低收盘 (跌破, 趋势破坏)
  - 相对买价涨幅 >= take_profit_pct (止盈)
  - 相对买价跌幅 <= stop_loss_pct (止损)
"""
from __future__ import annotations

import asyncio

from strategy import fdata_client
from trading.picker_strategies.base import PickCandidate, PickStrategy


class VolumeBreakoutPicker(PickStrategy):
    ID = "volume_breakout"
    TITLE = "放量突破"
    DESC = "收盘突破近N日高点且放量买入; 跌破近M日低点/止盈/止损卖出"
    PARAMS = {
        "breakout_days":   {"default": 20, "desc": "突破基准: 近N日最高收盘"},
        "vol_ratio":       {"default": 1.8, "desc": "当日量 / 前N日均量 下限"},
        "vol_days":        {"default": 5, "desc": "量比基准均量天数"},
        "max_gain_pct":    {"default": 7.0, "desc": "当日涨幅上限 % (防追高)"},
        "breakdown_days":  {"default": 5, "desc": "跌破基准: 近M日最低收盘"},
        "take_profit_pct": {"default": 15.0, "desc": "相对买价止盈 %"},
        "stop_loss_pct":   {"default": -4.0, "desc": "相对买价止损 %"},
        "kline_limit":     {"default": 60, "desc": "拉取日K根数"},
    }

    async def select(self, universe: list[str], params: dict) -> list[PickCandidate]:
        limit = int(self.p(params, "kline_limit", 60))
        n_break = int(self.p(params, "breakout_days", 20))
        vol_ratio = float(self.p(params, "vol_ratio", 1.8))
        vol_days = max(int(self.p(params, "vol_days", 5)), 1)
        max_gain = float(self.p(params, "max_gain_pct", 7.0))
        out: list[PickCandidate] = []
        for code in universe:                       # 串行拉取: serve 长连接复用
            try:
                bars = await asyncio.to_thread(
                    fdata_client.kline, code, "day", "stock", None, limit)
            except Exception:  # noqa: BLE001 单只失败跳过
                continue
            if len(bars) < max(n_break, vol_days) + 2:
                continue
            closes = self.closes(bars)
            last = bars[-1]
            px = float(last.get("close") or 0)
            vol = float(last.get("volume") or 0)
            if px <= 0 or vol <= 0:                 # 停牌/无成交
                continue
            prev_high = max(closes[-n_break - 1:-1])
            prev_vol = [float(b.get("volume") or 0) for b in bars[-vol_days - 1:-1]]
            avg_vol = sum(prev_vol) / vol_days
            if avg_vol <= 0 or prev_high <= 0:
                continue
            vr = vol / avg_vol
            pre_close = float(bars[-2].get("close") or 0)
            day_gain = self.pct(px, pre_close)
            if px > prev_high and vr >= vol_ratio and day_gain <= max_gain:
                # 名称不在日K数据里, 由引擎用实时快照补全, 这里只给空串占位
                out.append(PickCandidate(
                    code=code, name=str(last.get("name") or ""), price=px,
                    reason=(f"突破{prev_high:.2f} 放量{vr:.2f}倍 "
                            f"涨幅{day_gain:.2f}%≤{max_gain:g}%"),
                    score=vr))                      # 放量越显著越优先
        out.sort(key=lambda c: c.score, reverse=True)
        return out

    def sell_reason(self, pos: dict, quote: dict | None, bars: list[dict],
                    params: dict) -> str:
        last = float((quote or {}).get("last") or 0)
        if last <= 0 and bars:
            last = float(bars[-1].get("close") or 0)
        if last <= 0:
            return ""
        buy_px = float(pos.get("buy_price") or 0)
        pnl = self.pct(last, buy_px)
        tp = float(self.p(params, "take_profit_pct", 15.0))
        sl = float(self.p(params, "stop_loss_pct", -4.0))
        if buy_px > 0 and pnl <= sl:
            return f"止损 {pnl:.2f}% ≤ {sl:g}%"
        if buy_px > 0 and pnl >= tp:
            return f"止盈 {pnl:.2f}% ≥ {tp:g}%"
        n_low = int(self.p(params, "breakdown_days", 5))
        if len(bars) >= n_low + 1:
            lows = [float(b.get("close") or 0) for b in bars[-n_low - 1:-1]]
            prev_low = min(v for v in lows if v > 0) if any(v > 0 for v in lows) else 0
            if prev_low > 0 and last < prev_low:
                return f"跌破{n_low}日低点 {prev_low:.2f}"
        return ""

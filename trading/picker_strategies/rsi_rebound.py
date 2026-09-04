"""超跌反弹选股插件: 日线 RSI 深度超卖 + 量能确认 买入, RSI 修复/止盈止损 卖出.

买入条件 (全部满足):
  - RSI6 <= rsi_buy (默认 25, 深度超卖)
  - 当日量 >= vol_ratio × 前 vol_days 日均量 (下跌动能衰减中的承接)
  - 收盘 > 0 且当日未停牌

卖出条件 (命中其一):
  - RSI6 >= rsi_sell (超卖修复, 反弹兑现)
  - 相对买价涨幅 >= take_profit_pct (止盈)
  - 相对买价跌幅 <= stop_loss_pct (止损)
"""
from __future__ import annotations

import asyncio

from strategy import fdata_client
from trading.picker_strategies.base import PickCandidate, PickStrategy


class RsiReboundPicker(PickStrategy):
    ID = "rsi_rebound"
    TITLE = "超跌反弹 (RSI 超卖)"
    DESC = "日线 RSI6 深度超卖 + 放量承接买入; RSI 修复/止盈/止损卖出"
    PARAMS = {
        "rsi_buy":         {"default": 25.0, "desc": "RSI6 低于该值触发买入"},
        "vol_ratio":       {"default": 1.5, "desc": "当日量 / 前N日均量 下限"},
        "vol_days":        {"default": 5, "desc": "量比基准均量天数"},
        "rsi_sell":        {"default": 55.0, "desc": "RSI6 高于该值触发卖出"},
        "take_profit_pct": {"default": 10.0, "desc": "相对买价止盈 %"},
        "stop_loss_pct":   {"default": -5.0, "desc": "相对买价止损 %"},
        "kline_limit":     {"default": 60, "desc": "拉取日K根数"},
    }

    async def select(self, universe: list[str], params: dict) -> list[PickCandidate]:
        limit = int(self.p(params, "kline_limit", 60))
        rsi_buy = float(self.p(params, "rsi_buy", 25.0))
        vol_ratio = float(self.p(params, "vol_ratio", 1.5))
        vol_days = max(int(self.p(params, "vol_days", 5)), 1)
        out: list[PickCandidate] = []
        for code in universe:                       # 串行拉取: serve 长连接复用, 不轰数据源
            try:
                bars = await asyncio.to_thread(
                    fdata_client.kline, code, "day", "stock", None, limit)
            except Exception:  # noqa: BLE001 单只失败跳过, 不影响整轮扫描
                continue
            if len(bars) < vol_days + 2:
                continue
            closes = self.closes(bars)
            last = bars[-1]
            px = float(last.get("close") or 0)
            vol = float(last.get("volume") or 0)
            if px <= 0 or vol <= 0:                 # 停牌/无成交
                continue
            prev_vol = [float(b.get("volume") or 0) for b in bars[-vol_days - 1:-1]]
            avg_vol = sum(prev_vol) / vol_days if vol_days else 0
            if avg_vol <= 0:
                continue
            vr = vol / avg_vol
            r6 = self.rsi(closes, 6)
            if r6 <= rsi_buy and vr >= vol_ratio:
                # 名称不在日K数据里, 由引擎用实时快照补全, 这里只给空串占位
                out.append(PickCandidate(
                    code=code, name=str(last.get("name") or ""), price=px,
                    reason=f"RSI6={r6:.1f}≤{rsi_buy:g} 且量比{vr:.2f}≥{vol_ratio:g}",
                    score=-r6))                     # RSI 越低越优先
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
        tp = float(self.p(params, "take_profit_pct", 10.0))
        sl = float(self.p(params, "stop_loss_pct", -5.0))
        if buy_px > 0 and pnl <= sl:
            return f"止损 {pnl:.2f}% ≤ {sl:g}%"
        if buy_px > 0 and pnl >= tp:
            return f"止盈 {pnl:.2f}% ≥ {tp:g}%"
        rsi_sell = float(self.p(params, "rsi_sell", 55.0))
        r6 = self.rsi(self.closes(bars), 6)
        if r6 >= rsi_sell:
            return f"RSI6={r6:.1f}≥{rsi_sell:g} 超卖修复"
        return ""

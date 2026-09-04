"""选股插件基类: 选股自动交易系统的"策略"抽象.

与 strategy/ 框架的单标的 signal() (目标仓位 0/1) 不同, 选股系统是
"多标的批量选股 -> 买入组" 语义, 插件只需回答两个问题:

- select():      扫描股票池, 返回满足 买入条件 的候选股票
                 (引擎负责 去重/限仓/下单/成交后入库到该策略 ID 的买入组)
- sell_reason(): 对买入组里的某笔持仓判定 卖出条件, 命中返回原因串
                 (引擎据此发出卖出指令), 空串 = 继续持有

插件本身不做任何下单/记账 (Write Once: 同一插件可同时挂在多个策略组上,
参数按组覆盖; 组间天然隔离).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class PickCandidate:
    """select() 返回的候选股票: 引擎按 score 倒序依次买入 (受 max_positions 限)."""
    code: str
    name: str = ""
    price: float = 0.0        # 信号价 (下单参考价)
    reason: str = ""          # 命中买入条件的可读说明 (入库 buy_reason)
    score: float = 0.0        # 排序分, 大者优先


class PickStrategy(ABC):
    ID: str = ""               # 插件唯一 ID (策略组配置里的 picker 字段)
    TITLE: str = ""
    DESC: str = ""
    # 参数声明: name -> {"default": x, "desc": str}; 策略组 params 可按组覆盖
    PARAMS: dict[str, dict] = {}

    @abstractmethod
    async def select(self, universe: list[str], params: dict) -> list[PickCandidate]:
        """买入选股: 扫描 universe, 返回满足买入条件的候选 (score 倒序)."""

    @abstractmethod
    def sell_reason(self, pos: dict, quote: dict | None, bars: list[dict],
                    params: dict) -> str:
        """卖出条件判定. 命中返回原因串 (作为卖出指令留痕), 否则 ''.

        pos:   picker_positions 行 (code/qty/buy_price/buy_ts/...)
        quote: fdata quote 平铺 dict (last 可能 None), 可为 None
        bars:  日 K 列表 (升序, [{date,open,high,low,close,volume,...}])
        """

    # ------------------------------------------------------ 共用指标工具 ----
    @staticmethod
    def closes(bars: list[dict]) -> list[float]:
        return [float(b.get("close") or 0) for b in bars]

    @staticmethod
    def ma(values: list[float], n: int) -> float:
        """最近 n 个值的均值 (不足 n 返回 0)."""
        seg = [v for v in values[-n:] if v > 0]
        return sum(seg) / len(seg) if seg else 0.0

    @staticmethod
    def rsi(closes: list[float], n: int = 6) -> float:
        """Wilder RSI (升序 closes, 不足 n+1 根返回 50 中性值)."""
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
        rs = avg_gain / avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    @staticmethod
    def pct(a: float, b: float) -> float:
        """a 相对 b 的涨跌 % (b<=0 返回 0)."""
        return (a - b) / b * 100.0 if b > 0 else 0.0

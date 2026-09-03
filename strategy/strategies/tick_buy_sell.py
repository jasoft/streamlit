"""Tick 交替买卖测试策略.

目标: 验证 策略 on_bar -> 下单 -> 记账 -> 前端展示 全链路.

机制 (状态机, on_bar 自管计数, 无 state.json 副作用):
  每 switch_every 次 on_bar 切换一次目标仓位:
    bar 0..N-1   -> target=0 (空仓)
    bar N..2N-1  -> target=1 (持仓)
    bar 2N..3N-1 -> target=0
    ...
  signal 向量化版本基于 bar index 交替, 与 on_bar 语义对齐, 适合 vectorbt 回测.

对比旧实现: 不再依赖 state.json _call_count 副作用 (vectorbt 参数优化会乱跳),
改用实例级 _count (重启归零). 适合 paper 测试, 实盘长跑需另加 state 落盘.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategy.base import Strategy, INT


class TickBuySell(Strategy):
    NAME = "tick_buy_sell"
    TITLE = "Tick 交替买卖 (链路测试)"
    PARAMS = {"switch_every": {"type": INT, "default": 2, "min": 1, "max": 10}}
    SYMBOLS = ["sz159915"]
    TIMEFRAME = "5m"              # 5 分钟 K 线, 配合 TRIGGER_ON_CLOSE 每 5 分钟一根 bar
    TRIGGER_ON_CLOSE = True

    def __init__(self):
        self._count = 0          # on_bar 调用计数 (实例级, 重启归零)

    def signal(self, df: pd.DataFrame, params: dict) -> pd.Series:
        """向量化交替序列: signal[i] = (i // switch_every) % 2. 无副作用, vectorbt 友好.

        与 on_bar 状态机语义对齐: on_bar 第 i 次调用的 target == signal[i].
        """
        se = int(params.get("switch_every", 2))
        idx = np.arange(len(df))
        return pd.Series((idx // se) % 2, index=df.index)

    async def on_bar(self, bar, ctx) -> None:
        """状态机: 每 switch_every 次 on_bar 切换 target, 与 ctx.target 对比下单.

        覆盖 base.py 默认 on_bar (默认调 signal), 自管 _count 实现状态机交替.
        """
        se = int(ctx.params.get("switch_every", 2))
        stage = self._count // se
        target = stage % 2
        self._count += 1
        if target == 1 and ctx.target == 0:
            qty = ctx.qty_for()
            if qty > 0:
                await ctx.submit_order(side="buy", qty=qty, price=float(bar["close"]))
        elif target == 0 and ctx.target == 1 and ctx.position > 0:
            await ctx.submit_order(side="sell", qty=ctx.position, price=float(bar["close"]))

"""Strategy 基类: 策略只描述信号逻辑, 与运行时/回测引擎/实盘执行完全解耦.

双接口设计 (Write Once, Run Anywhere):
- signal(df, params) -> 0/1 目标仓位序列 (向量化, 给 vectorbt 回测/参数优化)
- on_bar(bar, ctx) -> None (事件驱动, 给实盘运行时; 默认实现桥接到 signal)

策略作者通常只实现 signal. 复杂策略(状态机/追踪止损/加仓)可覆盖 on_bar 自行实现,
signal 仍留给快速研究/参数优化. on_init/on_stop 用于初始化与收尾.

成交口径: 收盘出信号 -> 次日开盘成交 (无未来函数), 回测与实盘统一.

向后兼容: target_position 是 signal 的旧名, 保留为别名转发, 老策略不用改.
"""
from __future__ import annotations

import pandas as pd

INT = "int"
FLOAT = "float"


class Strategy:
    NAME: str = ""
    TITLE: str = ""
    PARAMS: dict = {}
    SYMBOLS: list = []
    TIMEFRAME: str = "day"            # "day" / "5m" / "15m" / "30m" ...
    TRIGGER_ON_CLOSE: bool = True     # True=K线收盘触发 on_bar (无未来函数); False=每tick触发
    LOOKBACK: int = 3000              # on_bar 默认实现取历史的 bar 数

    # ---------------- 核心信号接口 (向量化, 给 vectorbt) ----------------
    def signal(self, df: pd.DataFrame, params: dict) -> pd.Series:
        """返回 0/1 目标仓位序列, index 与 df 对齐, 按当日收盘决策.

        子类必须实现 signal 或旧名 target_position. 二者实现其一即可,
        基类提供双向别名转发 (检测子类覆盖避免递归).
        """
        if type(self).target_position is not Strategy.target_position:
            return self.target_position(df, params)
        raise NotImplementedError("子类必须实现 signal() 或 target_position()")

    # 向后兼容别名: 旧策略实现 target_position, 调 signal 时转发过去
    def target_position(self, df: pd.DataFrame, params: dict) -> pd.Series:
        """signal 的旧名, 保留兼容. 新策略请实现 signal."""
        if type(self).signal is not Strategy.signal:
            return self.signal(df, params)
        raise NotImplementedError("子类必须实现 signal() 或 target_position()")

    # ---------------- 事件驱动接口 (实盘运行时) ----------------
    def on_init(self, ctx: "Context") -> None:
        """策略启动时调用一次. 默认空. ctx 见 runtime.ctx.Context."""

    async def on_bar(self, bar, ctx: "Context") -> None:
        """每根K线触发一次. 默认实现: 取历史调 signal, 看最后一点的目标仓位,
        与当前 target 对比, 变化则提交订单意图 (订单意图层, 不直接碰 broker).

        on_bar 为 async: LiveBroker 下单需 asyncio.to_thread 跑 subprocess,
        不能阻塞事件循环. 策略覆盖 on_bar 时用 `async def`.
        子类通常只需实现同步 signal, 用此默认 on_bar 即可.

        bar: 当根K线 (pd.Series, 含 date/open/high/low/close/volume)
        ctx: Context (history/position/target/submit_order/params/qty_for)
        """
        df = ctx.history()
        if len(df) == 0:
            return
        tgt = int(pd.Series(self.signal(df, ctx.params)).fillna(0).astype(int).iloc[-1])
        if tgt == 1 and ctx.target == 0:
            qty = ctx.qty_for()
            if qty > 0:
                await ctx.submit_order(side="buy", qty=qty, price=ctx._last_price)
        elif tgt == 0 and ctx.target == 1 and ctx.position > 0:
            await ctx.submit_order(side="sell", qty=ctx.position, price=ctx._last_price)

    def on_stop(self, ctx: "Context") -> None:
        """策略停止时调用一次. 默认空."""

    # ---------------- 供看板/配置层使用 ----------------
    def default_params(self) -> dict:
        return {k: (v["default"] if isinstance(v, dict) else v)
                for k, v in self.PARAMS.items()}

    def validate_params(self, params: dict) -> dict:
        out = {}
        for k, spec in self.PARAMS.items():
            if not isinstance(spec, dict):  # 常量型参数 (如时间门槛)
                out[k] = params.get(k, spec)
                continue
            v = params.get(k, spec["default"])
            lo, hi = spec.get("min"), spec.get("max")
            if spec["type"] == INT:
                v = int(v)
            else:
                v = float(v)
            if lo is not None:
                v = max(lo, v)
            if hi is not None:
                v = min(hi, v)
            out[k] = v
        return out

    def params_schema(self) -> dict:
        """供前端 UI 自动生成参数控件的 schema (类型/默认/min/max)."""
        out = {}
        for k, spec in self.PARAMS.items():
            if not isinstance(spec, dict):
                out[k] = {"type": "const", "value": spec}
            else:
                out[k] = {"type": spec["type"], "default": spec["default"],
                          "min": spec.get("min"), "max": spec.get("max")}
        return out

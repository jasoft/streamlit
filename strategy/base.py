"""Strategy 基类: 策略只实现信号逻辑, 与回测引擎/实盘执行完全解耦.

子类约定:
- NAME / TITLE: 唯一标识与显示名
- PARAMS: 参数 schema, 回测看板据此自动生成控件, config.json 覆盖默认值
- SYMBOLS: 默认标的列表
- target_position(df, params): 输入单标的日K DataFrame (date/open/high/low/close/volume),
  返回 0/1 目标仓位序列, index 与 df 对齐, 按当日收盘决策.
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

    def target_position(self, df: pd.DataFrame, params: dict) -> pd.Series:
        raise NotImplementedError

    # ---- 供看板/配置层使用 ----
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

"""Tick 交替买卖测试策略.

目标: 验证从 策略信号 -> 下单 -> 记账 -> 前端展示 的全链路.

机制:
  每次 target_position() 被调用时,
  读取该策略 state.json 里记录的 "已切换次数" (_switch_count),
  按 switch_every 参数决定本次返回 1 还是 0:
    第 1..N 次调用 (N 次) -> 0 (卖出)
    第 N+1..2N 次调用 -> 1 (买入)
    第 2N+1..3N 次 -> 0 (卖出)
    ... 以此类推

run_once 中 compute_signal 触发 target_position,
计划订单后会调用 record(), 它会写入 state.json (记 dry:true 的持仓 + _switch_count+1).
下一轮 run_once 再读 state, 切换到下一个阶段.
完全自包含: 不依赖外部 evals 写入, dry-run 模式也能完整测试.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from strategy.base import Strategy, INT

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STATE_DIR = REPO_ROOT / "strategy" / "state"
STATE_FILE = STATE_DIR / "{name}.state.json"
EVALS_FILE = STATE_DIR / "{name}.evals.jsonl"


def _load_counter(strategy_name: str) -> int:
    """从 state.json 读 _call_count 计数器 (每次 target_position 被调用就自增)."""
    p = STATE_DIR / f"{strategy_name}.state.json"
    if not p.exists():
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return int(data.get("_call_count", 0))
    except (json.JSONDecodeError, ValueError):
        return 0


def _save_counter(strategy_name: str, count: int) -> None:
    """把 _call_count 写回 state.json (保留现有字段)."""
    p = STATE_DIR / f"{strategy_name}.state.json"
    STATE_DIR.mkdir(exist_ok=True, parents=True)
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            pass
    data["_call_count"] = count
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")


class TickBuySell(Strategy):
    NAME = "tick_buy_sell"
    TITLE = "Tick 交替买卖 (链路测试)"
    # switch_every: 每 N 次评估才切换一次 (默认 2 = 每 2 个 tick 切一次)
    PARAMS = {"switch_every": {"type": INT, "default": 2, "min": 1, "max": 10}}
    SYMBOLS = ["sz159915"]

    def target_position(self, df: pd.DataFrame, params: dict) -> pd.Series:
        """自增计数, 按 switch_every 交替返回 0/1.

        与回测兼容: 前面所有 bar 都返回 0, 只有最后一条是今天的目标.
        """
        switch_every = int(params.get("switch_every", 2))
        # 读取并自增计数器
        count = _load_counter(self.NAME) + 1  # +1 = 本次是第 count 次调用
        _save_counter(self.NAME, count)

        # 每 switch_every 次算一个阶段: 偶数阶段 = 0(卖), 奇数阶段 = 1(买)
        stage = (count - 1) // switch_every
        today_target = 1 if (stage % 2 == 1) else 0

        targets = pd.Series(0, index=df.index)
        if len(targets) > 0:
            targets.iloc[-1] = today_target
        return targets

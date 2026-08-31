"""策略自动发现: 扫描 strategies/ 目录, 加载暴露 Strategy 子类的模块, 零注册."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from strategy.base import Strategy

STRATEGY_DIR = Path(__file__).resolve().parent / "strategies"
_EXCLUDE = {"__init__.py", "__pycache__"}


def discover() -> dict[str, Strategy]:
    """返回 {NAME: Strategy 实例}. 新策略放一个 py 文件即可被发现."""
    out = {}
    for path in sorted(STRATEGY_DIR.glob("*.py")):
        if path.name in _EXCLUDE or path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(
            f"strategy.strategies.{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for attr in vars(mod).values():
            if (isinstance(attr, type) and issubclass(attr, Strategy)
                    and attr is not Strategy and attr.NAME):
                out[attr.NAME] = attr()
    return out


def get(name: str) -> Strategy:
    strategies = discover()
    if name not in strategies:
        raise KeyError(f"策略不存在: {name}, 可选: {list(strategies)}")
    return strategies[name]

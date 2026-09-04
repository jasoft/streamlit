"""选股插件自动发现: 扫描本目录, 加载暴露 PickStrategy 子类的模块, 零注册."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from trading.picker_strategies.base import PickStrategy

_DIR = Path(__file__).resolve().parent
_EXCLUDE = {"__init__.py", "base.py", "registry.py", "__pycache__"}


def discover() -> dict[str, PickStrategy]:
    """返回 {ID: PickStrategy 实例}. 新选股策略放一个 py 文件即可被发现."""
    out: dict[str, PickStrategy] = {}
    for path in sorted(_DIR.glob("*.py")):
        if path.name in _EXCLUDE or path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(
            f"trading.picker_strategies.{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for attr in vars(mod).values():
            if (isinstance(attr, type) and issubclass(attr, PickStrategy)
                    and attr is not PickStrategy and attr.ID):
                out[attr.ID] = attr()
    return out


def get(name: str) -> PickStrategy:
    pickers = discover()
    if name not in pickers:
        raise KeyError(f"选股插件不存在: {name}, 可选: {list(pickers)}")
    return pickers[name]


def catalog() -> list[dict]:
    """插件目录 (Web 端选插件 + 生成参数默认值用)."""
    return [{"id": p.ID, "title": p.TITLE, "desc": p.DESC,
             "params": {k: dict(v) for k, v in p.PARAMS.items()},
             "defaults": {k: v.get("default") for k, v in p.PARAMS.items()}}
            for p in discover().values()]

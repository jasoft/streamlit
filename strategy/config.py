"""config.json 读写: 每策略的 enabled/symbols/params/cash 与实盘运行参数."""
from __future__ import annotations

import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

DEFAULT_LIVE = {"dry_run": True, "execute_time": "14:55", "qfq": False,
                "poll_seconds": 60}


def defaults_for(strategy) -> dict:
    """策略缺失配置时, 用类属性生成默认配置."""
    return {
        "enabled": False,
        "symbols": list(strategy.SYMBOLS),
        "params": strategy.default_params(),
        "cash_per_symbol": 10000,
        "live": dict(DEFAULT_LIVE),
    }


def load(strategies: dict | None = None) -> dict:
    if strategies is None:
        from strategy.registry import discover
        strategies = discover()
    cfg = {"strategies": {}}
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for name, strat in strategies.items():
        saved = cfg["strategies"].get(name, {})
        base = defaults_for(strat)
        for k, v in saved.items():
            if k == "params" and isinstance(v, dict):
                base[k].update(v)
            elif k == "live" and isinstance(v, dict):
                base[k].update(v)
            else:
                base[k] = v
        cfg["strategies"][name] = base
    return cfg


def save(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

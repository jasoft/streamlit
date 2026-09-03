"""config.json 读写: 每策略的 enabled/symbols/params/cash + 交易成本(手续费/滑点)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

DEFAULT_LIVE = {"dry_run": True, "execute_time": "14:55", "qfq": False,
                "poll_seconds": 60}

# 默认交易成本 (三大类: 股票 stock / 期货 futures / 期权 options)
# 含义均为"成交金额比例", 例如 0.0001 = 万分之一
# 买入 = buy_fee + slippage (多付钱)
# 卖出 = sell_fee + stamp_duty + slippage (少收钱)
# 期权目前只支持股票, 所以默认跟股票一致
DEFAULT_TRADE_COSTS: dict[str, dict[str, float]] = {
    "stock": {
        "buy_fee": 0.0001,           # 买入手续费: 万分之一
        "sell_fee": 0.0001,          # 卖出手续费: 万分之一
        "sell_stamp_duty": 0.001,    # 卖出印花税: 千分之一 (A股卖出单边)
        "slippage": 0.0001,          # 滑点: 万分之一
    },
    "futures": {
        "buy_fee": 0.00002,          # 期货买入手续费 (示例, 用户可改)
        "sell_fee": 0.00002,         # 期货卖出手续费
        "sell_stamp_duty": 0.0,      # 期货无印花税
        "slippage": 0.0001,          # 滑点
    },
    "options": {
        "buy_fee": 0.0001,           # 期权 (暂复用股票默认, 用户说明 "只支持股票")
        "sell_fee": 0.0001,
        "sell_stamp_duty": 0.0,
        "slippage": 0.0001,
    },
}

# 所有合法大类, 防止前端/配置乱写
VALID_COST_CATEGORIES = ("stock", "futures", "options")


def defaults_for(strategy) -> dict:
    """策略缺失配置时, 用类属性生成默认配置."""
    return {
        "enabled": False,
        "symbols": list(strategy.SYMBOLS),
        "params": strategy.default_params(),
        "cash_per_symbol": 10000,
        "live": dict(DEFAULT_LIVE),
    }


def _merge_trade_costs(saved: dict) -> dict[str, dict[str, float]]:
    """把用户保存的 trade_costs 和默认值合并, 缺的大类/字段补默认."""
    out: dict[str, dict[str, float]] = {}
    for cat in VALID_COST_CATEGORIES:
        default = dict(DEFAULT_TRADE_COSTS[cat])
        user_cat = saved.get(cat) if isinstance(saved, dict) else None
        if isinstance(user_cat, dict):
            for k, dv in default.items():
                v = user_cat.get(k)
                if isinstance(v, (int, float)):
                    default[k] = float(v)
        out[cat] = default
    return out


def load(strategies: dict | None = None) -> dict:
    if strategies is None:
        from strategy.registry import discover
        strategies = discover()
    cfg: dict[str, Any] = {"strategies": {}, "trade_costs": {}}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = {"strategies": {}, "trade_costs": {}}
    # 1. 合并交易成本 (即使老的 config.json 没这个字段, 也会生成默认)
    cfg["trade_costs"] = _merge_trade_costs(cfg.get("trade_costs") or {})
    # 2. 合并每策略配置 (旧逻辑)
    if "strategies" not in cfg or not isinstance(cfg["strategies"], dict):
        cfg["strategies"] = {}
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


# ---- 方便回测 / 参数优化使用的工具函数 ----
def symbol_category(symbol: str) -> str:
    """根据交易所代码前缀推断大类: stock / futures / options.

    A股 (含ETF/LOF/指数): sh/sz 开头 → stock
    期权 (中国市场代码规则复杂, 简单处理以 mo/IO/HO 等市场名或用户前缀):
      - 带 ".C" / ".P" 后缀, 或 user-config 里显式分类 → options
      - 其它: stock (默认, 符合用户"期权目前只支持股票"的预期)
    期货: CFFEX/SHFE/DCE/CZCE/INE 一般以合约名数字结尾, 但用户量小,
      先用简单启发: 非 sh/sz 开头且含数字 → futures, 否则 stock.
    """
    if not isinstance(symbol, str):
        return "stock"
    s = symbol.strip().lower()
    # A股/ETF 绝大多数
    if s.startswith("sh") or s.startswith("sz") or s.startswith("bj"):
        return "stock"
    # 典型期权后缀 (简化, 用户扩展即可)
    if s.endswith(".c") or s.endswith(".p") or ".call" in s or ".put" in s:
        return "options"
    # 非 A股 代码里既有字母又有数字 → 通常是期货合约 (如 if2409 / rb2410)
    has_alpha = any(c.isalpha() for c in s)
    has_digit = any(c.isdigit() for c in s)
    if has_alpha and has_digit:
        return "futures"
    return "stock"


def trade_costs_for(cfg: dict, symbol: str) -> dict[str, float]:
    """从 config.load() 返回的 cfg 里按 symbol 取对应大类的交易成本."""
    costs = cfg.get("trade_costs") if isinstance(cfg, dict) else None
    merged = _merge_trade_costs(costs or {})
    cat = symbol_category(symbol)
    return dict(merged[cat])

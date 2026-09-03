"""条件单 Web 管理层: 引擎生命周期 + 订单持久化 (供 backend/main.py 调用).

设计:
- 引擎跑在 FastAPI 事件循环上 (ConditionEngine 内部全是 asyncio 协程 + to_thread),
  因此相关端点必须 async def, 保证 ensure_future 拿到当前 loop.
- 订单配置 + 运行时状态持久化在 backend/condition_orders.json (引擎 on_change 回调
  触发落盘), 引擎重启后恢复 WATCH/ARMED/DONE 状态; Portfolio 走
  strategy/state/condition_orders.state.json 恢复资金/持仓.
- 同一标的多单会交叉干扰 (引擎约定), add_order 层面拒绝重复 symbol.
"""
from __future__ import annotations

from typing import Optional

from strategy.runtime.portfolio import Portfolio
from trading.condition_orders import (ConditionEngine, ConditionOrder,
                                      load_orders_cfg, save_orders_cfg)

_engine: Optional[ConditionEngine] = None


def get_engine() -> Optional[ConditionEngine]:
    return _engine


def is_running() -> bool:
    eng = _engine
    return bool(eng is not None and eng.is_running)


def _persist() -> None:
    """引擎当前全量订单状态 -> condition_orders.json (on_change 回调入口)."""
    eng = _engine
    if eng is not None:
        save_orders_cfg(eng.export_configs())


def _restore_view_orders() -> list[dict]:
    """引擎未运行时, 从 JSON 恢复订单列表做只读展示."""
    out = []
    for c in load_orders_cfg():
        fields = {k: v for k, v in c.items()
                  if k in ConditionOrder.__dataclass_fields__}
        out.append(ConditionOrder(**fields).to_dict())  # type: ignore[arg-type]
    return out


def status() -> dict:
    """全量状态快照 (订单 + 组合 + 日志), 引擎停了也能看 (只读视图)."""
    eng = _engine
    if eng is not None:
        snap = eng.snapshot()
        return {
            "running": eng.is_running,
            "logs": list(eng.logs)[-80:],
            **snap,
        }
    pf = Portfolio.load("condition_orders", 0.0)
    return {
        "running": False,
        "live": None,
        "poll_seconds": None,
        "orders": _restore_view_orders(),
        "portfolio": pf.snapshot(),
        "logs": [],
    }


async def start(live: bool, cash: float, poll_seconds: float) -> dict:
    """启动引擎 (必须 async: ensure_future 需要事件循环)."""
    global _engine
    if is_running():
        return {"ok": False, "msg": "引擎已在运行, 无需重复启动"}
    eng = ConditionEngine(
        load_orders_cfg(), live=live, cash=cash, poll_seconds=poll_seconds,
        on_change=_persist,
    )
    eng.start_all()
    _engine = eng
    _persist()
    return {"ok": True,
            "msg": f"条件单引擎已启动 ({'实盘/同花顺' if live else '模拟'}), "
                   f"{len(eng.orders)} 单",
            "status": status()}


async def stop() -> dict:
    """停止引擎: 取消全部盯盘协程并落盘当前状态."""
    global _engine
    eng = _engine
    if eng is None:
        return {"ok": False, "msg": "引擎未在运行"}
    eng.stop_all()
    _persist()
    _engine = None
    return {"ok": True, "msg": "条件单引擎已停止 (订单状态已保存)"}


def add_order(cfg: dict) -> dict:
    """新增条件单: 写文件 + (引擎运行中则) 动态起协程, 无需重启引擎."""
    symbol = str(cfg.get("symbol", "")).strip()
    if not symbol:
        raise ValueError("标的代码 (symbol) 必填, 如 601899 或 sz159915")
    buy_qty = int(cfg.get("buy_qty") or 0)
    if buy_qty <= 0 or buy_qty % 100 != 0:
        raise ValueError("买入数量 (buy_qty) 必须是 >0 的 100 整数倍")
    cfgs = load_orders_cfg()
    if any(str(c.get("symbol")) == symbol for c in cfgs):
        raise ValueError(f"标的 {symbol} 已有条件单 (同一标的暂不支持多单, "
                         f"会交叉干扰)")
    entry = {
        "id": f"co_{symbol}",
        "symbol": symbol,
        "trigger_gap_pct": float(cfg.get("trigger_gap_pct") or 0),
        "buy_qty": buy_qty,
        "sell_rally_pct": float(cfg.get("sell_rally_pct") or 0),
        "open_window_min": int(cfg.get("open_window_min") or 3),
        "state": "WATCH",
    }
    cfgs.append(entry)
    save_orders_cfg(cfgs)
    if _engine is not None:
        _engine.add_order(entry)
    return entry


def remove_order(co_id: str) -> bool:
    """删除条件单: 从文件移除 + (引擎运行中则) 取消其协程."""
    cfgs = load_orders_cfg()
    kept = [c for c in cfgs if c.get("id") != co_id]
    if len(kept) == len(cfgs):
        return False
    save_orders_cfg(kept)
    if _engine is not None:
        _engine.remove_order(co_id)
    return True

"""网格单 Web 管理层: 引擎生命周期 + 网格持久化 (供 backend/main.py 调用).

与 backend/conditions.py 同款设计:
- 引擎跑在 FastAPI 事件循环上 (GridEngine 内部全是 asyncio 协程 + to_thread),
  相关端点必须 async def, 保证 ensure_future 拿到当前 loop.
- 网格配置 + 运行时状态持久化在 backend/grid_orders.json (引擎 on_change 回调
  触发落盘), 重启后恢复状态; Portfolio 走 strategy/state/grid_orders.state.json.
- 同一标的多网格共享 Portfolio 持仓会交叉干扰, add_grid 层面拒绝重复 symbol.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from strategy.runtime.portfolio import Portfolio
from trading.grid_orders import (GridEngine, GridOrder, calc_triggers,
                                 load_grids_cfg, save_grids_cfg)

_engine: Optional[GridEngine] = None


def get_engine() -> Optional[GridEngine]:
    return _engine


def is_running() -> bool:
    eng = _engine
    return bool(eng is not None and eng.is_running)


def _persist() -> None:
    """引擎当前全量网格状态 -> grid_orders.json (on_change 回调入口)."""
    eng = _engine
    if eng is not None:
        save_grids_cfg(eng.export_configs())


def _restore_view_grids() -> list[dict]:
    """引擎未运行时, 从 JSON 恢复网格列表做只读展示."""
    out = []
    for c in load_grids_cfg():
        fields = {k: v for k, v in c.items()
                  if k in GridOrder.__dataclass_fields__}
        out.append(GridOrder(**fields).to_dict())  # type: ignore[arg-type]
    return out


def status() -> dict:
    """全量状态快照 (网格 + 组合 + 日志), 引擎停了也能看 (只读视图)."""
    eng = _engine
    if eng is not None:
        snap = eng.snapshot()
        return {
            "running": eng.is_running,
            "logs": list(eng.logs)[-80:],
            **snap,
        }
    pf = Portfolio.load("grid_orders", 0.0)
    return {
        "running": False,
        "live": None,
        "poll_seconds": None,
        "grids": _restore_view_grids(),
        "portfolio": pf.snapshot(),
        "logs": [],
    }


async def start(live: bool, cash: float, poll_seconds: float) -> dict:
    """启动引擎 (必须 async: ensure_future 需要事件循环)."""
    global _engine
    if is_running():
        return {"ok": False, "msg": "引擎已在运行, 无需重复启动"}
    eng = GridEngine(
        load_grids_cfg(), live=live, cash=cash, poll_seconds=poll_seconds,
        on_change=_persist,
    )
    eng.start_all()
    _engine = eng
    _persist()
    return {"ok": True,
            "msg": f"网格引擎已启动 ({'实盘/同花顺' if live else '模拟'}), "
                   f"{len(eng.grids)} 个网格",
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
    return {"ok": True, "msg": "网格引擎已停止 (网格状态已保存)"}


def add_grid(cfg: dict) -> dict:
    """新增网格单: 校验 + 写文件 + (引擎运行中则) 动态起协程, 无需重启."""
    symbol = str(cfg.get("symbol", "")).strip()
    if not symbol:
        raise ValueError("标的代码 (symbol) 必填, 如 601899 或 sz159915")
    upper = float(cfg.get("upper") or 0)
    lower = float(cfg.get("lower") or 0)
    if upper <= 0 or lower <= 0 or upper <= lower:
        raise ValueError(f"区间非法: 需 0 < 下限({lower}) < 上限({upper})")
    grid_unit = str(cfg.get("grid_unit") or "pct")
    if grid_unit not in ("pct", "price"):
        raise ValueError("grid_unit 只能是 pct (等比) 或 price (等差)")
    step = float(cfg.get("step") or 0)
    if step <= 0:
        raise ValueError("网格间距 (step) 必须 > 0")
    if grid_unit == "pct" and step >= 100:
        raise ValueError("等比间距 (%) 不能 >= 100")
    if grid_unit == "price" and step * 2 >= upper - lower:
        raise ValueError("等差间距过大: 上下各一档就超出区间了")
    base_price = float(cfg.get("base_price") or 0)
    if not (lower <= base_price <= upper):
        raise ValueError(f"基准价 ({base_price}) 必须在区间 [{lower}, {upper}] 内")
    qty_mode = str(cfg.get("qty_mode") or "qty")
    if qty_mode not in ("qty", "cash"):
        raise ValueError("qty_mode 只能是 qty (固定股数) 或 cash (固定金额)")
    per_qty = int(cfg.get("per_qty") or 0)
    per_cash = float(cfg.get("per_cash") or 0)
    if qty_mode == "qty" and (per_qty < 100 or per_qty % 100 != 0):
        raise ValueError("固定股数模式: 每格数量 (per_qty) 必须 >=100 且为 100 整数倍")
    if qty_mode == "cash" and per_cash <= 0:
        raise ValueError("固定金额模式: 每格金额 (per_cash) 必须 > 0")
    multiplier = float(cfg.get("multiplier") or 1)
    if multiplier < 1:
        raise ValueError("梯度倍量 (multiplier) 必须 >= 1 (1=每格等量)")
    max_pos = int(cfg.get("max_position") or 0)
    min_pos = int(cfg.get("min_position") or 0)
    if max_pos and min_pos and max_pos < min_pos:
        raise ValueError(f"最大持仓 ({max_pos}) 不能小于最小底仓 ({min_pos})")
    expire = str(cfg.get("expire_date") or "").strip()
    if expire:
        try:
            _dt.date.fromisoformat(expire)
        except ValueError:
            raise ValueError(f"有效期格式非法: {expire} (应为 YYYY-MM-DD, 空=长期)")
    cfgs = load_grids_cfg()
    if any(str(c.get("symbol")) == symbol for c in cfgs):
        raise ValueError(f"标的 {symbol} 已有网格单 (同一标的暂不支持多网格, "
                         f"会共享持仓交叉干扰)")
    entry = {
        "id": f"gr_{symbol}",
        "symbol": symbol,
        "upper": upper,
        "lower": lower,
        "grid_unit": grid_unit,
        "step": step,
        "base_price": base_price,
        "qty_mode": qty_mode,
        "per_qty": per_qty if qty_mode == "qty" else 0,
        "per_cash": per_cash if qty_mode == "cash" else 0.0,
        "multiplier": multiplier,
        "max_position": max_pos,
        "min_position": min_pos,
        "sell_retrace_pct": float(cfg.get("sell_retrace_pct") or 0),
        "buy_rebound_pct": float(cfg.get("buy_rebound_pct") or 0),
        "pad_pct": float(cfg.get("pad_pct") or 0),
        "t1_protect": bool(cfg.get("t1_protect", True)),
        "expire_date": expire,
        "base_qty": int(cfg.get("base_qty") or 0),
        "state": "RUNNING",
    }
    buy_t, sell_t = calc_triggers(base_price, grid_unit, step)
    entry["buy_trigger"], entry["sell_trigger"] = buy_t, sell_t
    cfgs.append(entry)
    save_grids_cfg(cfgs)
    if _engine is not None:
        _engine.add_grid(entry)
    return entry


def remove_grid(gid: str) -> bool:
    """删除网格单: 从文件移除 + (引擎运行中则) 取消其协程. 持仓保留在 Portfolio."""
    cfgs = load_grids_cfg()
    kept = [c for c in cfgs if c.get("id") != gid]
    if len(kept) == len(cfgs):
        return False
    save_grids_cfg(kept)
    if _engine is not None:
        _engine.remove_grid(gid)
    return True


def _set_state(gid: str, from_state: str, to_state: str) -> bool:
    """状态切换: 引擎运行中走引擎, 否则直接改 JSON (下次启动生效)."""
    if _engine is not None:
        g = next((x for x in _engine.grids if x.id == gid), None)
        if g is None or g.state != from_state:
            return False
        if to_state == "PAUSED":
            return _engine.pause(gid)
        return _engine.resume(gid)
    cfgs = load_grids_cfg()
    for c in cfgs:
        if c.get("id") == gid and c.get("state") == from_state:
            c["state"] = to_state
            save_grids_cfg(cfgs)
            return True
    return False


def pause_grid(gid: str) -> bool:
    return _set_state(gid, "RUNNING", "PAUSED")


def resume_grid(gid: str) -> bool:
    return _set_state(gid, "PAUSED", "RUNNING")

"""选股自动交易 Web 管理层: 引擎生命周期 + 策略组 CRUD (供 backend/main.py 调用).

设计 (同 backend/conditions.py 模式):
- 引擎跑在 FastAPI 事件循环上 (StockPickerEngine 内部全是 asyncio 协程 + to_thread),
  相关端点必须 async def, 保证 ensure_future 拿到当前 loop.
- 单一事实来源是 SQLite (backend/stockpicker.db): 策略组配置 / 买入组持仓 / 指令流水
  都在库里, 引擎停了也能只读展示; 引擎启动时从库加载 enabled 组.
- run-once: 引擎在跑 -> 直接对指定组 force 跑一轮; 没在跑 -> 临时引擎跑一轮即弃
  (默认模拟, 验证选股/买卖链路用, 不触碰真实下单).
"""
from __future__ import annotations

import datetime as _dt
import secrets
from typing import Optional

from backend import picker_db
from trading.picker_strategies import registry
from trading.stock_picker import StockPickerEngine

_engine: Optional[StockPickerEngine] = None


def get_engine() -> Optional[StockPickerEngine]:
    return _engine


def is_running() -> bool:
    eng = _engine
    return bool(eng is not None and eng.is_running)


def status() -> dict:
    """全量状态快照 (引擎运行态 + 策略组 + 买入组持仓 + 事件), 停止也可只读查看."""
    eng = _engine
    if eng is not None:
        snap = eng.snapshot()
        return {"running": eng.is_running, "logs": list(eng.logs)[-80:], **snap}
    # 只读视图: 从 SQLite 恢复策略组 + 买入组持仓 (无实时报价)
    groups = []
    for g in picker_db.list_groups():
        holdings = picker_db.list_positions(strategy_id=g["strategy_id"],
                                            status="holding")
        selling = picker_db.list_positions(strategy_id=g["strategy_id"],
                                           status="selling")
        groups.append({
            **g, "running": False, "rounds": 0,
            "last_buy_scan": "", "last_sell_scan": "", "last_error": "",
            "holdings": holdings, "selling": selling, "pending_buys": [],
        })
    return {
        "running": False, "live": None, "poll_seconds": None,
        "groups": groups,
        "portfolios": {},
        "events": picker_db.list_events(limit=100),
        "logs": [],
    }


async def start(live: bool, cash: float, poll_seconds: float) -> dict:
    """启动引擎 (必须 async: ensure_future 需要事件循环)."""
    global _engine
    if is_running():
        return {"ok": False, "msg": "引擎已在运行, 无需重复启动"}
    eng = StockPickerEngine(picker_db.list_groups(), live=live, cash=cash,
                            poll_seconds=poll_seconds)
    eng.start_all()
    _engine = eng
    n_enabled = sum(1 for g in eng.groups.values() if g.enabled)
    return {"ok": True,
            "msg": f"选股引擎已启动 ({'实盘/同花顺' if live else '模拟'}), "
                   f"{len(eng.groups)} 组 (启用 {n_enabled})",
            "status": status()}


async def stop() -> dict:
    """停止引擎: 取消全部组协程 (持仓/流水在 SQLite, 不丢)."""
    global _engine
    eng = _engine
    if eng is None:
        return {"ok": False, "msg": "引擎未在运行"}
    eng.stop_all()
    _engine = None
    return {"ok": True, "msg": "选股引擎已停止 (买入组持仓与流水已入库)"}


# ---------------------------------------------------------------- 策略组 CRUD ----
def add_group(cfg: dict) -> dict:
    """新增策略组 (写库 + 引擎运行中则动态起协程, 无需重启)."""
    picker_id = str(cfg.get("picker") or "").strip()
    if not picker_id:
        raise ValueError("选股插件 (picker) 必填")
    try:
        registry.get(picker_id)
    except KeyError as e:
        raise ValueError(str(e))
    universe = cfg.get("universe") or []
    if isinstance(universe, str):
        universe = universe.replace(",", " ").split()
    universe = [str(c).strip() for c in universe if str(c).strip()]
    if not universe:
        raise ValueError("股票池 (universe) 不能为空, 如 ['601899', '600519']")
    per_qty = int(cfg.get("per_qty") or 0)
    if per_qty < 0 or per_qty % 100 != 0:
        raise ValueError("每只买入股数 (per_qty) 必须是 100 整数倍, 0=按资金自动整手")
    gid = str(cfg.get("strategy_id") or "").strip()
    if not gid:
        gid = f"sp_{_dt.datetime.now():%Y%m%d%H%M%S}_{secrets.token_hex(2)}"
    if picker_db.get_group(gid):
        raise ValueError(f"策略 ID {gid} 已存在 (买入组与策略 ID 挂钩, 需唯一)")
    entry = {
        "strategy_id": gid,
        "title": str(cfg.get("title") or "").strip() or picker_id,
        "picker": picker_id,
        "universe": universe,
        "params": cfg.get("params") or {},
        "per_qty": per_qty,
        "cash_per_symbol": float(cfg.get("cash_per_symbol") or 10000.0),
        "max_positions": int(cfg.get("max_positions") or 0),
        "buy_scan_every": max(int(cfg.get("buy_scan_every") or 60), 1),
        "t1_protect": bool(cfg.get("t1_protect", True)),
        "enabled": bool(cfg.get("enabled", True)),
    }
    saved = picker_db.upsert_group(entry)
    if _engine is not None:
        _engine.add_group(saved)
    return saved


def update_group(gid: str, patch: dict) -> dict:
    """更新策略组 (参数/启停/仓位约束). 引擎运行中即时生效."""
    cur = picker_db.get_group(gid)
    if cur is None:
        raise KeyError(f"策略组 {gid} 不存在")
    allowed = {"title", "picker", "universe", "params", "per_qty",
               "cash_per_symbol", "max_positions", "buy_scan_every",
               "t1_protect", "enabled"}
    merged = {**cur, **{k: v for k, v in patch.items() if k in allowed}}
    if isinstance(merged.get("universe"), str):
        merged["universe"] = merged["universe"].replace(",", " ").split()
    saved = picker_db.upsert_group({**merged, "strategy_id": gid})
    if _engine is not None:
        _engine.update_group(gid, {k: saved[k] for k in allowed if k in saved})
    return saved


def remove_group(gid: str) -> bool:
    """删除策略组 (持仓/流水保留审计). 引擎运行中则同时摘除协程."""
    if not picker_db.get_group(gid):
        return False
    if _engine is not None:
        _engine.remove_group(gid)
    return picker_db.delete_group(gid)


async def run_once(gid: str, live: bool = False) -> dict:
    """手动跑指定策略组一轮 (force 无视盘中时段). dry-run 友好."""
    eng = _engine
    if eng is not None and eng.is_running:
        snap = await eng.run_once(gid)
    else:
        g = picker_db.get_group(gid)
        if g is None:
            raise KeyError(f"策略组 {gid} 不存在")
        tmp = StockPickerEngine([g], live=live, poll_seconds=1.0)
        snap = await tmp.run_once(gid)
    grp = next((x for x in snap.get("groups", [])
                if x.get("strategy_id") == gid), None)
    return {"ok": True, "msg": f"策略组 {gid} 已跑一轮扫描", "group": grp,
            "events": snap.get("events", [])[:20], "logs": snap.get("logs", [])}

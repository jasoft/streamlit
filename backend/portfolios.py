"""组合交易 Web 管理层: 行情/真实持仓组装 + 执行编排 (供 backend/main.py 调用).

与 conditions/grids 不同, 组合交易无常驻引擎 — 是一次性手动操作 (创建/预览/
执行), 全部阻塞调用经 asyncio.to_thread 包装. 同花顺 GUI 是单一有状态面板,
持仓读取与真实下单共用一把 asyncio.Lock 串行, 避免并发 subprocess 互踩.

数据流:
  prices  <- strategy.fdata_client.quote (eltdx 长连接, 失败的 code 缺席)
  持仓    <- subprocess ths_trade.py positions (rows 内字段均为字符串, 需转换)
  计划/执行 <- trading/portfolios.py 纯函数 (alloc_buy/alloc_sell/sync_plan/
              execute_plan), dry_run=True 纯试算不碰同花顺.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

from strategy import fdata_client
from trading import portfolios as engine

REPO_ROOT = Path(__file__).resolve().parent.parent
THS_TRADE = REPO_ROOT / "trading" / "ths_trade.py"

_ths_lock = asyncio.Lock()  # 同花顺 GUI 串行 (持仓读取 + 真实下单互斥)


# ================================================================ 行情 / 持仓 ====

def _quote_one(code: str) -> dict | None:
    """单只最新价 (阻塞, 线程池跑). 失败/停牌无价返回 None."""
    try:
        q = fdata_client.quote(code)
    except Exception:  # noqa: BLE001 — 行情缺失不致命, 预览里会标注
        return None
    if not q or not q.get("last"):
        return None
    try:
        return {"last": float(q["last"]), "name": str(q.get("name") or "").strip()}
    except (TypeError, ValueError):
        return None


async def fetch_quotes(codes: list[str]) -> dict:
    """批量取最新价 {code: {last, name}}. 单只失败不影响其余."""
    def _all() -> dict:
        out = {}
        for c in dict.fromkeys(codes):
            q = _quote_one(c)
            if q is not None:
                out[c] = q
        return out

    return await asyncio.to_thread(_all)


def _parse_ths_positions(stdout: str) -> dict:
    """ths_trade positions JSON -> {code: {name, qty, available, cost, last}}."""
    try:
        data = json.loads(stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {}
    out: dict[str, dict] = {}
    for row in data.get("rows") or []:
        code = str(row.get("证券代码", "")).strip()
        if len(code) != 6 or not code.isdigit():
            continue

        def _f(key: str) -> float:
            try:
                return float(str(row.get(key, "0")).replace(",", "") or 0)
            except ValueError:
                return 0.0

        qty = int(_f("持仓数量"))
        raw_avail = str(row.get("可用数量", "")).strip()
        try:
            avail = int(float(raw_avail.replace(",", ""))) if raw_avail else qty
        except ValueError:
            avail = qty
        out[code] = {
            "name": str(row.get("证券名称", "")).strip(),
            "qty": qty,
            "available": max(0, avail),
            "cost": _f("成本价"),
            "last": _f("当前价"),
        }
    return out


async def fetch_ths_positions() -> dict:
    """同花顺真实持仓. 返回 {ok, positions, msg}; ok=False 时 positions 为空."""
    def _run() -> tuple[bool, dict, str]:
        cmd = [sys.executable, str(THS_TRADE), "positions"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=60, check=False)
        except (subprocess.TimeoutExpired, OSError) as e:
            return False, {}, f"ths_trade 调用失败: {e}"
        if r.returncode != 0:
            return False, {}, (r.stderr or r.stdout or f"exit={r.returncode}")[-200:]
        pos = _parse_ths_positions(r.stdout)
        return True, pos, ""

    async with _ths_lock:
        ok, pos, msg = await asyncio.to_thread(_run)
    return {"ok": ok, "positions": pos, "msg": msg}


# ================================================================ 组装视图 ====

def _prices_from(quotes: dict, ths_pos: dict) -> dict[str, float]:
    """fdata 优先, 同花顺持仓现价兜底 (停牌/行情缺失时仍可算市值)."""
    prices = {c: q["last"] for c, q in quotes.items()}
    for code, p in ths_pos.items():
        if p.get("last") and prices.get(code) is None:
            prices[code] = float(p["last"])
    return prices


async def overview(with_positions: bool = True) -> dict:
    """组合列表 + 实时价 + 真实持仓对照. THS 不可用时持仓字段为 null."""
    pfs = engine.load_portfolios()
    codes = sorted({it["code"] for pf in pfs for it in pf["items"]})
    quotes_task = fetch_quotes(codes)
    if with_positions:
        ths_task = fetch_ths_positions()
        quotes, ths = await asyncio.gather(quotes_task, ths_task)
    else:
        quotes = await quotes_task
        ths = {"ok": None, "positions": {}, "msg": "未请求"}

    ths_pos: dict = ths.get("positions") or {}
    prices = _prices_from(quotes, ths_pos)

    out = []
    for pf in pfs:
        rows = []
        mv_sum = 0.0
        for it in pf["items"]:
            code = it["code"]
            px = float(prices.get(code) or 0)
            pos = ths_pos.get(code)
            qty = pos["qty"] if pos else 0
            mv = qty * px
            mv_sum += mv
            rows.append({
                "code": code,
                "name": it.get("name") or (pos or {}).get("name", "")
                        or quotes.get(code, {}).get("name", ""),
                "weight": it["weight"],
                "price": px,
                "position": pos["qty"] if pos else None,     # None = 持仓不可得
                "available": pos["available"] if pos else None,
                "cost": pos["cost"] if pos else None,
                "market_value": round(mv, 2),
            })
        for r in rows:  # 实际权重 (市值全 0 时显示 0)
            r["actual_weight"] = round(r["market_value"] / mv_sum * 100, 2) if mv_sum else 0.0
            r["drift"] = round(r["actual_weight"] - r["weight"], 2)
        out.append({
            "id": pf["id"], "name": pf["name"], "note": pf.get("note", ""),
            "items": rows,
            "market_value": round(mv_sum, 2),
            "history": (pf.get("history") or [])[-10:],
            "created_at": pf.get("created_at", ""),
            "updated_at": pf.get("updated_at", ""),
        })
    return {"portfolios": out, "ths_ok": ths.get("ok"),
            "ths_msg": ths.get("msg", ""), "ts": _dt.datetime.now().isoformat(timespec="seconds")}


# ================================================================ CRUD ====

async def create(name: str, items: list[dict], note: str = "") -> dict:
    """创建组合; 名称缺失的标的顺手用行情补全."""
    normed = engine.normalize_items(items)
    missing = [it["code"] for it in normed if not it.get("name")]
    if missing:
        quotes = await fetch_quotes(missing)
        for it in normed:
            it["name"] = it.get("name") or quotes.get(it["code"], {}).get("name", "")
    return engine.create_portfolio(name, normed, note)


async def update(pid: str, items: list[dict] | None = None,
                 name: str | None = None, note: str | None = None) -> dict:
    if items is not None:
        normed = engine.normalize_items(items)
        missing = [it["code"] for it in normed if not it.get("name")]
        if missing:
            quotes = await fetch_quotes(missing)
            for it in normed:
                it["name"] = it.get("name") or quotes.get(it["code"], {}).get("name", "")
        items = normed
    return engine.update_portfolio(pid, items=items, name=name, note=note)


def delete(pid: str) -> bool:
    return engine.delete_portfolio(pid)


# ================================================================ 计划 / 执行 ====

async def _context(pid: str) -> tuple[dict, dict[str, float], dict, dict]:
    """组合 + prices + 持仓 {code: qty} + 可卖 {code: available}."""
    pf = engine.get_portfolio(pid)
    if pf is None:
        raise ValueError(f"组合 {pid} 不存在")
    quotes, ths = await asyncio.gather(
        fetch_quotes([it["code"] for it in pf["items"]]),
        fetch_ths_positions())
    prices = _prices_from(quotes, ths["positions"])
    qty_map = {c: p["qty"] for c, p in ths["positions"].items()}
    avail_map = {c: p["available"] for c, p in ths["positions"].items()}
    return pf, prices, qty_map, avail_map


def _build_plan(pf: dict, action: str, prices: dict, qty_map: dict,
                avail_map: dict, amount: float = 0.0,
                min_order_value: float = 1000.0) -> dict:
    """action -> 分配/调仓计划 (纯计算, 无 IO)."""
    items = pf["items"]
    if action == "buy":
        return {"plan": engine.alloc_buy(items, amount, prices),
                "default_side": "buy"}
    if action == "sell":
        return {"plan": engine.alloc_sell(items, amount, prices, avail_map),
                "default_side": "sell"}
    if action == "sync":
        return {"plan": engine.sync_plan(items, prices, qty_map, avail_map,
                                         min_order_value,
                                         liquidate=pf.get("removed_codes") or []),
                "default_side": "buy"}
    raise ValueError(f"未知 action: {action} (buy/sell/sync)")


async def preview(pid: str, action: str, amount: float = 0.0,
                  min_order_value: float = 1000.0) -> dict:
    """分配/调仓预览 (不下单). 同花顺不可用时卖出/同步会缺持仓数据."""
    pf, prices, qty_map, avail_map = await _context(pid)
    ctx = _build_plan(pf, action, prices, qty_map, avail_map, amount, min_order_value)
    return {"portfolio_id": pid, "name": pf["name"], "action": action,
            "amount": amount, "prices": prices, "plan": ctx["plan"],
            "ts": _dt.datetime.now().isoformat(timespec="seconds")}


async def execute(pid: str, action: str, amount: float = 0.0,
                  *, pad_pct: float = 0.3, min_order_value: float = 1000.0,
                  dry_run: bool = True) -> dict:
    """执行计划: dry_run=True 纯试算 (不碰同花顺); False 真实委托并记历史.

    真实下单持 _ths_lock 串行, 且执行期间阻塞持仓轮询 (同花顺面板互斥).
    """
    pf, prices, qty_map, avail_map = await _context(pid)
    ctx = _build_plan(pf, action, prices, qty_map, avail_map, amount, min_order_value)
    plan = ctx["plan"]
    rows = plan.get("rows") or []
    if not any(r.get("qty") for r in rows):
        return {"ok": False, "msg": "计划为空 (无待执行订单), 未调用同花顺",
                "plan": plan, "orders": []}

    if dry_run:
        result = engine.execute_plan(rows, default_side=ctx["default_side"],
                                     pad_pct=pad_pct, dry_run=True)
        return {"ok": True, "dry_run": True, "action": action,
                "orders": result["orders"], "plan": plan,
                "summary": f"试算 {action}: {len(result['orders'])} 笔待委托 (未调同花顺)"}

    async with _ths_lock:
        result = await asyncio.to_thread(
            engine.execute_plan, rows,
            default_side=ctx["default_side"], pad_pct=pad_pct, dry_run=False)

    entry = {
        "ts": _dt.datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "dry_run": False,
        "amount": amount,
        "pad_pct": pad_pct,
        "min_order_value": min_order_value,
        "ok_count": result["ok_count"],
        "fail_count": result["fail_count"],
        "orders": [{k: o.get(k) for k in
                    ("code", "name", "side", "qty", "limit_price", "status",
                     "result_text")} for o in result["orders"]],
        "summary": f"{action} 完成: 成功 {result['ok_count']} / 失败 "
                   f"{result['fail_count']} / 共 {len(result['orders'])} 笔",
    }
    engine.record_history(pid, entry)
    return {"ok": True, "dry_run": False, "action": action,
            "orders": result["orders"], "plan": plan, **entry}

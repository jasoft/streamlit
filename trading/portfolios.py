#!/usr/bin/env python3
"""组合交易引擎: 人工ETF — 按权重一键买卖一篮子股票/ETF, 并按新配比同步真实仓位.

定位 (与网格/条件单不同): 不是盯盘引擎, 而是"手动触发的一次性批量执行器".
  创建组合(权重) -> 按总金额+权重分配每只标的股数 -> 一键买入
  -> 按卖出总金额+权重卖出 -> 组合调整(增减个股/改权重)
  -> 同步仓位: 对比真实持仓市值与目标权重, 生成差额订单把仓位调回配比
     (类似人工调整权重的 ETF 申赎/调仓).

权重口径: item.weight 为百分比, 创建/调整时自动归一化到 100 (只存相对比例).

分配算法 (纯函数, 离线可测):
  买入  amount_i = total_amount * w_i/100, qty_i = floor(amount_i/price)取整手
        整手取整后的剩余预算按权重从大到小逐只补一手, 提高资金利用率
  卖出  amount_i = sell_amount * w_i/100, qty_i = round(amount_i/price)取整手
        上限为可卖数量; 零股持仓 (<100) 在该标的被列入卖出计划时一次性清掉
        (A 股零股只能整笔卖出, 买入不能)
  同步  total = Σ 组合内标的的真实市值 (不含现金, 不碰组合外持仓)
        target_i = total * w_i/100, delta = target_i - 市值_i
        买入方向凑不满一手跳过; 卖出方向贴近目标取整手, 零股尾仓直接清

执行: subprocess 调 trading/ths_trade.py buy/sell (与 LiveBroker 同款链路),
      显式传限价 (联动价未就绪会废单), 限价按品种最小变动价位取整:
      5/1 开头 (ETF/LOF/转债) 0.001, 其余 (股票) 0.01; 买向上/卖向下取整保成交.
      dry_run=True 纯试算不下单 (不碰同花顺); False 真实委托.
行情: 调用方 (backend/portfolios.py) 负责 fdata 取价, 引擎只收 prices dict.
持久化: backend/portfolios.json (CLI 与 Web 共用), 执行历史随组合存档 (最近 50 条).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
THS_TRADE = REPO_ROOT / "trading" / "ths_trade.py"

LOT = 100
STORE_PATH = REPO_ROOT / "backend" / "portfolios.json"
HISTORY_LIMIT = 50
THS_TIMEOUT = 120.0  # 单笔委托子进程超时 (确认框/结果框最慢 ~25s, 留足余量)

# 券商拒绝弹窗关键词 (与 LiveBroker._call_ths 同款)
_REJECT_KWS = ("警告", "错误", "失败", "关闭", "非交易", "禁止",
               "无效", "不足", "超过", "不允许", "拒绝")


# ================================================================ 持久化 ====

def load_portfolios() -> list[dict]:
    """读全部组合. 文件缺失/损坏返回空列表."""
    if not STORE_PATH.exists():
        return []
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_portfolios(portfolios: list[dict]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(
        json.dumps(portfolios, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def get_portfolio(pid: str) -> dict | None:
    return next((p for p in load_portfolios() if p.get("id") == pid), None)


def _save_portfolio(pf: dict) -> None:
    """upsert 单个组合 (按 id)."""
    pfs = load_portfolios()
    for i, p in enumerate(pfs):
        if p.get("id") == pf["id"]:
            pfs[i] = pf
            break
    else:
        pfs.append(pf)
    save_portfolios(pfs)


def record_history(pf: dict | str, entry: dict) -> None:
    """追加执行历史 (截断到 HISTORY_LIMIT) 并落盘. pf 传组合 dict 或组合 id 均可."""
    if isinstance(pf, str):
        found = get_portfolio(pf)
        if found is None:
            raise ValueError(f"组合 {pf} 不存在")
        pf = found
    hist = pf.setdefault("history", [])
    hist.append(entry)
    del hist[:-HISTORY_LIMIT]
    pf["updated_at"] = _now()
    _save_portfolio(pf)


# ================================================================ 工具 ====

def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def price_tick(code: str) -> float:
    """最小变动价位: 5/1 开头 (ETF/LOF/转债) 0.001, 其余 (股票) 0.01."""
    return 0.001 if str(code)[:1] in ("5", "1") else 0.01


def round_price(px: float, code: str, side: str) -> float:
    """限价按 tick 取整: 买向上取 (保成交), 卖向下取 (可成交)."""
    tick = price_tick(code)
    n = math.ceil(px / tick - 1e-9) if side == "buy" else math.floor(px / tick + 1e-9)
    return round(n * tick, 3)


def normalize_items(items: list[dict]) -> list[dict]:
    """校验 + 权重归一化到 100. items: [{code, name?, weight}].

    校验: 至少 1 条; code 为 6 位数字; weight > 0; 代码不重复.
    """
    if not items:
        raise ValueError("组合至少需要 1 个标的")
    seen: set[str] = set()
    clean: list[dict] = []
    for it in items:
        code = str(it.get("code", "")).strip()
        if not (code.isdigit() and len(code) == 6):
            raise ValueError(f"代码非法: {code!r} (需 6 位数字, 如 601899 / 510300)")
        if code in seen:
            raise ValueError(f"代码重复: {code}")
        seen.add(code)
        w = float(it.get("weight") or 0)
        if w <= 0:
            raise ValueError(f"{code} 权重必须 > 0")
        clean.append({"code": code, "name": str(it.get("name") or "").strip(),
                      "weight": w})
    total = sum(x["weight"] for x in clean)
    for x in clean:
        x["weight"] = round(x["weight"] / total * 100.0, 4)
    return clean


def validate_portfolio(name: str, items: list[dict]) -> None:
    if not str(name or "").strip():
        raise ValueError("组合名称必填")
    normalize_items(items)


# ================================================================ 分配算法 ====
# 全部纯函数: prices {code: float}, positions {code: 可卖股数}

def alloc_buy(items: list[dict], total_amount: float,
              prices: dict[str, float]) -> dict:
    """按权重把 total_amount 分配成整手买入单.

    返回 {rows: [{code,name,weight,price,qty,amount,note}], used, leftover}.
    price<=0 (行情缺失) 的标的跳过并在 note 说明.
    """
    if total_amount <= 0:
        raise ValueError("买入总金额必须 > 0")
    rows = []
    for it in items:
        code, px = it["code"], float(prices.get(it["code"]) or 0)
        row = {"code": code, "name": it.get("name", ""), "weight": it["weight"],
               "price": px, "qty": 0, "amount": 0.0, "note": ""}
        if px <= 0:
            row["note"] = "行情缺失, 跳过"
        else:
            budget = total_amount * it["weight"] / 100.0
            qty = int(budget / px // LOT * LOT)
            row["qty"], row["amount"] = qty, round(qty * px, 2)
            if qty < LOT:
                row["note"] = f"分配 {budget:.0f} 元不足一手 ({px:.3f})"
        rows.append(row)
    # 剩余预算按权重从大到小补一手 (资金利用率)
    leftover = total_amount - sum(r["amount"] for r in rows)
    for r in sorted(rows, key=lambda x: -x["weight"]):
        if r["price"] > 0 and leftover >= r["price"] * LOT:
            r["qty"] += LOT
            r["amount"] = round(r["qty"] * r["price"], 2)
            leftover -= r["price"] * LOT
    return {"rows": rows, "used": round(sum(r["amount"] for r in rows), 2),
            "leftover": round(max(0.0, leftover), 2)}


def _round_lots_half_up(x: float) -> int:
    """四舍五入到整手 (half-up, 避开 Python 银行家舍入)."""
    return int(x / LOT + 0.5) * LOT


def alloc_sell(items: list[dict], sell_amount: float,
               prices: dict[str, float], positions: dict[str, int]) -> dict:
    """按权重把 sell_amount 分摊到各标的卖出 (上限可卖数量).

    零股持仓 (<100) 在该标的进入卖出计划时一次性清掉; 可卖数量不足的标的
    按可卖数量封顶并在 note 说明.
    """
    if sell_amount <= 0:
        raise ValueError("卖出总金额必须 > 0")
    rows = []
    for it in items:
        code, px = it["code"], float(prices.get(it["code"]) or 0)
        pos = int(positions.get(code) or 0)
        row = {"code": code, "name": it.get("name", ""), "weight": it["weight"],
               "price": px, "position": pos, "qty": 0, "amount": 0.0, "note": ""}
        if px <= 0:
            row["note"] = "行情缺失, 跳过"
        elif pos <= 0:
            row["note"] = "无可卖持仓"
        else:
            target = sell_amount * it["weight"] / 100.0
            if pos < LOT:
                # 零股只能整笔卖: 分配金额够一半市值才值得动它
                if target >= pos * px * 0.5:
                    qty = pos
                    row["note"] = f"零股 {pos} 股一次性清仓"
                else:
                    qty = 0
                    row["note"] = "零股持仓, 分配金额过小不清仓"
            elif target >= pos * px * 0.999:
                qty = pos  # 清仓 (含零股尾)
            else:
                qty = min(_round_lots_half_up(target / px), pos // LOT * LOT)
            row["qty"], row["amount"] = qty, round(qty * px, 2)
            if qty <= 0 and not row["note"]:
                row["note"] = "分配金额不足一手"
        rows.append(row)
    return {"rows": rows, "used": round(sum(r["amount"] for r in rows), 2)}


def sync_plan(items: list[dict], prices: dict[str, float],
              positions: dict[str, int], available: dict[str, int] | None = None,
              min_order_value: float = 1000.0,
              liquidate: list[str] | set[str] | None = None) -> dict:
    """同步仓位 (人工ETF调仓): 真实市值 -> 目标权重 的差额订单计划.

    total = Σ 组合内标的 + 已移出标的 的持仓市值 (不含现金, 无关持仓原样列出不动).
    买入: 差额凑不满一手或 < min_order_value 跳过;
    卖出: 贴近目标取整手 (受可卖数量封顶), 零股尾仓直接清 (不受门槛限制);
    liquidate: 曾在组合中、调整时被移出的代码 (update_portfolio 记录在
    removed_codes) — 像 ETF 剔除成分一样整笔清仓, 市值并入 total 再分配.
    返回 {rows: [...], total_value, external: [...]}.
    """
    available = available if available is not None else positions
    seen = {it["code"] for it in items}
    liq_codes = sorted(c for c in (liquidate or [])
                       if c not in seen and int(positions.get(c) or 0) > 0)
    total = sum(int(positions.get(it["code"]) or 0) * float(prices.get(it["code"]) or 0)
                for it in items)
    total += sum(int(positions.get(c) or 0) * float(prices.get(c) or 0)
                 for c in liq_codes)
    rows, external = [], []
    for it in items:
        code, px = it["code"], float(prices.get(it["code"]) or 0)
        pos = int(positions.get(code) or 0)
        cur_value = pos * px
        target_value = total * it["weight"] / 100.0
        delta = target_value - cur_value
        row = {"code": code, "name": it.get("name", ""), "weight": it["weight"],
               "price": px, "position": pos, "target_value": round(target_value, 2),
               "cur_value": round(cur_value, 2), "delta_value": round(delta, 2),
               "side": "", "qty": 0, "note": ""}
        if px <= 0:
            row["note"] = "行情缺失, 跳过"
        elif delta > 0:
            qty = int(delta / px // LOT * LOT)
            if qty >= LOT and delta >= min_order_value:
                row["side"], row["qty"] = "buy", qty
            elif qty >= LOT:
                row["note"] = f"差额 {delta:.0f} < 门槛 {min_order_value:.0f}, 跳过"
            else:
                row["note"] = "差额不足一手, 跳过"
        elif delta < 0 and pos > 0:
            need = -delta
            avail = int(available.get(code) or 0)
            qty = min(_round_lots_half_up(need / px), pos)
            if pos < LOT:
                qty = min(pos, avail)  # 零股尾仓只能整笔清
                row["note"] = f"零股 {pos} 股清仓"
            elif avail < pos:
                qty = min(qty, avail)  # T+1: 当日买入不可卖, 按可卖封顶
                row["note"] = f"可卖 {avail} < 持仓 {pos} (T+1), 按可卖封顶"
            if qty > 0 and (need >= min_order_value or pos < LOT):
                row["side"], row["qty"] = "sell", qty
            elif qty > 0:
                row["note"] = row["note"] or f"差额 {need:.0f} < 门槛 {min_order_value:.0f}, 跳过"
                row["qty"] = 0
        rows.append(row)
    # 已移出组合的标的: 像 ETF 剔除成分一样整笔清仓 (不受门槛限制)
    for code in liq_codes:
        px = float(prices.get(code) or 0)
        pos = int(positions.get(code) or 0)
        avail = int(available.get(code) or 0)
        row = {"code": code, "name": "", "weight": 0.0, "price": px,
               "position": pos, "target_value": 0.0,
               "cur_value": round(pos * px, 2), "delta_value": round(-pos * px, 2),
               "side": "", "qty": 0, "note": "已移出组合, 清仓"}
        if px <= 0:
            row["note"] = "已移出组合, 行情缺失跳过"
        elif pos > 0:
            qty = min(pos, avail)
            if avail < pos:
                row["note"] = f"已移出组合, 可卖 {avail} < 持仓 {pos} (T+1), 余下次清"
            row["side"], row["qty"] = "sell", qty
        else:
            row["note"] = "已移出组合, 无持仓"
        rows.append(row)
    # 无关持仓 (从未在组合中): 只展示不动
    for code, pos in sorted(positions.items()):
        if code not in seen and code not in liq_codes and pos > 0:
            px = float(prices.get(code) or 0)
            external.append({"code": code, "position": pos, "price": px,
                             "value": round(pos * px, 2)})
    return {"rows": rows, "total_value": round(total, 2), "external": external}


# ================================================================ 执行 ====

def _call_ths_trade(side: str, code: str, qty: int, price: float,
                    dry_run: bool = False) -> dict:
    """同步调 ths_trade.py 下单 (与 LiveBroker._call_ths 同款解析).

    返回 {ok, result_text, stdout_tail}. side: buy/sell.
    """
    cmd = [sys.executable, str(THS_TRADE), side, code, str(qty),
           "--price", f"{price:.3f}", "--timeout", "10"]
    if dry_run:
        cmd.append("--dry-run")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=THS_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "result_text": f"ths_trade 超时: {e}",
                "stdout_tail": ""}
    out, err = r.stdout, r.stderr
    ok = r.returncode == 0
    result_text = ""
    if ok and out.strip():
        try:
            data = json.loads(out.strip().splitlines()[-1])
            ok = bool(data.get("ok", False))
            result_text = str(data.get("result_text", "") or "")
            if ok and any(kw in result_text for kw in _REJECT_KWS):
                ok = False
        except (json.JSONDecodeError, IndexError):
            pass
    if not ok and not result_text:
        result_text = (err[-200:] or out[-200:] or f"exit={r.returncode}")
    return {"ok": ok, "result_text": result_text, "stdout_tail": out[-300:]}


def execute_plan(plan_rows: list[dict], *, default_side: str = "buy",
                 pad_pct: float = 0.3, dry_run: bool = True) -> dict:
    """执行分配计划: 先卖后买 (腾资金), 逐笔调 ths_trade 委托.

    default_side: alloc_buy/alloc_sell 的行没有 side 字段, 由调用方指定方向;
    sync_plan 的行自带 side, 空侧 (跳过) 的行自动剔除.
    dry_run=True: 纯试算, 不碰同花顺, 订单标记 planned.
    dry_run=False: 真实委托 (限价 = 现价 x (1±pad%) 按 tick 取整).
    返回 {orders: [...], ok_count, fail_count}.
    """
    rows = []
    for r in plan_rows:
        side = r.get("side") or default_side
        if (int(r.get("qty") or 0) <= 0 or side not in ("buy", "sell")
                or float(r.get("price") or 0) <= 0):
            continue
        rows.append({**r, "qty": int(r["qty"]), "side": side})
    rows.sort(key=lambda r: 0 if r["side"] == "sell" else 1)  # 先卖后买
    orders = []
    for r in rows:
        px = float(r["price"])
        raw = px * (1 + pad_pct / 100.0) if r["side"] == "buy" \
            else px * (1 - pad_pct / 100.0)
        limit_px = round_price(raw, r["code"], r["side"])
        if dry_run:
            orders.append({**r, "status": "planned", "limit_price": limit_px,
                           "ok": None, "result_text": ""})
            continue
        res = _call_ths_trade(r["side"], r["code"], int(r["qty"]), limit_px)
        orders.append({**r, "status": "ok" if res["ok"] else "failed",
                       "limit_price": limit_px, "ok": res["ok"],
                       "result_text": res["result_text"]})
    return {"orders": orders,
            "ok_count": sum(1 for o in orders if o.get("ok") is True),
            "fail_count": sum(1 for o in orders if o.get("ok") is False)}


# ================================================================ CRUD ====

def create_portfolio(name: str, items: list[dict], note: str = "") -> dict:
    validate_portfolio(name, items)
    pf = {"id": f"pf_{dt.datetime.now():%Y%m%d%H%M%S}",
          "name": str(name).strip(),
          "items": normalize_items(items),
          "note": str(note or "").strip(),
          "history": [],
          "created_at": _now(),
          "updated_at": _now()}
    _save_portfolio(pf)
    return pf


def update_portfolio(pid: str, items: list[dict] | None = None,
                     name: str | None = None, note: str | None = None) -> dict:
    """组合调整: 增减个股 / 改权重 / 改名. 持仓不动, 下次买卖/同步按新配比."""
    pf = get_portfolio(pid)
    if pf is None:
        raise ValueError(f"组合 {pid} 不存在")
    if name is not None:
        if not str(name).strip():
            raise ValueError("组合名称不能为空")
        pf["name"] = str(name).strip()
    if note is not None:
        pf["note"] = str(note).strip()
    if items is not None:
        new_items = normalize_items(items)
        # 记录被移出的成分: 同步仓位时按 ETF 剔除成分口径整笔清仓
        old_codes = {it["code"] for it in pf["items"]}
        new_codes = {it["code"] for it in new_items}
        removed = (set(pf.get("removed_codes") or []) | (old_codes - new_codes)) - new_codes
        pf["removed_codes"] = sorted(removed)
        pf["items"] = new_items
    pf["updated_at"] = _now()
    _save_portfolio(pf)
    return pf


def delete_portfolio(pid: str) -> bool:
    pfs = load_portfolios()
    kept = [p for p in pfs if p.get("id") != pid]
    if len(kept) == len(pfs):
        return False
    save_portfolios(kept)
    return True


def main() -> None:
    """CLI 查看: uv run python trading/portfolios.py [list|show <pid>]"""
    ap_pfs = load_portfolios()
    if len(sys.argv) > 1 and sys.argv[1] == "show" and len(sys.argv) > 2:
        pf = get_portfolio(sys.argv[2])
        print(json.dumps(pf, ensure_ascii=False, indent=2) if pf
              else f"组合 {sys.argv[2]} 不存在")
        return
    for pf in ap_pfs:
        ws = " + ".join(f"{i['code']} {i['weight']:.1f}%" for i in pf["items"])
        print(f"{pf['id']}  {pf['name']}  [{ws}]  更新 {pf['updated_at']}")


if __name__ == "__main__":
    main()

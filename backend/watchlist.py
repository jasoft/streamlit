"""自选股 (watchlist) 管理层: 持仓自动同步 + 手动增删 (供 backend/main.py 调用).

设计:
- 存储 backend/watchlist.json: {"stocks": [...], "removed": [...], "auto_sync": bool}.
  stocks 每项 {symbol(sh601899), code(601899), name, source(manual|ths),
  added_at, last_seen_in_positions}.
- 同花顺持仓自动加入: FastAPI 进程内后台协程定期 (默认 120s) 跑
  `trading/ths_trade.py positions` 快速路径, 新出现的持仓代码自动入列表
  (source=ths); 同花顺未开/交易面板未开时报错静默记录在 last_sync_error.
- 手动删除的代码进 tombstone (removed), 自动同步不再回加; 手动重新添加会解除.
- symbol 归一化: 6 位纯数字按段位补 sh/sz/bj 前缀 (对齐 trading/fdata._norm_code
  并扩展 ETF/转债/北交所), 已带前缀的原样保留小写.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = Path(__file__).resolve().parent
WATCHLIST_FILE = BACKEND_ROOT / "watchlist.json"

AUTO_SYNC_INTERVAL = 120.0        # 后台持仓同步周期 (秒)
POSITIONS_TIMEOUT = 30            # ths_trade positions 子进程超时 (秒)
MAX_REMOVED = 200                 # tombstone 上限, 防止无限增长

_CODE_RE = re.compile(r"^\d{6}$")
# 持仓数量字段: 同花顺不同版本列名不同 (老版"持仓数量", 新版"实际数量"/"股票余额")
_QTY_FIELDS = ("持仓数量", "实际数量", "股票余额")

# 6 位纯数字 -> 交易所前缀. 两位前缀先判 (转债/ETF/北交所), 再判一位前缀.
_PREFIX_2 = {
    "sh": ("60", "68", "50", "51", "52", "56", "58", "11", "90"),
    "sz": ("12", "15", "16", "18"),
    "bj": ("43", "83", "87", "92"),
}
_PREFIX_1 = {"sh": ("5", "9"), "sz": ("0", "2", "3")}

_file_lock = threading.Lock()     # JSON 读写互斥 (后台线程 + API 线程)
_sync_lock = asyncio.Lock()       # 子进程同步互斥 (后台循环 + 手动同步端点)

_bg_task: Optional[asyncio.Task] = None


# ================================================================ symbol ====
def normalize_symbol(raw: str) -> tuple[str, str, str]:
    """归一化标的输入 -> (symbol, code, market).

    支持: "601899" / "sh601899" / "SZ159915" / " bj830799 " / "00700" /
    "700.hk" / "hk700" (港股通 5 位代码, 不足 5 位左侧补零) .
    期货/期权等非数字代码原样接受 (symbol=去除空白后小写, code="").
    无法判定交易所的 6 位代码 symbol=code, market="" (fdata 会自行尝试).
    """
    s = str(raw).strip().lower()
    if not s:
        raise ValueError("标的代码不能为空")
    if s.endswith(".hk"):
        s = s[:-3]
    # 显式市场前缀: hk00700 -> 港股; sh601899 / sz159915 / bj830799 -> A股
    if s.startswith("hk") and s[2:].isdigit() and 1 <= len(s) - 2 <= 5:
        code = s[2:].zfill(5)
        return "hk" + code, code, "hk"
    if s[:2] in ("sh", "sz", "bj") and _CODE_RE.match(s[2:]):
        return s, s[2:], s[:2]
    if s.isdigit():
        if len(s) <= 5:
            # 港股通: 同花顺显示 5 位代码 (00700), 短代码左侧补零
            code = s.zfill(5)
            return "hk" + code, code, "hk"
        if len(s) == 6:
            market = ""
            for mkt, pre in _PREFIX_2.items():
                if s.startswith(pre):
                    market = mkt
                    break
            if not market:
                for mkt, pre in _PREFIX_1.items():
                    if s.startswith(pre):
                        market = mkt
                        break
            return (market + s) if market else s, s, market
    # 非 A 股/港股数字代码 (期货/期权等): 原样接受, 交给下游数据源自查
    return s, "", ""


# ================================================================ storage ====
def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load() -> dict:
    with _file_lock:
        return _load_unlocked()


def _load_unlocked() -> dict:
    try:
        with open(WATCHLIST_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("stocks"), list):
            data.setdefault("removed", [])
            data.setdefault("auto_sync", True)
            return data
    except FileNotFoundError:
        pass
    except Exception:
        pass  # 损坏文件视同空列表, 下次 save 覆盖
    return {"version": 1, "auto_sync": True, "updated_at": "",
            "stocks": [], "removed": []}


def _save_unlocked(data: dict) -> None:
    data["updated_at"] = _now()
    tmp = WATCHLIST_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(WATCHLIST_FILE)  # 原子替换, 防止写一半损坏


def get_status() -> dict:
    d = load()
    return {
        "stocks": d["stocks"],
        "removed_count": len(d.get("removed", [])),
        "auto_sync": bool(d.get("auto_sync", True)),
        "last_sync": d.get("last_sync"),
        "last_sync_error": d.get("last_sync_error"),
    }


# ================================================================ CRUD ====
def add_stock(symbol: str, name: str = "", source: str = "manual") -> dict:
    """添加自选股 (已存在则补名称, added=False). 手动添加解除 tombstone."""
    sym, code, market = normalize_symbol(symbol)
    with _file_lock:
        d = _load_unlocked()
        d["removed"] = [c for c in d.get("removed", []) if c != code] if code \
            else d.get("removed", [])
        entry = None
        for st in d["stocks"]:
            if st.get("symbol") == sym or (code and st.get("code") == code):
                entry = st
                break
        added = entry is None
        if added:
            entry = {"symbol": sym, "code": code, "market": market,
                     "name": "", "source": source, "added_at": _now(),
                     "last_seen_in_positions": ""}
            d["stocks"].append(entry)
        if name and not entry.get("name"):
            entry["name"] = name
        _save_unlocked(d)
    return {**entry, "added": added}  # type: ignore[arg-type]


def remove_stock(symbol: str) -> bool:
    """删除自选股 (代码或带前缀均可). 进 tombstone, 持仓自动同步不再回加."""
    sym, code, _ = normalize_symbol(symbol)
    with _file_lock:
        d = _load_unlocked()
        kept = [st for st in d["stocks"]
                if st.get("symbol") != sym and st.get("code") != sym
                and (not code or st.get("code") != code)]
        if len(kept) == len(d["stocks"]):
            return False
        d["stocks"] = kept
        if code:
            removed = [c for c in d.get("removed", []) if c != code]
            removed.append(code)
            d["removed"] = removed[-MAX_REMOVED:]
        _save_unlocked(d)
    return True


def set_auto_sync(enabled: bool) -> bool:
    with _file_lock:
        d = _load_unlocked()
        d["auto_sync"] = bool(enabled)
        _save_unlocked(d)
    return bool(enabled)


# ============================================================ 持仓同步 ====
def _parse_qty(v) -> int:
    try:
        return int(float(str(v).replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        return 0


def _positions_from_rows(rows) -> list[dict]:
    """过滤 ths positions rows -> [{code, name}].

    接受 A股 6 位代码与港股通 5 位代码 (00700, 可能带 .HK 后缀);
    资金行 (证券代码非数字) 与清仓残留 0 持仓行跳过.
    """
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("证券代码", "")).strip().lower()
        if code.endswith(".hk"):
            code = code[:-3]
        if not code.isdigit():
            continue
        if len(code) <= 5:
            code = code.zfill(5)     # 港股通短码补零 (0700.HK -> 00700)
        elif len(code) != 6:
            continue
        qty = None
        for f in _QTY_FIELDS:
            if f in row:
                qty = _parse_qty(row[f])
                break
        if qty is not None and qty <= 0:
            continue  # 清仓后残留的 0 持仓行不入自选
        out.append({"code": code, "name": str(row.get("证券名称", "")).strip()})
    return out


def _run_ths_positions() -> list[dict]:
    """跑 ths_trade positions 子进程, 返回持仓 [{code, name}]. 失败抛 RuntimeError."""
    cmd = [sys.executable, str(REPO_ROOT / "trading" / "ths_trade.py"), "positions"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=POSITIONS_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ths_trade positions 超时 (>{POSITIONS_TIMEOUT}s), "
                           f"同花顺可能未打开")
    try:
        out = json.loads(r.stdout)
    except Exception:
        raise RuntimeError("ths_trade positions 输出无法解析为 JSON: "
                           + (r.stderr or r.stdout)[-200:])
    if not isinstance(out, dict) or not out.get("ok"):
        msg = ""
        if isinstance(out, dict):
            msg = str(out.get("error") or out.get("stderr") or "")
        raise RuntimeError(f"同花顺持仓查询失败: {msg or 'ok=False'}")
    return _positions_from_rows(out.get("rows", []))


def sync_positions() -> dict:
    """拉同花顺持仓并入自选股. 幂等: 已存在的只更新名称/最近持仓时间."""
    try:
        rows = _run_ths_positions()
    except Exception as e:
        with _file_lock:
            d = _load_unlocked()
            d["last_sync"] = _now()
            d["last_sync_error"] = str(e)
            _save_unlocked(d)
        return {"ok": False, "error": str(e), "added": [], "positions": 0}

    with _file_lock:
        d = _load_unlocked()
        existing_codes = {st.get("code") for st in d["stocks"] if st.get("code")}
        removed = set(d.get("removed", []))
        added = []
        now = _now()
        for r in rows:
            if r["code"] in existing_codes:
                for st in d["stocks"]:
                    if st.get("code") == r["code"]:
                        st["last_seen_in_positions"] = now
                        if not st.get("name") and r["name"]:
                            st["name"] = r["name"]
                continue
            if r["code"] in removed:
                continue  # 手动删除过, 不回加
            sym, code, market = normalize_symbol(r["code"])
            d["stocks"].append({
                "symbol": sym, "code": code, "market": market,
                "name": r["name"], "source": "ths",
                "added_at": now, "last_seen_in_positions": now,
            })
            added.append(sym)
        d["last_sync"] = now
        d["last_sync_error"] = ""
        _save_unlocked(d)
    return {"ok": True, "added": added, "positions": len(rows), "error": ""}


async def sync_positions_async() -> dict:
    """带互斥的异步同步 (后台循环与手动端点共用, 避免并发跑两个子进程)."""
    async with _sync_lock:
        return await asyncio.to_thread(sync_positions)


# ========================================================== 后台自动同步 ====
async def _auto_sync_loop():
    while True:
        try:
            if load().get("auto_sync", True):
                await sync_positions_async()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # 任何异常不终止循环, 错误已记录在 last_sync_error
        await asyncio.sleep(AUTO_SYNC_INTERVAL)


def startup() -> None:
    """FastAPI startup 挂载: 启动持仓自动同步后台协程."""
    global _bg_task
    if _bg_task is None or _bg_task.done():
        _bg_task = asyncio.get_running_loop().create_task(_auto_sync_loop())


def shutdown() -> None:
    global _bg_task
    if _bg_task is not None:
        _bg_task.cancel()
        _bg_task = None

"""选股自动交易系统 — SQLite 持久层 (策略组 / 买入组持仓 / 指令流水).

三张表:
- picker_groups    策略组配置: 一个"策略 ID"一行 (选股插件 + 股票池 + 参数 + 仓位约束)
- picker_positions 买入组持仓: 策略买入的股票逐笔入库, 以 strategy_id 挂钩;
                   同一 strategy_id 的 holding 行即该策略的"买入组", 不同策略组彼此独立
- picker_events    指令流水: 每次买入/卖出指令留痕 (含 dry-run 与拒单)

设计沿用 backend/store.py: stdlib sqlite3 + threading.Lock, 每次调用独立连接.
DB 路径用 __file__ 定位 (不依赖 CWD, 避免 worktree 混用), 可用 PICKER_DB_PATH 覆盖 (测试用).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
import threading
from pathlib import Path

_DB = Path(os.environ.get("PICKER_DB_PATH")
           or Path(__file__).resolve().parent / "stockpicker.db")
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS picker_groups (
    strategy_id     TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    picker          TEXT NOT NULL,               -- 选股插件 ID (trading/picker_strategies)
    universe        TEXT NOT NULL DEFAULT '[]',  -- json: 股票池 ["601899", ...]
    params          TEXT NOT NULL DEFAULT '{}',  -- json: 插件参数 (按组覆盖默认值)
    per_qty         INTEGER NOT NULL DEFAULT 0,  -- 每只买入股数 (0=按 cash_per_symbol 自动整手)
    cash_per_symbol REAL NOT NULL DEFAULT 10000.0,
    max_positions   INTEGER NOT NULL DEFAULT 0,  -- 买入组最大持仓只数 (0=不限)
    buy_scan_every  INTEGER NOT NULL DEFAULT 60, -- 每 N 轮 poll 跑一次买入选股 (卖出每轮都扫)
    t1_protect      INTEGER NOT NULL DEFAULT 1,  -- T+1: 当日买入批次当日不卖
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS picker_positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id   TEXT NOT NULL,
    code          TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    qty           INTEGER NOT NULL DEFAULT 0,
    buy_price     REAL NOT NULL DEFAULT 0,
    buy_ts        TEXT NOT NULL DEFAULT '',
    buy_order_id  TEXT NOT NULL DEFAULT '',
    buy_reason    TEXT NOT NULL DEFAULT '',      -- 选股命中原因 (插件 select 给出)
    status        TEXT NOT NULL DEFAULT 'holding',  -- holding / selling / sold
    sell_price    REAL,
    sell_ts       TEXT,
    sell_order_id TEXT,
    sell_reason   TEXT
);
-- 同一策略组同一代码同时最多一笔活动持仓 (holding/selling)
CREATE UNIQUE INDEX IF NOT EXISTS uq_picker_active
    ON picker_positions(strategy_id, code) WHERE status IN ('holding', 'selling');
CREATE TABLE IF NOT EXISTS picker_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    strategy_id TEXT NOT NULL DEFAULT '',
    code        TEXT NOT NULL DEFAULT '',
    side        TEXT NOT NULL DEFAULT '',        -- buy / sell
    qty         INTEGER NOT NULL DEFAULT 0,
    price       REAL NOT NULL DEFAULT 0,
    order_id    TEXT NOT NULL DEFAULT '',
    dry_run     INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT '',        -- filled / submitted / rejected ...
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS picker_strategies (
    id         TEXT PRIMARY KEY,                 -- 策略库 ID (用户创建或预置)
    title      TEXT NOT NULL,
    desc       TEXT NOT NULL DEFAULT '',
    buy_rules  TEXT NOT NULL DEFAULT '[]',       -- json: [{type, n, threshold, ...}]
    sell_rules TEXT NOT NULL DEFAULT '[]',
    builtin    INTEGER NOT NULL DEFAULT 0,       -- 预置策略标记 (可删除)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB), check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _row2dict(row) -> dict:
    d = dict(row)
    if isinstance(d.get("universe"), str):
        try:
            d["universe"] = json.loads(d["universe"])
        except json.JSONDecodeError:
            d["universe"] = []
    if isinstance(d.get("params"), str):
        try:
            d["params"] = json.loads(d["params"])
        except json.JSONDecodeError:
            d["params"] = {}
    return d


# ---------------------------------------------------------------- 策略组 CRUD ----
def upsert_group(g: dict) -> dict:
    """新增/更新策略组配置 (universe/params 为 list/dict, 这里 json 编码入库)."""
    now = _now()
    with _lock:
        c = _conn()
        try:
            c.execute(
                """INSERT INTO picker_groups (strategy_id, title, picker, universe,
                       params, per_qty, cash_per_symbol, max_positions,
                       buy_scan_every, t1_protect, enabled, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(strategy_id) DO UPDATE SET
                       title=excluded.title, picker=excluded.picker,
                       universe=excluded.universe, params=excluded.params,
                       per_qty=excluded.per_qty, cash_per_symbol=excluded.cash_per_symbol,
                       max_positions=excluded.max_positions,
                       buy_scan_every=excluded.buy_scan_every,
                       t1_protect=excluded.t1_protect, enabled=excluded.enabled,
                       updated_at=excluded.updated_at""",
                (g["strategy_id"], g.get("title", ""), g["picker"],
                 json.dumps(g.get("universe") or [], ensure_ascii=False),
                 json.dumps(g.get("params") or {}, ensure_ascii=False),
                 int(g.get("per_qty") or 0), float(g.get("cash_per_symbol") or 10000.0),
                 int(g.get("max_positions") or 0), int(g.get("buy_scan_every") or 60),
                 1 if g.get("t1_protect", True) else 0,
                 1 if g.get("enabled", True) else 0, g.get("created_at") or now, now))
            c.commit()
        finally:
            c.close()
    return get_group(g["strategy_id"])  # type: ignore[return-value]


def list_groups() -> list[dict]:
    with _lock:
        c = _conn()
        try:
            rows = c.execute(
                "SELECT * FROM picker_groups ORDER BY created_at").fetchall()
        finally:
            c.close()
    return [_row2dict(r) for r in rows]


def get_group(gid: str) -> dict | None:
    with _lock:
        c = _conn()
        try:
            r = c.execute("SELECT * FROM picker_groups WHERE strategy_id=?",
                          (gid,)).fetchone()
        finally:
            c.close()
    return _row2dict(r) if r else None


def delete_group(gid: str) -> bool:
    """删除策略组配置. 持仓/流水保留 (审计用), 只是不再扫描."""
    with _lock:
        c = _conn()
        try:
            cur = c.execute("DELETE FROM picker_groups WHERE strategy_id=?", (gid,))
            c.commit()
            return cur.rowcount > 0
        finally:
            c.close()


# ---------------------------------------------------------------- 买入组持仓 ----
def insert_position(strategy_id: str, code: str, name: str, qty: int,
                    buy_price: float, buy_ts: str, buy_order_id: str = "",
                    buy_reason: str = "") -> int:
    """买入成交 -> 计入该策略的买入组. 同组同码已有活动持仓时抛 ValueError."""
    with _lock:
        c = _conn()
        try:
            cur = c.execute(
                """INSERT INTO picker_positions
                       (strategy_id, code, name, qty, buy_price, buy_ts,
                        buy_order_id, buy_reason, status)
                   VALUES (?,?,?,?,?,?,?,?, 'holding')""",
                (strategy_id, code, name, int(qty), float(buy_price), buy_ts,
                 buy_order_id, buy_reason))
            c.commit()
            return int(cur.lastrowid or 0)
        except sqlite3.IntegrityError as e:
            raise ValueError(
                f"策略组 {strategy_id} 已持有 {code} (同组同码仅一笔活动持仓)") from e
        finally:
            c.close()


def mark_selling(pid: int, sell_order_id: str, sell_reason: str = "") -> None:
    """实盘卖出指令已提交未回报: 持仓转 selling, 等后续轮询推进."""
    with _lock:
        c = _conn()
        try:
            c.execute("UPDATE picker_positions SET status='selling', sell_order_id=?, "
                      "sell_reason=? WHERE id=?", (sell_order_id, sell_reason, pid))
            c.commit()
        finally:
            c.close()


def revert_selling(pid: int) -> None:
    """卖出被拒/撤销: 持仓回 holding (仍在买入组, 下一轮继续判定)."""
    with _lock:
        c = _conn()
        try:
            c.execute("UPDATE picker_positions SET status='holding', sell_order_id='', "
                      "sell_reason='' WHERE id=?", (pid,))
            c.commit()
        finally:
            c.close()


def close_position(pid: int, sell_price: float, sell_ts: str,
                   sell_order_id: str = "", sell_reason: str = "") -> None:
    """卖出成交 -> 移出买入组 (行保留为 sold 历史)."""
    with _lock:
        c = _conn()
        try:
            c.execute("UPDATE picker_positions SET status='sold', sell_price=?, "
                      "sell_ts=?, sell_order_id=?, sell_reason=? WHERE id=?",
                      (float(sell_price), sell_ts, sell_order_id, sell_reason, pid))
            c.commit()
        finally:
            c.close()


def list_positions(strategy_id: str | None = None,
                   status: str | None = None, limit: int = 0) -> list[dict]:
    q = "SELECT * FROM picker_positions WHERE 1=1"
    args: list = []
    if strategy_id is not None:
        q += " AND strategy_id=?"
        args.append(strategy_id)
    if status is not None:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY id DESC"
    if limit > 0:
        q += f" LIMIT {int(limit)}"
    with _lock:
        c = _conn()
        try:
            rows = c.execute(q, args).fetchall()
        finally:
            c.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 指令流水 ----
def add_event(strategy_id: str, code: str, side: str, qty: int, price: float,
              order_id: str = "", dry_run: bool = True, status: str = "",
              detail: str = "") -> None:
    with _lock:
        c = _conn()
        try:
            c.execute(
                "INSERT INTO picker_events (ts, strategy_id, code, side, qty, price,"
                " order_id, dry_run, status, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (_now(), strategy_id, code, side, int(qty), float(price),
                 order_id, 1 if dry_run else 0, status, detail))
            c.commit()
        finally:
            c.close()


def list_events(strategy_id: str | None = None, limit: int = 100) -> list[dict]:
    q = "SELECT * FROM picker_events WHERE 1=1"
    args: list = []
    if strategy_id is not None:
        q += " AND strategy_id=?"
        args.append(strategy_id)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with _lock:
        c = _conn()
        try:
            rows = c.execute(q, args).fetchall()
        finally:
            c.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 策略库 ----
# 预置策略 (规则化改写自原代码插件; 表为空时自动播种, 可删除后自建)
_PRESETS = [
    {
        "id": "preset_rsi_rebound", "title": "超跌反弹 (RSI 超卖)", "builtin": 1,
        "desc": "RSI6 深度超卖 + 放量承接买入; RSI 修复 / 止盈 / 止损卖出",
        "buy_rules": [{"type": "rsi_below", "n": 6, "threshold": 25},
                      {"type": "vol_ratio_above", "days": 5, "ratio": 1.5}],
        "sell_rules": [{"type": "take_profit", "pct": 10},
                       {"type": "stop_loss", "pct": -5},
                       {"type": "rsi_above", "n": 6, "threshold": 55}],
    },
    {
        "id": "preset_volume_breakout", "title": "放量突破", "builtin": 1,
        "desc": "收盘突破近20日高点且放量买入; 跌破近5日低点 / 止盈 / 止损卖出",
        "buy_rules": [{"type": "breakout_high", "days": 20},
                      {"type": "vol_ratio_above", "days": 5, "ratio": 1.8},
                      {"type": "pct_change_below", "pct": 7}],
        "sell_rules": [{"type": "take_profit", "pct": 15},
                       {"type": "stop_loss", "pct": -4},
                       {"type": "breakdown_low", "days": 5}],
    },
]


def ensure_seeded() -> None:
    """策略库为空时播种预置策略 (用户删光后重启会再播种, 想彻底清空可自行建一条)."""
    with _lock:
        c = _conn()
        try:
            n = c.execute("SELECT COUNT(*) FROM picker_strategies").fetchone()[0]
        finally:
            c.close()
    if n == 0:
        now = _now()
        for p in _PRESETS:
            upsert_strategy({**p, "created_at": now, "updated_at": now})


def _srow2dict(r) -> dict:
    d = dict(r)
    for k in ("buy_rules", "sell_rules"):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                d[k] = []
    d["builtin"] = bool(d.get("builtin"))
    return d


def upsert_strategy(s: dict) -> dict:
    now = _now()
    with _lock:
        c = _conn()
        try:
            c.execute(
                """INSERT INTO picker_strategies (id, title, desc, buy_rules,
                       sell_rules, builtin, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       title=excluded.title, desc=excluded.desc,
                       buy_rules=excluded.buy_rules, sell_rules=excluded.sell_rules,
                       builtin=excluded.builtin, updated_at=excluded.updated_at""",
                (s["id"], s.get("title", ""), s.get("desc", ""),
                 json.dumps(s.get("buy_rules") or [], ensure_ascii=False),
                 json.dumps(s.get("sell_rules") or [], ensure_ascii=False),
                 1 if s.get("builtin") else 0, s.get("created_at") or now, now))
            c.commit()
        finally:
            c.close()
    return get_strategy(s["id"])  # type: ignore[return-value]


def list_strategies() -> list[dict]:
    ensure_seeded()
    with _lock:
        c = _conn()
        try:
            rows = c.execute("SELECT * FROM picker_strategies ORDER BY created_at").fetchall()
        finally:
            c.close()
    return [_srow2dict(r) for r in rows]


def get_strategy(sid: str) -> dict | None:
    with _lock:
        c = _conn()
        try:
            r = c.execute("SELECT * FROM picker_strategies WHERE id=?", (sid,)).fetchone()
        finally:
            c.close()
    return _srow2dict(r) if r else None


def delete_strategy(sid: str) -> bool:
    with _lock:
        c = _conn()
        try:
            cur = c.execute("DELETE FROM picker_strategies WHERE id=?", (sid,))
            c.commit()
            return cur.rowcount > 0
        finally:
            c.close()

"""图表会话元数据持久化 (SQLite).

存的是"图会话"的轻量元数据 (id/symbol/tf/strategy/params/schema/mode 等),
bars/markers 等重料不存 — 刷新后按 symbol+tf 重新拉取.

跨浏览器/跨设备共享: 数据落在后端本地 charts.db, 不再依赖 localStorage.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import threading
from pathlib import Path

_DB = Path(__file__).resolve().parent / "charts.db"
_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(str(_DB), check_same_thread=False)
    c.execute(
        """CREATE TABLE IF NOT EXISTS chart_sessions (
               id         TEXT PRIMARY KEY,
               data       TEXT NOT NULL,
               updated_at TEXT NOT NULL
           )"""
    )
    return c


def load_sessions() -> list:
    """返回全部图会话元数据 (list[dict])."""
    with _lock:
        c = _conn()
        try:
            rows = c.execute("SELECT data FROM chart_sessions ORDER BY updated_at").fetchall()
        finally:
            c.close()
    out = []
    for (data,) in rows:
        try:
            out.append(json.loads(data))
        except Exception:  # noqa: BLE001 坏记录跳过
            continue
    return out


def save_sessions(metas: list) -> None:
    """整表替换图会话元数据 (与前端单一来源的图列表保持一致)."""
    now = _dt.datetime.now().isoformat(timespec="seconds")
    with _lock:
        c = _conn()
        try:
            c.execute("DELETE FROM chart_sessions")
            for m in metas:
                id_ = m.get("id") or ""
                if not id_:
                    continue
                c.execute(
                    "INSERT OR REPLACE INTO chart_sessions (id, data, updated_at) VALUES (?, ?, ?)",
                    (id_, json.dumps(m), now),
                )
            c.commit()
        finally:
            c.close()
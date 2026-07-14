import sqlite3
from datetime import datetime

DB_PATH = "market_data.db"

def get_connection():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    # 市场概览表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME,
            total_amount REAL,
            up_ratio REAL,
            limit_up_count INTEGER,
            limit_down_count INTEGER,
            median_change REAL,
            crowding_score REAL
        )
    """)
    # 指数分布表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS index_distribution (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME,
            index_code TEXT,
            index_name TEXT,
            amount_ratio REAL,
            change_pp REAL
        )
    """)
    # 用于绘图的分时曲线表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS intraday_curve (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME,
            metric_name TEXT,
            value REAL
        )
    """)
    conn.commit()
    conn.close()

def save_snapshot(data: dict):
    conn = get_connection()
    cursor = conn.cursor()
    ts = datetime.now()

    # 保存快照
    cursor.execute("""
        INSERT INTO market_snapshots
        (timestamp, total_amount, up_ratio, limit_up_count, limit_down_count, median_change, crowding_score)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (ts, data['total_amount'], data['up_ratio'], data['limit_up_count'], data['limit_down_count'], data['median_change'], data['crowding_score']))

    # 保存指数分布
    for idx_data in data['index_distribution']:
        cursor.execute("""
            INSERT INTO index_distribution (timestamp, index_code, index_name, amount_ratio, change_pp)
            VALUES (?, ?, ?, ?, ?)
        """, (ts, idx_data['code'], idx_data['name'], idx_data['amount_ratio'], idx_data.get('change_pp', 0)))

    # 保存分时曲线点（每个指标每分钟一个点）
    cursor.execute("DELETE FROM intraday_curve WHERE timestamp = ?", (ts,))
    for metric_name, value in data.get("intraday", {}).items():
        if value is None:
            continue
        cursor.execute("""
            INSERT INTO intraday_curve (timestamp, metric_name, value)
            VALUES (?, ?, ?)
        """, (ts, metric_name, value))

    conn.commit()
    conn.close()


def get_history(table="market_snapshots", limit=100):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"SELECT * FROM {table} ORDER BY timestamp DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_intraday_curve(metric_name: str, limit: int = 330) -> list:
    """获取指定指标的分时曲线（今日）"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT timestamp, value FROM intraday_curve WHERE metric_name = ? ORDER BY timestamp DESC LIMIT ?",
        (metric_name, limit)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows[::-1]  # 反转为时间正序


def get_snapshot_history(metric_name: str, days: int = 20) -> list:
    """获取最近 N 天的快照指标历史"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT timestamp, {metric_name} FROM market_snapshots ORDER BY timestamp DESC LIMIT ?",
        (days * 240,)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows[::-1]

init_db()

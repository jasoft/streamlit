"""实盘执行层: 策略信号 -> 份数计算 -> scripts/ths_trade.py 下单 -> state.json 记账.

策略应有的仓位 (target 0/1 + 份数) 记在 state/{name}.state.json,
与同花顺实际持仓 (ths_trade positions) 只做人工对账展示, 不自动纠偏.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from strategy import config as config_mod  # noqa: E402
from strategy import registry  # noqa: E402
from strategy.engine import LOT, today_target  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
THS_TRADE = REPO_ROOT / "scripts" / "ths_trade.py"
STATE_DIR = Path(__file__).resolve().parent / "state"


def _fetch(code: str, qfq: bool, timeframe: str = "day", limit: int | None = None) -> pd.DataFrame:
    """timeframe: "day" (tdx_source/fdata qfq) 或 "5m"/"30m" 等 (fdata, 不复权)."""
    if timeframe == "day":
        if qfq:
            r = subprocess.run(
                [sys.executable, str(REPO_ROOT / "scripts" / "fdata.py"),
                 "kline", code, "--adjust", "qfq", "--limit", "120"],
                capture_output=True, text=True, timeout=120, check=True)
            rows = json.loads(r.stdout)["data"]
            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
            return df[["date", "open", "high", "low", "close", "volume"]]
        from stockview.tdx_source import fetch_etf_daily
        return fetch_etf_daily(code)
    # eltdx 分页上限 800, 大 limit 会报错: 全量拉取后本地截断
    args = [sys.executable, str(REPO_ROOT / "scripts" / "fdata.py"),
            "kline", code, "--period", timeframe, "--limit", "0"]
    r = subprocess.run(args, capture_output=True, text=True, timeout=180, check=True)
    rows = json.loads(r.stdout)["data"]
    if limit is not None:
        rows = rows[-limit:]
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    return df[["date", "open", "high", "low", "close", "volume"]]


def qty_for(cash: float, price: float) -> int:
    """固定金额 -> 整手份数 (向下取整到 100 份)."""
    return int(cash // price // LOT * LOT)


def fetch_intraday(symbol: str, period: str = "5m", limit: int = 80) -> pd.DataFrame:
    """日内分钟K线 (fdata/eltdx, 盘中含当日实时bar)."""
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "fdata.py"),
         "kline", symbol, "--period", period, "--limit", str(limit)],
        capture_output=True, text=True, timeout=120, check=True)
    rows = json.loads(r.stdout)["data"]
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    return df[["date", "open", "high", "low", "close", "volume"]]


def evaluate(name: str, cfg: dict) -> list:
    """盘中实时评估: 每标的用最新数据(含当日实时bar)算目标仓位, 判断是否触发警报.

    每次取数调用一次, 结果写入 evals 日志 (state/{name}.evals.jsonl).
    """
    import datetime as dt
    strat = registry.get(name)
    state = load_state(name)
    display_w = int(cfg["params"].get("window", cfg["params"].get("slow", 20)))
    tf = getattr(strat, "TIMEFRAME", "day")
    out = []
    for symbol in cfg["symbols"]:
        df = _fetch(symbol, cfg["live"]["qfq"], tf, limit=3000)
        target_series = strat.target_position(df, cfg["params"])
        tgt = int(pd.Series(target_series).fillna(0).astype(int).iloc[-1])
        price = float(df["close"].iloc[-1])
        ma = (round(float(df["close"].rolling(display_w).mean().iloc[-1]), 3)
              if len(df) >= display_w else None)
        cur_t = state.get(symbol, {}).get("target", 0)
        alert = tgt != cur_t
        action = {1: "买入", 0: "卖出"}.get(tgt, "?")
        out.append({
            "ts": dt.datetime.now().isoformat(timespec="seconds"),
            "symbol": symbol, "price": round(price, 3), f"ma{display_w}": ma,
            "target": tgt, "alert": alert,
            "msg": f"⚠️ 触发{action}信号" if alert else "未触发",
        })
    return out


def append_evals(name: str, results: list) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    path = STATE_DIR / f"{name}.evals.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_evals(name: str, tail: int = 30) -> list:
    path = STATE_DIR / f"{name}.evals.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(l) for l in lines[-tail:]]


def six_digit(symbol: str) -> str:
    """sz159915 -> 159915 (ths_trade 用 6 位代码)."""
    return symbol[-6:]


def load_state(name: str) -> dict:
    path = STATE_DIR / f"{name}.state.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_state(name: str, state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    path = STATE_DIR / f"{name}.state.json"
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def compute_signal(name: str, symbols: list, params: dict, qfq: bool = False) -> list:
    """对每标的计算最新目标仓位. 返回 [{symbol, date, close, target}, ...]."""
    strat = registry.get(name)
    tf = getattr(strat, "TIMEFRAME", "day")
    out = []
    for symbol in symbols:
        df = _fetch(symbol, qfq, tf, limit=3000)
        target = strat.target_position(df, params)
        sig = today_target(df, target)
        sig["symbol"] = symbol
        out.append(sig)
    return out


def plan_orders(signals: list, cash_per_symbol: float, current: dict) -> list:
    """对比当前应有仓位, 生成需要下发的订单 (无变化不下单)."""
    orders = []
    for sig in signals:
        symbol, target, price = sig["symbol"], sig["target"], sig["close"]
        cur = current.get(symbol, {"target": 0, "qty": 0})
        if target == cur.get("target", 0):
            continue
        if target == 1:
            qty = qty_for(cash_per_symbol, price)
            if qty >= LOT:
                orders.append({"symbol": symbol, "action": "buy", "qty": qty,
                               "price": price, "date": sig["date"]})
        elif cur.get("qty", 0) > 0:
            orders.append({"symbol": symbol, "action": "sell",
                           "qty": cur["qty"], "price": price, "date": sig["date"]})
        else:  # target 0 且无持仓记录: 只更新目标, 不下卖单
            pass
    return orders


def execute(order: dict, dry_run: bool = True, timeout: float = 120.0) -> dict:
    """调 scripts/ths_trade.py 下单. dry_run=True 只填单不提交."""
    cmd = [sys.executable, str(THS_TRADE), order["action"],
           six_digit(order["symbol"]), str(order["qty"])]
    if dry_run:
        cmd.append("--dry-run")
    t0 = time.perf_counter()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        ok = r.returncode == 0
        out, err = r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        ok, out, err = False, "", f"timeout: {e}"
    return {"ok": ok, "cmd": cmd, "stdout": out[-2000:], "stderr": err[-500:],
            "elapsed_s": round(time.perf_counter() - t0, 1)}


def record(name: str, orders: list, dry_run: bool) -> None:
    """把已执行订单落到 state (dry-run 也记账, 标记 dry)."""
    state = load_state(name)
    for o in orders:
        state[o["symbol"]] = {
            "target": 1 if o["action"] == "buy" else 0,
            "qty": o["qty"] if o["action"] == "buy" else 0,
            "date": o["date"], "dry": dry_run,
            "price": o["price"],
        }
    save_state(name, state)


def run_once(name: str, cfg: dict, dry_run: bool | None = None) -> dict:
    """单策略跑一轮: 信号 -> 计划 -> 执行 -> 记账. 返回执行摘要."""
    live = cfg["live"]
    dry = live["dry_run"] if dry_run is None else dry_run
    summary = {"strategy": name, "signals": [], "orders": [], "executed": []}

    signals = compute_signal(name, cfg["symbols"], cfg["params"], live["qfq"])
    for s in signals:
        summary["signals"].append({
            "symbol": s["symbol"], "date": s["date"][:10],
            "close": s["close"], "target": s["target"]})
    orders = plan_orders(signals, cfg["cash_per_symbol"], load_state(name))
    summary["orders"] = orders
    for o in orders:
        res = execute(o, dry_run=dry)
        if res["ok"]:
            record(name, [o], dry_run=dry)
        summary["executed"].append({**o, "ok": res["ok"],
                                    "msg": (res["stdout"] or res["stderr"])[-300:]})
    return summary

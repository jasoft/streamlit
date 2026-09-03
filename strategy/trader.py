"""实盘执行层: 策略信号 -> 份数计算 -> scripts/ths_trade.py 下单 -> state.json 记账.

策略应有的仓位 (target 0/1 + 份数) 记在 state/{name}.state.json,
与同花顺实际持仓 (ths_trade positions) 只做人工对账展示, 不自动纠偏.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from strategy import config as config_mod  # noqa: E402
from strategy import fdata_client  # noqa: E402
from strategy import registry  # noqa: E402
from strategy.engine import LOT, today_target  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
THS_TRADE = REPO_ROOT / "trading" / "ths_trade.py"
STATE_DIR = Path(__file__).resolve().parent / "state"


def fetch_intraday_1m(symbol: str) -> tuple[pd.DataFrame, float]:
    """获取当日 (或最近一个完整交易日) 1m K 线 + 对应昨收价.

    行为:
      1) 取最近 560 根 1m bars (够装 2 个交易日 480 根 + 余量)
      2) 若当日 bars ≥ 180 根 → 用当日数据 (盘中/收盘中后段)
      3) 否则 → 回退到"上一个有 ≥200 根 bars 的完整交易日"(盘后/隔夜场景)
      4) pre_close 以"目标交易日首根 bar 日期的前一交易日收盘价"为准:
         - 若仍有前一交易日 bars, 取其最后 close
         - 否则回退到 snapshot.pre_close_price (足够稳)

    Returns:
        (df, pre_close) — df 列 [time, open, high, low, close, volume_lots, amount]
        volume 单位手, amount 元.
    """
    if os.environ.get("MOCK_MARKET") == "1":
        from strategy.mock_market import fetch_intraday_1m_mock
        return fetch_intraday_1m_mock(symbol)
    # 统一走 fdata (serve 长连接, 失败回退 CLI), 复用连接减少初始化开销
    bars = fdata_client.kline(symbol, period="1m", kind="auto", limit=640)
    snap = fdata_client.quote(symbol)
    snap_pre_close = float(snap["pre_close"]) if snap and snap.get("pre_close") else 0.0

    rows = []
    for b in bars:
        t = pd.to_datetime(b["date"]).tz_localize(None)
        rows.append({
            "time": t,
            "open": float(b["open"]), "high": float(b["high"]),
            "low": float(b["low"]), "close": float(b["close"]),
            "volume_lots": float(b["volume"]), "amount": float(b["amount"]),
        })
    df_all = pd.DataFrame(rows)
    if df_all.empty:
        return df_all, snap_pre_close
    df_all = df_all.sort_values("time").reset_index(drop=True)

    # 交易日归组: 期货夜盘 (>=20:00) 与周末凌晨尾盘归入下一个工作日,
    # 使夜盘 21:00 -> 次日 02:30 与日盘合成同一"交易日" (同花顺分时口径).
    # 股票/ETF 没有 20:00+ 或周末 bar, 映射为恒等, 行为不变.
    def _trading_day(ts):
        d = ts.date()
        if ts.hour >= 20 or d.weekday() >= 5:
            d += _dt.timedelta(days=1)
            while d.weekday() >= 5:
                d += _dt.timedelta(days=1)
        return d

    df_all["td"] = df_all["time"].map(_trading_day)
    by_day = df_all.groupby("td")

    # 目标交易日: 夜盘时段 (>=20:00) 与凌晨尾盘 (<03:00) 取正在进行中的交易日,
    # 其余时间取自然日今天; 不在分组里 (盘后/周末/停牌) 回退上一完整交易日
    now = _dt.datetime.now()
    if now.hour >= 20 or now.hour < 3:
        target_day = _trading_day(now)
    else:
        target_day = now.date()
    if target_day not in by_day.groups or len(by_day.get_group(target_day)) == 0:
        # 选上一个有 ≥200 根 bars 的完整交易日
        days_sorted = sorted(by_day.groups.keys(), reverse=True)
        fallback = None
        for d in days_sorted:
            if d == target_day:
                continue
            if len(by_day.get_group(d)) >= 200:
                fallback = d
                break
        if fallback is None:
            # 找不到完整日, 退而求其次: 选 bars 数最多的那天
            best_day = None
            best_len = 0
            for d in days_sorted:
                if d == target_day:
                    continue
                if len(by_day.get_group(d)) > best_len:
                    best_len = len(by_day.get_group(d))
                    best_day = d
            fallback = best_day
        target_day = fallback

    df = by_day.get_group(target_day).reset_index(drop=True) if target_day in by_day.groups else df_all.iloc[0:0]

    # pre_close: 取目标交易日之前的最后一根 bar 的 close (含其夜盘),
    # 没有则用快照 pre_close 兜底
    pre_close = snap_pre_close
    if target_day is not None:
        prior = df_all[df_all["td"] < target_day]
        if not prior.empty:
            pre_close = float(prior["close"].iloc[-1])
    return df, pre_close


def fetch_quote_snapshot(symbol: str) -> dict:
    """获取实时快照 (last_price, pre_close, total_hand, amount, high, low, open).

    环境变量 MOCK_MARKET=1 时改走模拟数据源 (休市测试用).
    """
    if os.environ.get("MOCK_MARKET") == "1":
        from strategy.mock_market import fetch_quote_snapshot_mock
        return fetch_quote_snapshot_mock(symbol)
    q = fdata_client.quote(symbol)
    if q is None:
        raise RuntimeError(f"quote 数据缺失: {symbol}")
    return {
        "name": q.get("name"),
        "last": q.get("last"),
        "pre_close": q.get("pre_close"),
        "open": q.get("open"),
        "high": q.get("high"),
        "low": q.get("low"),
        "total_hand": q.get("volume"),
        "amount": q.get("amount"),
        "change_pct": q.get("change_pct"),
    }


_NUMERIC_COLS = ["open", "high", "low", "close", "volume", "amount"]


def _coerce_numeric(df: pd.DataFrame) -> pd.DataFrame:
    """统一把 OHLCV/amount 强转为 float64, 无效值 → NaN.

    fdata/eltdx 分钟线返回的字段偶发是 str/None/Decimal 等 object dtype,
    导致 pandas groupby().cumsum() / .transform() 抛 "cumsum is not supported
    for object dtype". 在数据出口统一强转, 所有策略/回测链路受益.
    """
    for c in _NUMERIC_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _fetch(code: str, qfq: bool, timeframe: str = "day", limit: int | None = None) -> pd.DataFrame:
    """timeframe: day/5m/15m/30m/1h/1w (fdata). 1h->60m, 1w->week.

    环境变量 MOCK_MARKET=1 时改走模拟数据源 (休市测试用, 日线最后一根用 mock 实时价).
    """
    if os.environ.get("MOCK_MARKET") == "1":
        from strategy.mock_market import fetch_daily_mock
        df = fetch_daily_mock(code, limit or 3000)
        return _coerce_numeric(df)
    # 周期映射到 fdata 的 period 参数
    # "分时" 是前端分时图的标识, 回测/K线时按 1m 数据处理
    tf_map = {"1h": "60m", "1w": "week", "1d": "day", "分时": "1m"}
    period = tf_map.get(timeframe, timeframe)
    # 统一走 fdata kline (serve 或 CLI 回退), 全量拉取后本地截断
    rows = fdata_client.kline(code, period=period, kind="auto",
                              adjust="qfq" if qfq else None, limit=None)
    if limit is not None:
        rows = rows[-limit:]
    if not rows:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    if "date" not in df.columns:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df = _coerce_numeric(df)
    return df[["date", "open", "high", "low", "close", "volume"]]


def qty_for(cash: float, price: float) -> int:
    """固定金额 -> 整手份数 (向下取整到 100 份)."""
    return int(cash // price // LOT * LOT)


def fetch_intraday(symbol: str, period: str = "5m", limit: int = 80) -> pd.DataFrame:
    """日内分钟K线 (统一走 fdata, 盘中含当日实时bar)."""
    rows = fdata_client.kline(symbol, period=period, kind="auto", limit=limit)
    if not rows:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    if "date" not in df.columns:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df = _coerce_numeric(df)
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
        target_series = strat.signal(df, cfg["params"])
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
        target = strat.signal(df, params)
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
        out, err = r.stdout, r.stderr
        # 先按退出码判断, 再解析 ths_trade.py 输出的 JSON 中的 ok 字段
        ok = r.returncode == 0
        if ok and out.strip():
            try:
                data = json.loads(out.strip().splitlines()[-1])
                ok = bool(data.get("ok", False))
                # 如果 result_text 含错误信息但 ok 没被 ths_trade.py 识别, 再次兜底
                rt = str(data.get("result_text", "") or "")
                if ok and any(kw in rt for kw in
                              ("警告", "错误", "失败", "关闭", "非交易", "禁止",
                               "无效", "不足", "超过", "不允许", "拒绝")):
                    ok = False
            except (json.JSONDecodeError, IndexError):
                pass
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
        # dry-run 模式下始终记账 (即使同花顺客户端没打开, 也先记 dry 状态, 便于链路测试)
        # 真实模式只在执行成功后记账
        if dry or res["ok"]:
            record(name, [o], dry_run=dry)
        summary["executed"].append({**o, "ok": res["ok"],
                                    "msg": (res["stdout"] or res["stderr"])[-300:]})
    return summary


async def run_once_via_runner(name: str, cfg: dict, mode: str = "live",
                              dry_run: bool | None = None) -> dict:
    """用新 Runner 事件驱动跑一轮 (单标的, 新链路入口).

    流程: Runner.run_live(once=True) -> on_bar -> signal -> submit_order -> broker.
    与旧 run_once 并存: 旧版供 strategy/runner.py 常驻循环用, 本函数供新前端/新链路.

    mode: backtest/paper/live. dry_run 仅 live 模式生效 (paper 用 SimulatedBroker).
    """
    from strategy.runtime.runner import Runner
    strat = registry.get(name)
    live = cfg["live"]
    dry = live["dry_run"] if dry_run is None else dry_run
    symbols = cfg.get("symbols") or strat.SYMBOLS
    if not symbols:
        return {"error": "no symbol"}
    symbol = symbols[0]
    runner = Runner(
        strategy=strat, symbol=symbol, params=cfg["params"],
        mode=mode, cash=cfg.get("cash_per_symbol", 10000) * 10,
        cash_per_symbol=cfg.get("cash_per_symbol", 10000),
        qfq=live.get("qfq", False), dry_run=dry,
        fetch_fn=_fetch, poll_seconds=int(live.get("poll_seconds", 5)),
    )
    return await runner.run_live(once=True)

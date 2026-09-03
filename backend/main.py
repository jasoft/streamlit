"""量化交易后端 — FastAPI REST + WebSocket.

启动 (项目根目录):
  uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

API 设计:
  REST:
    GET  /api/strategies                 策略列表 + 运行状态
    GET  /api/config                     当前 config.json
    PUT  /api/config                     更新 config.json
    POST /api/backtest                   跑回测, 返回 {stats, df, equity, markers}
    POST /api/strategies/{name}/start
    POST /api/strategies/{name}/stop
    POST /api/strategies/{name}/run-once  dry-run 执行一轮
    GET  /api/evals/{name}               最近 N 条评估记录
    GET  /api/positions                  同花顺实际持仓 (subprocess ths_trade)
    GET  /api/quote/{symbol}             实时快照
  WebSocket:
    WS /ws/market?symbols=sz159915,sh510300
      每秒推送 tick: {"type":"tick", "data": {...}}
      前端 ECharts appendData 增量更新, 无闪烁
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import sys
import subprocess
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from strategy import config as config_mod  # noqa: E402
from strategy import manager, registry, trader  # noqa: E402
from strategy.backtest import vbt_adapter  # noqa: E402
from strategy.fdata_client import is_server_available  # noqa: E402
from strategy.trader import _fetch  # noqa: E402
from strategy.trader import fetch_intraday_1m, fetch_quote_snapshot  # noqa: E402
from strategy.mock_market import advance_bar as mock_advance_bar  # noqa: E402
from strategy.mock_market import get_session_bars as mock_get_session_bars  # noqa: E402
from backend import store as chart_store  # noqa: E402

app = FastAPI(title="量化交易后端", version="0.1.0")

# 前端 Next.js 在 localhost:3000, 允许跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ================================================================ 工具 ====
def _jsonify(obj):
    """把 pandas/numpy 对象转成 JSON 兼容结构.

    关键: 不仅要处理 np.floating, 还要处理 Python 原生 float(nan/inf),
    否则 FastAPI JSONResponse 内部 json.dumps -> "Out of range float values are not JSON compliant".
    """
    import math as _math
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return None if (np.isnan(obj) or np.isinf(obj)) else float(obj)
    if isinstance(obj, float):
        return None if (_math.isnan(obj) or _math.isinf(obj)) else obj
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, pd.Timestamp):
        return obj.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(obj, pd.Series):
        return [{"time": _jsonify(idx), "value": _jsonify(val)} for idx, val in obj.items()]
    if isinstance(obj, pd.DataFrame):
        return obj.to_dict(orient="records")
    if isinstance(obj, dict):
        return {k: _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonify(v) for v in obj]
    return obj


# ================================================================ REST ====
@app.get("/api/health")
def health():
    return {"ok": True, "time": _dt.datetime.now().isoformat()}


# --- 图会话持久化 (SQLite, 跨浏览器/设备共享) ---
@app.get("/api/charts/sessions")
def get_chart_sessions():
    return {"sessions": chart_store.load_sessions()}


@app.put("/api/charts/sessions")
def put_chart_sessions(sessions: list[dict] = Body(...)):
    chart_store.save_sessions(sessions)
    return {"ok": True, "count": len(sessions)}


@app.get("/api/strategies")
def list_strategies():
    """策略列表 + 运行状态, 合并 config.json 参数."""
    strats = registry.discover()
    cfg = config_mod.load(strats)["strategies"]
    rows = manager.status()
    out = []
    for row in rows:
        name = row["name"]
        scfg = cfg.get(name, {})
        out.append({
            "name": name,
            "title": row["title"],
            "enabled": scfg.get("enabled", False),
            "running": row["running"],
            "pid": row["pid"],
            "symbols": scfg.get("symbols", []),
            "params": scfg.get("params", {}),
            "live": scfg.get("live", {}),
            "status": row["status"],
            "next_run": row["next_run"],
            "last_run": row["last_run"],
        })
    return out


@app.get("/api/config")
def get_config():
    strats = registry.discover()
    return config_mod.load(strats)


@app.put("/api/config")
def put_config(body: dict):
    """整体替换 config.json (前端传回完整结构)."""
    registry.discover()
    config_mod.save(body)
    return {"ok": True, "msg": "已保存 config.json"}


# --- 回测 ---
class BacktestReq(BaseModel):
    strategy: str
    symbols: list[str] = []
    symbol: str = ""           # 单标的 (图会话挂载策略用)
    params: dict
    tf: str = ""               # 指定周期 (空则用策略默认)
    qfq: bool = False
    cash: float = 100_000


@app.post("/api/backtest")
async def run_backtest(req: BacktestReq):
    """vectorbt 回测: signal -> entries/exits -> vbt.Portfolio. 次日开盘口径.

    兼容两种调用:
    - symbols=[...] (多标的, 批量回测)
    - symbol="sz159915" + tf="5m" (单标的, 图会话挂载策略按当前周期回测)
    """
    strat = registry.get(req.strategy)
    tf = req.tf or getattr(strat, "TIMEFRAME", "day")
    syms = req.symbols or ([req.symbol] if req.symbol else [])
    out = {}
    for symbol in syms:
        df = await asyncio.to_thread(_fetch, symbol, req.qfq, tf, 3000)
        r = await asyncio.to_thread(
            vbt_adapter.backtest, strat, df, req.params, req.cash)
        out[symbol] = _jsonify(r)
    return out


class OptimizeReq(BaseModel):
    strategy: str
    symbol: str
    param_grid: dict
    qfq: bool = False
    cash: float = 100_000


@app.post("/api/optimize")
async def optimize_params(req: OptimizeReq):
    """参数网格搜索: 遍历 param_grid, 按 total_return 排序返回 top N."""
    strat = registry.get(req.strategy)
    tf = getattr(strat, "TIMEFRAME", "day")
    df = await asyncio.to_thread(_fetch, req.symbol, req.qfq, tf, 3000)
    r = await asyncio.to_thread(
        vbt_adapter.optimize, strat, df, req.param_grid, req.cash)
    return _jsonify(r)


@app.get("/api/strategies/{name}/schema")
def get_strategy_schema(name: str):
    """策略参数 schema + 元信息, 供前端 UI 自动生成参数控件."""
    strat = registry.get(name)
    return {
        "name": strat.NAME,
        "title": strat.TITLE,
        "params": strat.params_schema(),
        "timeframe": getattr(strat, "TIMEFRAME", "day"),
        "symbols": list(strat.SYMBOLS),
        "trigger_on_close": bool(getattr(strat, "TRIGGER_ON_CLOSE", True)),
    }


class ChartRunReq(BaseModel):
    """新建图 -> 挂策略 -> 指定参数 -> 跑一轮 (新事件驱动链路)."""
    strategy: str
    symbol: str
    params: dict = {}
    mode: str = "live"           # backtest / paper / live
    dry_run: bool = True
    cash_per_symbol: float = 10_000.0


@app.post("/api/charts/run")
async def chart_run(req: ChartRunReq):
    """图会话跑一轮: Runner.run_live(once=True) -> on_bar -> signal -> broker."""
    strat = registry.get(req.strategy)
    from strategy.runtime.runner import Runner
    runner = Runner(
        strategy=strat, symbol=req.symbol, params=req.params,
        mode=req.mode, cash=req.cash_per_symbol * 10,
        cash_per_symbol=req.cash_per_symbol,
        dry_run=req.dry_run, fetch_fn=_fetch, poll_seconds=5.0,
    )
    res = await runner.run_live(once=True)
    # 附完整订单列表 (供前端画买卖 markers)
    res["orders"] = [
        {"order_id": o.order_id, "symbol": o.symbol, "side": o.side,
         "qty": o.qty, "price": round(o.price, 3), "status": o.status,
         "filled_qty": o.filled_qty, "avg_fill_price": round(o.avg_fill_price, 3),
         "ts": o.ts, "error": o.error}
        for o in runner.pf.orders.values()
    ]
    return res


@app.get("/api/kline")
async def get_kline(symbol: str, tf: str = "day", qfq: bool = True,
                    limit: int = 3000):
    """K线 OHLCV 数据 (供前端 K线图). tf: day/5m/15m/30m/60m."""
    df = await asyncio.to_thread(_fetch, symbol, qfq, tf, limit)
    return {
        "symbol": symbol, "tf": tf,
        "bars": [{"date": str(r["date"]), "open": float(r["open"]),
                  "high": float(r["high"]), "low": float(r["low"]),
                  "close": float(r["close"]), "volume": float(r["volume"])}
                 for _, r in df.iterrows()],
    }


# --- 启停 / 执行 ---
@app.post("/api/strategies/{name}/start")
def start_strategy(name: str):
    return manager.start(name)


@app.post("/api/strategies/{name}/stop")
def stop_strategy(name: str):
    return manager.stop(name)


@app.post("/api/strategies/{name}/run-once")
async def run_once(name: str):
    strats = registry.discover()
    cfg = config_mod.load(strats)
    summary = await asyncio.to_thread(trader.run_once, name, cfg["strategies"][name], True)
    return summary


# --- 评估记录 / 持仓 / 快照 ---
@app.get("/api/evals/{name}")
def get_evals(name: str, tail: int = 60):
    return trader.read_evals(name, tail=tail)


@app.get("/api/positions")
async def get_positions():
    """同花顺实际持仓 (subprocess ths_trade)."""
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "ths_trade.py"), "positions"]
    r = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=120)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"ok": False, "stderr": r.stderr[-500:], "stdout": r.stdout[-500:]}


@app.get("/api/quote/{symbol}")
async def get_quote(symbol: str):
    return await asyncio.to_thread(trader.fetch_quote_snapshot, symbol)


@app.get("/api/intraday/{symbol}")
async def get_intraday(symbol: str):
    """当日 1m K 线 + VWAP + MACD + 昨收 — 前端首次加载全量, 后续走 WS 增量."""
    df, pre_close = await asyncio.to_thread(fetch_intraday_1m, symbol)
    if len(df) == 0:
        return {"symbol": symbol, "pre_close": pre_close, "bars": []}

    # VWAP
    vol_shares = df["volume_lots"] * 100
    df["vwap"] = df["amount"].cumsum() / vol_shares.cumsum()
    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = (dif - dea) * 2

    bars = []
    for i, row in df.iterrows():
        bars.append({
            "time": row["time"].strftime("%Y-%m-%d %H:%M:%S"),
            "open": _jsonify(row["open"]),
            "high": _jsonify(row["high"]),
            "low": _jsonify(row["low"]),
            "close": _jsonify(row["close"]),
            "volume": _jsonify(row["volume_lots"]),
            "amount": _jsonify(row["amount"]),
            "vwap": _jsonify(row["vwap"]),
            "dif": _jsonify(dif.iloc[i]),
            "dea": _jsonify(dea.iloc[i]),
            "macd_hist": _jsonify(hist.iloc[i]),
        })
    return {"symbol": symbol, "pre_close": pre_close, "bars": bars}


# ================================================================ WebSocket ====
# 全局连接管理 + 后台 tick 推送循环

class ConnectionManager:
    def __init__(self):
        self.active: dict[str, set[WebSocket]] = {}  # symbol -> {ws}
        self.lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, symbols: list[str]):
        await ws.accept()
        async with self.lock:
            for s in symbols:
                self.active.setdefault(s, set()).add(ws)

    async def disconnect(self, ws: WebSocket, symbols: list[str]):
        async with self.lock:
            for s in symbols:
                if s in self.active:
                    self.active[s].discard(ws)
                    if not self.active[s]:
                        del self.active[s]

    async def broadcast(self, symbol: str, msg: dict):
        async with self.lock:
            targets = list(self.active.get(symbol, set()))
        for ws in targets:
            try:
                await ws.send_json(msg)
            except Exception:
                pass


_ws_mgr = ConnectionManager()


async def _tick_loop(symbols: list[str]):
    """后台循环: 广播行情给订阅了对应 symbol 的 WebSocket.

    防护与优化机制:
      1) 孤儿进程保护: 探测到父进程退出 (ppid=1) 自动安全退出, 防止孤儿 worker 无限空跑.
      2) 按需轮询: 仅在有活跃前端 WebSocket 订阅该 symbol 时才发起网络取数, 零订阅时低功耗待机.
      3) 自适应间隔: fdata serve 在线时 1s tick; 若回退 CLI 模式则放宽至 5s, 坚决防止 CPU 进程风暴.
    """
    import os
    import time as _time
    while True:
        # 1. 孤儿进程保护
        if os.getppid() == 1:
            print("[backend] 探测到父进程已终止（当前处于孤儿状态），安全退出 tick 循环", flush=True)
            break

        # 2. 按需订阅过滤: 仅对当前有 WebSocket 连接关注的 symbol 抓取
        active_symbols = [s for s in symbols if _ws_mgr.active.get(s)]
        if not active_symbols:
            # 无任何前端订阅, 彻底休眠 2 秒, 零 CPU 开销
            await asyncio.sleep(2.0)
            continue

        # 3. 探活与自适应间隔: serve 模式 1s, CLI 降级模式 5s
        server_ok = is_server_available()
        interval = 1.0 if server_ok else 5.0

        t0 = _time.perf_counter()
        for symbol in active_symbols:
            try:
                # 在线程池中运行阻塞的 eltdx 调用, 不阻塞事件循环
                df, pre_close = await asyncio.to_thread(fetch_intraday_1m, symbol)
                snp = await asyncio.to_thread(fetch_quote_snapshot, symbol)

                if len(df) == 0:
                    continue

                # 用快照覆写最后一个 bar 的 close (实时价)
                df.loc[df.index[-1], "close"] = snp["last"]

                # VWAP / MACD (只算最后一根, 前端全量已有, 这里给增量)
                vol_shares = df["volume_lots"] * 100
                vwap_last = float(df["amount"].cumsum().iloc[-1] / vol_shares.cumsum().iloc[-1])
                close_all = df["close"]
                ema12 = close_all.ewm(span=12, adjust=False).mean()
                ema26 = close_all.ewm(span=26, adjust=False).mean()
                dif = ema12 - ema26
                dea = dif.ewm(span=9, adjust=False).mean()

                # 增量: 只发最后 1-2 根 (新 bar + 实时更新的当前 bar)
                tail = df.tail(2).reset_index()
                bars_delta = []
                for i, (_, row) in enumerate(tail.iterrows()):
                    real_idx = len(df) - len(tail) + i
                    bars_delta.append({
                        "time": row["time"].strftime("%Y-%m-%d %H:%M:%S"),
                        "close": float(row["close"]),
                        "vwap": float(vwap_last) if i == len(tail) - 1 else None,
                        "dif": float(dif.iloc[real_idx]),
                        "dea": float(dea.iloc[real_idx]),
                        "macd_hist": float((dif.iloc[real_idx] - dea.iloc[real_idx]) * 2),
                    })

                await _ws_mgr.broadcast(symbol, {
                    "type": "tick",
                    "symbol": symbol,
                    "ts": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "snapshot": {
                        "last": snp["last"], "pre_close": pre_close,
                        "change_pct": snp["change_pct"],
                        "high": snp["high"], "low": snp["low"],
                        "amount": snp["amount"],
                    },
                    "bars": bars_delta,
                })
            except Exception as e:
                await _ws_mgr.broadcast(symbol, {"type": "error", "msg": str(e)})

        # 节流: 依据自适应间隔减去已耗时
        elapsed = _time.perf_counter() - t0
        await asyncio.sleep(max(0.0, interval - elapsed))


_tick_task: Optional[asyncio.Task] = None
_tick_symbols: list[str] = []


@app.on_event("startup")
async def _start_tick_loop():
    """进程启动后后台跑 tick 循环, 覆盖最常用的 symbol (按需扩展)."""
    global _tick_task, _tick_symbols
    _tick_symbols = ["sz159915", "sh510300", "sh000001"]
    _tick_task = asyncio.create_task(_tick_loop(_tick_symbols))


@app.on_event("shutdown")
async def _stop_tick_loop():
    global _tick_task
    if _tick_task:
        _tick_task.cancel()
        _tick_task = None


@app.websocket("/ws/market")
async def ws_market(ws: WebSocket):
    """前端分时图 WebSocket.

    订阅 symbols 通过 query params: ?symbols=sz159915,sh510300
    """
    raw = ws.query_params.get("symbols", "sz159915")
    symbols = [s.strip() for s in raw.split(",") if s.strip()]

    # 扩展 tick 循环覆盖的 symbol 列表 (不重启)
    global _tick_symbols
    for s in symbols:
        if s not in _tick_symbols:
            _tick_symbols.append(s)

    await _ws_mgr.connect(ws, symbols)
    try:
        # 保持连接, 不读 (前端只收)
        while True:
            await asyncio.sleep(30)
    except WebSocketDisconnect:
        pass
    finally:
        await _ws_mgr.disconnect(ws, symbols)


# ================================================================ 流式测试 WS ====
_SPEED_INTERVAL = {"1x": 5.0, "2x": 2.5, "5x": 1.0, "10x": 0.5, "20x": 0.25}


@app.websocket("/ws/mock_stream")
async def ws_mock_stream(ws: WebSocket):
    """流式测试 WebSocket: mock 数据逐根推进 + 策略实时运行给信号.

    Query: ?symbol=sz159915&strategy=ma20_trend&tf=5m&speed=1x&params={"window":20}

    流程: 按 speed 间隔 advance_bar -> 调 strategy.signal(df) -> target 变化 ->
          paper 成交 -> 推 {bar, orders, markers, snapshot} 给前端.
    成交口径: 当根收盘出信号 -> 当根 close 成交 (简化, 不 shift; 仅供测试).
    """
    symbol = ws.query_params.get("symbol", "sz159915")
    strat_name = ws.query_params.get("strategy", "")
    tf = ws.query_params.get("tf", "5m")
    speed = ws.query_params.get("speed", "1x")
    try:
        params = json.loads(ws.query_params.get("params", "{}") or "{}")
    except Exception:
        params = {}
    interval = _SPEED_INTERVAL.get(speed, 5.0)

    await ws.accept()
    if not strat_name:
        await ws.send_json({"type": "error", "msg": "缺 strategy 参数"})
        await ws.close()
        return
    try:
        strat = registry.get(strat_name)
    except Exception as e:
        await ws.send_json({"type": "error", "msg": f"策略不存在: {e}"})
        await ws.close()
        return

    # paper portfolio 状态
    cash = 100000.0
    position = 0
    avg_cost = 0.0
    markers: list = []

    # 取 pre_close (mock 市场昨日收盘价) 给前端分时图作 0% 轴
    from strategy.mock_market import _load as _mm_load, _BASE_PRICE
    mm_state = _mm_load()
    sym_state = mm_state.get("symbols", {}).get(symbol, {})
    pre_close = sym_state.get(
        "pre_close",
        _BASE_PRICE.get(symbol, 10.0),
    )
    # 推送 init: 仅含元信息, 前端用来初始化分时图的 pre_close 参考轴
    await ws.send_json({
        "type": "info", "msg": f"mock stream 启动 {symbol}@{tf}/{speed}",
        "pre_close": pre_close,
    })

    try:
        while True:
            bar = await asyncio.to_thread(mock_advance_bar, symbol, tf)
            if bar is None:
                await ws.send_json({"type": "info", "msg": "跨日/未凑齐, 继续"})
                await asyncio.sleep(interval)
                continue
            # 跑策略: 取已生成的历史 bars + 新 bar 调 signal
            session_bars = await asyncio.to_thread(mock_get_session_bars, symbol, tf)
            df = pd.DataFrame(session_bars)
            if len(df) < 3:
                await ws.send_json({"type": "bar", "bar": bar, "orders": [],
                                    "snapshot": {"cash": cash, "position": 0},
                                    "msg": "bars 不足, 等待"})
                await asyncio.sleep(interval)
                continue
            # 调 signal 拿 target 序列
            try:
                p = strat.validate_params(params)
                target = strat.signal(df, p)
                t_arr = pd.Series(target).fillna(0).to_numpy().astype(int)
                last_t = int(t_arr[-1])
                prev_t = int(t_arr[-2]) if len(t_arr) > 1 else 0
            except Exception as e:
                await ws.send_json({"type": "bar", "bar": bar, "orders": [],
                                    "snapshot": {"cash": cash, "position": position},
                                    "error": f"signal: {e}"})
                await asyncio.sleep(interval)
                continue

            # target 变化 -> 订单 (paper, 当根 close 成交)
            orders = []
            if last_t == 1 and prev_t == 0:
                qty = int(cash * 0.9 / bar["close"] // 100) * 100
                if qty > 0:
                    cash -= qty * bar["close"]
                    position = qty
                    avg_cost = bar["close"]
                    orders.append({"side": "buy", "qty": qty,
                                   "price": bar["close"], "status": "filled"})
                    markers.append({"date": bar["date"], "price": bar["close"],
                                    "action": "买入", "qty": qty})
            elif last_t == 0 and prev_t == 1 and position > 0:
                pnl = round((bar["close"] - avg_cost) * position, 2)
                cash += position * bar["close"]
                orders.append({"side": "sell", "qty": position,
                               "price": bar["close"], "status": "filled",
                               "pnl": pnl})
                markers.append({"date": bar["date"], "price": bar["close"],
                                "action": "卖出", "qty": position, "pnl": pnl})
                position = 0

            equity = round(cash + position * bar["close"], 2)
            snapshot = {"cash": round(cash, 2), "position": position,
                        "avg_cost": avg_cost, "equity": equity}
            await ws.send_json({
                "type": "bar", "bar": bar, "orders": orders,
                "markers": markers[-10:], "snapshot": snapshot,
                "target": last_t,
            })
            await asyncio.sleep(interval)
    except WebSocketDisconnect:
        pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

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
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from strategy import config as config_mod  # noqa: E402
from strategy import manager, registry, trader  # noqa: E402
from strategy.engine import backtest  # noqa: E402
from strategy.trader import _fetch  # noqa: E402
from strategy.trader import fetch_intraday_1m, fetch_quote_snapshot  # noqa: E402

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
    """把 pandas/numpy 对象转成 JSON 兼容结构."""
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return None if np.isnan(obj) else float(obj)
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
        strat = strats.get(name)
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
    strats = registry.discover()
    config_mod.save(body)
    return {"ok": True, "msg": "已保存 config.json"}


# --- 回测 ---
class BacktestReq(BaseModel):
    strategy: str
    symbols: list[str]
    params: dict
    qfq: bool = False
    cash: float = 100_000


@app.post("/api/backtest")
async def run_backtest(req: BacktestReq):
    strat = registry.get(req.strategy)
    tf = getattr(strat, "TIMEFRAME", "day")
    out = {}
    for symbol in req.symbols:
        df = await asyncio.to_thread(_fetch, symbol, req.qfq, tf, 3000)
        target = strat.target_position(df, req.params)
        r = backtest(df, target, cash=req.cash)
        # 只传必要数据: stats + equity (资金曲线) + markers (买卖点) + df 列 (收盘价+日期)
        out[symbol] = {
            "stats": _jsonify(r["stats"]),
            "equity": [{"time": _jsonify(idx), "value": _jsonify(val)}
                       for idx, val in r["equity"].items()],
            "markers": _jsonify(r["markers"]),
            "close": [{"time": _jsonify(row["date"]), "value": _jsonify(row["close"])}
                       for _, row in r["df"].iterrows()],
        }
    return out


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
    """后台循环: 每秒拉一次 eltdx, 广播给订阅了对应 symbol 的 WebSocket.

    关键: eltdx 是同步阻塞 TCP 客户端, 必须用 asyncio.to_thread 包一层
    否则会卡住整个事件循环, 导致 REST API 请求无法响应.
    """
    import time as _time
    while True:
        t0 = _time.perf_counter()
        for symbol in symbols:
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

        # 节流: 每秒一次, 减去已耗时
        elapsed = _time.perf_counter() - t0
        await asyncio.sleep(max(0.0, 1.0 - elapsed))


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

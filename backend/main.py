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
    GET  /api/quote/{symbol}             实时快照
    GET  /api/conditions                 条件单全量状态 (引擎+订单+组合+日志)
    POST /api/conditions                 新增条件单
    DELETE /api/conditions/{id}          删除条件单
    POST /api/conditions/engine/start    启动条件单引擎 {live, cash, poll_seconds}
    POST /api/conditions/engine/stop     停止条件单引擎
    GET  /api/grids                      网格单全量状态 (引擎+网格+组合+日志)
    POST /api/grids                      新建网格单
    DELETE /api/grids/{id}               删除网格单
    POST /api/grids/{id}/pause           暂停单个网格
    POST /api/grids/{id}/resume          恢复单个网格
    POST /api/grids/engine/start         启动网格引擎 {live, cash, poll_seconds}
    POST /api/grids/engine/stop          停止网格引擎
    GET  /api/portfolios                 组合列表 + 实时价 + 同花顺真实持仓对照
    POST /api/portfolios                 创建组合 {name, items:[{code, weight}]}
    PUT  /api/portfolios/{pid}           组合调整 (增减个股/改权重)
    DELETE /api/portfolios/{pid}         删除组合
    GET  /api/portfolios/{pid}/preview   分配/调仓预览 ?action=buy|sell|sync&amount=
    POST /api/portfolios/{pid}/buy       按权重买入一篮子 {total_amount, dry_run}
    POST /api/portfolios/{pid}/sell      按权重卖出一篮子 {total_amount, dry_run}
    POST /api/portfolios/{pid}/sync      同步仓位 (人工ETF调仓) {dry_run, min_order_value}
    GET  /api/positions                  同花顺实际持仓 (subprocess ths_trade)
    GET  /api/watchlist                  自选股列表 + 同步状态
    POST /api/watchlist                  添加自选股 {symbol, name?}
    DELETE /api/watchlist/{symbol}       删除自选股 (tombstone, 自动同步不回加)
    POST /api/watchlist/sync             立即同步同花顺持仓入自选股
    PUT  /api/watchlist/settings         {auto_sync: bool} 开关持仓自动同步
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

import logging
import traceback
from logging.handlers import TimedRotatingFileHandler

import numpy as np
import pandas as pd
from fastapi import Body, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))


# ---- 文件日志系统: backend/logs/app.log, 按天分卷, 保留 30 天 ----
def _setup_file_logging() -> None:
    """幂等配置文件日志 (uvicorn --reload 多进程 / 多次导入时不重复加 handler).

    日志写入 backend/logs/app.log, 每天分卷 (app.log -> app.log.YYYY-MM-DD), 保留 30 天.
    捕获根 logger + uvicorn.* + strategy.* + fdata_client INFO 级以上日志.
    """
    log_dir = BACKEND_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "app.log"

    root = logging.getLogger()
    # 幂等检测: 已有同类 TimedRotatingFileHandler 指向同路径则跳过
    for h in root.handlers:
        if isinstance(h, TimedRotatingFileHandler) and getattr(h, "baseFilename", "") == str(log_path):
            return

    root.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = TimedRotatingFileHandler(
        log_path, when="midnight", backupCount=30, encoding="utf-8", utc=False,
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.INFO)
    root.addHandler(fh)

    # 确保关键命名 logger 也有 INFO 级别 + propagate=True (默认), 让 root handler 收集到
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error",
                 "strategy", "fdata_client", "backend", "trader"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.propagate = True

    # 启动标记
    logging.info("[app] FastAPI 后端启动 - 文件日志就绪: %s", log_path)


_setup_file_logging()

from strategy import config as config_mod  # noqa: E402
from strategy import manager, registry, trader  # noqa: E402
from strategy.backtest import vbt_adapter  # noqa: E402
from strategy.backtest import param_optimizer  # noqa: E402
from strategy.fdata_client import is_server_available  # noqa: E402
from strategy.trader import _fetch  # noqa: E402
from strategy.trader import fetch_intraday_1m, fetch_quote_snapshot  # noqa: E402
from strategy.mock_market import advance_bar as mock_advance_bar  # noqa: E402
from strategy.mock_market import get_session_bars as mock_get_session_bars  # noqa: E402
from backend import store as chart_store  # noqa: E402

app = FastAPI(title="量化交易后端", version="0.1.0")

# ======================================================= 参数优化异步任务 =====
# 贝叶斯 60 次通常 1-5 分钟，不能同步阻塞。用内存里的 job 字典 + 后台线程。
# 单实例后端够用；要跨进程/持久化后续改 Redis/SQLite。
import threading as _th
import uuid as _uuid
from dataclasses import dataclass as _dc, field as _field

@_dc
class OptimJob:
    job_id: str
    req: "ParamOptimizeReq"
    status: str = "running"        # running / done / failed
    progress: dict = _field(default_factory=dict)   # {current, total, latest_score, elapsed_ms, latest_info, history[]}
    result: dict | None = None
    error: str | None = None
    created_at: float = _field(default_factory=lambda: _dt.datetime.now().timestamp())

_OPTIM_JOBS: dict[str, OptimJob] = {}
_OPTIM_JOBS_LOCK = _th.Lock()
_OPTIM_MAX_JOBS = 64

def _cleanup_jobs():
    """只保留最近 _OPTIM_MAX_JOBS 个，老的丢弃（LRU 按 created_at）."""
    if len(_OPTIM_JOBS) <= _OPTIM_MAX_JOBS:
        return
    items = sorted(_OPTIM_JOBS.items(), key=lambda kv: kv[1].created_at)
    for k, _ in items[:-_OPTIM_MAX_JOBS]:
        _OPTIM_JOBS.pop(k, None)

def _run_job_worker(job_id: str):
    from strategy.backtest import param_optimizer as _po
    job = _OPTIM_JOBS.get(job_id)
    if job is None:
        return
    req = job.req
    try:
        strat = registry.get(req.strategy)
        # 交易成本: 从 config 解析 (后台线程内再解一次即可, 不会拖慢)
        try:
            _cfg = config_mod.load(registry.discover())
            _costs = config_mod.trade_costs_for(_cfg, req.symbol)
        except Exception:
            # 回退到默认值, 保证任务不因为 config 解析意外挂掉
            _costs = {"buy_fee": 0.0001, "sell_fee": 0.0001,
                      "sell_stamp_duty": 0.001, "slippage": 0.0001}
        def _prog(i: int, n: int, score: float, info: dict) -> None:
            pct = f"{i}/{n}" if n else f"{i}"
            logging.info("[opt %s job=%s] %s score=%.4f tf=%s params=%s",
                         req.mode, job_id[:6], pct, score,
                         info.get("tf", ""), info.get("params", {}))
            with _OPTIM_JOBS_LOCK:
                j = _OPTIM_JOBS.get(job_id)
                if j is None:
                    return
                hist = j.progress.setdefault("history", [])
                if len(hist) < 500:
                    hist.append(info)
                else:
                    hist.pop(0)
                    hist.append(info)
                j.progress["current"] = i
                j.progress["total"] = n or 0
                j.progress["latest_score"] = float(score)
                j.progress["latest_info"] = info
                j.progress["elapsed_ms"] = int((_dt.datetime.now().timestamp() - j.created_at) * 1000)
        result = _po.run(
            mode=req.mode, strategy=strat, symbol=req.symbol,
            param_grid=req.param_grid or None,
            timeframes=req.timeframes or None,
            overrides=req.overrides or None,
            metric=req.metric,
            n_calls=req.n_calls, n_initial_points=req.n_initial_points,
            base_estimator=req.base_estimator,
            qfq=req.qfq, cash=req.cash,
            fees=None,
            buy_fee=_costs["buy_fee"], sell_fee=_costs["sell_fee"],
            sell_stamp_duty=_costs["sell_stamp_duty"], slippage=_costs["slippage"],
            limit=req.limit or None, top_n=req.top_n,
            progress_cb=_prog,
        )
        with _OPTIM_JOBS_LOCK:
            j = _OPTIM_JOBS.get(job_id)
            if j is None:
                return
            j.status = "done"
            d = _po.result_to_dict(result)
            d["history"] = result.history  # 全部历史，API 返回时前端可画收敛曲线
            j.result = _jsonify(d)
            j.progress["elapsed_ms"] = int((_dt.datetime.now().timestamp() - j.created_at) * 1000)
    except Exception as e:
        tb = traceback.format_exc()
        logging.error("[opt job=%s] 失败 %s: %s", job_id[:6], type(e).__name__, e)
        with _OPTIM_JOBS_LOCK:
            j = _OPTIM_JOBS.get(job_id)
            if j is None:
                return
            j.status = "failed"
            j.error = f"{type(e).__name__}: {e}\n{tb[:1500]}"

# 前端 Next.js 在 localhost:3000, 允许跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- 全局异常 handler: 所有未捕获异常一律返回 JSON {detail: 完整错误+traceback}
# 取代 uvicorn 默认的 "Internal Server Error" 纯文本, 便于前端显示和 debug
@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    tb = traceback.format_exc()
    detail = f"{type(exc).__name__}: {exc}\n\n{tb}"
    # 截断过长避免前端弹 2MB 错误
    if len(detail) > 4000:
        detail = detail[:2000] + "\n... [truncated] ...\n" + detail[-2000:]
    _log = logging.getLogger("backend")
    _log.error("[EXCEPTION] %s %s -> %s: %s\n%s",
               request.method, request.url.path,
               type(exc).__name__, exc, tb, exc_info=(type(exc), exc, exc.__traceback__))
    print(f"[EXCEPTION] {request.method} {request.url.path} -> {type(exc).__name__}: {exc}",
          file=sys.stderr, flush=True)
    return JSONResponse(
        status_code=500,
        content={"detail": detail, "error": type(exc).__name__, "message": str(exc)},
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
    """服务探活: 前端每 5s 调用一次. fdata 状态 0.5s 超时快速检测, 不拖慢."""
    return {
        "ok": True,
        "time": _dt.datetime.now().isoformat(),
        "backend": True,
        "fdata": is_server_available(),
    }


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
    limit: int = 0             # K线根数上限, 0 = 拉取全部


@app.post("/api/backtest")
async def run_backtest(req: BacktestReq):
    """vectorbt 回测: signal -> entries/exits -> vbt.Portfolio. 次日开盘口径.

    手续费/滑点: 自动从 config.json 的 trade_costs 按 symbol 大类 (股票/期货/期权) 选取.
    兼容两种调用:
    - symbols=[...] (多标的, 批量回测)
    - symbol="sz159915" + tf="5m" (单标的, 图会话挂载策略按当前周期回测)
    """
    strat = registry.get(req.strategy)
    tf = req.tf or getattr(strat, "TIMEFRAME", "day")
    syms = req.symbols or ([req.symbol] if req.symbol else [])
    # 加载 config 并解析交易成本配置 (每个 symbol 单独解析: 可能跨大类)
    strats_discovered = registry.discover()
    cfg = config_mod.load(strats_discovered)
    out = {}
    for symbol in syms:
        costs = config_mod.trade_costs_for(cfg, symbol)
        df = await asyncio.to_thread(
            _fetch, symbol, req.qfq, tf, req.limit or None)
        r = await asyncio.to_thread(
            vbt_adapter.backtest,
            strat, df, req.params, req.cash,
            fees=None,                                # 用新四项 (非 None 才走旧兜底)
            buy_fee=costs["buy_fee"],
            sell_fee=costs["sell_fee"],
            sell_stamp_duty=costs["sell_stamp_duty"],
            slippage=costs["slippage"],
        )
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
    """参数网格搜索 (旧兼容端点): 按 symbol 大类取交易成本配置."""
    strat = registry.get(req.strategy)
    tf = getattr(strat, "TIMEFRAME", "day")
    cfg = config_mod.load(registry.discover())
    costs = config_mod.trade_costs_for(cfg, req.symbol)
    df = await asyncio.to_thread(_fetch, req.symbol, req.qfq, tf, 3000)
    r = await asyncio.to_thread(
        vbt_adapter.optimize, strat, df, req.param_grid, req.cash,
        fees=None,
        buy_fee=costs["buy_fee"], sell_fee=costs["sell_fee"],
        sell_stamp_duty=costs["sell_stamp_duty"], slippage=costs["slippage"],
    )
    return _jsonify(r)


class ParamOptimizeReq(BaseModel):
    """新版参数优化: 支持网格 + 贝叶斯 + 多周期搜索."""
    strategy: str
    symbol: str
    mode: str = "grid"                    # "grid" | "bayesian"
    param_grid: dict = {}                 # grid 模式必传
    timeframes: list[str] = []            # 可选，如 ["1m","2m","5m"]；空则用策略默认 TIMEFRAME
    overrides: dict = {}                  # bayesian 模式用，收窄维度范围，如 {"rsi_fast": [4,6,8]} 或 {"oversold": {"lo":15,"hi":35}}
    metric: str = "calmar"                # total_return / buyhold_alpha / sharpe / calmar / win_rate / calmar_alpha
    n_calls: int = 50                     # bayesian 用
    n_initial_points: int = 12
    base_estimator: str = "GP"            # GP / ET / RF
    qfq: bool = False
    cash: float = 100_000
    fees: float = 0.0001
    limit: int = 0                        # 0 = 全量
    top_n: int = 10


@app.post("/api/param-optimize")
async def param_optimize(req: ParamOptimizeReq):
    """新版参数优化 API (同步). 自动从 config 取 symbol 对应大类的交易成本."""
    strat = registry.get(req.strategy)
    cfg = config_mod.load(registry.discover())
    costs = config_mod.trade_costs_for(cfg, req.symbol)
    def _progress(i: int, n: int, score: float, info: dict) -> None:
        pct = f"{i}/{n}" if n else f"{i}"
        logging.info("[opt %s] %s score=%.4f tf=%s params=%s",
                     req.mode, pct, score, info.get("tf", ""), info.get("params", {}))
    r = await asyncio.to_thread(
        param_optimizer.run,
        mode=req.mode, strategy=strat, symbol=req.symbol,
        param_grid=req.param_grid or None,
        timeframes=req.timeframes or None,
        overrides=req.overrides or None,
        metric=req.metric,
        n_calls=req.n_calls, n_initial_points=req.n_initial_points,
        base_estimator=req.base_estimator,
        qfq=req.qfq, cash=req.cash,
        fees=None,                         # 关闭旧兜底, 用新四项
        buy_fee=costs["buy_fee"], sell_fee=costs["sell_fee"],
        sell_stamp_duty=costs["sell_stamp_duty"], slippage=costs["slippage"],
        limit=req.limit or None, top_n=req.top_n,
        progress_cb=_progress,
    )
    return _jsonify(param_optimizer.result_to_dict(r))


@app.post("/api/param-optimize/start")
async def param_optimize_start(req: ParamOptimizeReq):
    """提交异步参数优化任务, 返回 job_id. 前端用 /api/param-optimize/poll/{job_id} 轮询."""
    job_id = _uuid.uuid4().hex[:12]
    job = OptimJob(job_id=job_id, req=req)
    with _OPTIM_JOBS_LOCK:
        _cleanup_jobs()
        _OPTIM_JOBS[job_id] = job
    t = _th.Thread(target=_run_job_worker, args=(job_id,), daemon=True)
    t.start()
    return {"job_id": job_id, "status": "running"}


@app.get("/api/param-optimize/poll/{job_id}")
def param_optimize_poll(job_id: str):
    """轮询进度/结果. running -> 返回 progress; done -> 返回 result; failed -> 返回 error."""
    with _OPTIM_JOBS_LOCK:
        j = _OPTIM_JOBS.get(job_id)
    if j is None:
        return JSONResponse(status_code=404, content={"error": "not_found",
                            "message": f"任务 {job_id} 不存在或已过期"})
    out = {
        "job_id": j.job_id,
        "status": j.status,
        "progress": j.progress,
    }
    if j.status == "done":
        out["result"] = j.result
    elif j.status == "failed":
        out["error"] = j.error
    return out


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
    """图会话 -> 挂策略 -> 跑一轮 (新事件驱动链路).

    模式语义 (由前端 "实盘" checkbox 驱动):
      - liveMode=ON  → mode="live" + dry_run=False (真实同花顺下单, 无 dry-run 兜底)
      - liveMode=OFF → mode="paper"                (纸面模拟, 走 SimulatedBroker)
    为兼容老前端请求, 保留 dry_run/mode 字段:
      * 显式传 mode="live" 且 dry_run=True 时也生效 (仅旧版本前端用)
    """
    strategy: str
    symbol: str
    params: dict = {}
    mode: str = "paper"          # paper / live
    dry_run: bool = False        # 默认 False. 前端实盘 checkbox: ON=始终 False
    cash_per_symbol: float = 10_000.0


@app.post("/api/charts/run")
async def chart_run(req: ChartRunReq):
    """图会话跑一轮: Runner.run_live(once=True) -> on_bar -> signal -> broker.

    语义保证 (对齐前端实盘 checkbox):
      1. mode=paper → 永远用 SimulatedBroker, dry_run 字段被忽略
      2. mode=live  → 用 LiveBroker:
         * dry_run=True  (旧前端极少传, 只保留兼容): 填单但不确认
         * dry_run=False (前端"实盘=ON" 时传这个): 真实扣款 & 提交订单
    """
    strat = registry.get(req.strategy)
    from strategy.runtime.runner import Runner
    # 防御性归一化: 如果前端传了 mode=live 但 dry_run=True 是"填单不提交",
    # 按大王的最新要求, 这种组合在新 UI 不会出现; 即便出现也保留旧行为以安全兼容.
    if req.mode == "paper":
        runner_mode = "paper"
        runner_dry = False
    else:
        runner_mode = "live"
        runner_dry = bool(req.dry_run)
    runner = Runner(
        strategy=strat, symbol=req.symbol, params=req.params,
        mode=runner_mode, cash=req.cash_per_symbol * 10,
        cash_per_symbol=req.cash_per_symbol,
        dry_run=runner_dry, fetch_fn=_fetch, poll_seconds=5.0,
    )
    res = await runner.run_live(once=True)
    # 把本请求实际使用的执行模式回传给前端, 便于日志/核对
    res.setdefault("exec_mode", {
        "mode": runner_mode,
        "dry_run": runner_dry,
        "broker": "LiveBroker(同花顺)" if runner_mode == "live" else "SimulatedBroker",
    })
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


# --- 条件单 (trading/condition_orders.py 引擎的 Web 管理端) ---
from backend import conditions as cond_mgr  # noqa: E402


class ConditionAddReq(BaseModel):
    symbol: str
    trigger_gap_pct: float = -4.0   # 相对昨收跌幅触发买入 (负=低开买)
    buy_qty: int = 100
    sell_rally_pct: float = 1.0     # 反弹 (买入价+X%) 触发卖出
    open_window_min: int = 3        # 开盘后判定买入的窗口分钟数


class ConditionEngineReq(BaseModel):
    live: bool = False
    cash: float = 100_000.0
    poll_seconds: float = 5.0


@app.get("/api/conditions")
async def get_conditions():
    """条件单全量状态: 引擎运行态 + 订单 + 组合 + 日志. 引擎停止也可只读查看."""
    return cond_mgr.status()


@app.post("/api/conditions")
async def add_condition(req: ConditionAddReq):
    """新增条件单 (同一标的只允许一单). 引擎运行中则动态加单, 无需重启."""
    try:
        entry = cond_mgr.add_order(req.model_dump())
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    return {"ok": True, "order": entry}


@app.delete("/api/conditions/{co_id}")
async def delete_condition(co_id: str):
    if not cond_mgr.remove_order(co_id):
        return JSONResponse(status_code=404, content={"detail": f"条件单 {co_id} 不存在"})
    return {"ok": True}


@app.post("/api/conditions/engine/start")
async def start_condition_engine(req: ConditionEngineReq):
    return await cond_mgr.start(req.live, req.cash, req.poll_seconds)


@app.post("/api/conditions/engine/stop")
async def stop_condition_engine():
    return await cond_mgr.stop()


# --- 网格单 (trading/grid_orders.py 引擎的 Web 管理端) ---
from backend import grids as grid_mgr  # noqa: E402


class GridAddReq(BaseModel):
    symbol: str
    upper: float                       # 网格上限
    lower: float                       # 网格下限
    grid_unit: str = "pct"             # pct=等比(%) / price=等差(元)
    step: float = 2.0                  # 网格间距
    base_price: float = 0.0            # 基准价 (首次触发计算基准, 成交后滚动)
    qty_mode: str = "qty"              # qty=固定股数 / cash=固定金额
    per_qty: int = 1000                # 每格股数 (qty 模式)
    per_cash: float = 5000.0           # 每格金额 (cash 模式)
    multiplier: float = 1.0            # 梯度倍量 (每深一档 x m, 1=等量)
    max_position: int = 0              # 最大持仓 (0=不限)
    min_position: int = 0              # 最小底仓 (0=不限)
    sell_retrace_pct: float = 0.0      # 卖出回落确认 % (0=到价即卖)
    buy_rebound_pct: float = 0.0       # 买入反弹确认 % (0=到价即买)
    pad_pct: float = 0.0               # 下单价格浮动 % (买加/卖降保成交)
    t1_protect: bool = True            # T+1: 当日买入批次当日不卖
    expire_date: str = ""              # 有效期 YYYY-MM-DD (空=长期)
    base_qty: int = 0                  # 启动底仓 (0=不买)


class GridEngineReq(BaseModel):
    live: bool = False
    cash: float = 100_000.0
    poll_seconds: float = 5.0


@app.get("/api/grids")
async def get_grids():
    """网格单全量状态: 引擎运行态 + 网格 + 组合 + 日志. 引擎停止也可只读查看."""
    return grid_mgr.status()


@app.post("/api/grids")
async def add_grid(req: GridAddReq):
    """新建网格单 (同一标的只允许一个). 引擎运行中则动态加单, 无需重启."""
    try:
        entry = grid_mgr.add_grid(req.model_dump())
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    return {"ok": True, "grid": entry}


@app.delete("/api/grids/{gid}")
async def delete_grid(gid: str):
    if not grid_mgr.remove_grid(gid):
        return JSONResponse(status_code=404, content={"detail": f"网格单 {gid} 不存在"})
    return {"ok": True}


@app.post("/api/grids/{gid}/pause")
async def pause_grid(gid: str):
    if not grid_mgr.pause_grid(gid):
        return JSONResponse(status_code=400,
                            content={"detail": f"网格 {gid} 暂停失败 (不存在或非运行态)"})
    return {"ok": True}


@app.post("/api/grids/{gid}/resume")
async def resume_grid(gid: str):
    if not grid_mgr.resume_grid(gid):
        return JSONResponse(status_code=400,
                            content={"detail": f"网格 {gid} 恢复失败 (不存在或非暂停态)"})
    return {"ok": True}


@app.post("/api/grids/engine/start")
async def start_grid_engine(req: GridEngineReq):
    return await grid_mgr.start(req.live, req.cash, req.poll_seconds)


@app.post("/api/grids/engine/stop")
async def stop_grid_engine():
    return await grid_mgr.stop()


# --- 组合交易 (trading/portfolios.py 人工ETF: 按权重买卖篮子 + 同步真实仓位) ---
from backend import portfolios as pf_mgr  # noqa: E402


class PortfolioCreateReq(BaseModel):
    name: str
    note: str = ""
    items: list[dict]              # [{code, weight, name?}] weight 为百分比 (自动归一到 100)


class PortfolioUpdateReq(BaseModel):
    name: str | None = None
    note: str | None = None
    items: list[dict] | None = None   # 传则整体替换组合标的 (组合调整)


class PortfolioTradeReq(BaseModel):
    total_amount: float              # buy: 买入总金额 / sell: 卖出总金额
    dry_run: bool = True             # true=纯试算不碰同花顺; false=真实委托
    pad_pct: float = 0.3             # 限价浮动 % (买加价/卖降价保成交)


class PortfolioSyncReq(BaseModel):
    dry_run: bool = True
    pad_pct: float = 0.3
    min_order_value: float = 1000.0  # 差额低于该值不下单 (零股清尾除外)


@app.get("/api/portfolios")
async def get_portfolios(with_positions: bool = True):
    """组合列表 + 实时价 + 同花顺真实持仓对照. with_positions=false 跳过持仓读取."""
    return await pf_mgr.overview(with_positions)


@app.post("/api/portfolios")
async def create_portfolio(req: PortfolioCreateReq):
    """创建组合 (权重自动归一到 100, 标的名称自动按行情补全)."""
    try:
        pf = await pf_mgr.create(req.name, req.items, req.note)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    return {"ok": True, "portfolio": pf}


@app.put("/api/portfolios/{pid}")
async def update_portfolio(pid: str, req: PortfolioUpdateReq):
    """组合调整: 增减个股 / 改权重 / 改名. 只改配比, 不动真实持仓."""
    try:
        pf = await pf_mgr.update(pid, items=req.items, name=req.name, note=req.note)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    return {"ok": True, "portfolio": pf}


@app.delete("/api/portfolios/{pid}")
async def delete_portfolio(pid: str):
    if not pf_mgr.delete(pid):
        return JSONResponse(status_code=404, content={"detail": f"组合 {pid} 不存在"})
    return {"ok": True}


@app.get("/api/portfolios/{pid}/preview")
async def preview_portfolio(pid: str, action: str = "buy", amount: float = 0.0,
                            min_order_value: float = 1000.0):
    """分配/调仓预览 (不下单): action=buy|sell (按 amount 分配) 或 sync (按权重对齐持仓)."""
    try:
        return await pf_mgr.preview(pid, action, amount,
                                    min_order_value=min_order_value)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})


@app.post("/api/portfolios/{pid}/buy")
async def buy_portfolio(pid: str, req: PortfolioTradeReq):
    """按权重买入一篮子 (total_amount 分配到各标的). dry_run=false 真实下单!"""
    try:
        return await pf_mgr.execute(pid, "buy", req.total_amount,
                                    pad_pct=req.pad_pct, dry_run=req.dry_run)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})


@app.post("/api/portfolios/{pid}/sell")
async def sell_portfolio(pid: str, req: PortfolioTradeReq):
    """按权重卖出一篮子 (卖出 total_amount, 各标的按权重分摊). dry_run=false 真实下单!"""
    try:
        return await pf_mgr.execute(pid, "sell", req.total_amount,
                                    pad_pct=req.pad_pct, dry_run=req.dry_run)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})


@app.post("/api/portfolios/{pid}/sync")
async def sync_portfolio(pid: str, req: PortfolioSyncReq):
    """同步仓位 (人工ETF调仓): 真实持仓按新配比对齐, 生成差额订单 (先卖后买)."""
    try:
        return await pf_mgr.execute(pid, "sync",
                                    pad_pct=req.pad_pct,
                                    min_order_value=req.min_order_value,
                                    dry_run=req.dry_run)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
# --- 自选股 (watchlist): 手动增删 + 同花顺持仓自动同步 (backend/watchlist.py) ---
from backend import watchlist as watchlist_mgr  # noqa: E402


class WatchlistAddReq(BaseModel):
    symbol: str
    name: str = ""          # 可选; 空则尝试用实时快照补全


class WatchlistSettingsReq(BaseModel):
    auto_sync: Optional[bool] = None


@app.get("/api/watchlist")
def get_watchlist():
    """自选股全量: 股票列表 + 自动同步开关 + 最近一次同步时间/错误."""
    return watchlist_mgr.get_status()


@app.post("/api/watchlist")
async def add_watch_stock(req: WatchlistAddReq):
    """添加自选股. 未传 name 时后台取一次实时快照补全 (失败不阻塞添加)."""
    name = req.name.strip()
    if not name:
        try:
            snap = await asyncio.to_thread(trader.fetch_quote_snapshot,
                                           req.symbol.strip())
            name = str(snap.get("name") or "")
        except Exception:
            name = ""  # 数据源不可用也允许先加入, 名称留空
    try:
        entry = watchlist_mgr.add_stock(req.symbol, name=name, source="manual")
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    return {"ok": True, "stock": entry}


@app.delete("/api/watchlist/{symbol}")
async def delete_watch_stock(symbol: str):
    if not watchlist_mgr.remove_stock(symbol):
        return JSONResponse(status_code=404,
                            content={"detail": f"自选股 {symbol} 不存在"})
    return {"ok": True}


@app.post("/api/watchlist/sync")
async def sync_watchlist():
    """立即拉一次同花顺持仓并入自选股 (与后台自动同步互斥, 不会并发跑)."""
    return await watchlist_mgr.sync_positions_async()


@app.put("/api/watchlist/settings")
async def watchlist_settings(req: WatchlistSettingsReq):
    if req.auto_sync is not None:
        watchlist_mgr.set_auto_sync(req.auto_sync)
    st = watchlist_mgr.get_status()
    return {"ok": True, "auto_sync": st["auto_sync"]}


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
    cmd = [sys.executable, str(REPO_ROOT / "trading" / "ths_trade.py"), "positions"]
    r = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=120)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"ok": False, "stderr": r.stderr[-500:], "stdout": r.stdout[-500:]}


@app.get("/api/quote/{symbol}")
async def get_quote(symbol: str):
    return await asyncio.to_thread(trader.fetch_quote_snapshot, symbol)


# --- 选股自动交易 (trading/stock_picker.py 引擎的 Web 管理端) ---
from backend import stockpicker as picker_mgr  # noqa: E402
from trading import picker_rules  # noqa: E402


class PickerGroupReq(BaseModel):
    strategy_id: str = ""              # 策略 ID (空则自动生成, 买入组与此 ID 挂钩)
    title: str = ""
    picker: str                        # 选股插件 ID (GET /api/picker/pickers 可选)
    universe: list[str] = []           # 股票池
    params: dict = {}                  # 插件参数 (按组覆盖默认值)
    per_qty: int = 0                   # 每只买入股数 (0=按 cash_per_symbol 自动整手)
    cash_per_symbol: float = 10000.0
    max_positions: int = 0             # 买入组最大持仓只数 (0=不限)
    buy_scan_every: int = 60           # 每 N 轮 poll 跑一次选股 (卖出每轮都扫)
    t1_protect: bool = True            # T+1: 当日买入当日不卖
    enabled: bool = True


class PickerGroupPatchReq(BaseModel):
    title: Optional[str] = None
    universe: Optional[list[str]] = None
    params: Optional[dict] = None
    per_qty: Optional[int] = None
    cash_per_symbol: Optional[float] = None
    max_positions: Optional[int] = None
    buy_scan_every: Optional[int] = None
    t1_protect: Optional[bool] = None
    enabled: Optional[bool] = None


class PickerEngineReq(BaseModel):
    live: bool = False
    cash: float = 100_000.0
    poll_seconds: float = 5.0


@app.get("/api/picker")
async def get_picker():
    """选股系统全量状态: 引擎运行态 + 策略组 + 买入组持仓 + 指令流水. 停止也可只读."""
    return picker_mgr.status()


@app.get("/api/picker/pickers")
def get_picker_catalog():
    """选股策略全目录 (策略库 预置/自定义 + 代码插件) — 策略组下拉框用."""
    return picker_mgr.strategy_catalog()


class PickerStrategyReq(BaseModel):
    id: str = ""                       # 策略库 ID (空则按名称自动生成)
    title: str
    desc: str = ""
    buy_rules: list[dict] = []         # 买入条件原语 [{type, n, threshold, ...}]
    sell_rules: list[dict] = []        # 卖出条件原语 (任一命中即卖)


class PickerStrategyPatchReq(BaseModel):
    title: Optional[str] = None
    desc: Optional[str] = None
    buy_rules: Optional[list[dict]] = None
    sell_rules: Optional[list[dict]] = None


class PickerBacktestReq(BaseModel):
    universe: list[str]
    days: int = 250                    # 回测交易日数
    cash: float = 100_000.0
    max_positions: int = 3
    t1_protect: bool = True


@app.get("/api/picker/rule-types")
def get_picker_rule_types():
    """条件原语目录 (类型/参数默认值/说明) — 前端规则编辑器据此渲染."""
    return {"buy": picker_rules.BUY_RULE_TYPES, "sell": picker_rules.SELL_RULE_TYPES}


@app.get("/api/picker/strategies")
def list_picker_strategies():
    """策略库全目录 (source: preset 预置 / user 自定义 / code 代码插件)."""
    return picker_mgr.strategy_catalog()


@app.post("/api/picker/strategies")
async def add_picker_strategy(req: PickerStrategyReq):
    """新建策略库策略 (规则化, 可回测, 可被策略组引用)."""
    try:
        saved = picker_mgr.add_strategy(req.model_dump())
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    return {"ok": True, "strategy": saved}


@app.put("/api/picker/strategies/{sid}")
async def update_picker_strategy(sid: str, req: PickerStrategyPatchReq):
    try:
        saved = picker_mgr.update_strategy(sid, req.model_dump(exclude_none=True))
    except KeyError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    return {"ok": True, "strategy": saved}


@app.delete("/api/picker/strategies/{sid}")
async def delete_picker_strategy(sid: str):
    """删除策略库策略 (被策略组引用时拒绝)."""
    try:
        picker_mgr.delete_strategy(sid)
    except KeyError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    return {"ok": True}


@app.post("/api/picker/strategies/{sid}/backtest")
async def backtest_picker_strategy(sid: str, req: PickerBacktestReq):
    """策略历史回测 (真实日K, 无未来函数): 净值曲线/交易流水/胜率/最大回撤."""
    try:
        result = await asyncio.to_thread(
            picker_mgr.backtest_strategy, sid, req.universe, days=req.days,
            cash=req.cash, max_positions=req.max_positions,
            t1_protect=req.t1_protect)
    except KeyError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    return {"ok": True, "strategy_id": sid, **result}


@app.post("/api/picker/groups")
async def add_picker_group(req: PickerGroupReq):
    """新建策略组: 选股插件 + 股票池 -> 买入的股票入库到该策略 ID 的买入组."""
    try:
        entry = picker_mgr.add_group(req.model_dump())
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    return {"ok": True, "group": entry}


@app.put("/api/picker/groups/{gid}")
async def update_picker_group(gid: str, req: PickerGroupPatchReq):
    """更新策略组 (参数/启停/仓位约束). 引擎运行中即时生效."""
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    try:
        saved = picker_mgr.update_group(gid, patch)
    except KeyError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    return {"ok": True, "group": saved}


@app.delete("/api/picker/groups/{gid}")
async def delete_picker_group(gid: str):
    """删除策略组 (买入组持仓与流水保留审计)."""
    if not picker_mgr.remove_group(gid):
        return JSONResponse(status_code=404, content={"detail": f"策略组 {gid} 不存在"})
    return {"ok": True}


@app.post("/api/picker/groups/{gid}/run-once")
async def run_picker_once(gid: str, live: bool = False):
    """手动跑指定策略组一轮扫描 (force 无视盘中时段, 默认模拟下单)."""
    try:
        return await picker_mgr.run_once(gid, live=live)
    except KeyError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})


@app.post("/api/picker/engine/start")
async def start_picker_engine(req: PickerEngineReq):
    return await picker_mgr.start(req.live, req.cash, req.poll_seconds)


@app.post("/api/picker/engine/stop")
async def stop_picker_engine():
    return await picker_mgr.stop()


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

        # 节流: 依据自适应间隔减去已耗时 (至少 1s 下限, 避免 fdata 慢/挂时 elapsed>interval 导致 sleep(0) 忙等 CPU 风暴)
        elapsed = _time.perf_counter() - t0
        await asyncio.sleep(max(1.0, interval - elapsed))


_tick_task: Optional[asyncio.Task] = None
_tick_symbols: list[str] = []


@app.on_event("startup")
async def _start_tick_loop():
    """进程启动后后台跑 tick 循环, 覆盖最常用的 symbol (按需扩展)."""
    global _tick_task, _tick_symbols
    _tick_symbols = ["sz159915", "sh510300", "sh000001"]
    _tick_task = asyncio.create_task(_tick_loop(_tick_symbols))


@app.on_event("startup")
async def _start_watchlist_sync():
    """自选股: 启动同花顺持仓自动同步后台协程 (默认 120s 一轮)."""
    watchlist_mgr.startup()


@app.on_event("shutdown")
async def _stop_watchlist_sync():
    watchlist_mgr.shutdown()


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
        # 阻塞在 receive 上等客户端消息: 前端断开立即抛 WebSocketDisconnect
        # (旧实现用 asyncio.sleep(30) 循环不感知断开, 导致 _ws_mgr.active 拋留已关闭 ws,
        #  tick 循环误判"还有订阅者"持续空转抓行情 → CPU 风暴)
        while True:
            await ws.receive()
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

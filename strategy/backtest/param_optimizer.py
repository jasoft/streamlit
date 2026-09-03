"""参数优化框架: 网格搜索 + 贝叶斯优化, 把 K线周期(TIMEFRAME)作为优化维度.

设计要点 (来自经验教训):
1. 参数名 -> space 维度 顺序一致性: 用 `dim.name` 列表严格 zip，禁止依赖 dict key 顺序。
2. 无交易/异常值惩罚: objective 返回 负的惩罚分，而不是 0 或 NaN，避免优化器被吸引到无效边界。
3. 分母保护: Calmar / Sharpe 等比值型指标统一用 _safe_div 加 epsilon。
4. 数据缓存: 按 (symbol, timeframe, qfq, limit) 缓存 DataFrame，同一 symbol+tf 跨多轮 objective 调用只拉一次。
5. 多周期支持: TIMEFRAME 作为虚拟参数 "_tf" 注入搜索空间，objective 内按 tf 取不同 df 再跑 signal。
"""
from __future__ import annotations

import datetime as dt
import math
import time
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import product
from typing import Any, Callable, Iterable, Optional

import numpy as np
import pandas as pd

from strategy.backtest import vbt_adapter
from strategy.base import FLOAT, INT, Strategy
from strategy.trader import _fetch


# ---------- 评分/指标工具 ----------
EPS = 1e-9


def _safe_div(n: float, d: float, default: float = 0.0) -> float:
    """安全除法: 分母绝对值<=EPS 时返回 default."""
    return default if abs(d) <= EPS else n / d


def _metric_from_stats(stats: dict, metric: str) -> float:
    """从 vbt stats + 扩展字段 (total_return/buyhold_return/max_drawdown)
    计算单值评分. 分数越大越好 (优化器全部走 maximize).

    支持的 metric:
      - total_return  : 总收益率 (默认)
      - buyhold_alpha : total_return - buyhold_return (跑赢买入持有)
      - sharpe        : Sharpe Ratio (年化, 已有 vbt 字段)
      - calmar        : Calmar Ratio = 年化收益 / |最大回撤|
      - win_rate      : 胜率 (Trade Win Rate %, 0-1)
      - calmar_alpha  : calmar × sign(buyhold_alpha)  兼顾风险与跑赢
    """
    tr = float(stats.get("total_return") or 0.0)
    bhr = float(stats.get("buyhold_return") or 0.0)
    mdd = abs(float(stats.get("max_drawdown") or 0.0))
    sharpe = float(stats.get("Sharpe Ratio") or stats.get("sharpe_ratio") or 0.0)
    wr_raw = stats.get("Trade Win Rate") or stats.get("win_rate") or 0.0
    try:
        win_rate = float(wr_raw.rstrip("%")) / 100.0 if isinstance(wr_raw, str) else float(wr_raw)
    except Exception:
        win_rate = 0.0
    # 年化收益近似: vbt 的总收益 * 252 / bars 太粗，直接用总收益估 calmar 也可
    calmar = _safe_div(tr, mdd, 0.0) if mdd > EPS else 0.0
    return {
        "total_return": tr,
        "buyhold_alpha": tr - bhr,
        "sharpe": sharpe,
        "calmar": calmar,
        "win_rate": win_rate,
        "calmar_alpha": calmar * (1.0 if tr > bhr else -0.5),
    }.get(metric, tr)


# ---------- 数据缓存 (per-process，足够，因为优化是单进程串行) ----------
_df_cache: dict[tuple, pd.DataFrame] = {}


def _cached_fetch(symbol: str, qfq: bool, tf: str, limit: Optional[int]) -> pd.DataFrame:
    key = (symbol, qfq, tf, -1 if limit is None else limit)
    if key not in _df_cache:
        _df_cache[key] = _fetch(symbol, qfq, tf, limit)
    return _df_cache[key]


def clear_cache() -> None:
    _df_cache.clear()


# ---------- 空间定义 ----------
@dataclass
class Dim:
    """单个搜索维度. 与 skopt.space.Dimension 语义对齐但不强制依赖 skopt.

    kind:
      - "int"     : integers  [lo, hi]
      - "float"   : real      [lo, hi]
      - "categ"   : categorical, choices=[...]  用于 tf/离散参数
    """
    name: str
    kind: str           # "int" | "float" | "categ"
    lo: float = 0.0
    hi: float = 0.0
    choices: tuple = ()
    prior: str = "uniform"   # for float: uniform / log-uniform

    # -- 方便构造的类方法 --
    @classmethod
    def i(cls, name, lo, hi):
        return cls(name, "int", lo=float(lo), hi=float(hi))

    @classmethod
    def r(cls, name, lo, hi, prior="uniform"):
        return cls(name, "float", lo=float(lo), hi=float(hi), prior=prior)

    @classmethod
    def c(cls, name, choices: Iterable):
        return cls(name, "categ", choices=tuple(choices))


def build_space_from_params(
    strategy: Strategy,
    *,
    timeframes: Iterable[str] = ("5m",),
    overrides: Optional[dict[str, Any]] = None,
) -> list[Dim]:
    """从 strategy.PARAMS 定义 + 可选 tf 列表 构造搜索空间.

    overrides: 可选 {param_name: Dim | list_of_choices}，覆盖 PARAMS 里的 (min,max) 或直接指定离散列表.
               对 "_tf" 也支持，但 timeframes 参数更方便。
    """
    space: list[Dim] = []
    overrides = overrides or {}
    # 1. 周期维度 (放在最前面，方便读日志)
    tf_ov = overrides.get("_tf")
    if tf_ov is not None:
        if isinstance(tf_ov, Dim):
            space.append(tf_ov)
        else:
            space.append(Dim.c("_tf", list(tf_ov)))
    else:
        tfs = list(timeframes)
        if len(tfs) > 1 or (len(tfs) == 1 and tfs[0] != strategy.TIMEFRAME):
            # 只有当用户显式传了多个 tf，或覆盖默认 tf，才把 tf 加入搜索维度；
            # 否则走策略默认 TIMEFRAME，不用浪费一维。
            space.append(Dim.c("_tf", tfs))
    # 2. 策略参数
    for k, spec in strategy.PARAMS.items():
        if not isinstance(spec, dict):
            continue   # 常量参数不进搜索
        ov = overrides.get(k)
        if ov is not None:
            if isinstance(ov, Dim):
                d = ov
                d.name = k   # 强制对齐名字，防止串线
                space.append(d)
            elif isinstance(ov, (list, tuple)):
                space.append(Dim.c(k, list(ov)))
            elif isinstance(ov, dict):
                # {lo, hi, prior?}
                ptype = spec.get("type", FLOAT)
                if ptype == INT:
                    space.append(Dim.i(k, ov["lo"], ov["hi"]))
                else:
                    space.append(Dim.r(k, ov["lo"], ov["hi"], prior=ov.get("prior", "uniform")))
            else:
                raise ValueError(f"override for {k} 格式不支持: {type(ov)}")
        else:
            lo, hi = spec["min"], spec["max"]
            if spec.get("type") == INT:
                space.append(Dim.i(k, lo, hi))
            else:
                space.append(Dim.r(k, lo, hi))
    return space


# ---------- 维度名字 -> 实际取值 的绑定 (顺序安全核心) ----------
def _param_names(space: list[Dim]) -> list[str]:
    """**唯一**的 param_names 来源. 所有 zip(x) 都必须用这个函数."""
    return [d.name for d in space]


def _point_to_params(point: list, space: list[Dim]) -> tuple[str, dict]:
    """把优化器的一个点 (list of values, 与 space 顺序对齐) -> (tf, strategy_params).

    关键点: 用 param_names(space) zip，所以 "_tf" 一定对应到 space[*].name=="_tf" 那维，
    不会因为以后改了 space 顺序而串线.
    """
    names = _param_names(space)
    assert len(point) == len(names), f"长度不匹配 {len(point)} vs {len(names)}"
    tf: Optional[str] = None
    params: dict = {}
    for name, val in zip(names, point):
        if name == "_tf":
            tf = str(val)
        else:
            params[name] = (int(val) if isinstance(val, (int, np.integer)) else float(val))
    return (tf or "5m"), params


# ---------- Objective ----------
@dataclass
class ObjectiveContext:
    strategy: Strategy
    symbol: str
    qfq: bool
    limit: Optional[int]
    cash: float
    # 成本模型 (v1: 旧 fees 保留作为兜底; v2: 四项细分)
    fees: float
    buy_fee: float = 0.0001
    sell_fee: float = 0.0001
    sell_stamp_duty: float = 0.001
    slippage: float = 0.0001
    # 评分 / 惩罚
    metric: str = "calmar"
    penalty: float = -1.0       # 无交易/报错的惩罚分 (必须是负数)
    min_trades: int = 2        # 最少完成交易数，否则惩罚
    buyhold_ref: bool = True   # True 时若 buyhold 是正收益，惩罚那些还没 buyhold 高的组合 (乘以 0.5)

    call_count: int = field(default=0, init=False)
    start_ts: float = field(default_factory=time.time, init=False)


def make_objective(ctx: ObjectiveContext,
                   space: list[Dim],
                   ) -> Callable[[list], tuple[float, dict]]:
    """返回一个 objective(point) -> (score, info_dict).

    info_dict 里含完整 stats+params+tf，供优化过程记录/绘制.
    """
    strat = ctx.strategy
    default_tf = getattr(strat, "TIMEFRAME", "day")

    def _obj(point: list) -> tuple[float, dict]:
        ctx.call_count += 1
        t0 = time.time()
        tf, params = _point_to_params(point, space)
        try:
            df = _cached_fetch(ctx.symbol, ctx.qfq, tf or default_tf, ctx.limit)
            if len(df) < 30:
                return ctx.penalty, {"error": f"数据不足 {len(df)} bars", "tf": tf, "params": params}
            result = vbt_adapter.backtest(
                strat, df, params,
                cash=ctx.cash,
                fees=ctx.fees,                 # 兜底兼容 (非 None 时覆盖)
                buy_fee=ctx.buy_fee,
                sell_fee=ctx.sell_fee,
                sell_stamp_duty=ctx.sell_stamp_duty,
                slippage=ctx.slippage,
            )
            stats = result["stats"]
            n_trades = int(stats.get("Total Trades") or stats.get("total_trades") or 0)
            if n_trades < ctx.min_trades:
                score = ctx.penalty
            else:
                score = _metric_from_stats(stats, ctx.metric)
                if not math.isfinite(score):
                    score = ctx.penalty
                elif (ctx.buyhold_ref
                      and score > 0
                      and float(stats.get("total_return") or 0.0) < float(stats.get("buyhold_return") or 0.0)):
                    # 能跑成正收益但没超过买入持有：打 5 折，让超买持有的组合优先
                    score *= 0.5
            dt_ms = int((time.time() - t0) * 1000)
            info = {
                "tf": tf,
                "params": params,
                "score": score,
                "stats": {k: stats.get(k) for k in (
                    "total_return", "buyhold_return", "max_drawdown",
                    "Total Trades", "Trade Win Rate", "Sharpe Ratio",
                    "Total Return [%]", "Max Drawdown [%]",
                ) if k in stats},
                "n_trades": n_trades,
                "call": ctx.call_count,
                "elapsed_ms": dt_ms,
            }
            return float(score), info
        except Exception as e:
            return ctx.penalty, {"error": f"{type(e).__name__}: {e}",
                                 "tf": tf, "params": params}
    return _obj


# ---------- 1. 网格搜索 ----------
def _iter_grid(space: list[Dim], param_grid: dict[str, list]) -> Iterable[list]:
    """按 param_grid 展开网格. 不在 param_grid 里的维度取默认值(中点/第一个choice).

    param_grid 格式和老 vbt_adapter.optimize 兼容: {"avg_days": [3,5,7], "oversold": [20,25], ...}
    额外支持 "_tf": ["1m","5m","15m"] 来扫周期.
    """
    names = _param_names(space)
    dim_by_name = {d.name: d for d in space}
    axes: list[list] = []
    for n in names:
        if n in param_grid:
            axes.append(list(param_grid[n]))
        else:
            # 单点: int/float 用中点, categ 用第一个
            d = dim_by_name[n]
            if d.kind == "categ":
                axes.append([d.choices[0]])
            elif d.kind == "int":
                axes.append([int((d.lo + d.hi) / 2)])
            else:
                axes.append([(d.lo + d.hi) / 2.0])
    for combo in product(*axes):
        yield list(combo)


@dataclass
class OptimizeResult:
    mode: str                            # "grid" | "bayesian"
    metric: str
    best_score: float
    best_tf: str
    best_params: dict
    best_stats: dict
    n_combos: int
    n_valid: int                         # score > penalty (有效组合数)
    top: list[dict]                      # 前 N 名: {score, tf, params, stats}
    history: list[dict]                  # 完整搜索历史 (info_dict)
    elapsed_sec: float


def run_grid(
    strategy: Strategy,
    symbol: str,
    param_grid: dict[str, list],
    *,
    timeframes: Optional[Iterable[str]] = None,
    qfq: bool = False,
    cash: float = 100_000.0,
    fees: float = 0.0001,
    buy_fee: float = 0.0001,
    sell_fee: float = 0.0001,
    sell_stamp_duty: float = 0.001,
    slippage: float = 0.0001,
    limit: Optional[int] = None,
    metric: str = "calmar",
    top_n: int = 10,
    progress_cb: Optional[Callable[[int, int, float, dict], None]] = None,
) -> OptimizeResult:
    """网格搜索. param_grid 兼容老 API，周期通过 timeframes 或 param_grid["_tf"] 指定."""
    t0 = time.time()
    clear_cache()
    grid = dict(param_grid)
    if timeframes and "_tf" not in grid:
        grid["_tf"] = list(timeframes)
    space = build_space_from_params(strategy, timeframes=grid.get("_tf", []),
                                    overrides={k: v for k, v in grid.items()})
    ctx = ObjectiveContext(
        strategy=strategy, symbol=symbol, qfq=qfq,
        limit=limit, cash=cash, metric=metric,
        fees=fees,                              # 兜底 (None 表示用新四项)
        buy_fee=buy_fee, sell_fee=sell_fee,
        sell_stamp_duty=sell_stamp_duty, slippage=slippage,
    )
    obj = make_objective(ctx, space)

    all_points = list(_iter_grid(space, grid))
    n = len(all_points)
    history: list[dict] = []
    best_score = float("-inf")
    best_info: dict = {}
    for i, pt in enumerate(all_points, 1):
        score, info = obj(pt)
        info["mode"] = "grid"
        history.append(info)
        if score > best_score:
            best_score = score
            best_info = info
        if progress_cb:
            try:
                progress_cb(i, n, score, info)
            except Exception:
                pass

    # top_n: 只取 score > ctx.penalty 的有效组合
    valid = [h for h in history if h.get("score", float("-inf")) > ctx.penalty + 1e-9]
    valid.sort(key=lambda h: h["score"], reverse=True)
    top = [{"score": h["score"], "tf": h["tf"], "params": h["params"],
            "stats": h.get("stats", {}), "n_trades": h.get("n_trades", 0)}
           for h in valid[:top_n]]
    elapsed = round(time.time() - t0, 2)
    return OptimizeResult(
        mode="grid", metric=metric,
        best_score=best_score, best_tf=best_info.get("tf", ""),
        best_params=best_info.get("params", {}), best_stats=best_info.get("stats", {}),
        n_combos=n, n_valid=len(valid), top=top, history=history, elapsed_sec=elapsed,
    )


# ---------- 2. 贝叶斯优化 (scikit-optimize) ----------
def _space_to_skopt(space: list[Dim]):
    """把我们的 Dim 列表 -> skopt.space.Dimension 列表 + names.
    延迟 import，没装 skopt 也能跑网格搜索.
    """
    from skopt.space import Integer, Real, Categorical
    out = []
    for d in space:
        if d.kind == "int":
            out.append(Integer(int(d.lo), int(d.hi), name=d.name))
        elif d.kind == "float":
            out.append(Real(d.lo, d.hi, prior=d.prior, name=d.name))
        else:
            out.append(Categorical(list(d.choices), name=d.name, transform="label"))
    return out


def run_bayesian(
    strategy: Strategy,
    symbol: str,
    *,
    timeframes: Iterable[str] = ("5m",),
    overrides: Optional[dict[str, Any]] = None,
    qfq: bool = False,
    cash: float = 100_000.0,
    fees: float = 0.0001,
    buy_fee: float = 0.0001,
    sell_fee: float = 0.0001,
    sell_stamp_duty: float = 0.001,
    slippage: float = 0.0001,
    limit: Optional[int] = None,
    metric: str = "calmar",
    n_calls: int = 50,
    n_initial_points: int = 15,
    base_estimator: str = "GP",        # "GP" | "ET" | "RF"
    top_n: int = 10,
    progress_cb: Optional[Callable[[int, int, float, dict], None]] = None,
    random_state: int = 42,
) -> OptimizeResult:
    """贝叶斯优化. 对大空间 (参数数>5 或 总组合 > 200) 推荐用这个，n_calls 通常 40-80 就够.

    overrides: 同 build_space_from_params，用来收窄某参数范围或指定离散候选.
    """
    t0 = time.time()
    clear_cache()
    try:
        from skopt import gp_minimize, forest_minimize
    except ImportError as e:
        raise RuntimeError("贝叶斯优化需要 scikit-optimize: pip install scikit-optimize") from e
    space = build_space_from_params(strategy, timeframes=timeframes, overrides=overrides)
    ctx = ObjectiveContext(
        strategy=strategy, symbol=symbol, qfq=qfq,
        limit=limit, cash=cash, metric=metric,
        fees=fees,
        buy_fee=buy_fee, sell_fee=sell_fee,
        sell_stamp_duty=sell_stamp_duty, slippage=slippage,
    )
    obj = make_objective(ctx, space)
    # 关键: skopt 是 minimize，我们的 score 是越大越好 -> 取负
    history: list[dict] = []
    best_score = float("-inf")
    best_info: dict = {}

    def _skopt_obj(point: list) -> float:
        nonlocal best_score, best_info
        score, info = obj(point)
        info["mode"] = "bayesian"
        history.append(info)
        if score > best_score:
            best_score = score
            best_info = info
        if progress_cb:
            try:
                progress_cb(len(history), n_calls, score, info)
            except Exception:
                pass
        return -score   # minimize 负分

    sk_space = _space_to_skopt(space)
    names = _param_names(space)
    # 初始点: 用策略默认参数 + 各 tf 各跑一次
    x0: list[list] = []
    default_point: list = []
    for d in space:
        if d.name == "_tf":
            default_point.append(d.choices[0] if d.choices else "5m")
        else:
            v = strategy.default_params().get(d.name)
            if v is None:
                if d.kind == "categ":
                    v = d.choices[0]
                elif d.kind == "int":
                    v = int((d.lo + d.hi) / 2)
                else:
                    v = (d.lo + d.hi) / 2.0
            default_point.append(v)
    x0.append(default_point)
    # 如果有多个 tf，补一个跑其它 tf 的默认参数组合
    tf_dim = next((d for d in space if d.name == "_tf"), None)
    if tf_dim is not None and len(tf_dim.choices) > 1:
        for tfc in tf_dim.choices[1:3]:   # 最多补 2 个，避免 x0 太大
            pt = list(default_point)
            pt[names.index("_tf")] = tfc
            x0.append(pt)

    minimizer = gp_minimize if base_estimator == "GP" else forest_minimize
    extra = {}
    if base_estimator != "GP":
        extra["base_estimator"] = base_estimator
    try:
        result = minimizer(
            _skopt_obj, sk_space,
            n_calls=max(n_calls, len(x0)),
            n_initial_points=max(n_initial_points, 5),
            x0=x0, random_state=random_state, verbose=False, **extra,
        )
    except Exception:
        # 比如某些老版本 skopt 对 x0 长度挑剔，退化成无 x0 跑
        result = minimizer(
            _skopt_obj, sk_space,
            n_calls=n_calls,
            n_initial_points=n_initial_points,
            random_state=random_state, verbose=False, **extra,
        )
    # 取最佳: skopt 的 result.x 已经是空间维度顺序，但保险起见还是用 history 里追的 best_info
    valid = [h for h in history if h.get("score", float("-inf")) > ctx.penalty + 1e-9]
    valid.sort(key=lambda h: h["score"], reverse=True)
    top = [{"score": h["score"], "tf": h["tf"], "params": h["params"],
            "stats": h.get("stats", {}), "n_trades": h.get("n_trades", 0)}
           for h in valid[:top_n]]
    # 兜底: 如果 history 里 best 比 result.fun 映射回的分差就用 best_info 里的
    elapsed = round(time.time() - t0, 2)
    return OptimizeResult(
        mode="bayesian", metric=metric,
        best_score=best_score, best_tf=best_info.get("tf", ""),
        best_params=best_info.get("params", {}), best_stats=best_info.get("stats", {}),
        n_combos=len(history), n_valid=len(valid), top=top, history=history,
        elapsed_sec=elapsed,
    )


# ---------- 统一入口 ----------
def run(
    mode: str,
    strategy: Strategy,
    symbol: str,
    *,
    # grid 专用
    param_grid: Optional[dict[str, list]] = None,
    # bayesian 专用
    n_calls: int = 50,
    n_initial_points: int = 15,
    base_estimator: str = "GP",
    overrides: Optional[dict[str, Any]] = None,
    # 共有
    timeframes: Optional[Iterable[str]] = None,
    qfq: bool = False,
    cash: float = 100_000.0,
    fees: float = 0.0001,
    buy_fee: float = 0.0001,
    sell_fee: float = 0.0001,
    sell_stamp_duty: float = 0.001,
    slippage: float = 0.0001,
    limit: Optional[int] = None,
    metric: str = "calmar",
    top_n: int = 10,
    progress_cb: Optional[Callable[[int, int, float, dict], None]] = None,
    random_state: int = 42,
) -> OptimizeResult:
    """统一入口: mode="grid" or "bayesian"."""
    common_costs = dict(
        fees=fees,
        buy_fee=buy_fee, sell_fee=sell_fee,
        sell_stamp_duty=sell_stamp_duty, slippage=slippage,
    )
    if mode == "grid":
        if not param_grid:
            raise ValueError("grid 模式必须传 param_grid")
        return run_grid(
            strategy, symbol, param_grid,
            timeframes=timeframes or [],
            qfq=qfq, cash=cash, limit=limit, metric=metric,
            top_n=top_n, progress_cb=progress_cb,
            **common_costs,
        )
    elif mode == "bayesian":
        return run_bayesian(
            strategy, symbol,
            timeframes=timeframes or (getattr(strategy, "TIMEFRAME", "5m"),),
            overrides=overrides,
            qfq=qfq, cash=cash, limit=limit, metric=metric,
            n_calls=n_calls, n_initial_points=n_initial_points,
            base_estimator=base_estimator, top_n=top_n, progress_cb=progress_cb,
            random_state=random_state,
            **common_costs,
        )
    else:
        raise ValueError(f"mode 只能是 grid 或 bayesian, 收到 {mode!r}")


# ---------- 便于 JSON 序列化 ----------
def result_to_dict(r: OptimizeResult) -> dict:
    return {
        "mode": r.mode,
        "metric": r.metric,
        "best_score": float(r.best_score),
        "best_tf": r.best_tf,
        "best_params": r.best_params,
        "best_stats": r.best_stats,
        "n_combos": int(r.n_combos),
        "n_valid": int(r.n_valid),
        "elapsed_sec": float(r.elapsed_sec),
        "top": r.top,
        # history 默认不塞到 API 返回里，太大；只给 top，需要时本地脚本自取
    }

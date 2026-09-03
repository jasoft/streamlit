"""vectorbt 回测适配器: 把策略 signal 喂给 vbt.Portfolio.

策略 signal(df, params) 返回 0/1 目标仓位序列, 转成 entries/exits 布尔序列:
- entries: target 0->1 (买入信号)
- exits:    target 1->0 (卖出信号)

成交口径对齐自研 engine: 收盘出信号 -> 次日开盘成交 (无未来函数).
实现: entries/exits shift(1), price 传 df['open'] -> 信号 t 收盘产生, t+1 开盘成交.

参数网格搜索: 遍历 param_grid, 每组合调 backtest, 按 total_return 排序返回 top N.
"""
from __future__ import annotations

from itertools import product
from typing import Optional

import numpy as np
import pandas as pd

import vectorbt as vbt

from strategy.base import Strategy


def _entries_exits(target: pd.Series) -> tuple[pd.Series, pd.Series]:
    """0/1 目标仓位 -> (entries, exits) 布尔序列. shift(1) 实现次日成交.

    用 numpy 构造 bool 数组, 避开 pandas 2.2 的 fillna(bool) downcasting
    FutureWarning (fillna(False).astype(bool) 触发).
    """
    t = pd.Series(target).fillna(0).to_numpy().astype(int)
    prev = np.zeros_like(t)
    prev[1:] = t[:-1]
    # 信号: target 变化点 (0->1 买入, 1->0 卖出)
    entries_raw = (t == 1) & (prev == 0)
    exits_raw = (t == 0) & (prev == 1)
    # shift(1): 信号 t 收盘产生 -> t+1 成交; 首行无前置信号填 False
    entries = np.zeros_like(entries_raw, dtype=bool)
    exits = np.zeros_like(exits_raw, dtype=bool)
    entries[1:] = entries_raw[:-1]
    exits[1:] = exits_raw[:-1]
    idx = pd.Series(target).index
    return pd.Series(entries, index=idx), pd.Series(exits, index=idx)


def _is_bad_float(v) -> bool:
    """判断是不是 JSON 不合法的 float (nan/inf), 兼容 numpy / 原生 float / int."""
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, np.integer)):
        return False
    try:
        f = float(v)
    except Exception:
        return False
    return f != f or f in (float("inf"), float("-inf"))


def _stats_to_dict(stats) -> dict:
    """vbt stats (Series/dict) -> 普通 dict, 处理 nan/inf.

    关键: vectorbt 把没有交易的字段写成原生 float('nan')(不是 np.floating 类型),
    只检查 isinstance(np.floating) 会漏掉, 导致 FastAPI JSONResponse 抛出
    "ValueError: Out of range float values are not JSON compliant: nan".
    所以这里对一切 float 兼容类型统一用 math.isnan / math.isinf 检查.
    """
    import math
    out = {}
    if isinstance(stats, pd.Series):
        stats = stats.to_dict()
    for k, v in dict(stats).items():
        if isinstance(v, (np.integer,)):
            out[k] = int(v)
        elif isinstance(v, (np.floating,)):
            out[k] = None if (np.isnan(v) or np.isinf(v)) else float(v)
        elif isinstance(v, (np.bool_,)):
            out[k] = bool(v)
        elif isinstance(v, pd.Timestamp):
            out[k] = v.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(v, float):
            out[k] = None if (math.isnan(v) or math.isinf(v)) else v
        else:
            out[k] = None if _is_bad_float(v) else v
    return out


def backtest(strategy: Strategy, df: pd.DataFrame, params: dict,
             cash: float = 100_000.0,
             fees: float | None = None,
             buy_fee: float = 0.0001,
             sell_fee: float = 0.0001,
             sell_stamp_duty: float = 0.001,
             slippage: float = 0.0001) -> dict:
    """用 vectorbt 跑单标的回测. 返回 {stats, equity, markers, close, target}.

    成交口径: 收盘信号 -> 次日开盘成交 (price=open, entries/exits shift 1).

    成本模型 (两个入口任选):
      - 旧 API: 传 `fees=0.0001` 对称费率 + 无滑点 (兼容老调用方)
      - 新 API: 传 `buy_fee` / `sell_fee` / `sell_stamp_duty` / `slippage`.
        合成单测 fees = (buy_fee + sell_fee + stamp_duty) / 2, 用于 vbt 内部;
        slippage 直接走 vbt.Portfolio slippage 参数, 双向生效.
    """
    p = strategy.validate_params(params)
    df = df.copy()
    daily = _is_daily(df)
    target_raw = strategy.signal(df, p)

    dates = pd.to_datetime(df.pop("date"))
    df.index = dates
    num_cols = ["open", "high", "low", "close", "volume", "amount"]
    for c in num_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in ["open", "high", "low", "close"]:
        if c in df.columns and df[c].isna().all():
            df[c] = 0.0
    target_arr = target_raw.to_numpy() if isinstance(target_raw, pd.Series) else target_raw
    target = pd.Series(target_arr, index=df.index).fillna(0).astype(np.int64)
    entries, exits = _entries_exits(target)
    entries = entries.astype(bool)
    exits = exits.astype(bool)
    price_col = "open" if "open" in df.columns else "close"
    price = df[price_col].astype(np.float64)

    # ---- 成本模型: 新 API 优先, 旧 fees 参数为兜底 ----
    if fees is None:
        # 一次完整交易循环(买入+卖出)的总成本率 = 买入费 + 卖出费 + 卖出印花税
        # 分给每个单边 (vbt fees 是对称每笔): 平均 ≈ 总/2
        # 注: 买入不付 stamp, 卖出付, 这个近似在交易次数多时误差很小
        total_roundtrip = float(buy_fee) + float(sell_fee) + float(sell_stamp_duty)
        effective_fees = total_roundtrip / 2.0
    else:
        effective_fees = float(fees)
    effective_slippage = float(slippage)

    pf = vbt.Portfolio.from_signals(
        close=df["close"].astype(np.float64),
        entries=entries,
        exits=exits,
        price=price,
        init_cash=float(cash),
        fees=effective_fees,
        slippage=effective_slippage,
        accumulate=True,
        freq="D" if daily else "5min",
    )

    stats = _stats_to_dict(pf.stats())
    equity = [{"time": _ts(idx), "value": round(float(v), 2)}
              for idx, v in pf.value().items()]
    close = [{"time": _ts(d), "value": float(c)}
             for d, c in zip(dates, df["close"])]
    markers = _markers_from_pf(pf)

    # 买入持有对比曲线: 首根开盘全仓建仓, 之后权益 = 持仓量 × 收盘价
    open0 = float(df["open"].iloc[0]) if "open" in df.columns else float(df["close"].iloc[0])
    buy_qty = cash / open0 if open0 else 0.0
    buyhold = []
    for i, (d, c) in enumerate(zip(dates, df["close"].astype(np.float64))):
        v = float(cash if i == 0 else round(buy_qty * float(c), 2))
        buyhold.append({"time": _ts(d), "value": round(v, 2)})

    stats["exec_pricing"] = "next_open"
    stats["strategy"] = strategy.NAME
    stats["total_return"] = float(pf.total_return())
    stats["max_drawdown"] = float(pf.max_drawdown())
    stats["buyhold_return"] = (round(buy_qty * float(df["close"].iloc[-1]) / cash - 1, 6)
                               if buy_qty and cash else 0.0)
    # 附带本次回测实际使用的成本参数, 便于前端展示/校验
    trade_costs_used = {
        "buy_fee": float(buy_fee),
        "sell_fee": float(sell_fee),
        "sell_stamp_duty": float(sell_stamp_duty),
        "slippage": float(effective_slippage),
        "effective_fees_per_trade": float(effective_fees),
    }
    stats["trade_costs"] = trade_costs_used
    return {
        "stats": stats,
        "equity": equity,
        "buyhold": buyhold,
        "markers": markers,
        "close": close,
        "target": [{"time": _ts(d), "value": int(t)}
                   for d, t in zip(dates, target)],
    }


def _is_daily(df: pd.DataFrame) -> bool:
    """判断是否日线 (date 列/索引含 00:00:00 或纯 date).

    兼容两种 df 形状:
    - "date" 仍是列 (signal 调用前 / 外部传参)
    - date 已被 pop 且 set 为 index (vbt 内部)
    """
    try:
        if "date" in df.columns:
            ts = pd.to_datetime(df["date"])
        else:
            ts = pd.to_datetime(pd.Series(df.index))
        return (ts.dt.time == pd.Timestamp("00:00:00").time()).all()
    except Exception:
        return True


def _ts(val) -> str:
    try:
        return pd.Timestamp(val).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(val)


def _markers_from_pf(pf) -> list:
    """从 vbt Portfolio.trades.records_readable 生成买卖标记 + 盈亏.

    vbt 1.0 字段: Entry Timestamp, Avg Entry Price, Exit Timestamp, Avg Exit Price,
                  Size, PnL, Direction, Status.
    每笔交易生成 2 个 marker (买入 + 卖出), 卖出 marker 带 pnl (该笔盈亏金额).
    只处理 Status=Closed 的交易 (Open 的不计, 因为没平仓无 pnl).
    """
    markers = []
    try:
        df = pf.trades.records_readable
    except Exception:
        return markers
    for _, r in df.iterrows():
        try:
            status = str(r.get("Status", "")).strip()
            if status.lower() not in ("closed", "win", "loss"):
                continue
            size = abs(float(r.get("Size", 0)))
            entry_px = float(r.get("Avg Entry Price", 0))
            exit_px = float(r.get("Avg Exit Price", 0))
            entry_ts = _ts(r.get("Entry Timestamp"))
            exit_ts = _ts(r.get("Exit Timestamp"))
            pnl = float(r.get("PnL", 0))
            markers.append({
                "date": entry_ts, "price": round(entry_px, 3),
                "action": "买入", "qty": int(size), "pnl": None,
            })
            markers.append({
                "date": exit_ts, "price": round(exit_px, 3),
                "action": "卖出", "qty": int(size),
                "pnl": round(pnl, 2),
            })
        except Exception:
            continue
    markers.sort(key=lambda m: m["date"])
    return markers


def _markers_from_signals(entries: pd.Series, exits: pd.Series,
                          df: pd.DataFrame, price: pd.Series) -> list:
    """直接从 entries/exits 布尔序列生成买卖标记 (不依赖 vbt trades 字段名).

    entries/exits 已 shift(1), True 的位置即成交 bar, 用 price (open) 当成交价.
    """
    markers = []
    for arr, action in ((entries, "买入"), (exits, "卖出")):
        idxs = np.where(arr.to_numpy())[0]
        for i in idxs:
            if i < len(df):
                markers.append({
                    "date": _ts(df.iloc[int(i)]["date"]),
                    "price": round(float(price.iloc[int(i)]), 3),
                    "action": action,
                })
    # 按时间排序, 买卖交替更清晰
    markers.sort(key=lambda m: m["date"])
    return markers


def optimize(strategy: Strategy, df: pd.DataFrame, param_grid: dict,
             cash: float = 100_000.0, fees: float | None = None,
             buy_fee: float = 0.0001,
             sell_fee: float = 0.0001,
             sell_stamp_duty: float = 0.001,
             slippage: float = 0.0001,
             metric: str = "total_return", top_n: int = 10) -> dict:
    """参数网格搜索 (旧 API, 保留兼容). param_grid: {param_name: [values]}.

    返回 top N 组合 + 绩效. metric: 排序指标, 默认 total_return (vbt stats 字段名).
    """
    keys = list(param_grid.keys())
    combos = list(product(*[param_grid[k] for k in keys]))
    results = []
    for combo in combos:
        params = dict(zip(keys, combo))
        try:
            r = backtest(strategy, df, params, cash=cash,
                         fees=fees, buy_fee=buy_fee, sell_fee=sell_fee,
                         sell_stamp_duty=sell_stamp_duty, slippage=slippage)
            score = r["stats"].get(metric)
            if score is None:
                continue
            results.append({"params": params, "score": float(score),
                             "stats": r["stats"]})
        except Exception:
            continue
    # 降序排 (total_return 越大越好; max_drawdown 越小越好需特殊处理)
    reverse = metric not in ("max_drawdown",)
    results.sort(key=lambda x: x["score"], reverse=reverse)
    return {
        "metric": metric,
        "n_combos": len(combos),
        "n_valid": len(results),
        "top": results[:top_n],
    }

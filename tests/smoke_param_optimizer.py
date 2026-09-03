"""param_optimizer smoke test: 不用联网, 自己合成 5m OHLCV 喂给 objective,
验证: 1) _point_to_params 名字顺序不串线; 2) objective 能返回 score > 惩罚;
     3) grid 搜索能选出 top; 4) bayesian 能跑(若装了 skopt)."""
from __future__ import annotations

import math
import sys
import os
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from strategy.backtest import param_optimizer as po
from strategy.strategies.intraday_t import IntradayT


def make_fake_5m(days: int = 5, seed: int = 0) -> pd.DataFrame:
    """造 days 个交易日 × 48 根 5m bar = 240 根. 带明显日内趋势方便策略触发交易."""
    rng = np.random.RandomState(seed)
    rows = []
    base = 2.0
    for d in range(days):
        day_start = pd.Timestamp("2026-04-01") + pd.Timedelta(days=d)
        # 每天开盘价相对昨日收盘 + 随机小跳
        open_px = base * (1 + rng.uniform(-0.005, 0.005))
        # 日内做一个 V 形：前半跌 1%, 后半涨 1.5%, 这样日内做T应该能成交
        n_bars = 48
        for i in range(n_bars):
            frac = i / (n_bars - 1)   # 0..1
            # V 形路径: 跌到 0.5 (24bar) 最低, 之后拉回
            v_shape = 1 - 0.012 * (1 - 2 * frac if frac < 0.5 else 2 * frac - 1) * 2
            drift = 1.0 + 0.01 * (frac - 0.5)    # 稍弱日内上涨
            close = open_px * v_shape * drift * (1 + rng.normal(0, 0.0015))
            high = close * (1 + abs(rng.normal(0, 0.0015)))
            low = close * (1 - abs(rng.normal(0, 0.0015)))
            o = open_px if i == 0 else rows[-1]["close"]
            vol_base = 8000 + 12000 * (1 - abs(frac - 0.5) * 2)   # 早盘尾盘放量
            volume = int(vol_base * (1 + rng.uniform(-0.3, 0.3)))
            # 时间戳: 09:30 + i*5min, 跨午间
            minutes_from_open = i * 5
            # 11:30-13:00 午休 90 分钟, 把 >= 24 根 bar 的分钟加 90
            if i >= 24:
                minutes_from_open += 90
            ts = day_start + pd.Timedelta(hours=9, minutes=30) + pd.Timedelta(minutes=minutes_from_open)
            rows.append({"date": ts, "open": round(o, 4), "high": round(high, 4),
                         "low": round(low, 4), "close": round(close, 4), "volume": volume})
        base = close
    return pd.DataFrame(rows)


def test_point_to_params_no_mixup():
    """核心防串线: space 顺序任意打乱后, param_name->value 必须对应."""
    strat = IntradayT()
    space = po.build_space_from_params(
        strat,
        timeframes=["1m", "5m", "15m"],
        overrides={
            "rsi_fast": [4, 6, 8],
            "oversold": {"lo": 15, "hi": 35},
            "avg_days": [3, 5, 7],
        },
    )
    # 手动造一个点，位置对应 space 的顺序
    # 先看 space 顺序: _tf, rsi_fast (categ 列表), oversold (float 15..35), avg_days, 其它按 PARAMS 顺序
    names = po._param_names(space)
    # 造一个确定的点
    point = []
    expected = {}
    for d in space:
        if d.name == "_tf":
            v = "5m"; point.append(v); expected["_tf"] = v
        elif d.name == "rsi_fast":
            v = 6; point.append(v); expected["rsi_fast"] = v
        elif d.name == "oversold":
            v = 25.0; point.append(v); expected["oversold"] = v
        elif d.name == "avg_days":
            v = 5; point.append(v); expected["avg_days"] = v
        elif d.kind == "int":
            v = int((d.lo + d.hi) / 2); point.append(v); expected[d.name] = v
        elif d.kind == "float":
            v = (d.lo + d.hi) / 2.0; point.append(v); expected[d.name] = v
        else:
            v = d.choices[0]; point.append(v); expected[d.name] = v
    tf, params = po._point_to_params(point, space)
    assert tf == expected["_tf"], f"tf 串线: {tf} != {expected['_tf']}"
    for k, v in params.items():
        exp = expected[k]
        if k == "oversold" or isinstance(exp, float):
            ok = abs(float(v) - float(exp)) < 1e-9
        else:
            ok = v == exp or int(v) == int(exp)
        assert ok, f"参数串线 {k}: {v} != {exp}"
    print("[PASS] _point_to_params 不串线, names=", names)


def test_objective_and_grid_smoke():
    """打桩 _cached_fetch，把 objective + run_grid 整条链路跑通."""
    strat = IntradayT()
    fake_df = make_fake_5m(days=6, seed=1)
    # 打桩: 不管 symbol/tf 都返回同一批 5m 数据
    po._cached_fetch = lambda symbol, qfq, tf, limit: fake_df.copy()

    space = po.build_space_from_params(
        strat,
        timeframes=["1m", "5m"],   # 虽然 tf 维度不同，数据都是 fake 5m (为了快速)
        overrides={
            "rsi_fast": [5, 6],
            "oversold": [20, 30],
        },
    )
    ctx = po.ObjectiveContext(strategy=strat, symbol="sz159915", qfq=False,
                              limit=None, cash=100_000.0, fees=0.0001,
                              metric="total_return", min_trades=0, penalty=-99)
    obj = po.make_objective(ctx, space)

    # 1. 两个随机点都要能出分且非 NaN
    scores = []
    for pt_override in (("1m", 5, 20), ("5m", 6, 30)):
        # 构造 point: 顺序要对齐 space 名字列表
        names = po._param_names(space)
        pt = []
        for d in space:
            if d.name == "_tf":
                pt.append(pt_override[0])
            elif d.name == "rsi_fast":
                pt.append(pt_override[1])
            elif d.name == "oversold":
                pt.append(pt_override[2])
            elif d.kind == "int":
                pt.append(int((d.lo + d.hi) / 2))
            elif d.kind == "float":
                pt.append((d.lo + d.hi) / 2.0)
            else:
                pt.append(d.choices[0])
        sc, info = obj(pt)
        scores.append(sc)
        assert math.isfinite(sc), f"score 非有限 {sc} {info}"
        # 不能比惩罚分还低（只要没报错）
        assert sc > ctx.penalty + 1e-9, f"命中惩罚分 {info.get('error')}"
        print(f"  obj pt={pt_override} score={sc:.4f} stats={info.get('stats')}")
    print(f"[PASS] objective 能正常评分, 两组合分={scores}")

    # 2. run_grid 小网格 2x2x2=8 组合
    result = po.run_grid(
        strat, "sz159915",
        param_grid={"rsi_fast": [5, 6], "oversold": [20, 30]},
        timeframes=["1m", "5m"],
        metric="total_return",
        top_n=5,
    )
    assert result.n_combos == 8, f"n_combos 应该是 8，实际 {result.n_combos}"
    assert result.best_score > ctx.penalty, f"最佳分居然是惩罚分: {result.best_score}"
    # 关键: best_tf 必须是我们在 param_grid 的候选项之一
    assert result.best_tf in ("1m", "5m"), f"best_tf 异常: {result.best_tf}"
    # best_params 必须含 rsi_fast/oversold，且值在候选集中
    assert "rsi_fast" in result.best_params
    assert result.best_params["rsi_fast"] in (5, 6)
    assert result.best_params["oversold"] in (20, 30)
    print(f"[PASS] run_grid OK best=tf={result.best_tf} "
          f"rsi_fast={result.best_params.get('rsi_fast')} "
          f"oversold={result.best_params.get('oversold')} "
          f"score={result.best_score:.4f} valid={result.n_valid}/{result.n_combos}")


def test_metric_safety():
    """除零保护 + 无交易惩罚."""
    s1 = po._metric_from_stats({"total_return": 0.1, "max_drawdown": 0.0}, "calmar")
    # max_drawdown=0 时 calmar 必须不是 inf
    assert math.isfinite(s1), f"calmar(分母=0) = {s1} 不是有限值"
    s2 = po._metric_from_stats({"total_return": 0.25, "max_drawdown": 0.05}, "calmar")
    assert abs(s2 - 5.0) < 0.01, f"calmar(0.25/0.05)={s2} 期望 5"
    s3 = po._metric_from_stats({"Trade Win Rate": "60%", "total_return": 0}, "win_rate")
    assert abs(s3 - 0.6) < 1e-9, f"win_rate 解析 '60%' 得 {s3}"
    print("[PASS] 评分安全: calmar 除零/正常值/win_rate % 解析 OK")


if __name__ == "__main__":
    test_point_to_params_no_mixup()
    test_metric_safety()
    test_objective_and_grid_smoke()
    # skopt 可选检测
    try:
        import skopt  # noqa
        strat2 = IntradayT()
        fake_df2 = make_fake_5m(days=5, seed=2)
        po._cached_fetch = lambda s, q, tf, li: fake_df2.copy()
        r = po.run_bayesian(
            strat2, "sz159915",
            timeframes=["5m"],
            overrides={"rsi_fast": [4, 8], "oversold": {"lo": 18, "hi": 32}},
            metric="total_return", n_calls=8, n_initial_points=5,
            base_estimator="RF",   # RF 不调 scipy.optimize，启动更快
        )
        assert math.isfinite(r.best_score), f"bayesian best_score={r.best_score} 非法"
        # skopt 若因为 x0 超出 min(n_calls) 会自动扩一轮; 只要 >= n_calls 就算跑满
        assert r.n_combos >= 8, f"bayesian n_calls 没跑满 {r.n_combos}"
        print(f"[PASS] 贝叶斯优化 smoke OK, best={r.best_score:.4f} "
              f"params={r.best_params}")
    except ImportError:
        print("[SKIP] 未安装 scikit-optimize，跳过贝叶斯 smoke test.  pip install scikit-optimize")
    print("\n== ALL SMOKE TESTS PASSED ==")

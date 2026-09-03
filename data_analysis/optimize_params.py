"""策略参数优化 CLI (grid + 贝叶斯 + 多周期).

使用示例:
  # 1. 网格搜索: 扫 RSI 周期 + 超卖阈值 + 三个周期
  python data_analysis/optimize_params.py grid intraday_t sz159915 \
      --grid rsi_fast=4,6,8 --grid oversold=20,25,30 \
      --timeframes 1m,5m,15m --metric calmar --limit 2000

  # 2. 贝叶斯优化 (默认 GP，n_calls=60，收窄部分参数范围)
  python data_analysis/optimize_params.py bayesian intraday_t sz159915 \
      --timeframes 1m,2m,5m \
      --override-ints rsi_fast=4,10 --override-ints rsi_slow=8,20 \
      --override-floats oversold=15,40 --override-floats vol_burst=1.2,3.0 \
      --metric calmar_alpha --n-calls 60 --limit 2000

  # 3. 离散 override (直接给候选列表)
  python data_analysis/optimize_params.py bayesian intraday_t sz159915 \
      --timeframes 5m \
      --choice avg_days=3,5,7 --choice vwap_band=0.002,0.003,0.005,0.008 \
      --metric total_return --n-calls 40 --limit 1500
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# 允许从项目根导入
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from strategy import registry
from strategy.backtest import param_optimizer


def _parse_grid_item(s: str) -> tuple[str, list]:
    """--grid rsi_fast=4,6,8 -> ("rsi_fast", [4,6,8])"""
    name, vals = s.split("=", 1)
    name = name.strip()
    raw = [v.strip() for v in vals.split(",") if v.strip()]
    parsed: list = []
    for v in raw:
        try:
            if "." in v:
                parsed.append(float(v))
            else:
                parsed.append(int(v))
        except ValueError:
            parsed.append(v)
    return name, parsed


def _parse_range_ints(s: str) -> tuple[str, dict]:
    """--override-ints rsi_fast=4,10 -> ("rsi_fast", {"lo":4,"hi":10})"""
    name, vals = s.split("=", 1)
    lo, hi = vals.split(",")
    return name.strip(), {"lo": int(lo), "hi": int(hi)}


def _parse_range_floats(s: str) -> tuple[str, dict]:
    """--override-floats oversold=15,40 -> ("oversold", {"lo":15.0,"hi":40.0})"""
    name, vals = s.split("=", 1)
    lo, hi = vals.split(",")
    return name.strip(), {"lo": float(lo), "hi": float(hi)}


def _parse_choice(s: str) -> tuple[str, list]:
    """--choice avg_days=3,5,7 -> ("avg_days", [3,5,7])"""
    name, vals = s.split("=", 1)
    raw = [v.strip() for v in vals.split(",") if v.strip()]
    parsed: list = []
    for v in raw:
        try:
            if "." in v:
                parsed.append(float(v))
            else:
                parsed.append(int(v))
        except ValueError:
            parsed.append(v)
    return name.strip(), parsed


def _progress(i: int, n: int, score: float, info: dict):
    pct = f"{i:>4}/{n:<4}" if n else f"{i:>4}   "
    tf = info.get("tf", "?")
    params = info.get("params", {})
    err = info.get("error")
    if err:
        print(f"[opt] {pct} score={score:8.4f} tf={tf:>4} ERR: {err[:80]}")
        return
    pstr = " ".join(f"{k}={v}" for k, v in sorted(params.items()))
    stats = info.get("stats", {})
    tr = stats.get("total_return") or stats.get("Total Return [%]")
    mdd = stats.get("max_drawdown") or stats.get("Max Drawdown [%]")
    nt = info.get("n_trades", "?")
    hint = ""
    if tr is not None:
        hint += f" ret={float(tr)*100:+.2f}%" if abs(float(tr)) < 5 else f" ret={float(tr):+.2f}%"
    if mdd is not None:
        hint += f" mdd={float(mdd)*100:.2f}%" if abs(float(mdd)) < 5 else f" mdd={float(mdd):.2f}%"
    print(f"[opt] {pct} score={score:8.4f} tf={tf:>4} trades={nt:>3} {hint}  {pstr}")


def main():
    ap = argparse.ArgumentParser(description="策略参数优化 (grid / 贝叶斯)")
    sub = ap.add_subparsers(dest="mode", required=True)

    # ---- grid ----
    g = sub.add_parser("grid", help="网格搜索")
    g.add_argument("strategy", help="策略名, 如 intraday_t / ma20_trend")
    g.add_argument("symbol", help="标的代码, 如 sz159915")
    g.add_argument("--grid", action="append", default=[], metavar="k=v1,v2,v3",
                   help="网格维度参数，可重复; 如 --grid rsi_fast=4,6,8 --grid oversold=20,25,30")
    g.add_argument("--timeframes", default="", help="周期搜索，逗号分隔: 1m,2m,5m")
    g.add_argument("--metric", default="calmar", help="评分指标")
    g.add_argument("--limit", type=int, default=0, help="K线根数上限 (0=全量)")
    g.add_argument("--qfq", action="store_true")
    g.add_argument("--cash", type=float, default=100000.0)
    g.add_argument("--fees", type=float, default=0.0001)
    g.add_argument("--top-n", type=int, default=10)
    g.add_argument("-o", "--out", default="", help="结果输出 JSON 路径 (可选)")

    # ---- bayesian ----
    b = sub.add_parser("bayesian", help="贝叶斯优化 (scikit-optimize)")
    b.add_argument("strategy", help="策略名")
    b.add_argument("symbol", help="标的代码")
    b.add_argument("--timeframes", default="", help="周期候选，逗号分隔: 1m,2m,5m")
    b.add_argument("--override-ints", action="append", default=[], metavar="k=lo,hi",
                   help="收窄整数参数范围，如 --override-ints rsi_fast=4,10 (可重复)")
    b.add_argument("--override-floats", action="append", default=[], metavar="k=lo,hi",
                   help="收窄浮点参数范围，如 --override-floats oversold=15,40 (可重复)")
    b.add_argument("--choice", action="append", default=[], metavar="k=v1,v2,v3",
                   help="把某参数变成离散候选，如 --choice avg_days=3,5,7 (可重复)")
    b.add_argument("--metric", default="calmar", help="评分指标")
    b.add_argument("--n-calls", type=int, default=50)
    b.add_argument("--n-init", type=int, default=12, dest="n_init")
    b.add_argument("--base", default="GP", choices=["GP", "ET", "RF"],
                   help="贝叶斯基估计器: GP=高斯过程, ET=极端随机树, RF=随机森林")
    b.add_argument("--limit", type=int, default=0)
    b.add_argument("--qfq", action="store_true")
    b.add_argument("--cash", type=float, default=100000.0)
    b.add_argument("--fees", type=float, default=0.0001)
    b.add_argument("--top-n", type=int, default=10)
    b.add_argument("--seed", type=int, default=42)
    b.add_argument("-o", "--out", default="", help="结果输出 JSON 路径")

    args = ap.parse_args()
    strat = registry.get(args.strategy)
    if strat is None:
        print(f"[x] 未找到策略: {args.strategy}；可用: {list(registry.list_all().keys())}")
        sys.exit(2)

    tfs = [s.strip() for s in args.timeframes.split(",") if s.strip()]
    limit = args.limit or None

    if args.mode == "grid":
        if not args.grid and not tfs:
            print("[x] grid 模式至少传一个 --grid 或 --timeframes")
            sys.exit(2)
        param_grid: dict = {}
        for item in args.grid:
            k, v = _parse_grid_item(item)
            param_grid[k] = v
        print(f"== 网格搜索 == strategy={args.strategy} symbol={args.symbol} "
              f"metric={args.metric} timeframes={tfs or ['默认']}")
        print(f"   param_grid={param_grid}  limit={limit}")
        result = param_optimizer.run_grid(
            strat, args.symbol, param_grid,
            timeframes=tfs or [], metric=args.metric,
            qfq=args.qfq, cash=args.cash, fees=args.fees,
            limit=limit, top_n=args.top_n, progress_cb=_progress,
        )
    else:  # bayesian
        overrides: dict = {}
        for s in args.override_ints:
            k, v = _parse_range_ints(s)
            overrides[k] = v
        for s in args.override_floats:
            k, v = _parse_range_floats(s)
            overrides[k] = v
        for s in args.choice:
            k, lst = _parse_choice(s)
            overrides[k] = lst
        print(f"== 贝叶斯优化 == strategy={args.strategy} symbol={args.symbol} "
              f"metric={args.metric} timeframes={tfs or ['默认']}")
        print(f"   n_calls={args.n_calls} n_init={args.n_init} base={args.base} "
              f"overrides={overrides}  limit={limit}")
        result = param_optimizer.run_bayesian(
            strat, args.symbol,
            timeframes=tfs or (getattr(strat, "TIMEFRAME", "5m"),),
            overrides=overrides or None,
            metric=args.metric,
            n_calls=args.n_calls, n_initial_points=args.n_init,
            base_estimator=args.base,
            qfq=args.qfq, cash=args.cash, fees=args.fees,
            limit=limit, top_n=args.top_n, progress_cb=_progress,
            random_state=args.seed,
        )

    print()
    print("=" * 70)
    print(f"最佳组合 (metric={result.metric} score={result.best_score:.4f} "
          f"tf={result.best_tf}  耗时 {result.elapsed_sec:.1f}s)")
    for k, v in sorted(result.best_params.items()):
        print(f"   {k}: {v}")
    if result.best_stats:
        print(" 关键指标:")
        for k, v in result.best_stats.items():
            print(f"   {k}: {v}")
    print(f" 搜索空间: {result.n_combos} 总, {result.n_valid} 有效")
    if result.top:
        print(f"\nTop {min(len(result.top), args.top_n)}:")
        for i, t in enumerate(result.top, 1):
            tr = t.get("stats", {}).get("total_return") or t.get("stats", {}).get("Total Return [%]")
            mdd = t.get("stats", {}).get("max_drawdown") or t.get("stats", {}).get("Max Drawdown [%]")
            wr = t.get("stats", {}).get("Trade Win Rate")
            tr_s = f"{float(tr)*100:+.2f}%" if tr is not None and abs(float(tr)) < 5 else str(tr)
            mdd_s = f"{float(mdd)*100:.2f}%" if mdd is not None and abs(float(mdd)) < 5 else str(mdd)
            print(f"  #{i:>2} score={t['score']:8.4f} tf={t.get('tf',''):>4} "
                  f"ret={tr_s} mdd={mdd_s} win={wr or '-'} "
                  f"params={t.get('params', {})}")

    d = param_optimizer.result_to_dict(result)
    d["history"] = result.history  # 本地 CLI 可以把全历史带上
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n结果已写入: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

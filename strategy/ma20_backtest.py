"""CLI 包装: MA20 趋势跟踪回测 (逻辑已迁入 strategy/engine.py + strategies/ma20_trend.py).

用法 (项目根目录):
  uv run python strategy/ma20_backtest.py                 # 回测 + 今日信号 (tdx_source, 不复权)
  uv run python strategy/ma20_backtest.py --qfq           # 用 fdata 前复权数据
  uv run python strategy/ma20_backtest.py --code sh510300 --window 30
  uv run python strategy/ma20_backtest.py --dry-order     # 信号触发时 ths_trade --dry-run 联调
完整看板: uv run streamlit run strategy/dashboard.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy import registry, trader  # noqa: E402
from strategy.engine import backtest, today_target  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--code", default="sz159915", help="ETF 代码, 带交易所前缀 (默认 sz159915)")
    ap.add_argument("--window", type=int, default=20)
    ap.add_argument("--qfq", action="store_true", help="用 fdata 前复权数据")
    ap.add_argument("--dry-order", action="store_true",
                    help="对今日信号执行 ths_trade.py --dry-run (需同花顺在运行)")
    args = ap.parse_args()

    strat = registry.get("ma20_trend")
    params = strat.validate_params({"window": args.window})
    df = trader._fetch(args.code, args.qfq)
    print(f"数据: {args.code}, {len(df)} 根日K "
          f"({'fdata 前复权' if args.qfq else '通达信, 不复权'})")

    result = backtest(df, strat.target_position(df, params))
    print(f"\n=== MA{args.window} 趋势跟踪回测 ===")
    print(json.dumps(result["stats"], ensure_ascii=False, indent=2))
    print("逐年收益%:", json.dumps(result["逐年收益%"], ensure_ascii=False))
    print("\n最近 5 笔交易:")
    for t in result["trades"][-5:]:
        print(f"  {t['entry_date'][:10]} 买入@{t['entry']} -> "
              f"{t['exit_date'][:10]} 卖出@{t['exit']}  {t['ret_pct']:+.2f}%")

    sig = today_target(df, strat.target_position(df, params))
    signal = "持有" if sig["target"] == 1 else "卖出/空仓"
    print(f"\n=== 今日信号 ({sig['date'][:10]}) ===")
    print(f"收盘 {sig['close']} -> {signal}")

    if args.dry_order and sig["target"] == 1:
        qty = trader.qty_for(10_000, sig["close"])
        order = {"symbol": args.code, "action": "buy", "qty": qty, "price": sig["close"]}
        print(json.dumps(trader.execute(order, dry_run=True), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

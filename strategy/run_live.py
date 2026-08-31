"""定时任务入口 (cron): 全部启用策略各跑一轮, 默认 dry-run, 不常驻.

用法 (项目根目录):
  uv run python strategy/run_live.py                 # 全部启用策略, dry-run
  uv run python strategy/run_live.py --execute       # 真实下单 (需同花顺客户端在运行)
  uv run python strategy/run_live.py --only ma20_trend
cron 例 (交易日 14:55): 55 14 * * 1-5  cd /Users/weiwang/Projects/streamlit && uv run python strategy/run_live.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy import config as config_mod  # noqa: E402
from strategy import trader  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true",
                    help="真实下单 (默认跟随 config live.dry_run)")
    ap.add_argument("--only", default=None, help="只跑指定策略")
    args = ap.parse_args()

    cfg = config_mod.load()["strategies"]
    for name, scfg in cfg.items():
        if args.only and name != args.only:
            continue
        if not scfg["enabled"]:
            print(f"[{name}] 停用, 跳过")
            continue
        summary = trader.run_once(name, scfg,
                                  dry_run=None if args.execute else True)
        print(f"[{name}] {json.dumps(summary, ensure_ascii=False)}", flush=True)


if __name__ == "__main__":
    main()

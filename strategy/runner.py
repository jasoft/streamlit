"""策略实盘运行进程: 常驻循环.

盘中 (交易日 9:25-11:30 / 13:00-15:05) 每 poll_seconds 评估一轮:
每次取数都输出处理结果 (价格/均线/目标仓位/是否触发警报), 写入心跳与 evals 日志;
到 execute_time 当天执行一次 trader.run_once (信号->下单).
其余时间休眠到下一个评估窗口.

状态文件 (state/):
  {name}.heartbeat.json  心跳: status/最近评估结果/下次时间/最近执行摘要
  {name}.evals.jsonl     每次取数的处理结果流水 (看板读取展示)
  {name}.log             运行日志
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys_path = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(sys_path))

from strategy import config as config_mod  # noqa: E402
from strategy import registry, trader  # noqa: E402

STATE_DIR = Path(__file__).resolve().parent / "state"

SESSIONS = [(dt.time(9, 25), dt.time(11, 30)),
            (dt.time(13, 0), dt.time(15, 5))]


def is_trading_day(now: dt.datetime) -> bool:
    """粗判: 周末剔除. 法定节假日评估照跑 (数据日期不变, 信号不变, 无副作用)."""
    return now.weekday() < 5


def in_session(now: dt.datetime) -> bool:
    t = now.time()
    return any(lo <= t <= hi for lo, hi in SESSIONS)


def next_session_start(now: dt.datetime) -> dt.datetime:
    """下一个评估窗口开始时刻 (今天未开盘则今天 9:25, 否则下个交易日 9:25)."""
    run = now.replace(hour=9, minute=25, second=0, microsecond=0)
    while not (is_trading_day(run) and run > now):
        run += dt.timedelta(days=1)
    return run


def heartbeat(name: str, payload: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    payload["ts"] = dt.datetime.now().isoformat(timespec="seconds")
    path = STATE_DIR / f"{name}.heartbeat.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def sleep_until(run_at: dt.datetime, name: str, status: str) -> None:
    """分段睡眠 (5s 粒度), 保证 SIGTERM 及时生效."""
    while True:
        remain = (run_at - dt.datetime.now()).total_seconds()
        if remain <= 0:
            return
        heartbeat(name, {"status": status,
                         "next_eval": run_at.isoformat(timespec="seconds")})
        time.sleep(min(5, remain))


def run_loop(name: str, once: bool = False) -> None:
    registry.discover()  # 触发策略校验 (首次)
    cfg = config_mod.load().get("strategies", {}).get(name)
    if cfg is None:
        raise SystemExit(f"config.json 中没有策略 {name}")
    if not cfg.get("enabled", True):
        # 启动时就禁用, 直接退出 (manager.start 前应已检查, 兜底)
        heartbeat(name, {"status": "disabled", "msg": "config.enabled=false"})
        raise SystemExit(f"{name} 在 config 中已禁用")
    live = cfg["live"]
    poll = int(live.get("poll_seconds", 60))
    h, m = map(int, live["execute_time"].split(":"))
    executed_date = None
    skip_session = live.get("skip_session_check", False)

    # 每多少轮重新读一次 config (检查 enabled/参数变更), 避免每次 reload I/O
    RELOAD_EVERY = 3  # 每 3 轮 (~15s) 检查一次 enabled

    loop_count = 0
    while True:
        loop_count += 1
        # 周期性 reload config 检测 enabled=false (前端改 config 后应能自动退出)
        if loop_count % RELOAD_EVERY == 0:
            try:
                latest = config_mod.load().get("strategies", {}).get(name)
            except Exception as e:
                latest = None
                print(f"[{name}] reload config 失败: {e!r}", flush=True)
            if latest is None:
                heartbeat(name, {"status": "stopped", "msg": "config 中策略已删除"})
                print(f"[{name}] config 中已删除策略, 退出", flush=True)
                break
            if not latest.get("enabled", True):
                heartbeat(name, {"status": "stopped", "msg": "config.enabled=false"})
                print(f"[{name}] enabled 已置 false, 优雅退出", flush=True)
                break
            cfg = latest  # 顺便用最新参数覆盖
            live = cfg["live"]
            poll = int(live.get("poll_seconds", 60))
            skip_session = live.get("skip_session_check", False)
            # 注意: execute_time 不在这里更新 (避免半夜 reload 跳过当天下单)

        if once:
            summary = trader.run_once(name, cfg)
            heartbeat(name, {"status": "ran-once", "summary": summary})
            print(f"[{name}] {json.dumps(summary, ensure_ascii=False)}", flush=True)
            break

        now = dt.datetime.now()
        if not skip_session and (not is_trading_day(now) or not in_session(now)):
            nxt = next_session_start(now)
            print(f"[{name}] 休市, 下次评估 {nxt}", flush=True)
            sleep_until(nxt, name, "waiting")
            continue

        # ---- 盘中: 评估 + 执行 ----
        hb = {"status": "watching", "last_eval": now.isoformat(timespec="seconds")}
        try:
            if live.get("execute_every_poll"):
                # 测试策略: 每轮直接 run_once (信号+下单+记账一次完成)
                # run_once 内部调 compute_signal → target_position, 不再单独 evaluate 避免计数器重复自增
                summary = trader.run_once(name, cfg)
                hb["last_run"] = now.isoformat(timespec="seconds")
                hb["last_run_summary"] = summary
                # 从 summary 构造 evals (供前端展示)
                evals = []
                for s in summary.get("signals", []):
                    evals.append({
                        "ts": now.isoformat(timespec="seconds"),
                        "symbol": s["symbol"], "price": s["close"],
                        "target": s["target"],
                        "alert": bool(summary.get("executed")),
                        "msg": (f"⚠️ 执行{summary['executed'][0]['action']}" 
                                if summary.get("executed") else "未触发"),
                    })
                trader.append_evals(name, evals)
                hb["results"] = evals
                hb["alert"] = any(e["alert"] for e in evals)
                for r in evals:
                    print(f"[{name}] {r['ts']} {r['symbol']} price={r['price']} "
                          f"target={r['target']} {r['msg']}", flush=True)
                if summary["executed"]:
                    print(f"[{name}] 执行: {json.dumps(summary, ensure_ascii=False)}",
                          flush=True)
            else:
                # 正常策略: 先评估, 到点才下单
                results = trader.evaluate(name, cfg)
                trader.append_evals(name, results)
                hb["results"] = results
                hb["alert"] = any(r["alert"] for r in results)
                for r in results:
                    print(f"[{name}] {r['ts']} {r['symbol']} price={r['price']} "
                          f"target={r['target']} {r['msg']}", flush=True)

                run_at = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if now >= run_at and executed_date != now.date().isoformat():
                    summary = trader.run_once(name, cfg)
                    executed_date = now.date().isoformat()
                    hb["last_run"] = now.isoformat(timespec="seconds")
                    hb["last_run_summary"] = summary
                    print(f"[{name}] 执行: {json.dumps(summary, ensure_ascii=False)}",
                          flush=True)
        except Exception as e:
            hb["status"], hb["error"] = "error", repr(e)
            print(f"[{name}] ERROR {e!r}", flush=True)
        heartbeat(name, hb)
        time.sleep(poll)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--once", action="store_true", help="立即跑一轮后退出 (cron 用)")
    args = ap.parse_args()
    registry.discover()  # 触发策略校验
    run_loop(args.strategy, once=args.once)

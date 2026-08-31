"""策略管理器: 启动/停止/状态 (每策略一个独立常驻进程).

用法 (项目根目录):
  uv run python strategy/manager.py list            # 列出全部策略与运行状态
  uv run python strategy/manager.py start ma20_trend
  uv run python strategy/manager.py stop  ma20_trend
  uv run python strategy/manager.py status
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

STATE_DIR = Path(__file__).resolve().parent / "state"
RUNNER = Path(__file__).resolve().parent / "runner.py"


def pid_path(name: str) -> Path:
    return STATE_DIR / f"{name}.pid"


def is_running(name: str) -> bool:
    p = pid_path(name)
    if not p.exists():
        return False
    pid = int(p.read_text().strip())
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        p.unlink(missing_ok=True)  # 清理陈旧 pid 文件
        return False


def start(name: str) -> dict:
    if is_running(name):
        return {"ok": False, "msg": f"{name} 已在运行 (pid {pid_path(name).read_text().strip()})"}
    STATE_DIR.mkdir(exist_ok=True)
    log = open(STATE_DIR / f"{name}.log", "ab")
    proc = subprocess.Popen(
        [sys.executable, str(RUNNER), "--strategy", name],
        stdout=log, stderr=subprocess.STDOUT,
        cwd=str(Path(__file__).resolve().parent.parent),
        start_new_session=True,  # 脱离父进程, 看板/终端退出不影响
    )
    pid_path(name).write_text(str(proc.pid) + "\n")
    return {"ok": True, "msg": f"{name} 已启动 (pid {proc.pid})"}


def stop(name: str) -> dict:
    if not is_running(name):
        return {"ok": False, "msg": f"{name} 未在运行"}
    pid = int(pid_path(name).read_text().strip())
    os.killpg(os.getpgid(pid), signal.SIGTERM)
    for _ in range(20):  # 最多等 2s
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
    pid_path(name).unlink(missing_ok=True)
    return {"ok": True, "msg": f"{name} 已停止 (pid {pid})"}


def status() -> list:
    from strategy import config as config_mod
    from strategy.registry import discover
    strategies = discover()
    cfg = config_mod.load(strategies)["strategies"]
    rows = []
    for name, strat in strategies.items():
        running = is_running(name)
        hb_path = STATE_DIR / f"{name}.heartbeat.json"
        hb = json.loads(hb_path.read_text(encoding="utf-8")) if hb_path.exists() else {}
        rows.append({
            "name": name, "title": strat.TITLE, "enabled": cfg[name]["enabled"],
            "symbols": cfg[name]["symbols"], "running": running,
            "pid": int(pid_path(name).read_text()) if running else None,
            "status": hb.get("status", "-"),
            "next_run": hb.get("next_eval", hb.get("next_run", "-"))[:16],
            "last_run": hb.get("last_run", "-")[:16],
        })
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["list", "start", "stop", "status"])
    ap.add_argument("name", nargs="?", default=None)
    args = ap.parse_args()

    if args.cmd == "list":
        for row in status():
            flag = "▶" if row["running"] else "·"
            print(f" {flag} {row['name']:<12} {row['title']:<10} "
                  f"{'启用' if row['enabled'] else '停用'}  标的={','.join(row['symbols'])}")
    elif args.cmd == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif args.cmd == "start":
        if not args.name:
            raise SystemExit("用法: manager.py start <name>")
        print(start(args.name)["msg"])
    elif args.cmd == "stop":
        if not args.name:
            raise SystemExit("用法: manager.py stop <name>")
        print(stop(args.name)["msg"])


if __name__ == "__main__":
    main()

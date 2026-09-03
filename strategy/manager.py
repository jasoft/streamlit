"""策略管理器: 启动/停止/状态 (每策略一个独立常驻进程).

用法 (项目根目录):
  uv run python strategy/manager.py list            # 列出全部策略与运行状态
  uv run python strategy/manager.py start ma20_trend
  uv run python strategy/manager.py stop  ma20_trend
  uv run python strategy/manager.py status

环境变量继承: 启动子进程时自动继承父进程的 MOCK_MARKET / MOCK_SPEED 等,
  确保休市 mock 模式下前端点启动也能正常产生数据.
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
# 启动 runner 时需要从父进程继承的环境变量 (mock/调试相关)
_PASS_ENV = ("MOCK_MARKET", "MOCK_SPEED")


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


def _find_runner_pids_by_name(name: str) -> list[int]:
    """按命令行关键词兜底查 runner 进程 (处理 pid 文件缺失的场景)."""
    import re
    marker = f"--strategy {name}"
    out = subprocess.run(
        ["ps", "-eo", "pid,command"], capture_output=True, text=True,
    ).stdout
    pids = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\s*(\d+)\s+(.*)", line)
        if not m:
            continue
        pid_s, cmd = m.group(1), m.group(2)
        if ("runner.py" in cmd or "strategy.runner" in cmd) and marker in cmd:
            try:
                pids.append(int(pid_s))
            except ValueError:
                pass
    return pids


def start(name: str) -> dict:
    if is_running(name):
        return {"ok": False, "msg": f"{name} 已在运行 (pid {pid_path(name).read_text().strip()})"}
    # 即使 pid 文件没了, 也要防止同 name 重复启动 (之前手动启动的 runner 不写 pid)
    existing = _find_runner_pids_by_name(name)
    if existing:
        # 找到同名 runner, 尝试写入 pid 文件后复用, 避免再开一个
        pid = existing[0]
        STATE_DIR.mkdir(exist_ok=True)
        pid_path(name).write_text(str(pid) + "\n")
        return {"ok": True, "msg": f"{name} 已接管现有进程 (pid {pid})"}

    STATE_DIR.mkdir(exist_ok=True)
    log = open(STATE_DIR / f"{name}.log", "ab")

    # 继承父进程的 MOCK_* 环境变量
    env = os.environ.copy()
    for k in _PASS_ENV:
        v = os.environ.get(k)
        if v is not None:
            env[k] = v

    proc = subprocess.Popen(
        [sys.executable, str(RUNNER), "--strategy", name],
        stdout=log, stderr=subprocess.STDOUT,
        cwd=str(Path(__file__).resolve().parent.parent),
        start_new_session=True,  # 脱离父进程, 看板/终端退出不影响
        env=env,
    )
    pid_path(name).write_text(str(proc.pid) + "\n")
    return {"ok": True, "msg": f"{name} 已启动 (pid {proc.pid})"}


def stop(name: str) -> dict:
    stopped_any = False
    # 先按 pid 文件杀
    if is_running(name):
        pid = int(pid_path(name).read_text().strip())
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            stopped_any = True
        except (ProcessLookupError, PermissionError):
            pass
        # 等待退出
        for _ in range(20):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        pid_path(name).unlink(missing_ok=True)

    # 兜底: 按 name 关键词再杀一遍 (防止 pid 文件缺失/手动启动的进程)
    orphans = _find_runner_pids_by_name(name)
    for pid in orphans:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            stopped_any = True
        except (ProcessLookupError, PermissionError):
            pass
        for _ in range(20):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break

    if not stopped_any:
        return {"ok": False, "msg": f"{name} 未在运行"}
    return {"ok": True, "msg": f"{name} 已停止"}


def status() -> list:
    from strategy import config as config_mod
    from strategy.registry import discover
    strategies = discover()
    cfg = config_mod.load(strategies)["strategies"]
    rows = []
    for name, strat in strategies.items():
        running = is_running(name)
        # 兜底: pid 文件没了但进程还在, 仍标 running
        if not running and _find_runner_pids_by_name(name):
            running = True
        hb_path = STATE_DIR / f"{name}.heartbeat.json"
        hb = json.loads(hb_path.read_text(encoding="utf-8")) if hb_path.exists() else {}
        rows.append({
            "name": name, "title": strat.TITLE, "enabled": cfg[name]["enabled"],
            "symbols": cfg[name]["symbols"], "running": running,
            "pid": int(pid_path(name).read_text()) if pid_path(name).exists() else None,
            "status": hb.get("status", "-"),
            "next_run": str(hb.get("next_eval", hb.get("next_run", "-")))[:16],
            "last_run": str(hb.get("last_run", "-"))[:16],
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

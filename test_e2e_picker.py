#!/usr/bin/env python3
"""选股自动交易系统 全链路 e2e 测试 (真实 fdata 行情 + 模拟下单, 不碰同花顺).

覆盖:
1. 运行文件保障: .env / strategy/config.json / backend/charts.db 从主 worktree 复制
2. 独立端口起后端 (空闲端口 + 独立临时 SQLite), 不影响主 worktree 的 8000/3001/9701
3. API 全量: 插件目录 / 组 CRUD + 参数校验 / 引擎启停 / run-once / 只读状态视图
4. 真实行情选股买入: rsi_rebound 宽松参数 -> 持仓入库;
   校验 名称 != 代码 (选股名称 bug 回归) / 数量 / 买入原因 / 指令流水 / 资金扣减
5. 买卖闭环: t1_protect=false + 宽松卖出参数 -> 第二轮扫描卖出 -> sold 记录 + 卖出流水
6. volume_breakout 插件真实行情扫描不崩
7. 前端链路: next dev (BACKEND_PORT 指向测试后端) -> /picker 200 + /api/backend/picker 代理
8. 结束清理全部子进程; PASS/FAIL 汇总, 退出码 0/1

前提: fdata serve 已在运行 (主 worktree dev.sh 会拉起, 端口 9701).
用法: uv run python test_e2e_picker.py [--skip-frontend]
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
SKIP_FRONTEND = "--skip-frontend" in sys.argv
TS = time.strftime("%H%M%S")

_results: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""), flush=True)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(method: str, url: str, body: dict | None = None, timeout: float = 120):
    """请求 JSON API; 返回 (status, json). 4xx/5xx 也返回 (不抛异常)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:  # noqa: BLE001
            return e.code, {}


def wait_http(url: str, want: int = 200, timeout: float = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if r.status == want:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(1)
    return False


def copy_runtime_files() -> None:
    """从主 worktree 复制运行所需文件 (.env / 配置 / 图会话库 / 策略状态)."""
    main = next(Path(l.split("worktree ", 1)[1].strip())
                for l in subprocess.run(
                    ["git", "worktree", "list", "--porcelain"], cwd=REPO,
                    capture_output=True, text=True).stdout.splitlines()
                if l.startswith("worktree "))
    pairs = [
        (main / ".env", REPO / ".env"),
        (main / "strategy" / "config.json", REPO / "strategy" / "config.json"),
        (main / "backend" / "charts.db", REPO / "backend" / "charts.db"),
    ]
    for src, dst in pairs:
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    state_src = main / "strategy" / "state"
    if state_src.exists():
        (REPO / "strategy" / "state").mkdir(exist_ok=True)
        for f in list(state_src.glob("*.state.json")) + list(state_src.glob("*.evals.jsonl")):
            dst = REPO / "strategy" / "state" / f.name
            if not dst.exists():
                shutil.copy2(f, dst)
    missing = [str(d) for _, d in pairs if not d.exists()]
    step("运行文件 (.env/config.json/charts.db)", not missing,
         "缺失: " + ", ".join(missing) if missing else "齐备")


def main() -> int:
    print("== 选股自动交易系统 e2e ==")
    copy_runtime_files()

    tmpdir = tempfile.mkdtemp(prefix="picker_e2e_")
    backend_port, frontend_port = free_port(), free_port()
    env = {**os.environ, "PICKER_DB_PATH": str(Path(tmpdir) / "e2e.db"),
           "BACKEND_PORT": str(backend_port)}
    procs: list[subprocess.Popen] = []
    try:
        # ---- 后端 ----
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "backend.main:app",
             "--host", "127.0.0.1", "--port", str(backend_port)],
            cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        procs.append(proc)
        base = f"http://127.0.0.1:{backend_port}"
        if not wait_http(f"{base}/api/health", timeout=90):
            step("后端启动", False, "90s 内 /api/health 未就绪")
            return _summary()
        _, health = http("GET", f"{base}/api/health")
        if not health.get("fdata"):
            step("fdata serve 可用", False,
                 f"health={health}; 请先启动 fdata (主 worktree dev.sh 或 "
                 f"uv run python trading/fdata.py serve)")
            return _summary()
        step("后端启动 (独立端口 + 临时 SQLite)", True, f"port={backend_port}")

        # ---- 插件目录 ----
        _, plugins = http("GET", f"{base}/api/picker/pickers")
        ids = {p["id"] for p in plugins}
        step("插件目录 (rsi_rebound + volume_breakout)",
             {"rsi_rebound", "volume_breakout"} <= ids, str(sorted(ids)))

        # ---- 组 CRUD + 校验 ----
        st, _ = http("POST", f"{base}/api/picker/groups",
                     {"picker": "no_such", "universe": ["601899"]})
        step("非法插件 -> 400", st == 400)
        st, _ = http("POST", f"{base}/api/picker/groups",
                     {"picker": "rsi_rebound", "universe": ["601899"], "per_qty": 150})
        step("per_qty 非 100 整数倍 -> 400", st == 400)
        uni = ["601899", "600519", "000858", "601318", "300750", "002594", "600036"]
        ga = f"e2e_rsi_{TS}"
        st, r = http("POST", f"{base}/api/picker/groups", {
            "strategy_id": ga, "title": "e2e 超跌反弹", "picker": "rsi_rebound",
            "universe": uni, "params": {"rsi_buy": 45, "vol_ratio": 0.5},
            "per_qty": 1000, "max_positions": 3})
        step("新建 rsi_rebound 策略组", st == 200 and r.get("group", {}).get("picker") == "rsi_rebound")
        gb = f"e2e_vbreak_{TS}"
        st, r = http("POST", f"{base}/api/picker/groups", {
            "strategy_id": gb, "picker": "volume_breakout", "universe": uni,
            "per_qty": 1000, "max_positions": 2})
        step("新建 volume_breakout 策略组", st == 200)
        st, r = http("PUT", f"{base}/api/picker/groups/{gb}", {"max_positions": 5})
        step("PUT 更新组参数", st == 200 and r.get("group", {}).get("max_positions") == 5)

        # ---- 引擎启动 + 买入选股 (真实行情) ----
        st, r = http("POST", f"{base}/api/picker/engine/start",
                     {"live": False, "cash": 100000, "poll_seconds": 5})
        step("引擎启动 (模拟)", st == 200 and r.get("status", {}).get("running") is True)
        st, r = http("POST", f"{base}/api/picker/groups/{ga}/run-once")
        grp = r.get("group") or {}
        holdings = grp.get("holdings") or []
        step("run-once 选股买入", st == 200 and 0 < len(holdings) <= 3,
             f"持仓 {[p['code'] for p in holdings]}" if holdings else
             "本轮无候选 (放宽 rsi_buy/vol_ratio 后重试)")
        if holdings:
            names_ok = all(p["name"] and p["name"] != p["code"] for p in holdings)
            step("持仓名称 != 代码 (名称 bug 回归)", names_ok,
                 str([(p["code"], p["name"]) for p in holdings]))
            step("持仓数量/买入价/原因入库",
                 all(0 < p["qty"] <= 1000 and p["qty"] % 100 == 0
                     and p["buy_price"] > 0 and p["buy_reason"] for p in holdings),
                 f"qty={[p['qty'] for p in holdings]} (现金约束可下调整手数)")
        _, st_ = http("GET", f"{base}/api/picker")
        buys = [e for e in st_["events"] if e["strategy_id"] == ga
                and e["side"] == "buy" and e["status"] == "filled"]
        step("买入指令流水 (filled [模拟])", len(buys) == len(holdings))
        pf = st_["portfolios"].get(ga, {})
        step("组资金扣减 (独立 Portfolio)", pf.get("cash", 1e9) < 100000,
             f"cash={pf.get('cash')}")

        # 去重: 第二轮相同候选不重复买入
        http("POST", f"{base}/api/picker/groups/{ga}/run-once")
        _, st_ = http("GET", f"{base}/api/picker")
        ga2 = next(g for g in st_["groups"] if g["strategy_id"] == ga)
        step("第二轮去重 (已持有不再买)", len(ga2["holdings"]) == len(holdings))

        # ---- 买卖闭环: t1_protect=false + 宽松卖出参数 ----
        gc = f"e2e_loop_{TS}"
        http("POST", f"{base}/api/picker/groups", {
            "strategy_id": gc, "picker": "rsi_rebound", "universe": ["600036"],
            "params": {"rsi_buy": 100, "vol_ratio": 0, "take_profit_pct": -10,
                       "stop_loss_pct": -100, "rsi_sell": 101},
            "per_qty": 1000, "max_positions": 1, "t1_protect": False})
        http("POST", f"{base}/api/picker/groups/{gc}/run-once")   # 买入
        _, st_ = http("GET", f"{base}/api/picker")
        gc_holding = next((g for g in st_["groups"] if g["strategy_id"] == gc), {})
        bought = len(gc_holding.get("holdings") or []) == 1
        step("闭环组买入 600036", bought)
        http("POST", f"{base}/api/picker/groups/{gc}/run-once")   # 卖出 (pnl>=-10% 必触发)
        _, st_ = http("GET", f"{base}/api/picker")
        gc2 = next(g for g in st_["groups"] if g["strategy_id"] == gc)
        sells = [e for e in st_["events"] if e["strategy_id"] == gc
                 and e["side"] == "sell" and e["status"] == "filled"]
        step("卖出指令成交 -> 移出买入组", len(gc2["holdings"]) == 0 and len(sells) == 1,
             f"sold={sells[0]['detail'] if sells else '无'}")

        # ---- volume_breakout 真实行情扫描 ----
        st, r = http("POST", f"{base}/api/picker/groups/{gb}/run-once")
        step("volume_breakout 扫描不崩", st == 200 and r.get("ok") is True)

        # ---- 策略库: 规则策略创建 -> 引擎即时可用 -> 买卖闭环 -> 回测 -> 删除保护 ----
        _, rt = http("GET", f"{base}/api/picker/rule-types")
        step("条件原语目录", bool(rt.get("buy")) and bool(rt.get("sell")))
        gs = f"st_e2e_{TS}"
        st, r = http("POST", f"{base}/api/picker/strategies", {
            "id": gs, "title": "e2e 规则策略",
            "buy_rules": [{"type": "pct_change_above", "pct": -100}],   # 必中
            "sell_rules": [{"type": "take_profit", "pct": -100}]})      # 必卖
        step("新建规则策略", st == 200 and r.get("strategy", {}).get("id") == gs)
        gd = f"e2e_rule_{TS}"
        http("POST", f"{base}/api/picker/groups", {
            "strategy_id": gd, "picker": gs, "universe": ["600036"],
            "per_qty": 1000, "max_positions": 1, "t1_protect": False})
        http("POST", f"{base}/api/picker/groups/{gd}/run-once")
        _, st_ = http("GET", f"{base}/api/picker")
        gd_h = next((g for g in st_["groups"] if g["strategy_id"] == gd), {})
        gd_ok = len(gd_h.get("holdings") or []) == 1
        step("规则策略组选股买入 (引擎运行中创建即时生效)", gd_ok)
        http("POST", f"{base}/api/picker/groups/{gd}/run-once")
        _, st_ = http("GET", f"{base}/api/picker")
        gd2 = next((g for g in st_["groups"] if g["strategy_id"] == gd), {})
        step("规则策略卖出 -> 移出买入组", len(gd2.get("holdings") or []) == 0)
        st, r = http("POST", f"{base}/api/picker/strategies/{gs}/backtest",
                     {"universe": ["600036"], "days": 120, "cash": 100000,
                      "max_positions": 2})
        m = r.get("metrics") or {}
        step("策略回测 (净值/胜率/回撤)", st == 200 and len(r.get("equity") or []) == 120
             and "total_return_pct" in m and "win_rate_pct" in m,
             f"收益 {m.get('total_return_pct')}% / {m.get('trades')} 笔")
        st, _ = http("DELETE", f"{base}/api/picker/strategies/{gs}")
        step("删除被引用策略 -> 400", st == 400)
        http("DELETE", f"{base}/api/picker/groups/{gd}")
        st, _ = http("DELETE", f"{base}/api/picker/strategies/{gs}")
        step("删除未引用策略 -> 200", st == 200)

        # ---- 组管理: 停用/删除 ----
        st, _ = http("PUT", f"{base}/api/picker/groups/{gb}", {"enabled": False})
        _, st_ = http("GET", f"{base}/api/picker")
        gb_enabled = next(g["enabled"] for g in st_["groups"] if g["strategy_id"] == gb)
        step("PUT 停用组", st == 200 and not gb_enabled)
        st, _ = http("DELETE", f"{base}/api/picker/groups/{gb}")
        _, st_ = http("GET", f"{base}/api/picker")
        step("DELETE 组 (不再出现在状态)", st == 200
             and all(g["strategy_id"] != gb for g in st_["groups"]))

        # ---- 引擎停止 ----
        st, _ = http("POST", f"{base}/api/picker/engine/stop")
        _, st_ = http("GET", f"{base}/api/picker")
        step("引擎停止 (只读视图仍在)", st == 200 and st_["running"] is False
             and len(st_["groups"]) >= 2)

        # ---- 前端链路 ----
        if not SKIP_FRONTEND:
            fproc = subprocess.Popen(
                ["npm", "run", "dev"], cwd=REPO / "frontend",
                env={**os.environ, "PORT": str(frontend_port),
                     "BACKEND_PORT": str(backend_port)},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            procs.append(fproc)
            fbase = f"http://127.0.0.1:{frontend_port}"
            ok = wait_http(f"{fbase}/picker", timeout=120)
            step("前端 /picker 页可访问 (next dev)", ok, f"port={frontend_port}")
            if ok:
                st, r = http("GET", f"{fbase}/api/backend/picker")
                step("前端代理 -> 测试后端 (BACKEND_PORT rewrite)",
                     st == 200 and "groups" in r)
    finally:
        for p in procs:
            try:
                os.killpg(os.getpgid(p.pid), 15)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(1)
        for p in procs:
            if p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), 9)
                except Exception:  # noqa: BLE001
                    pass
        print(f"(临时 DB: {tmpdir}; 测试端口已释放)", flush=True)
    return _summary()


def _summary() -> int:
    failed = [n for n, ok, _ in _results if not ok]
    print(f"\n== 汇总: {len(_results) - len(failed)}/{len(_results)} 通过 ==")
    for n in failed:
        print(f"  ✗ FAILED: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

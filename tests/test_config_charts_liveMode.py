"""设置页 + charts 实盘开关 集成测试.

T1: GET  /api/config      → 返回体只剩 trade_costs 有意义 (strategies 字段保留兼容, 不再 UI 展示)
T2: POST /api/charts/run mode=paper → 返回 exec_mode={mode:"paper", dry_run:false, broker:"SimulatedBroker"}
T3: POST /api/charts/run mode=live,dry_run=False → 返回 exec_mode={mode:"live", dry_run:false, broker:"LiveBroker(同花顺)"}
T4: POST /api/charts/run mode=live,dry_run=True (旧兼容) → exec_mode.dry_run=True
T5: 前端三页面 HTTP 20x: /config /charts /backtest
T6: charts 页面 HTML 里含有 "⚡ 实盘" 文字 (证明 checkbox 已渲染)
T7: config 页面 HTML 里已不再包含 "🧩 实盘策略配置" 文字
退出码 0 = 全部通过.
"""
from __future__ import annotations
import json
import subprocess
import sys

BASE = "http://127.0.0.1:8000/api"
FRONT = "http://127.0.0.1:3001"


def curl(method: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
    args = ["curl", "-sS", "-X", method, "-w", "\n%{http_code}"]
    if body is not None:
        args += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    args.append(BASE + path)
    r = subprocess.run(args, capture_output=True, check=False)
    blob = r.stdout
    nl = blob.rfind(b"\n")
    if nl < 0:
        return 0, blob
    return int(blob[nl + 1:].strip()), blob[:nl]


def front_get(path: str) -> tuple[int, bytes]:
    r = subprocess.run(["curl", "-sS", "-w", "\n%{http_code}", FRONT + path],
                       capture_output=True, check=False)
    blob = r.stdout
    nl = blob.rfind(b"\n")
    if nl < 0:
        return 0, blob
    return int(blob[nl + 1:].strip()), blob[:nl]


SEP = "=" * 54
ok = 0
fail = 0
def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1; print(f"  ✓ {name}")
        if detail: print(f"      ↳ {detail}")
    else:
        fail += 1; print(f"  ✗ {name}  FAIL")
        if detail: print(f"      ↳ {detail}")


# ========== T1 ==========
print(f"\n{SEP}\n【T1】GET /api/config → strategies 保留兼容，但前端不再用它")
code, body = curl("GET", "/config")
check("HTTP 200", code == 200, f"code={code}")
cfg = json.loads(body)
check("顶层键 trade_costs", "trade_costs" in cfg)
check("顶层键 strategies (兼容保留)", "strategies" in cfg)

# ========== T2 ==========
print(f"\n{SEP}\n【T2】POST charts/run mode=paper → SimulatedBroker")
code2, body2 = curl("POST", "/charts/run", {
    "strategy": "ma20_trend", "symbol": "sz159915",
    "params": {"window": 20}, "mode": "paper", "dry_run": False,
    "cash_per_symbol": 10000,
})
check("HTTP 200", code2 == 200, f"code={code2}")
if code2 == 200:
    r = json.loads(body2)
    em = r.get("exec_mode") or {}
    check("exec_mode 字段存在", bool(em))
    check("mode=paper", em.get("mode") == "paper")
    check("dry_run=false (paper 忽略 dry_run)", em.get("dry_run") is False)
    check("broker=SimulatedBroker", em.get("broker") == "SimulatedBroker")
    check("orders 字段存在", "orders" in r)

# ========== T3 ==========
print(f"\n{SEP}\n【T3】POST charts/run mode=live,dry_run=False → LiveBroker(同花顺) 真实模式")
# 注意: 因为没有同花顺窗口, LiveBroker.run_live 可能抛错, 但 HTTP 应该仍然 200
# 并且请求里的 mode/dry_run 会反映在 exec_mode (即使后面失败, 也是 order 里有 error)
code3, body3 = curl("POST", "/charts/run", {
    "strategy": "ma20_trend", "symbol": "sz159915",
    "params": {"window": 20}, "mode": "live", "dry_run": False,
    "cash_per_symbol": 10000,
})
check("HTTP 200", code3 == 200, f"code={code3}")
if code3 == 200:
    r = json.loads(body3)
    em = r.get("exec_mode") or {}
    check("exec_mode 字段存在", bool(em))
    check("mode=live", em.get("mode") == "live", f"实得 {em.get('mode')}")
    check("dry_run=False (关了 dry-run)", em.get("dry_run") is False,
          f"实得 {em.get('dry_run')}")
    check("broker=LiveBroker(同花顺)", em.get("broker") == "LiveBroker(同花顺)")
    # 如果有错误, 显示但不判定为 fail
    print(f"      ↳ msg/err: {r.get('msg') or r.get('error') or '(订单直接返回)'}")

# ========== T4 ==========
print(f"\n{SEP}\n【T4】POST charts/run mode=live,dry_run=True → 兼容旧前端 dry_run=True")
code4, body4 = curl("POST", "/charts/run", {
    "strategy": "ma20_trend", "symbol": "sz159915",
    "params": {"window": 20}, "mode": "live", "dry_run": True,
    "cash_per_symbol": 10000,
})
check("HTTP 200", code4 == 200, f"code={code4}")
if code4 == 200:
    r = json.loads(body4)
    em = r.get("exec_mode") or {}
    check("exec_mode 字段存在", bool(em))
    check("mode=live", em.get("mode") == "live")
    check("dry_run=True (旧兼容保留)", em.get("dry_run") is True,
          f"实得 {em.get('dry_run')}")

# ========== T5 ==========
print(f"\n{SEP}\n【T5】前端三页面 HTTP 访问")
for p in ("/config", "/charts", "/backtest"):
    c, _ = front_get(p)
    want_2xx = 200 <= c < 400
    check(f"{p} HTTP 2xx (含 307 重定向)", want_2xx, f"实得 {c}")

# ========== T6 ==========
print(f"\n{SEP}\n【T6】/charts 页面已渲染 ⚡ 实盘 checkbox 文字")
c6, html6 = front_get("/charts")
text6 = html6.decode("utf-8", errors="ignore")
check("/charts 响应包含 实盘 checkbox 文案",
      "⚡ 实盘" in text6 or "liveMode" in text6 or "accent-[#ef5350]" in text6,
      f"HTML len={len(text6)} 片段命中: "
      + ("⚡实盘(✓)" if "⚡" in text6 and "实盘" in text6 else
         ("liveMode(✓)" if "liveMode" in text6 else "未命中")))

# ========== T7 ==========
print(f"\n{SEP}\n【T7】/config 页面 HTML 已无「🧩 实盘策略配置」旧标题")
c7, html7 = front_get("/config")
text7 = html7.decode("utf-8", errors="ignore")
check("已删除旧策略配置 UI",
      "🧩 实盘策略配置" not in text7 and "enabled=scfg" not in text7 and "实盘策略配置" not in text7,
      f"命中旧标题? {'是 (FAIL)' if '实盘策略配置' in text7 else '否 (OK)'}")
check("仍然保留 交易成本/手续费文案",
      "交易成本" in text7 and "手续费" in text7)

print(f"\n{SEP}\n📊 总计: {ok} 个通过, {fail} 个失败")
sys.exit(0 if fail == 0 else 1)

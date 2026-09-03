"""交易成本 + 配置 自动化烟雾测试.

T1: GET  /api/config        → trade_costs 三大类 + 默认值
T2: PUT  /api/config        → 修改→保存→重读 持久化
T3: POST /api/backtest      → 回测 stats.trade_costs 字段 + effective_fees 计算正确
T4: 前端 3 页面 HTTP 200    → / (重定向) /config /backtest
T5: symbol_category         → 7 个边界用例
退出码 0 = 全部通过.
"""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

# 把 strategy 加入 sys.path (从仓库根启动时保证 import 通)
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

BASE = "http://127.0.0.1:8000/api"
FRONT = "http://127.0.0.1:3001"


def curl(method: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
    args = ["curl", "-sS", "-X", method, "-w", "\n%{http_code}"]
    if body is not None:
        args += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    args.append(BASE + path)
    r = subprocess.run(args, capture_output=True, check=False)
    blob = r.stdout
    # 最后一行是 HTTP code (我们通过 -w 塞的)
    nl = blob.rfind(b"\n")
    if nl < 0:
        return 0, blob
    code = int(blob[nl + 1:].strip())
    data = blob[:nl]
    return code, data


def curl_front(path: str) -> int:
    r = subprocess.run(["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", FRONT + path],
                       capture_output=True, check=False)
    return int(r.stdout.decode().strip())


SEP = "=" * 54
ok = 0
fail = 0
def check(name: str, cond: bool, detail: str = ""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✓ {name}")
        if detail:
            print(f"      ↳ {detail}")
    else:
        fail += 1
        print(f"  ✗ {name}  FAIL")
        if detail:
            print(f"      ↳ {detail}")


# ============ T1 ============
print(f"\n{SEP}\n【T1】GET /api/config → trade_costs 大类 + 默认值")
code, body = curl("GET", "/config")
check("HTTP 200", code == 200, f"code={code}")
cfg = json.loads(body)
tc = cfg.get("trade_costs") or {}
for cat in ("stock", "futures", "options"):
    check(f"大类[{cat}] 存在", cat in tc)
    if cat in tc:
        for k in ("buy_fee", "sell_fee", "sell_stamp_duty", "slippage"):
            check(f"  字段 {k}", k in tc[cat])

st = tc.get("stock", {})
check("stock.buy_fee=0.0001 (万1)", abs(st.get("buy_fee", -1) - 0.0001) < 1e-12)
check("stock.sell_fee=0.0001", abs(st.get("sell_fee", -1) - 0.0001) < 1e-12)
check("stock.sell_stamp_duty=0.001 (千1)", abs(st.get("sell_stamp_duty", -1) - 0.001) < 1e-12)
check("stock.slippage=0.0001", abs(st.get("slippage", -1) - 0.0001) < 1e-12)

# ============ T2 ============
print(f"\n{SEP}\n【T2】PUT /api/config → 持久化修改 + 还原")
modified = json.loads(json.dumps(cfg))  # deep copy
modified["trade_costs"]["stock"]["buy_fee"] = 0.0003
modified["trade_costs"]["stock"]["slippage"] = 0.0002
code2, body2 = curl("PUT", "/config", modified)
put_resp = json.loads(body2) if body2 else {}
check("PUT HTTP 200 + ok=True", code2 == 200 and put_resp.get("ok"))
code3, body3 = curl("GET", "/config")
cfg2 = json.loads(body3)
v1 = cfg2["trade_costs"]["stock"]["buy_fee"]
v2 = cfg2["trade_costs"]["stock"]["slippage"]
check("保存后 buy_fee=0.0003", abs(v1 - 0.0003) < 1e-12, f"实得 {v1}")
check("保存后 slippage=0.0002", abs(v2 - 0.0002) < 1e-12, f"实得 {v2}")
# 还原
cfg2["trade_costs"]["stock"]["buy_fee"] = 0.0001
cfg2["trade_costs"]["stock"]["slippage"] = 0.0001
curl("PUT", "/config", cfg2)
code4, body4 = curl("GET", "/config")
cfg3 = json.loads(body4)
check("还原后 buy_fee=0.0001", abs(cfg3["trade_costs"]["stock"]["buy_fee"] - 0.0001) < 1e-12)
check("还原后 slippage=0.0001", abs(cfg3["trade_costs"]["stock"]["slippage"] - 0.0001) < 1e-12)

# ============ T3 ============
print(f"\n{SEP}\n【T3】POST /api/backtest → 回测 sz159915 返回 stats.trade_costs")
req = {
    "strategy": "ma20_trend",
    "symbols": ["sz159915"],
    "symbol": "",
    "params": {"window": 20},
    "tf": "day",
    "qfq": False,
    "cash": 100_000,
    "limit": 500,
}
code5, body5 = curl("POST", "/backtest", req)
check("POST HTTP 200", code5 == 200, f"code={code5}")
if code5 != 200:
    print("    错误体:", body5.decode()[:800])
res = json.loads(body5)
r = res.get("sz159915")
check("sz159915 结果键存在", r is not None)
stats = (r or {}).get("stats") or {}
tc2 = stats.get("trade_costs")
check("stats.trade_costs 存在", tc2 is not None)
if tc2:
    for k in ("buy_fee", "sell_fee", "sell_stamp_duty", "slippage", "effective_fees_per_trade"):
        check(f"  trade_costs.{k}", k in tc2)
    check("  buy_fee=0.0001", abs(tc2.get("buy_fee", -1) - 0.0001) < 1e-12)
    check("  sell_fee=0.0001", abs(tc2.get("sell_fee", -1) - 0.0001) < 1e-12)
    check("  stamp=0.001", abs(tc2.get("sell_stamp_duty", -1) - 0.001) < 1e-12)
    check("  slippage=0.0001", abs(tc2.get("slippage", -1) - 0.0001) < 1e-12)
    exp_eff = (0.0001 + 0.0001 + 0.001) / 2.0
    check(f"  effective_fees = {exp_eff} (总 roundtrip/2)",
          abs(tc2.get("effective_fees_per_trade", -1) - exp_eff) < 1e-12,
          f"实得 {tc2.get('effective_fees_per_trade')}")
tr = stats.get("total_return")
mdd = stats.get("max_drawdown")
n = stats.get("Total Trades") or stats.get("total_trades")
print(f"  ↳ total_return = {float(tr):.4%}, max_dd = {float(mdd):.4%}, trades = {n}")

# ============ T4 ============
# 注: "/" 根路径按项目规范必须 30x 重定向到 /charts (非 200)，其它页面应为 200
print(f"\n{SEP}\n【T4】前端页面可访问性")
for p, exp in (("/", "redirect"), ("/config", 200), ("/backtest", 200)):
    c = curl_front(p)
    if exp == "redirect":
        check(f"{p} 返回 30x 重定向", 300 <= c < 400, f"实得 {c}")
    else:
        check(f"{p} HTTP {exp}", c == exp, f"实得 {c}")

# ============ T5 ============
print(f"\n{SEP}\n【T5】symbol_category 大类识别 7 用例")
from strategy.config import symbol_category  # noqa: E402
cases = [
    ("sz159915", "stock"),
    ("sh600519", "stock"),
    ("bj430047", "stock"),
    ("if2409", "futures"),
    ("rb2410", "futures"),
    ("mo.option.C", "options"),
    ("SO.900005.P", "options"),
]
for sym, exp in cases:
    got = symbol_category(sym)
    check(f"{sym} -> {exp}", got == exp, f"实得 {got}")

# ============ SUMMARY ============
print(f"\n{SEP}\n📊 总计: {ok} 个通过, {fail} 个失败")
sys.exit(0 if fail == 0 else 1)

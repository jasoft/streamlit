#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同花顺交易脚本压力测试: 大量买卖/撤单/查询, 统计成功率和错误."""
import sys, os, time, json, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

from trading import ths_trade as t
from ApplicationServices import AXUIElementPerformAction

# ── 初始化 ──
app, pid, app_el = t.find_app(activate=False)
results = []  # [{step, action, ok, detail, elapsed_ms}]

def log(step, action, ok, detail="", elapsed_ms=0):
    tag = "OK" if ok else "FAIL"
    line = f"[{step:02d}] {tag} {action} ({elapsed_ms:.0f}ms) {detail}"
    print(line, flush=True)
    results.append({"step": step, "action": action, "ok": ok,
                     "detail": detail, "elapsed_ms": round(elapsed_ms)})

def do_buy(code, qty, price=None):
    """完整买入流程 (复用 ths_trade 内部函数)."""
    t0 = time.perf_counter()
    try:
        return _do_buy_inner(code, qty, price, t0)
    except SystemExit as e:
        return False, f"SystemExit: {e}", t0
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", t0

def _do_buy_inner(code, qty, price, t0):
    win = t.main_window(app_el)
    # 清弹窗
    t._close_all_dialogs(app_el)
    # 切买入面板
    tab = t.find_button(win, "买入")
    if tab:
        AXUIElementPerformAction(tab, "AXPress"); time.sleep(0.5)
        win = t.main_window(app_el)
    code_f, price_f, qty_f = t.scan_fields(win)
    # 填代码
    linked = t.fill_code(code_f, price_f, code)
    if not linked:
        time.sleep(0.5)
        win = t.main_window(app_el)
        code_f, price_f, qty_f = t.scan_fields(win)
        linked = t.fill_code(code_f, price_f, code)
    if not linked:
        return False, "联动失败", t0
    time.sleep(0.5)
    # 填价格
    if price:
        t.set_text(price_f, str(price)); time.sleep(0.2)
    # 填数量
    t.set_text(qty_f, str(qty)); time.sleep(0.2)
    # 提交
    btn = t.find_button(win, "确定买入")
    if not btn:
        return False, "找不到确定买入", t0
    AXUIElementPerformAction(btn, "AXPress")
    # 确认框
    dlg = t.wait_dialog(app_el, has_cancel=True, timeout=8)
    if dlg is None:
        warn = t.wait_dialog(app_el, has_cancel=False, timeout=1.0)
        txt = " | ".join(warn[1]) if warn else "未出现确认框"
        return False, txt, t0
    btns, texts = dlg
    if code not in " ".join(texts).replace(" ", ""):
        return False, f"确认框代码不符", t0
    AXUIElementPerformAction(btns["确认"], "AXPress")
    # 结果框
    res = t.wait_dialog(app_el, has_cancel=False, timeout=8)
    if res:
        rt = " | ".join(res[1])
        if "确认" in res[0]: AXUIElementPerformAction(res[0]["确认"], "AXPress")
        time.sleep(0.2)
        t._close_all_dialogs(app_el)  # 确保残留弹窗全部关闭
        ERR = ("警告","错误","失败","不足","超过","不允许","拒绝")
        if any(k in rt.replace(" ","") for k in ERR):
            return False, rt, t0
        return True, rt or "提交成功", t0
    return True, "提交成功(无弹窗)", t0

def do_sell(code, qty):
    """完整卖出流程."""
    t0 = time.perf_counter()
    try:
        return _do_sell_inner(code, qty, t0)
    except SystemExit as e:
        return False, f"SystemExit: {e}", t0
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", t0

def _do_sell_inner(code, qty, t0):
    win = t.main_window(app_el)
    for _, btns, _ in t.dialogs_with_buttons(app_el):
        for name in ("确认", "确定"):
            if name in btns:
                AXUIElementPerformAction(btns[name], "AXPress"); break
        time.sleep(0.2)
    tab = t.find_button(win, "卖出")
    if tab:
        AXUIElementPerformAction(tab, "AXPress"); time.sleep(0.5)
        win = t.main_window(app_el)
    code_f, price_f, qty_f = t.scan_fields(win)
    linked = t.fill_code(code_f, price_f, code)
    if not linked:
        time.sleep(0.5)
        win = t.main_window(app_el)
        code_f, price_f, qty_f = t.scan_fields(win)
        linked = t.fill_code(code_f, price_f, code)
    if not linked:
        return False, "联动失败", t0
    time.sleep(0.5)
    t.set_text(qty_f, str(qty)); time.sleep(0.2)
    btn = t.find_button(win, "确定卖出")
    if not btn:
        return False, "找不到确定卖出", t0
    AXUIElementPerformAction(btn, "AXPress")
    dlg = t.wait_dialog(app_el, has_cancel=True, timeout=8)
    if dlg is None:
        warn = t.wait_dialog(app_el, has_cancel=False, timeout=1.0)
        txt = " | ".join(warn[1]) if warn else "未出现确认框"
        return False, txt, t0
    btns, texts = dlg
    AXUIElementPerformAction(btns["确认"], "AXPress")
    res = t.wait_dialog(app_el, has_cancel=False, timeout=8)
    if res:
        rt = " | ".join(res[1])
        if "确认" in res[0]: AXUIElementPerformAction(res[0]["确认"], "AXPress")
        time.sleep(0.2)
        t._close_all_dialogs(app_el)
        ERR = ("警告","错误","失败","不足","超过","不允许","拒绝")
        if any(k in rt.replace(" ","") for k in ERR):
            return False, rt, t0
        return True, rt or "提交成功", t0
    return True, "提交成功(无弹窗)", t0

def do_query(table):
    t0 = time.perf_counter()
    try:
        _, cols, rows, popup = t.read_table(app_el, table)
        return True, f"{table}: {len(rows)}行", t0
    except SystemExit as e:
        return False, f"SystemExit: {e}", t0
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", t0

def do_cancel_all():
    t0 = time.perf_counter()
    try:
        ok, msg = t.cancel_all_button(app_el, timeout=6)
        return ok, msg, t0
    except SystemExit as e:
        return False, f"SystemExit: {e}", t0
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", t0

# ── 压力测试 ──
step = 0
print("=" * 60)
print("压力测试开始")
print("=" * 60)

# 1. 基线查询
for tbl in ("持仓", "委托", "成交"):
    step += 1
    ok, detail, t0 = do_query(tbl)
    log(step, f"查询{tbl}", ok, detail, (time.perf_counter()-t0)*1000)
    time.sleep(0.5)

# 2. ETF 513120 T+0 买卖循环 (5轮)
for i in range(5):
    # 买入
    step += 1
    ok, detail, t0 = do_buy("513120", 100)
    log(step, f"买入513120 x{i+1}", ok, detail, (time.perf_counter()-t0)*1000)
    time.sleep(1)

    # 查询委托
    step += 1
    ok, detail, t0 = do_query("委托")
    log(step, f"查询委托 x{i+1}", ok, detail, (time.perf_counter()-t0)*1000)
    time.sleep(0.5)

    # 卖出
    step += 1
    ok, detail, t0 = do_sell("513120", 100)
    log(step, f"卖出513120 x{i+1}", ok, detail, (time.perf_counter()-t0)*1000)
    time.sleep(1)

# 3. 限价挂单+撤单循环 (5轮)
for i in range(5):
    # 挂限价单
    step += 1
    ok, detail, t0 = do_buy("513120", 100, price=1.170)
    log(step, f"限价挂单 x{i+1}", ok, detail, (time.perf_counter()-t0)*1000)
    time.sleep(1)

    # 查委托确认有单
    step += 1
    ok, detail, t0 = do_query("委托")
    log(step, f"查委托(挂单后) x{i+1}", ok, detail, (time.perf_counter()-t0)*1000)
    time.sleep(0.5)

    # 撤单
    step += 1
    ok, detail, t0 = do_cancel_all()
    log(step, f"撤单 x{i+1}", ok, detail, (time.perf_counter()-t0)*1000)
    time.sleep(1)

# 4. 601899 买卖 (T+1, 只买不卖)
step += 1
ok, detail, t0 = do_buy("601899", 100)
log(step, "买入601899", ok, detail, (time.perf_counter()-t0)*1000)
time.sleep(1)

# 5. T+1 卖出 601899 (应该失败)
step += 1
ok, detail, t0 = do_sell("601899", 200)
log(step, "卖出601899(T+1限制)", ok, detail, (time.perf_counter()-t0)*1000)
time.sleep(1)

# 6. 异常价格测试
step += 1
ok, detail, t0 = do_buy("513120", 100, price=1.0)
log(step, "超涨跌限价", ok, detail, (time.perf_counter()-t0)*1000)
time.sleep(1)

# 7. 最终查询
for tbl in ("持仓", "委托", "成交", "资金明细"):
    step += 1
    ok, detail, t0 = do_query(tbl)
    log(step, f"最终查询{tbl}", ok, detail, (time.perf_counter()-t0)*1000)
    time.sleep(0.5)

# ── 汇总 ──
print("\n" + "=" * 60)
total = len(results)
ok_count = sum(1 for r in results if r["ok"])
fail_count = total - ok_count
print(f"总计: {total} 步 | 成功: {ok_count} | 失败: {fail_count}")
print(f"成功率: {ok_count/total*100:.1f}%")
if fail_count:
    print("\n失败详情:")
    for r in results:
        if not r["ok"]:
            print(f"  [{r['step']:02d}] {r['action']}: {r['detail']}")
print("=" * 60)

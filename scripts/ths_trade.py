#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同花顺 Mac 客户端 GUI 自动交易 (macOS Accessibility, 纯后台无坐标定位)

定位策略 (2026-08 实测; 不依赖任何绝对屏幕坐标, 窗口移动/缩放不受影响):
  - 买入面板输入框: 用 "代码"/"价格"/"数量" 文字标签锚定, 取标签右侧
    同一行的 AXTextField (工具栏搜索框旁没有这些标签, 天然排除)
  - 功能 tab (持仓/委托/成交/资金明细): 按 title 找按钮; "持仓" 有两个
    (tab 和表头), 表头是 disabled, 用 AXEnabled 区分
  - 账户切换: 点 title 为 "A股"/"模拟" 的按钮, 以左侧账户名文字变化确认
  - 表格: 列头 = 功能 tab 按钮下方区域的带 title 按钮 (排除 今天/全撤 等
    筛选按钮); 单元格 = 列头下方区域的 AXStaticText, y 聚行 (行高~24,
    相邻差<12px 归同行), x 映射列头 (容差 10px, 相对量); 右界 = 最右
    列头 x + 一个列宽; 真数据行首列必须对齐第一个列头 (排除弹窗杂项)
  - 代码框联动: 先 AX set focused=True 再 AX set value 即触发联动
    (识别市场/带出对手价); 直接 set value 不聚焦则不联动, 提交报
    "市场代码不允许为空"
  - 委托确认框是 attached sheet, 不出现在 AXWindows 枚举里, 从
    AXFocusedWindow 拿; 点确认后券商返回结果弹窗 (只有 确认 按钮)
  - 全程 AX 后台操作, 不需要激活窗口; --keyboard 走键盘输入备用路径 (需前台)

实测速度 (2026-08-30): 填单 ~0.5s + 提交自动确认 ~0.7s, 全流程 ~1.3s.

用法:
  # 只填不提交 (测填写速度)
  uv run --with pyobjc python scripts/ths_trade.py buy 601899 100 --dry-run
  # 完整委托: 填单 -> 确定买入 -> 自动点确认 -> 读结果
  uv run --with pyobjc python scripts/ths_trade.py buy 601899 100
  # 指定限价 (不给则用联动出的对手价)
  uv run --with pyobjc python scripts/ths_trade.py buy 601899 100 --price 34.65
  # 卖出 / 键盘备用路径
  uv run --with pyobjc python scripts/ths_trade.py sell 601899 100
  uv run --with pyobjc python scripts/ths_trade.py buy 601899 100 --keyboard
  # 持仓/委托/成交/资金明细查询: --account 切账户 (A股/real 或 模拟/sim), 缺省查当前
  uv run --with pyobjc python scripts/ths_trade.py positions --account real
  uv run --with pyobjc python scripts/ths_trade.py orders      # 委托 (默认只查"今天")
  uv run --with pyobjc python scripts/ths_trade.py trades      # 成交
  uv run --with pyobjc python scripts/ths_trade.py funds       # 资金明细
  # 撤单: 双击委托行 (需同花顺前台, 会自动激活); 撤完自动复核剩余
  uv run --with pyobjc python scripts/ths_trade.py cancel --contract 1140009957
  uv run --with pyobjc python scripts/ths_trade.py cancel --code 601899
  uv run --with pyobjc python scripts/ths_trade.py cancel --all

权限: 运行脚本的终端 App 需在 系统设置 -> 隐私与安全性 -> 辅助功能 中勾选。
登录: 所有命令执行前自动检测登录状态, 未登录时读环境变量 THS_USER / THS_PASS
     自动登录 (点"立即登录" -> 登录窗用"交易帐户/交易密码"标签锚定填账号密码);
     --no-login 跳过; `login` 子命令单独触发登录。
输出: JSON 一行, ok / side|table / account / steps(各步耗时ms) / result_text(券商返回)
"""
import argparse
import json
import subprocess
import sys
import time

import AppKit
from ApplicationServices import (
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
    AXUIElementSetAttributeValue,
    AXUIElementPerformAction,
    AXValueGetValue,
    AXValueGetType,
    kAXValueCGPointType,
    kAXValueCGSizeType,
)

BUNDLE_ID = "cn.com.10jqka.macstock"

TABLE_TABS = {"positions": "持仓", "orders": "委托",
              "trades": "成交", "funds": "资金明细"}
# 与列头混在同一区域的筛选/操作按钮, 不算列
FILTER_BUTTONS = {"今天", "本周", "本月", "全撤", "撤买", "撤卖", "刷新", "导出"}
# 买卖面板的输入框标签 -> 字段名
FIELD_LABELS = ("代码", "价格", "数量")


def ax_get(el, attr):
    err, val = AXUIElementCopyAttributeValue(el, attr, None)
    if err != 0:
        return None
    return val


def ax_set(el, attr, val):
    return AXUIElementSetAttributeValue(el, attr, val)


def walk(el, out):
    """深度优先收集 (element, role, title, value, frame|None)."""
    role = ax_get(el, "AXRole")
    title = ax_get(el, "AXTitle")
    value = ax_get(el, "AXValue")
    out.append((el, role, title, value))
    kids = ax_get(el, "AXChildren")
    if kids:
        for k in kids:
            walk(k, out)
    return out


def tree(el):
    return walk(el, [])


def get_frame(el):
    def decode(v, vtype):
        if v is None:
            return None
        ok, pair = AXValueGetValue(v, vtype, None)
        return pair if ok else None

    pos = decode(ax_get(el, "AXPosition"), kAXValueCGPointType)
    size = decode(ax_get(el, "AXSize"), kAXValueCGSizeType)
    if not pos or not size:
        return None
    return (pos[0], pos[1], size[0], size[1])


def enabled(el):
    return bool(ax_get(el, "AXEnabled"))


def osa(script):
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"osascript 失败: {r.stderr.strip()}")
    return r.stdout.strip()


def find_app(activate=False):
    apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE_ID)
    if not apps:
        raise SystemExit("同花顺未运行")
    pid = apps[0].processIdentifier()
    if activate:
        osa('tell application "System Events" to set frontmost of '
            '(first process whose bundle identifier is "%s") to true' % BUNDLE_ID)
        time.sleep(0.3)
    return apps[0], pid, AXUIElementCreateApplication(pid)


def main_window(app_el):
    wins = None
    for _ in range(5):  # 同花顺偶发 AX 瞬时失败(-25211), 重试即可
        wins = ax_get(app_el, "AXWindows")
        if wins:
            break
        time.sleep(0.3)
    if not wins:
        raise SystemExit("读不到同花顺窗口")
    best, best_area = None, 0
    for w in wins:
        f = get_frame(w)
        if f and f[2] * f[3] > best_area:
            best, best_area = w, f[2] * f[3]
    return best


def find_button(win, title, only_enabled=False):
    for el, role, t, v in tree(win):
        if role == "AXButton" and t == title:
            if only_enabled and not enabled(el):
                continue
            return el
    return None


def scan_fields(win):
    """按 "代码/价格/数量" 文字标签锚定三个输入框.

    标签与输入框同一行 (中心 y 差 < 行高一半), 输入框在标签右侧.
    返回 {标签: field element}; 找不齐三个时抛错.
    """
    labels, fields = {}, []
    for el, role, t, v in tree(win):
        if role not in ("AXStaticText", "AXTextField"):
            continue
        f = get_frame(el)
        if not f or f[3] <= 0:
            continue
        cx, cy = f[0] + f[2] / 2, f[1] + f[3] / 2
        if role == "AXStaticText" and isinstance(v, str) and v.strip() in FIELD_LABELS:
            labels[v.strip()] = (cx, cy)
        elif role == "AXTextField":
            fields.append((cx, cy, el))

    out = {}
    for name, (lx, ly) in labels.items():
        cands = [(cy - ly if cy >= ly else ly - cy, cx, el)
                 for cx, cy, el in fields
                 if abs(cy - ly) < 15 and cx > lx]  # 同行(半行高容差), 在标签右侧
        if cands:
            out[name] = min(cands)[2]
    missing = [n for n in FIELD_LABELS if n not in out]
    if missing:
        raise SystemExit(f"按标签找不到输入框: {missing} (面板未打开?)")
    return out["代码"], out["价格"], out["数量"]


def account_name(win):
    """左侧账户名: "添加" 按钮左侧同一行的 statictext (相对按钮定位)."""
    add = find_button(win, "添加")
    if add is None:
        return ""
    af = get_frame(add)
    if not af:
        return ""
    acx, acy = af[0] + af[2] / 2, af[1] + af[3] / 2
    best, best_cx = "", -1
    for el, role, t, v in tree(win):
        f = get_frame(el) if role == "AXStaticText" else None
        if not f or not (isinstance(v, str) and v.strip()):
            continue
        cx, cy = f[0] + f[2] / 2, f[1] + f[3] / 2
        if abs(cy - acy) < 15 and cx < acx and cx > best_cx:  # 同行、在按钮左侧、最近
            best, best_cx = v.strip(), cx
    return best


def switch_account(app_el, name, timeout=5.0):
    """点左侧 A股/模拟 tab 切换账户, 等账户名变化. 返回切换后的账户名."""
    win = main_window(app_el)
    old = account_name(win)
    tab = find_button(win, name)
    if tab is None:
        raise SystemExit(f"找不到账户 tab {name!r}")
    AXUIElementPerformAction(tab, "AXPress")
    # 等账户名变化; 已在该账户时名字不变, 超时按已切换处理
    t0 = time.perf_counter()
    cur = old
    while time.perf_counter() - t0 < timeout:
        cur = account_name(main_window(app_el))
        if cur and cur != old:
            break
        time.sleep(0.2)
    return cur


def field_value(field):
    v = ax_get(field, "AXValue")
    return v if isinstance(v, str) else ""


def set_text(field, text):
    ax_set(field, "AXValue", text)


def poll(fn, timeout, interval=0.03):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        r = fn()
        if r is not None:
            return r
        time.sleep(interval)
    return None


def dialogs_with_buttons(app_el):
    """返回每个弹窗的 (window, {title: element}, [texts]).

    弹窗来源: AXWindows 里的非主窗口 + AXFocusedWindow (委托确认框是
    attached sheet, 不出现在 AXWindows 枚举里, 只能从 FocusedWindow 拿).
    """
    wins = ax_get(app_el, "AXWindows") or []
    frames = [(get_frame(w), w) for w in wins]
    frames = [(f, w) for f, w in frames if f]
    if not frames:
        return []
    main_area = max(f[2] * f[3] for f, _ in frames)
    small = [(f, w) for f, w in frames if f[2] * f[3] < main_area * 0.5]

    err, fw = AXUIElementCopyAttributeValue(app_el, "AXFocusedWindow", None)
    if err == 0 and fw is not None:
        ff = get_frame(fw)
        if ff and ff[2] * ff[3] < main_area * 0.5 and not any(w == fw for _, w in small):
            small.append((ff, fw))

    out = []
    for f, w in small:
        btns, texts = {}, []
        for el, role, t, v in tree(w):
            if role == "AXButton" and isinstance(t, str) and t:
                btns[t] = el
            elif role == "AXStaticText" and isinstance(v, str) and v.strip():
                texts.append(v.strip())
        if btns:
            out.append((w, btns, texts))
    return out


def wait_dialog(app_el, has_cancel, timeout):
    """等一个弹窗; has_cancel=True 找委托确认框, False 找结果/警告框."""
    def check():
        for _, btns, texts in dialogs_with_buttons(app_el):
            if has_cancel and "取消" in btns and "确认" in btns:
                return (btns, texts)
            if not has_cancel and "确认" in btns and "取消" not in btns:
                return (btns, texts)
        return None
    return poll(check, timeout)


def fill_code(code_f, price_f, code, keyboard=False):
    """填代码并等联动 (识别市场/带出对手价). 返回面板是否就绪.

    纯 AX 后台路径: 先 focused 再 set value 即触发联动.
    就绪判据: 价格框出现非空值 (联动带出对手价; 同股重复下单时价格
    本来就在, 立即通过). 超时则服务器可能没响应, 只 warn 不阻断.
    """
    if keyboard:
        type_text(code, pre_field=code_f)
    else:
        ax_set(code_f, "AXFocused", True)
        time.sleep(0.03)
        ax_set(code_f, "AXValue", code)

    return poll(lambda: (True if field_value(price_f) else None), 3.0) is not None


def type_text(text, pre_field=None, interval=0.02):
    """逐字 keystroke (需同花顺前台, --keyboard 备用路径).

    一次敲整串会被联想吃字符, 必须逐字 keystroke + delay;
    合并成一次 osascript 调用省去每字一次进程启动的开销.
    """
    if pre_field is not None:
        ax_set(pre_field, "AXValue", "")
        time.sleep(0.03)
        ax_set(pre_field, "AXFocused", True)
        time.sleep(0.05)
    chars = ", ".join('"%s"' % c for c in text)
    osa('tell application "System Events"\n'
        'repeat with c in {%s}\n'
        'keystroke c\ndelay %s\n'
        'end repeat\nend tell' % (chars, interval))


def read_table(app_el, tab_name):
    """切到指定功能 tab 并读表格, 返回 (账户名, 列名, 行[dict], 弹窗文本)."""
    win = main_window(app_el)
    account = account_name(win)
    tab = find_button(win, tab_name, only_enabled=True)  # 排除 disabled 的同名表头
    if tab is None:
        raise SystemExit(f"找不到 {tab_name} tab")
    tab_f = get_frame(tab)
    AXUIElementPerformAction(tab, "AXPress")
    time.sleep(0.5)

    win = main_window(app_el)
    buttons, cells = {}, []
    header_top = tab_f[1] if tab_f else 0
    for el, role, t, v in tree(win):
        f = get_frame(el)
        if not f:
            continue
        x, y = f[0], f[1]
        # 列头候选: 功能 tab 按钮下方区域内的带 title、enabled 按钮 (相对 tab 定位)
        if header_top + 10 < y < header_top + 150 and role == "AXButton" \
                and isinstance(t, str) and t and t not in FILTER_BUTTONS \
                and enabled(el):
            buttons.setdefault(round(y / 15), []).append((x, y, t))
        elif role == "AXStaticText" and isinstance(v, str) and v.strip():
            cells.append((x, y, v.strip()))

    # 列头行 = 候选中按钮最多的一排 (列头一行有十几个按钮, 其他杂项零散)
    header_row = max(buttons.values(), key=len) if buttons else []
    headers = {x: (y, t) for x, y, t in header_row}
    hxs = sorted(headers)
    if not cells or not hxs:
        popup = [" | ".join(ts) for _, _, ts in dialogs_with_buttons(app_el)]
        return account, [headers[h][1] for h in hxs], [], popup

    header_bottom = max(y for y, _ in headers.values()) + 15
    right = hxs[-1] + 110  # 最右列头 + 一个列宽, 排除右侧自选股列表
    cells = [(x, y, v) for x, y, v in cells if y > header_bottom and x < right]

    # 行聚类 (相邻 y 差 >12px 换行) + 首列对齐过滤 (排除弹窗杂项)
    cells.sort(key=lambda c: (c[1], c[0]))
    first_x = hxs[0]
    rows, cur, cur_y = [], [], None
    for x, y, v in cells:
        if cur_y is not None and y - cur_y > 12:
            rows.append(cur)
            cur = []
        cur.append((x, v))
        cur_y = y
    if cur:
        rows.append(cur)

    out = []
    for row in rows:
        if abs(row[0][0] - first_x) > 10:
            continue
        d = {}
        for x, v in row:
            name = next((headers[h][1] for h in hxs if abs(h - x) < 10), f"col{x}")
            d[name] = v
        out.append(d)
    popup = [" | ".join(ts) for _, _, ts in dialogs_with_buttons(app_el)]
    return account, [headers[h][1] for h in hxs], out, popup


def cmd_read_table(args):
    app, pid, app_el = find_app(activate=args.keyboard)
    t0 = time.perf_counter()
    maybe_login(app_el, no_login=args.no_login)
    if args.account:
        tab_name = ACCOUNT_NAMES.get(args.account.lower(), args.account)
        switch_account(app_el, tab_name)
    account, columns, rows, popup = read_table(app_el, TABLE_TABS[args.table])
    print(json.dumps({"ok": True, "table": args.table, "account": account,
                      "count": len(rows), "columns": columns, "rows": rows,
                      "popup": popup,
                      "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)},
                     ensure_ascii=False))


def double_click_at(x, y):
    """System Events 双击 (撤单入口). 需要同花顺在前台 (全局合成点击)."""
    osa('tell application "System Events"\n'
        'click at {%d, %d}\n'
        'delay 0.08\n'
        'click at {%d, %d}\n'
        'end tell' % (int(x), int(y), int(x), int(y)))


def find_order_row(win, needle):
    """在表格里找子树文本包含 needle 的 AXRow, 返回 (row_el, [texts]) 或 None."""
    def scan(e):
        if ax_get(e, "AXRole") == "AXRow":
            texts = [v for _, r, _, v in tree(e)
                     if r == "AXStaticText" and isinstance(v, str)]
            if needle in " ".join(texts):
                return (e, texts)
        for k in ax_get(e, "AXChildren") or []:
            r = scan(k)
            if r:
                return r
        return None
    return scan(win)


def cancel_one(app_el, win, needle, timeout=5.0):
    """双击委托行撤单, 自动点确认. 返回 (ok, 描述文本)."""
    row = find_order_row(win, needle)
    if row is None:
        return False, f"找不到委托行: {needle}"
    row_el, texts = row
    f = get_frame(row_el)
    if not f:
        return False, "读不到委托行位置"
    double_click_at(f[0] + f[2] / 2, f[1] + f[3] / 2)

    dlg = wait_dialog(app_el, has_cancel=True, timeout=timeout)
    if dlg is None:
        return False, "双击后未出现撤单确认框"
    btns, dlg_texts = dlg
    if "撤销" not in " ".join(dlg_texts):
        return False, "确认框内容异常: " + " | ".join(dlg_texts)
    AXUIElementPerformAction(btns["确认"], "AXPress")
    # 关掉结果提示框 (若有)
    res = wait_dialog(app_el, has_cancel=False, timeout=timeout)
    res_text = " | ".join(res[1]) if res else None
    if res and "确认" in res[0]:
        AXUIElementPerformAction(res[0]["确认"], "AXPress")
    return True, res_text or "撤单指令已提交"


def do_login(app_el, user, password, timeout=20.0):
    """自动登录: 点"立即登录" -> 登录窗填账号/密码 -> 点登录.

    账号/密码从环境变量 THS_USER / THS_PASS 读取.
    登录窗字段用文字标签锚定: "交易帐户"右侧 combobox, "交易密码"右侧 textfield.
    成功判定: 登录窗消失且主窗口不再有"立即登录"按钮.
    返回 (ok, msg).
    """
    win = main_window(app_el)
    btn = find_button(win, "立即登录")
    if btn is None:
        return True, "已是登录状态"
    AXUIElementPerformAction(btn, "AXPress")

    def login_win():
        for w, btns, _ in dialogs_with_buttons(app_el):
            if "登录" in btns:
                return w
        return None

    dwin = poll(login_win, 10.0)
    if dwin is None:
        return False, "登录窗未出现"

    # 标签锚点: 找 "交易帐户" / "交易密码" 标签右侧同行 (中心 y 差<15) 的控件
    account_f = pwd_f = None
    for el, role, t, v in tree(dwin):
        if role != "AXStaticText" or not isinstance(v, str):
            continue
        lf = get_frame(el)
        if not lf:
            continue
        name = v.strip()
        if name not in ("交易帐户", "交易密码"):
            continue
        lx, ly = lf[0] + lf[2] / 2, lf[1] + lf[3] / 2
        best = None
        for el2, role2, t2, v2 in tree(dwin):
            f2 = get_frame(el2) if role2 in ("AXComboBox", "AXTextField") else None
            if not f2 or f2[3] <= 0:
                continue
            cx, cy = f2[0] + f2[2] / 2, f2[1] + f2[3] / 2
            if abs(cy - ly) < 15 and cx > lx:
                d = cx - lx
                if best is None or d < best[0]:
                    best = (d, el2)
        if name == "交易帐户" and best:
            account_f = best[1]
        elif name == "交易密码" and best:
            pwd_f = best[1]
    if pwd_f is None:
        return False, "登录窗里找不到 交易密码 输入框"

    if account_f is not None and user and not field_value(account_f):
        ax_set(account_f, "AXFocused", True)
        time.sleep(0.05)
        ax_set(account_f, "AXValue", user)
    ax_set(pwd_f, "AXFocused", True)
    time.sleep(0.05)
    ax_set(pwd_f, "AXValue", password)
    time.sleep(0.1)

    login_btn = find_button(dwin, "登录")
    if login_btn is None:
        return False, "找不到 登录 按钮"
    AXUIElementPerformAction(login_btn, "AXPress")

    # 结果: 成功=登录窗消失且主窗口无"立即登录"; 失败=弹出警告框
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        win = main_window(app_el)
        if win is not None and find_button(win, "立即登录") is None \
                and login_win() is None:
            return True, "登录成功"
        for _, btns, texts in dialogs_with_buttons(app_el):
            if "登录" in btns and "取消" in btns:
                break  # 还在登录窗, 继续等
            if "确认" in btns and "取消" not in btns:
                msg = " | ".join(texts)
                AXUIElementPerformAction(btns["确认"], "AXPress")
                return False, f"登录失败: {msg}"
        time.sleep(0.3)
    return False, "登录超时"


def load_dotenv(path=None):
    """加载 .env 到 os.environ (不覆盖已有变量). 凭据不落日志."""
    import os
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


def ensure_login(app_el):
    """未登录则自动登录 (读 THS_USER/THS_PASS); 已登录直接返回 None."""
    import os
    win = main_window(app_el)
    if win is None or find_button(win, "立即登录") is None:
        return None
    user = os.environ.get("THS_USER", "")
    password = os.environ.get("THS_PASS", "")
    if not user or not password:
        return "未登录且缺少 THS_USER/THS_PASS 环境变量"
    ok, msg = do_login(app_el, user, password)
    return None if ok else msg


def add_login_arg(ap):
    ap.add_argument("--no-login", action="store_true",
                    help="跳过自动登录检测 (默认未登录时用 THS_USER/THS_PASS 自动登录)")


def maybe_login(app_el, no_login=False):
    if no_login:
        return
    msg = ensure_login(app_el)
    if msg:
        raise SystemExit(f"自动登录失败: {msg}")


def cmd_cancel(args):
    # 双击依赖全局合成点击, 必须把同花顺带到前台
    app, pid, app_el = find_app(activate=True)
    t0 = time.perf_counter()
    maybe_login(app_el, no_login=args.no_login)
    if args.account:
        tab_name = ACCOUNT_NAMES.get(args.account.lower(), args.account)
        switch_account(app_el, tab_name)

    _, columns, rows, popup = read_table(app_el, "委托")
    if not rows:
        print(json.dumps({"ok": True, "cancelled": [], "note": "当前没有可撤委托",
                          "popup": popup}, ensure_ascii=False))
        return

    # 选出要撤的委托: --contract 精确匹配 / --code 匹配该代码全部 / --all 全部
    def match(r):
        if args.contract:
            return r.get("合同编号") == args.contract
        if args.code:
            return r.get("证券代码") == args.code
        return True  # --all

    targets = [r for r in rows if match(r)]
    if not targets:
        print(json.dumps({"ok": False, "error": "没有匹配的可撤委托",
                          "orders": rows, "popup": popup}, ensure_ascii=False))
        return

    cancelled, failed = [], []
    for r in targets:
        needle = r.get("合同编号") or (r.get("证券代码", "") + " " + r.get("委托时间", ""))
        win = main_window(app_el)
        ok, msg = cancel_one(app_el, win, needle, timeout=args.timeout)
        item = {k: r.get(k) for k in ("合同编号", "证券代码", "证券名称",
                                      "操作", "委托价格", "委托数量")}
        (cancelled if ok else failed).append({**item, "msg": msg})
        time.sleep(0.5)

    # 复核: 重读委托表确认
    _, _, remain, popup = read_table(app_el, "委托")
    print(json.dumps({"ok": not failed, "cancelled": cancelled, "failed": failed,
                      "remaining": len(remain), "popup": popup,
                      "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)},
                     ensure_ascii=False))


ACCOUNT_NAMES = {"A股": "A股", "real": "A股",
                 "模拟": "模拟", "sim": "模拟", "mock": "模拟"}


def main():
    if len(sys.argv) > 1 and sys.argv[1] in TABLE_TABS:
        table = sys.argv.pop(1)
        ap = argparse.ArgumentParser()
        ap.add_argument("--account", default=None,
                        help="切换账户: A股(真实)/real 或 模拟/sim; 缺省用当前账户")
        ap.add_argument("--keyboard", action="store_true",
                        help="激活同花顺到前台 (默认纯后台)")
        add_login_arg(ap)
        args = ap.parse_args()
        args.table = table
        cmd_read_table(args)
        return

    if len(sys.argv) > 1 and sys.argv[1] == "login":
        ap = argparse.ArgumentParser()
        add_login_arg(ap)
        args = ap.parse_args(sys.argv[2:])
        app, pid, app_el = find_app()
        msg = ensure_login(app_el)
        print(json.dumps({"ok": msg is None, "msg": msg or "已登录"},
                         ensure_ascii=False))
        return

    if len(sys.argv) > 1 and sys.argv[1] == "cancel":
        sys.argv.pop(1)
        ap = argparse.ArgumentParser()
        g = ap.add_mutually_exclusive_group()
        g.add_argument("--contract", default=None, help="按合同编号撤指定一笔")
        g.add_argument("--code", default=None, help="撤该证券代码的全部可撤委托")
        g.add_argument("--all", action="store_true", help="撤销全部可撤委托")
        ap.add_argument("--account", default=None,
                        help="切换账户: A股(真实)/real 或 模拟/sim; 缺省用当前账户")
        ap.add_argument("--timeout", type=float, default=5.0)
        add_login_arg(ap)
        args = ap.parse_args()
        cmd_cancel(args)
        return

    ap = argparse.ArgumentParser()
    ap.add_argument("side", choices=["buy", "sell"])
    ap.add_argument("code")
    ap.add_argument("qty", type=int)
    ap.add_argument("--price", default=None, help="限价; 缺省用联动对手价")
    ap.add_argument("--dry-run", action="store_true", help="只填单不提交")
    ap.add_argument("--keyboard", action="store_true",
                    help="代码用键盘输入 (需前台); AX 联动失效时的备用路径")
    ap.add_argument("--timeout", type=float, default=5.0, help="等弹窗超时秒数")
    add_login_arg(ap)
    args = ap.parse_args()

    steps = {}
    t0 = time.perf_counter()

    app, pid, app_el = find_app(activate=args.keyboard)
    win = main_window(app_el)
    if win is None:
        raise SystemExit("找不到同花顺主窗口")
    maybe_login(app_el, no_login=args.no_login)

    # 卖出: 先切到卖出面板
    if args.side == "sell":
        tab = find_button(win, "卖出")
        if tab is None:
            raise SystemExit("找不到 卖出 tab")
        AXUIElementPerformAction(tab, "AXPress")
        time.sleep(0.3)
        win = main_window(app_el)

    code_f, price_f, qty_f = scan_fields(win)
    submit_title = "确定买入" if args.side == "buy" else "确定卖出"

    # 1. 代码: 聚焦+写值触发联动 (识别市场/带出对手价)
    linked = fill_code(code_f, price_f, args.code, keyboard=args.keyboard)
    if not linked:
        steps["link_warn"] = "价格未就绪 (服务器没响应?), 建议核对名称/价格"
    steps["type_code_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    # 2. 价格: 代码联动会自动带出对手价, 不要清空它! 只有 --price 才覆盖
    t1 = time.perf_counter()
    if args.price:
        set_text(price_f, args.price)
    else:
        got = poll(lambda: field_value(price_f) or None, 3.0)
        if got is None:
            steps["price_warn"] = "价格未联动, 需手动指定 --price"
    steps["price_ms"] = round((time.perf_counter() - t1) * 1000, 1)

    # 3. 数量
    t1 = time.perf_counter()
    set_text(qty_f, str(args.qty))
    steps["qty_ms"] = round((time.perf_counter() - t1) * 1000, 1)
    steps["fill_total_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    fill = {
        "code": field_value(code_f),
        "price": field_value(price_f),
        "qty": field_value(qty_f),
    }

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "side": args.side,
                          "fill": fill, "steps": steps}, ensure_ascii=False))
        return

    # 4. 提交
    btn = find_button(win, submit_title)
    if btn is None:
        raise SystemExit(f"找不到 {submit_title} 按钮")
    t1 = time.perf_counter()
    AXUIElementPerformAction(btn, "AXPress")
    steps["submit_press_ms"] = round((time.perf_counter() - t1) * 1000, 1)

    # 5. 委托确认框 (含取消) -> 校验代码 -> 点确认
    t1 = time.perf_counter()
    dlg = wait_dialog(app_el, has_cancel=True, timeout=args.timeout)
    steps["confirm_dialog_wait_ms"] = round((time.perf_counter() - t1) * 1000, 1)
    if dlg is None:
        warn = wait_dialog(app_el, has_cancel=False, timeout=1.0)
        result = warn[1] if warn else None
        print(json.dumps({"ok": False, "side": args.side, "fill": fill,
                          "steps": steps,
                          "result_text": result or "未出现委托确认框"}, ensure_ascii=False))
        return
    btns, texts = dlg
    confirm_text = " ".join(texts)
    if args.code not in confirm_text.replace(" ", ""):
        print(json.dumps({"ok": False, "error": "确认框代码不符, 未提交",
                          "confirm_text": confirm_text}, ensure_ascii=False))
        return
    t1 = time.perf_counter()
    AXUIElementPerformAction(btns["确认"], "AXPress")
    steps["confirm_press_ms"] = round((time.perf_counter() - t1) * 1000, 1)
    steps["submit_total_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    # 6. 结果框 (只有确认按钮): 券商应答
    t1 = time.perf_counter()
    res = wait_dialog(app_el, has_cancel=False, timeout=args.timeout)
    steps["result_wait_ms"] = round((time.perf_counter() - t1) * 1000, 1)
    result_text = None
    if res:
        btns2, texts2 = res
        result_text = " | ".join(texts2)
        if "确认" in btns2:  # 关掉结果框, 留干净面板给下一次
            AXUIElementPerformAction(btns2["确认"], "AXPress")

    print(json.dumps({"ok": True, "side": args.side, "fill": fill,
                      "steps": steps, "result_text": result_text},
                     ensure_ascii=False))


if __name__ == "__main__":
    load_dotenv()
    main()

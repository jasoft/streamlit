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

用法 (项目内 pyobjc 已是依赖, 直接 uv run; 脱离项目需 --with pyobjc):
  # 只填不提交 (测填写速度)
  uv run python scripts/ths_trade.py buy 601899 100 --dry-run
  # 完整委托: 填单 -> 确定买入 -> 自动点确认 -> 读结果
  uv run python scripts/ths_trade.py buy 601899 100
  # 指定限价 (不给则用联动出的对手价)
  uv run python scripts/ths_trade.py buy 601899 100 --price 34.65
  # 卖出 / 键盘备用路径
  uv run python scripts/ths_trade.py sell 601899 100
  uv run python scripts/ths_trade.py buy 601899 100 --keyboard
  # 持仓/委托/成交/资金明细查询: --account 切账户 (A股/real 或 模拟/sim), 缺省查当前
  uv run python scripts/ths_trade.py positions --account real
  uv run python scripts/ths_trade.py orders      # 委托 (默认只查"今天")
  uv run python scripts/ths_trade.py trades      # 成交
  uv run python scripts/ths_trade.py funds       # 资金明细
  # 撤单: 双击委托行 (需同花顺前台, 会自动激活); 撤完自动复核剩余
  uv run python scripts/ths_trade.py cancel --contract 1140009957
  uv run python scripts/ths_trade.py cancel --code 601899
  uv run python scripts/ths_trade.py cancel --all
  # 完整帮助 (自解释, Agent 可直接读):  uv run python scripts/ths_trade.py help

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


def walk(el, out, _depth=0):
    """深度优先收集 (element, role, title, value, frame|None). 限制深度防 AX 环引用爆栈."""
    if _depth > 500:
        return out
    role = ax_get(el, "AXRole")
    title = ax_get(el, "AXTitle")
    value = ax_get(el, "AXValue")
    out.append((el, role, title, value))
    kids = ax_get(el, "AXChildren")
    if kids:
        for k in kids:
            walk(k, out, _depth + 1)
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


def _real_window(w):
    """校验是真实可操作的窗口 (不是 AX 瞬时故障返回的 AXApplication 等杂物)."""
    if w is None:
        return False
    role = ax_get(w, "AXRole")
    if role != "AXWindow":
        return False
    # 买量窗口必有 frame 和可见子元素 (测试中发现故障时 frame=None、0 子元素)
    f = get_frame(w)
    if not f or f[2] * f[3] <= 0:
        return False
    kids = ax_get(w, "AXChildren")
    return bool(kids)


def main_window(app_el):
    """取同花顺主窗口 (面积最大者). 校验元素有效, 容忍 AX 瞬时故障返回脏数据."""
    for _ in range(5):  # 同花顺偶发 AX 瞬时失败, 返回错 role/空 frame, 重试即可
        wins = ax_get(app_el, "AXWindows")
        cands = []
        if wins:
            for w in wins:
                if _real_window(w):
                    f = get_frame(w)
                    cands.append((f[2] * f[3], w))
        if cands:
            return max(cands, key=lambda p: p[0])[1]
        time.sleep(0.3)
    # 同花顺主窗口不在 AXWindows 枚举里 (实测返回空数组), 用 AXMainWindow /
    # AXFocusedWindow 兜底 (二者通常指向同一铺满屏的主窗口).
    for attr in ("AXMainWindow", "AXFocusedWindow"):
        w = ax_get(app_el, attr)
        if _real_window(w):
            return w
    raise SystemExit("读不到同花顺窗口")


# NOTE: 旧的几何/整窗口遍历函数 (find_button / scan_fields / account_name /
#       _scan_account_tab_area / read_table / find_order_row) 已全部删除.
#       所有命令均改为语义化快速定位:
#         - 按钮/输入框/账户区: _fast_find_button / _fast_find_all_buttons /
#           _fast_scan_fields / _fast_scan_account_area / _fast_account_name
#           (深度受限 DFS, 不进 AXStaticText 叶子)
#         - 读表: read_table_fast  (直接用同花顺原生 AXTable/AXRow/AXHeader)
#         - 委托表内找行: _fast_find_table_row  (先 AXTable 再在 AXRows 里搜)
#       dialogs_with_buttons 是弹窗边界检测 (AXSheet/AXDialog/AXPopover 可能
#       挂在任意层级), 不在"表格操作"范围内, 继续使用 tree() 以保证可靠性.

def _ax_selected(tab_el):
    """tab 是否处于选中态. THS Mac 的 tab 有时用 AXValue=1, 有时用 AXSelected=1."""
    v = ax_get(tab_el, "AXValue") or ax_get(tab_el, "AXSelected")
    return bool(v) and (v == 1 or v is True)


def switch_account(app_el, name, timeout=2.0):
    """点左侧 A股/模拟 tab 切换账户.

    性能要点 (避免 3-5s 死等):
      1. 单次 tree() 同时找两个 tab 和账户名, 省去 2 次冗余整窗口遍历
      2. tab.AXValue/AXSelected 能判选中就直接返回 (0 等待)
      3. 对向 tab 被选中才表明真的需要切换, 此时用轮询精确等待
      4. 没有可靠选中信号 (THS Mac 常见): 按一下 tab, 不 sleep / 不轮询 直接返回.
         后续 read_table / 实际操作 都自带 0.5s UI settle, 足够兜底.
         真切换通常 0.2~0.6s 完成, 就算慢一点也会被后续操作的等待覆盖;
         假切换 (本来就在目标 tab) 也只多一次 AXPress, <10ms, 几乎无感.
    """
    win = main_window(app_el)

    tab_map, acct = _fast_scan_account_area(win)
    tab = tab_map.get(name)
    if tab is None:
        raise SystemExit(f"找不到账户 tab {name!r}")

    # 1. 本 tab 明确被 AX 标记为选中 → 0 等待返回
    if _ax_selected(tab):
        return acct or ""

    # 2. 对向 tab 明确被选中 → 真正需要切换; 用轮询精确检测完成
    alt_names = [n for n in tab_map if n != name]
    alt_ax_selected = False
    for alt in alt_names:
        if _ax_selected(tab_map[alt]):
            alt_ax_selected = True
            break

    # 执行 tab 切换
    AXUIElementPerformAction(tab, "AXPress")

    # 3. 有信号就轮询; 没信号就不等待 (后续操作自带足够 UI settle)
    if alt_ax_selected or acct:
        t0 = time.perf_counter()
        cur_win = win
        while time.perf_counter() - t0 < timeout:
            elapsed = time.perf_counter() - t0
            # 每隔一段刷新一次 win + 重新扫描 tab 选中态 / 账户名
            if elapsed > 0.5:
                try:
                    cur_win = main_window(app_el)
                    new_tabs, new_acct = _fast_scan_account_area(cur_win)
                except SystemExit:
                    new_tabs, new_acct = None, ""
                # 变化信号 A: 目标 tab 被 AX 标记选中
                if new_tabs and name in new_tabs and _ax_selected(new_tabs[name]):
                    return new_acct or ""
                # 变化信号 B: 账户名变了
                if new_acct and new_acct != acct:
                    return new_acct
            else:
                # 前 0.5s: 还没刷新 win, 直接检查 tab 的 AX 属性 (通常同一个引用仍有效)
                if _ax_selected(tab):
                    return acct or ""
            time.sleep(0.12)
        # timeout: 返回当前账户名
        return acct or ""

    # 4. 无可靠选中信号 + 无可读账户名: 按完直接返回, 不 sleep / 不轮询.
    #    THS Mac 客户端 tab 切换通常 0.2~0.6s 完成.
    #    后续 read_table -> 点"持仓/委托/..."tab -> sleep(0.5) -> tree(win),
    #    合计至少 0.5s 已经在后面等了, 足够覆盖真切换.
    return ""


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
    """返回每个弹窗的 (window/sheet_el, {title: element}, [texts]).

    弹窗来源 (按优先级):
      1) AXWindows 枚举中的小窗口 (非主窗口)
      2) AXFocusedWindow (委托确认框是 attached sheet, 不出现在 AXWindows)
      3) 主窗口子树中的 AXSheet / AXDialog 节点 (如收盘后"夜市委托提示"
         是挂在主窗口树上的 AXSheet, 不在 AXWindows 里, 之前完全漏掉)

    文本收集 (同时覆盖几种常见承载角色, 避免"夜市委托提示"藏在 AXTextArea 被漏掉):
      AXStaticText (最常见) / AXTextArea (多行文本区, 用于提示详情)
      / AXHeading / AXTextField (偶尔用)
    """
    wins = ax_get(app_el, "AXWindows") or []
    frames = [(get_frame(w), w) for w in wins]
    frames = [(f, w) for f, w in frames if f]
    main_area = None
    if frames:
        main_area = max(f[2] * f[3] for f, _ in frames)
    small = []
    if main_area:
        small = [(f, w) for f, w in frames if f[2] * f[3] < main_area * 0.5]

    # FocusedWindow (app-modal 对话框通常在这里)
    err, fw = AXUIElementCopyAttributeValue(app_el, "AXFocusedWindow", None)
    if err == 0 and fw is not None:
        ff = get_frame(fw)
        if ff and main_area and ff[2] * ff[3] < main_area * 0.5 and not any(w == fw for _, w in small):
            small.append((ff, fw))

    # 主窗口子树里的 AXSheet / AXDialog / AXAlert (sheet-modal 对话框)
    # "夜市委托提示"就是这种: 挂在主窗口子树上, 不在 AXWindows 枚举里.
    try:
        win_root = main_window(app_el)
        for el, role, t, v in tree(win_root):
            if role in ("AXSheet", "AXDialog", "AXAlert", "AXPopover"):
                ff = get_frame(el)
                # 只有一个主窗口时 (AXWindows 为空), main_area 可能是 None.
                # 无论有没有主窗口面积参照, 只要是这四类节点且没被加入过, 都收进来,
                # 靠外层 "btns 非空" 过滤掉假阳性.
                if not any(w == el for _, w in small):
                    small.append((ff, el))
    except SystemExit:
        pass

    out = []
    for f, w in small:
        btns, texts = {}, []
        for e2, r2, t2, v2 in tree(w):
            if r2 == "AXButton" and isinstance(t2, str) and t2:
                btns[t2] = e2
            elif r2 in ("AXStaticText", "AXHeading") and isinstance(v2, str) and v2.strip():
                texts.append(v2.strip())
            elif r2 in ("AXTextArea", "AXTextField"):
                # AXTextArea 的内容放在 AXValue 里 (夜市委托提示的详情)
                txt = v2 if isinstance(v2, str) else ax_get(e2, "AXValue")
                if isinstance(txt, str) and txt.strip():
                    # 多行文本按行拆开, 去除首尾空行
                    for line in txt.splitlines():
                        line = line.strip()
                        if line:
                            texts.append(line)
        if btns:
            out.append((w, btns, texts))
    return out


def wait_dialog(app_el, has_cancel, timeout, early_ok_after=None):
    """等一个弹窗; has_cancel=True 找委托确认框, False 找结果/警告框.

    early_ok_after: 仅用于 has_cancel=False (结果框等待). 若此秒数内未等到任何结果框,
                    就提前返回 None 判为"无弹窗=成功", 不再等满 timeout.
                    券商失败警告/拒绝弹窗通常 0.2~0.8s 弹出, 成功路径通常不弹窗.
                    把阈值设为 ~0.6s 足以捕获绝大多数拒绝类弹窗, 避免成功时白等数秒.
    """
    def check():
        for _, btns, texts in dialogs_with_buttons(app_el):
            if has_cancel and "取消" in btns and "确认" in btns:
                return (btns, texts)
            if not has_cancel and "确认" in btns and "取消" not in btns:
                return (btns, texts)
        return None

    if not has_cancel and early_ok_after is not None and early_ok_after < timeout:
        # 两阶段短等待: 先用 30ms 间隔密集等到 early_ok_after; 仍无弹窗就判成功返回.
        # 极端罕见的慢失败 (>early_ok_after) 才会被漏判为成功, 但比白等 timeout 值得.
        r = poll(check, early_ok_after, interval=0.04)
        if r is not None:
            return r
        return None  # early_ok_after 内没出结果框 → 视为委托成功 (无弹窗=成功)

    return poll(check, timeout)


def fill_code(code_f, price_f, code, keyboard=False):
    """填代码并等联动 (识别市场/带出对手价). 返回面板是否就绪.

    纯 AX 后台路径: 先清空旧代码和旧价格, 聚焦, 再 set value 触发联动.
    联动判据: 价格框在 ~1s 内出现非空值 (对手价).
    超时则联动未完成, 继续操作必失败 (市场代码为空).
    """
    # 先清空旧价格 (防止 poll 误判: 旧价格残留导致以为联动成功)
    old_price = field_value(price_f)
    if old_price and old_price.strip():
        ax_set(price_f, "AXValue", "")
        time.sleep(0.1)
    # 清空旧代码, 确保值变化事件触发联动 (同股/换股重复下单时尤为重要)
    old = field_value(code_f)
    if old and old.strip():
        ax_set(code_f, "AXValue", "")
        time.sleep(0.3)
    if keyboard:
        type_text(code, pre_field=code_f)
    else:
        ax_set(code_f, "AXFocused", True)
        time.sleep(0.2)
        ax_set(code_f, "AXValue", code)

    # 等价格联动跳出 (通常 0.5-1s), 最多等 5s
    return poll(lambda: (True if field_value(price_f) else None), 5.0) is not None


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


# (旧 read_table 已删除, 统一使用 read_table_fast)

# ---------------------------------------------------------------------------
# 快速读表 (高频查询专用)
#
# 设计原则 (与低频的 buy/sell/cancel 路径严格隔离):
#   1. 不做登录检测 / 账户切换 / 弹窗扫描 — 交给独立的 switch-account 子命令
#   2. 深度受限 DFS 定位 AXButton/AXTable, 不遍历 AXStaticText 叶子
#   3. 利用同花顺已暴露的原生 AXTable/AXRow/AXHeader 语义, 跳过几何聚类
#   4. tab 切换后按 30ms 间隔轮询 AXTable 就绪, 替代 sleep(0.5) 盲等
#
# 实测提速: positions/orders/trades ~180ms (原 1350ms), 7~8x
# ---------------------------------------------------------------------------

_SKIP_DESCEND = frozenset({
    "AXStaticText", "AXTextField", "AXTextArea", "AXHeading",
    "AXImage", "AXValueIndicator", "AXScrollBar", "AXCheckBox",
    "AXRadioButton", "AXPopover",
})


def _fast_find_button(win_root, title, only_enabled=False, max_depth=14):
    """深度受限 DFS 找 AXButton. 忽略静态文本等叶子节点.

    对比 find_button() 的整窗口 tree(): ax_get 调用量从 5000+ 降到 ~700.
    典型耗时 50-70ms (原 150ms+).
    """
    stack = [(win_root, 0)]
    while stack:
        node, d = stack.pop()
        if d > max_depth:
            continue
        role = ax_get(node, "AXRole")
        if role == "AXButton":
            if ax_get(node, "AXTitle") == title:
                if only_enabled and not enabled(node):
                    continue
                return node
            continue
        if role in _SKIP_DESCEND:
            continue
        kids = ax_get(node, "AXChildren")
        if kids:
            for c in kids:
                stack.append((c, d + 1))
    return None


def _fast_find_trade_table(win_root, below_y=0, min_area=100_000,
                           min_width=400, max_depth=14):
    """深度受限 DFS 找持仓/委托/成交/资金明细对应的 AXTable.

    按 4 个过滤条件锁定目标表 (排除左侧 29 行的行情大表和左侧小表):
      - AXTable 角色
      - frame y > tab 按钮 y (在面板下方)
      - area >= 10 万 px² (够大, 不是工具条)
      - width >= 400px (排除 3 列的窄表)
    典型耗时 3-5ms.
    """
    stack = [(win_root, 0)]
    while stack:
        node, d = stack.pop()
        if d > max_depth:
            continue
        role = ax_get(node, "AXRole")
        if role == "AXTable":
            f = get_frame(node)
            if not f:
                continue
            area = f[2] * f[3]
            if area > min_area and f[1] > below_y and f[2] > min_width:
                return node, f
            continue  # AXTable 内部 AXRow/AXCell 不用进入
        if role in _SKIP_DESCEND:
            continue
        kids = ax_get(node, "AXChildren")
        if kids:
            for c in kids:
                stack.append((c, d + 1))
    return None, None


def _collect_row_static_values(row_el):
    """读一个 AXRow 里所有 AXStaticText 的值, 保持从左到右顺序.

    基于迭代栈 (非递归函数调用), 对每条 15 列持仓行 ~0.1ms.
    """
    vals = []
    stack = [row_el]
    while stack:
        nd = stack.pop()
        role = ax_get(nd, "AXRole")
        if role == "AXStaticText":
            v = ax_get(nd, "AXValue")
            if isinstance(v, str) and v.strip():
                vals.append(v.strip())
            continue
        kids = ax_get(nd, "AXChildren")
        if kids:
            stack.extend(reversed(kids))  # 保持左→右 (LIFO 需反转)
    return vals


def _read_columns_from_header(header_el):
    """AXTable.AXHeader (AXGroup) 里按 BFS 找 AXButton, 标题即列名.

    过滤 FILTER_BUTTONS (今天/本周/本月/全撤/... 等非列头操作按钮)
    只保留 enabled=True 的 (表头区域里 disabled 的按钮不算列).
    典型耗时 < 1ms.
    """
    if header_el is None:
        return []
    frontier = ax_get(header_el, "AXChildren") or []
    for _ in range(3):  # AXGroup 嵌套通常不超过 3 层
        found, next_frontier = [], []
        for nd in frontier:
            role = ax_get(nd, "AXRole")
            if role == "AXButton":
                ttl = ax_get(nd, "AXTitle")
                if (isinstance(ttl, str) and ttl
                        and ttl not in FILTER_BUTTONS
                        and enabled(nd)):
                    found.append(ttl)
            else:
                cc = ax_get(nd, "AXChildren")
                if cc:
                    next_frontier.extend(cc)
        if found:
            return found
        frontier = next_frontier
    return []


# ---------------------------------------------------------------------------
# 快速定位辅助 (统一使用深度受限 DFS, 不做整窗口 tree() 遍历)
# ---------------------------------------------------------------------------

def _fast_collect_labeled(win_root, want_roles, max_depth=12):
    """通用: 深度受限 DFS 按角色收集元素 + frame + value.

    want_roles: 只收集 AXRole in want_roles 的元素. 常用组合:
      - 表单标签+输入框:  {"AXStaticText", "AXTextField", "AXComboBox", "AXSecureTextField"}
      - 账户区 (tab + 账户名): {"AXStaticText", "AXButton"}

    value 规则: AXStaticText -> strip(AXValue); AXButton -> AXTitle; 其他 -> None.
    返回: [(element, role, frame_tuple, value_or_None), ...]
          frame_tuple = (x, y, w, h) 且 h>0 (仅返回有有效几何位置的元素)
    对 _SKIP_DESCEND 中的角色 (AXImage/AXStaticText 等叶子) 仅记录 (若 want_roles 需要), 不进入子树.
    对比整窗口 tree(): 对于想找的 2~3 个特定角色, ax_get 调用量下降 80%+.
    """
    results = []
    stack = [(win_root, 0)]
    while stack:
        node, d = stack.pop()
        if d > max_depth:
            continue
        role = ax_get(node, "AXRole")
        val = None
        if role == "AXStaticText":
            v = ax_get(node, "AXValue")
            if isinstance(v, str) and v.strip():
                val = v.strip()
        elif role == "AXButton":
            val = ax_get(node, "AXTitle")
        # AXTextField / AXComboBox / AXSecureTextField -> 不预取值 (调用方按需读值)
        if role in want_roles:
            f = get_frame(node)
            if f and f[3] > 0:
                results.append((node, role, f, val))
        if role in _SKIP_DESCEND:
            continue
        kids = ax_get(node, "AXChildren")
        if kids:
            for c in kids:
                stack.append((c, d + 1))
    return results


def _fast_find_all_buttons(win_root, title=None, only_enabled=False, max_depth=12):
    """返回所有 (或按 title 过滤的) AXButton 列表. 用于 立即登录 有多个时的排序.

    与 _fast_find_button (只返回第一个命中) 互补. 同样基于深度受限 DFS.
    返回: [button_el, ...]  (如果 title=None 则返回全部可按 role 判断的按钮; 否则只匹配 title)
    """
    out = []
    stack = [(win_root, 0)]
    while stack:
        node, d = stack.pop()
        if d > max_depth:
            continue
        role = ax_get(node, "AXRole")
        if role == "AXButton":
            if title is None or ax_get(node, "AXTitle") == title:
                if only_enabled and not enabled(node):
                    pass
                else:
                    out.append(node)
            # AXButton 下有 AXStaticText (title overlay) 但我们不进入, 不影响
            continue
        if role in _SKIP_DESCEND:
            continue
        kids = ax_get(node, "AXChildren")
        if kids:
            for c in kids:
                stack.append((c, d + 1))
    return out


def _fast_scan_fields(win_root):
    """scan_fields 的快速版本: 按 "代码"/"价格"/"数量" 标签锚定右侧 3 个输入框.

    深度受限 DFS 收集 AXStaticText + AXTextField (不进 AXImage 等叶子), 然后用原有的
    同行几何匹配 (y 差 < 15, x > label_x).
    返回 (code_field, price_field, qty_field); 缺失则 raise SystemExit.
    """
    want = {"AXStaticText", "AXTextField", "AXComboBox", "AXSecureTextField"}
    all_items = _fast_collect_labeled(win_root, want)

    labels = {}   # label_text -> (cx, cy)
    fields = []   # [(cx, cy, element)]
    for el, role, f, val in all_items:
        cx = f[0] + f[2] / 2
        cy = f[1] + f[3] / 2
        if role == "AXStaticText" and val in FIELD_LABELS:
            labels[val] = (cx, cy)
        elif role in ("AXTextField", "AXComboBox", "AXSecureTextField"):
            fields.append((cx, cy, el))

    out = {}
    for name, (lx, ly) in labels.items():
        cands = [(abs(cy - ly), cx, el)
                 for cx, cy, el in fields
                 if abs(cy - ly) < 15 and cx > lx]
        if cands:
            out[name] = min(cands)[2]
    missing = [n for n in FIELD_LABELS if n not in out]
    if missing:
        raise SystemExit(f"按标签找不到输入框: {missing} (面板未打开?)")
    return out["代码"], out["价格"], out["数量"]


def _fast_scan_account_area(win_root):
    """_scan_account_tab_area 的快速版本: 找 A股/模拟/添加 按钮 + 账户名文字.

    单次受限 DFS 同时收集: AXButton (tab+添加) + AXStaticText (账户名候选).
    返回 (tab_map, account_name_string) — 同旧 _scan_account_tab_area 签名, 直接替换.
    """
    want = {"AXStaticText", "AXButton"}
    items = _fast_collect_labeled(win_root, want)

    tab_map = {}
    add_btn = None
    texts = []   # [(cx, cy, value)]
    for el, role, f, val in items:
        cx = f[0] + f[2] / 2
        cy = f[1] + f[3] / 2
        if role == "AXButton":
            if val in ("A股", "模拟"):
                tab_map[val] = el
            elif val == "添加":
                add_btn = el
        elif role == "AXStaticText" and val:
            texts.append((cx, cy, val))

    acct = ""
    if add_btn is not None:
        af = get_frame(add_btn)
        if af:
            acx = af[0] + af[2] / 2
            acy = af[1] + af[3] / 2
            best, best_cx = "", -1
            for cx, cy, v in texts:
                # 同行 (y 差 < 15px), 在"添加"按钮左侧, 且最靠近按钮者
                if abs(cy - acy) < 15 and cx < acx and cx > best_cx:
                    best, best_cx = v, cx
            acct = best
    return tab_map, acct


def _fast_account_name(win_root):
    """account_name 的快速版本: 只返回账户名字符串, 无 tab_map."""
    _, name = _fast_scan_account_area(win_root)
    return name


def _fast_find_table_row(app_el, tab_name_cn, needle):
    """find_order_row 的语义快速版本: 先按 tab 定位 AXTable, 再在 AXRows 里找 needle.

    流程: 定位 [tab_name_cn] 按钮 -> press -> 轮询 AXTable 就绪 (同 read_table_fast)
          -> 遍历 AXTable.AXRows, 用 _collect_row_static_values 取每行字符串
          -> 首个 needle in ' '.join(values) 返回 (row_el, values_list)
    不做整窗口 tree() 扫行 (旧 find_order_row 的行为), 只进目标表子树.
    返回: (row_AXUIElement, [value_str, ...]) 或 None
    """
    win = main_window(app_el)
    tab_btn = _fast_find_button(win, tab_name_cn, only_enabled=True)
    if tab_btn is None:
        return None
    tab_f = get_frame(tab_btn)
    tab_y = tab_f[1] if tab_f else 0
    AXUIElementPerformAction(tab_btn, "AXPress")
    # 轮询表就绪 (与 read_table_fast 相同逻辑, 但不读列/行全量)
    t0 = time.perf_counter()
    tbl_el = None
    n_last = -1
    stable_count = 0
    while time.perf_counter() - t0 < 0.5:
        tbl_el, _tbl_f = _fast_find_trade_table(win, below_y=tab_y - 2)
        rows_attr = ax_get(tbl_el, "AXRows") if tbl_el else None
        n = len(rows_attr) if rows_attr else 0
        if n > 0 and n == n_last:
            stable_count += 1
            if stable_count >= 2:
                break
        else:
            stable_count = 0
            n_last = n
        if n == 0 and time.perf_counter() - t0 > 0.12:
            break
        time.sleep(0.03)
    if tbl_el is None:
        return None
    rows_attr = ax_get(tbl_el, "AXRows") or []
    needle_str = str(needle)
    for row_el in rows_attr:
        vals = _collect_row_static_values(row_el)
        if needle_str in " ".join(vals):
            return (row_el, vals)
    return None


def read_table_fast(app_el, tab_name_cn):
    """高频查询专用的快速读表 (AXTable 语义).

    返回格式与旧 read_table 兼容: (account, columns, rows, popup)
    但 account 恒为 "" (不在查询命令里判断账户), popup 恒为 [].
    """
    win = main_window(app_el)  # 已含 _real_window 5 次重试, ~15ms

    # 1. 定位功能 tab 按钮 (持仓/委托/成交/资金明细)  —  ~60ms
    tab_btn = _fast_find_button(win, tab_name_cn, only_enabled=True)
    if tab_btn is None:
        raise SystemExit(f"找不到 {tab_name_cn} tab")

    tab_f = get_frame(tab_btn)
    tab_y = tab_f[1] if tab_f else 0

    # 2. 按 tab + 轮询 AXTable 就绪 (替代 sleep(0.5))  — 典型 10~100ms
    AXUIElementPerformAction(tab_btn, "AXPress")
    t_press = time.perf_counter()
    tbl_el, tbl_f = None, None
    n_rows_last = 0
    while time.perf_counter() - t_press < 0.5:  # 0.5s 兜底
        tbl_el, tbl_f = _fast_find_trade_table(win, below_y=tab_y - 2)
        rows_attr = ax_get(tbl_el, "AXRows") if tbl_el else None
        n_rows = len(rows_attr) if rows_attr else 0
        elapsed = time.perf_counter() - t_press
        # 两个判定条件二选一即可结束:
        #   a) 已经有行数据 → 立即可读
        #   b) 无行数据但已经过了 150ms (空表场景也给 UI 足够 settle)
        if n_rows > 0:
            # 连续两次读到相同行数 → 稳定了
            if n_rows == n_rows_last or elapsed > 0.18:
                break
            n_rows_last = n_rows
        elif elapsed > 0.15:
            # 空表 (资金明细通常 0 行), 不再等
            break
        time.sleep(0.03)

    if tbl_el is None:
        return "", [], [], []

    # 3. 列头 (AXHeader AXGroup → AXButton titles)  —  < 1ms
    columns = _read_columns_from_header(ax_get(tbl_el, "AXHeader"))

    # 4. 行: AXRows → 每 AXRow 的 AXStaticText 列表, 按列数 zip 成 dict
    rows_attr = ax_get(tbl_el, "AXRows") or []
    out_rows = []
    n_cols = len(columns)
    for row_el in rows_attr:
        vals = _collect_row_static_values(row_el)
        if not vals:
            continue
        if n_cols and len(vals) == n_cols:
            out_rows.append(dict(zip(columns, vals)))
        elif n_cols:
            # 列数不匹配 (罕见: UI 未完全渲染 / 行夹了过滤按钮文字)
            # 按 min 长度截断兜底, 剩余挂 _raw_tail 便于排查
            n = min(n_cols, len(vals))
            row_d = dict(zip(columns[:n], vals[:n]))
            if len(vals) > n:
                row_d["_raw_tail"] = vals[n:]
            out_rows.append(row_d)
        else:
            # 没读到列头 (极罕见), 原样返回 values
            out_rows.append({"_vals": vals})

    # account 字段留空 (高频查询不做账户判断), popup 留空 (不扫弹窗)
    return "", columns, out_rows, []


def cmd_read_table(args):
    """高频 4 表查询 (positions/orders/trades/funds) — 极简快速路径.

    已移除: 自动登录检测, 账户切换, 弹窗扫描.
    这些操作请使用独立的 switch-account 子命令 (低频, 周期性运行).
    """
    app, pid, app_el = find_app(activate=False)  # 查询纯后台, 不激活窗口
    t0 = time.perf_counter()
    account, columns, rows, popup = read_table_fast(app_el, TABLE_TABS[args.table])
    print(json.dumps({"ok": True, "table": args.table, "account": account,
                      "count": len(rows), "columns": columns, "rows": rows,
                      "popup": popup,
                      "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)},
                     ensure_ascii=False))


def cmd_switch_account(args):
    """低频维护命令: 登录检查 + 账户切换 (用于定期循环保障状态正确).

    流程:
      1. 加载 .env (THS_USER / THS_PASS)
      2. 找同花顺主窗口 → 检测登录状态
         - 未登录: 用 THS_USER/THS_PASS do_login 自动登录 (最长20s)
         - 已登录: 直接下一步
      3. 解析 target (real/A=A股, sim/模拟=模拟) → switch_account 切 tab
      4. 切换后读一次 account_name 确认账户名
      5. 输出 JSON: {ok, target, login_performed, account_before, account_after, msg, elapsed_ms}
    """
    import os
    t_total = time.perf_counter()
    load_dotenv()
    app, pid, app_el = find_app(activate=False)  # 全程后台, 不抢前台
    login_performed = False
    msg = ""

    # ---- 1. 登录检查 (未登录则自动登录) ----
    win0 = main_window(app_el)
    account_before = _fast_account_name(win0) or ""
    needs_login = _fast_find_button(win0, "立即登录") is not None
    if needs_login:
        user = os.environ.get("THS_USER", "")
        password = os.environ.get("THS_PASS", "")
        if not user or not password:
            print(json.dumps({
                "ok": False,
                "target": args.target,
                "login_performed": False,
                "account_before": account_before,
                "account_after": account_before,
                "msg": "未登录且缺少 THS_USER/THS_PASS 环境变量, 无法自动登录",
                "elapsed_ms": round((time.perf_counter() - t_total) * 1000, 1),
            }, ensure_ascii=False))
            return
        ok, msg_login = do_login(app_el, user, password, timeout=min(20.0, args.timeout - 2))
        login_performed = True
        if not ok:
            print(json.dumps({
                "ok": False,
                "target": args.target,
                "login_performed": True,
                "account_before": account_before,
                "account_after": account_before,
                "msg": f"登录失败: {msg_login}",
                "elapsed_ms": round((time.perf_counter() - t_total) * 1000, 1),
            }, ensure_ascii=False))
            return
        msg = msg_login or "登录成功"

    # ---- 2. 账户 tab 名称映射 ----
    # ACCOUNT_NAMES: {"A股":"A股","real":"A股","模拟":"模拟","sim":"模拟","mock":"模拟"}
    tab_cn = ACCOUNT_NAMES.get(args.target, args.target)

    # ---- 3. 切换账户 ----
    switch_account(app_el, tab_cn)
    time.sleep(0.4)  # 给 UI settle (切换后立即读 account_name 可能还是旧值)

    # ---- 4. 读切换后账户名确认 ----
    win1 = main_window(app_el)
    account_after = _fast_account_name(win1) or ""

    ok = True
    if not account_after:
        # 切换后读不到账户名 → 可能 UI 还在渲染, 多等一次再读
        time.sleep(0.3)
        win1 = main_window(app_el)
        account_after = _fast_account_name(win1) or ""

    print(json.dumps({
        "ok": ok,
        "target": args.target,
        "mapped_tab": tab_cn,
        "login_performed": login_performed,
        "account_before": account_before,
        "account_after": account_after,
        "msg": msg or f"已切换到 {tab_cn}",
        "elapsed_ms": round((time.perf_counter() - t_total) * 1000, 1),
    }, ensure_ascii=False))


def double_click_at(x, y):
    """System Events 双击 (撤单入口). 需要同花顺在前台 (全局合成点击)."""
    osa('tell application "System Events"\n'
        'click at {%d, %d}\n'
        'delay 0.08\n'
        'click at {%d, %d}\n'
        'end tell' % (int(x), int(y), int(x), int(y)))


# (旧 find_order_row 已删除, 撤单场景使用语义化的 _fast_find_table_row)

def cancel_one(app_el, win, needle, timeout=5.0):
    """双击委托行撤单, 自动点确认. 返回 (ok, 描述文本)."""
    # 语义定位: 先按"委托"tab 找 AXTable, 再在 AXRows 里找 needle (不再整窗口 tree 扫行)
    row = _fast_find_table_row(app_el, "委托", needle)
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
    # 确认按钮可能是 "确认"/"确定"/"是"
    confirm_btn = None
    for name in ("确认", "确定", "是"):
        if name in btns:
            confirm_btn = btns[name]
            break
    if confirm_btn:
        AXUIElementPerformAction(confirm_btn, "AXPress")
    # 关掉结果提示框 (若有)
    res = wait_dialog(app_el, has_cancel=False, timeout=timeout)
    res_text = " | ".join(res[1]) if res else None
    if res:
        for name in ("确认", "确定"):
            if name in res[0]:
                AXUIElementPerformAction(res[0][name], "AXPress")
                break
    return True, res_text or "撤单指令已提交"


def cancel_all_button(app_el, timeout=5.0):
    """用"全撤"按钮撤销全部委托 (比双击行更可靠). 返回 (ok, msg)."""
    win = main_window(app_el)
    btn = _fast_find_button(win, "全撤")
    if btn is None:
        return False, "找不到全撤按钮"
    AXUIElementPerformAction(btn, "AXPress")
    time.sleep(0.5)
    # 撤单确认框 (有取消+确认) 或 警告框 (只有确认)
    dlg = wait_dialog(app_el, has_cancel=True, timeout=timeout)
    if dlg is None:
        # 可能直接出警告框 (无取消按钮)
        warn = wait_dialog(app_el, has_cancel=False, timeout=1.0)
        if warn:
            res_text = " | ".join(warn[1])
            for name in ("确认", "确定"):
                if name in warn[0]:
                    AXUIElementPerformAction(warn[0][name], "AXPress")
                    break
            time.sleep(0.2)
            # 循环关闭残留弹窗
            _close_all_dialogs(app_el)
            return True, res_text
        return True, "全撤已执行 (无确认框)"
    btns, dlg_texts = dlg
    for name in ("确认", "确定", "是"):
        if name in btns:
            AXUIElementPerformAction(btns[name], "AXPress")
            break
    # 循环关闭所有结果提示框 (可能有多笔撤单产生多个弹窗)
    res_texts = []
    while True:
        res = wait_dialog(app_el, has_cancel=False, timeout=2.0)
        if res is None:
            break
        res_texts.append(" | ".join(res[1]))
        for name in ("确认", "确定"):
            if name in res[0]:
                AXUIElementPerformAction(res[0][name], "AXPress")
                break
        time.sleep(0.3)
    # 最后再扫一遍, 确保没有残留弹窗
    _close_all_dialogs(app_el)
    return True, " | ".join(res_texts) if res_texts else "全撤已提交"


# 常见关闭/确认类按钮名 (同花顺各类弹窗都可能出现, 扩展后避免卡弹窗)
DIALOG_CLOSE_NAMES = (
    "确认", "确定", "是", "好的", "同意", "继续", "完成",
    "我知道了", "知道了", "知道", "明白了", "OK",
    "关闭", "关掉",  # 带"关闭"按钮的通知
    "查看详情",  # 红包/活动类弹窗的跳转关闭替代 (点它也能消掉弹窗, 只是跳到新页)
    "以后再说", "暂不", "不", "否",  # 拒绝类也算关闭
    "取消",  # 兜底: 没有确认按钮时, "取消"至少能让它消失
)

def _close_all_dialogs(app_el, extra_names=()):
    """循环关闭所有残留弹窗, 直到没有可关闭的弹窗为止.

    extra_names: 额外允许的关闭按钮名 (例如"取消"默认不触发, 传参时可覆盖)."""
    names = tuple(dict.fromkeys(DIALOG_CLOSE_NAMES + tuple(extra_names)))  # 去重保序
    for _ in range(15):  # 最多关15个, 防死循环 (红包+公告+风险+欢迎会连弹4-5个)
        dlgs = dialogs_with_buttons(app_el)
        if not dlgs:
            break
        closed = False
        for _, btns, _ in dlgs:
            for name in names:
                if name in btns:
                    try:
                        AXUIElementPerformAction(btns[name], "AXPress")
                    except Exception:
                        continue
                    closed = True
                    break
            if closed:
                break
        if not closed:
            break
        time.sleep(0.3)  # 给弹窗退出动画留时间, 避免下一轮又把刚关掉的加回来


def do_login(app_el, user, password, timeout=20.0):
    """自动登录: 点"立即登录" -> 登录窗填账号/密码 -> 点登录.

    账号/密码从环境变量 THS_USER / THS_PASS 读取.
    登录窗字段用文字标签锚定: "交易帐户"右侧 combobox, "交易密码"右侧 textfield.
    成功判定: 登录窗消失且主窗口不再有"立即登录"按钮, 之后再扫2s关所有延迟弹窗.
    返回 (ok, msg).
    """
    win = main_window(app_el)
    # 选择距离"确定买入"按钮最近的"立即登录" (避免同时存在行情面板登录按钮时点错)
    # 深度受限 DFS 取所有立即登录按钮 (不再整窗口 tree())
    login_btns = _fast_find_all_buttons(win, title="立即登录")
    candidates = []
    for b in login_btns:
        f = get_frame(b)
        if f:
            candidates.append((f, b))
    if not candidates:
        return True, "已是登录状态"
    buy_btn = _fast_find_button(win, "确定买入") or _fast_find_button(win, "确定卖出")
    if buy_btn is not None:
        bf = get_frame(buy_btn)
        if bf:
            bcx, bcy = bf[0] + bf[2] / 2, bf[1] + bf[3] / 2
            # 距离买入按钮的中心距离最近的立即登录
            candidates.sort(key=lambda p: abs((p[0][0]+p[0][2]/2) - bcx) + abs((p[0][1]+p[0][3]/2) - bcy))
    btn = candidates[0][1]
    AXUIElementPerformAction(btn, "AXPress")

    def login_win():
        for w, btns, _ in dialogs_with_buttons(app_el):
            if "登录" in btns:
                return w
        return None

    dwin = poll(login_win, 10.0)
    if dwin is None:
        _close_all_dialogs(app_el)
        return False, "登录窗未出现"

    # 标签锚点: 单次受限 DFS 同时收集 "交易帐户"/"交易密码" 标签 + 候选输入控件,
    # 再按同行 (y 差 <15, 标签右侧, 水平距离最短) 配对. 替代原先 2 次嵌套 tree(dwin).
    want_dlg = {"AXStaticText", "AXComboBox", "AXTextField", "AXSecureTextField"}
    items = _fast_collect_labeled(dwin, want_dlg)
    labels = {}   # name -> (lx, ly)
    fields = []   # [(cx, cy, el)]
    for el, role, f, val in items:
        cx = f[0] + f[2] / 2
        cy = f[1] + f[3] / 2
        if role == "AXStaticText" and val in ("交易帐户", "交易密码"):
            labels[val] = (cx, cy)
        elif role in ("AXComboBox", "AXTextField", "AXSecureTextField"):
            fields.append((cx, cy, el))

    account_f = pwd_f = None
    for name, (lx, ly) in labels.items():
        cands = [(cx - lx, el)
                 for cx, cy, el in fields
                 if abs(cy - ly) < 15 and cx > lx]
        if cands:
            cands.sort()
            best_el = cands[0][1]
            if name == "交易帐户":
                account_f = best_el
            elif name == "交易密码":
                pwd_f = best_el
    if pwd_f is None:
        _close_all_dialogs(app_el)
        return False, "登录窗里找不到 交易密码 输入框"

    if account_f is not None and user and not field_value(account_f):
        ax_set(account_f, "AXFocused", True)
        time.sleep(0.05)
        ax_set(account_f, "AXValue", user)
    ax_set(pwd_f, "AXFocused", True)
    time.sleep(0.05)
    ax_set(pwd_f, "AXValue", password)
    time.sleep(0.1)

    login_btn = _fast_find_button(dwin, "登录")
    if login_btn is None:
        _close_all_dialogs(app_el)
        return False, "找不到 登录 按钮"
    AXUIElementPerformAction(login_btn, "AXPress")

    # 结果: 成功=登录窗消失且主窗口无"立即登录"; 失败=弹出警告框 (含确认/取消以外各种按钮)
    t0 = time.perf_counter()
    ok = False
    msg = "登录超时"
    fail_reason = ""
    while time.perf_counter() - t0 < timeout:
        try:
            w = main_window(app_el)
        except SystemExit:
            w = None
        if w is not None and _fast_find_button(w, "立即登录") is None \
                and login_win() is None:
            ok = True
            msg = "登录成功"
            break
        # 失败弹窗: 任何只有"确认/确定/是/我知道了/..."类按钮 (没有"取消"以免误伤登录窗)
        for _, btns, texts in dialogs_with_buttons(app_el):
            if "登录" in btns or "取消" in btns:
                break  # 还是登录窗/有取消按钮, 继续等
            # 找任意可关闭按钮并关闭
            closed_name = None
            for name in ("确认", "确定", "是", "我知道了", "知道了", "完成", "好的", "同意"):
                if name in btns:
                    closed_name = name
                    break
            if closed_name is not None:
                fail_reason = " | ".join(texts)
                AXUIElementPerformAction(btns[closed_name], "AXPress")
                time.sleep(0.2)
                _close_all_dialogs(app_el)
                return False, f"登录失败: {fail_reason}"
        time.sleep(0.3)

    # 登录成功后, 额外 2 秒连续扫延迟弹窗 (今日公告/活动红包/风险提醒等会在 0.5-1.5s 后冒出)
    if ok:
        t1 = time.perf_counter()
        _close_all_dialogs(app_el)
        while time.perf_counter() - t1 < 2.0:
            time.sleep(0.2)
            _close_all_dialogs(app_el)

    return ok, msg


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


def notify_bark(title, body="", group="交易"):
    """通过 Bark 推送到 iPhone. 读环境变量 THS_BARK_KEY (可放 .env). 无 key 则静默跳过.

    Bark API: https://api.day.app/<key>/<title>/<body>[?group=<group>]
    失败(网络/无key)静默返回 False, 绝不影响交易主流程.
    """
    import os
    import urllib.parse
    import urllib.request
    key = os.environ.get("THS_BARK_KEY", "").strip()
    if not key or "//" in key:  # 未配置或误填完整 URL 时跳过
        return False
    try:
        path = f"https://api.day.app/{key}/{urllib.parse.quote(title)}"
        if body:
            path += "/" + urllib.parse.quote(body)
        if group:
            path += "?group=" + urllib.parse.quote(group)
        with urllib.request.urlopen(path, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_login(app_el):
    """未登录则自动登录 (读 THS_USER/THS_PASS); 已登录直接返回 None."""
    import os
    win = main_window(app_el)
    if win is None or _fast_find_button(win, "立即登录") is None:
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


def cmd_query_order(args):
    """查询单笔/多笔委托状态. 复用 read_table_fast("委托") 读委托表 (AXTable 语义快速路径, ~200ms),
    与 orders 命令读取同一份底层数据. 再按 code 或 contract 过滤并推断状态.

    与 orders 命令的区别:
      - orders : 直接输出委托表原始行列 (只读, 纯表格数据)
      - query_order: 在委托表之上做:
          * --code / --contract 过滤
          * 根据 备注/委托属性/成交价格/撤销数量 推断 filled / cancelled / rejected / partial / pending
          * 输出 status_map: 合同编号 -> {status, filled_qty, avg_price}

    委托表实际列: 委托日期/委托时间/证券代码/证券名称/操作/备注/委托数量/撤销数量/
                  委托价格/成交价格/合同编号/委托属性
    无显式"状态"列, 从 备注 + 成交价格 + 撤销数量 推断:
      成交价格>0 且 撤销数量=0 -> filled
      撤销数量>=委托数量 -> cancelled
      废单/拒绝关键字 -> rejected
      成交价格>0 且 撤销数量>0 -> partial (部成部撤)
      其余 -> pending
    输出 JSON: {ok, columns, orders, count, status_map:{合同编号:{status,filled_qty,avg_price}}}
    """
    app, pid, app_el = find_app(activate=args.keyboard)
    t0 = time.perf_counter()
    maybe_login(app_el, no_login=args.no_login)
    if args.account:
        tab_name = ACCOUNT_NAMES.get(args.account.lower(), args.account)
        switch_account(app_el, tab_name)
    # 直接用 AXTable 语义路径读『委托』表, 和 orders 命令同一份底层实现, ~200ms
    _, columns, rows, popup = read_table_fast(app_el, "委托")
    if not rows:
        print(json.dumps({"ok": True, "orders": [], "status_map": {},
                          "note": "今日无委托", "popup": popup,
                          "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)},
                         ensure_ascii=False))
        return

    def match(r):
        if args.contract:
            return r.get("合同编号") == args.contract
        if args.code:
            return r.get("证券代码") == args.code
        return True

    matched = [r for r in rows if match(r)]

    def num(v):
        try:
            return float(str(v).replace(",", ""))
        except (ValueError, TypeError):
            return 0.0

    def norm_status(r):
        remark = (r.get("备注") or "").replace(" ", "")
        attr = (r.get("委托属性") or "").replace(" ", "")
        order_qty = num(r.get("委托数量"))
        cancel_qty = num(r.get("撤销数量"))
        fill_price = num(r.get("成交价格"))
        text = remark + attr
        if "废单" in text or "拒绝" in text:
            return "rejected"
        if "已成" in text or "全部成交" in text:
            return "filled"
        if "部分" in text:
            return "partial"
        if "撤" in text:
            return "cancelled"
        # 从数值推断
        if cancel_qty >= order_qty > 0:
            return "cancelled"
        if fill_price > 0:
            return "partial" if cancel_qty > 0 else "filled"
        if cancel_qty > 0:
            return "cancelled"
        return "pending"

    status_map = {}
    for r in matched:
        cno = r.get("合同编号", "")
        order_qty = num(r.get("委托数量"))
        cancel_qty = num(r.get("撤销数量"))
        fill_price = num(r.get("成交价格"))
        st = norm_status(r)
        # filled_qty: 委托数量 - 撤销数量 (成交的部分)
        filled_qty = int(order_qty - cancel_qty) if fill_price > 0 else 0
        status_map[cno] = {
            "status": st,
            "filled_qty": filled_qty,
            "avg_price": fill_price,
        }
    print(json.dumps({"ok": True, "columns": columns, "orders": matched,
                      "count": len(matched), "status_map": status_map,
                      "popup": popup,
                      "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)},
                     ensure_ascii=False))


def cmd_cancel(args):
    # 双击依赖全局合成点击, 必须把同花顺带到前台
    app, pid, app_el = find_app(activate=True)
    t0 = time.perf_counter()
    maybe_login(app_el, no_login=args.no_login)
    if args.account:
        tab_name = ACCOUNT_NAMES.get(args.account.lower(), args.account)
        switch_account(app_el, tab_name)

    _, columns, rows, popup = read_table_fast(app_el, "委托")
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

    # --all 直接走"全撤"按钮 (比双击行更可靠)
    if args.all:
        ok, msg = cancel_all_button(app_el, timeout=args.timeout)
        item = {k: targets[0].get(k) for k in ("合同编号", "证券代码", "证券名称",
                                                "操作", "委托价格", "委托数量")}
        cancelled = [item] if ok else []
        failed = [] if ok else [{**item, "msg": msg}]
    else:
        cancelled, failed = [], []
        for r in targets:
            needle = r.get("合同编号") or (r.get("证券代码", "") + " " + r.get("委托时间", ""))
            win = main_window(app_el)
            ok, msg = cancel_one(app_el, win, needle, timeout=args.timeout)
            item = {k: r.get(k) for k in ("合同编号", "证券代码", "证券名称",
                                          "操作", "委托价格", "委托数量")}
            (cancelled if ok else failed).append({**item, "msg": msg})
            time.sleep(0.5)

        # 双击失败时回退到"全撤"按钮
        if failed and not cancelled:
            ok, msg = cancel_all_button(app_el, timeout=args.timeout)
            if ok:
                cancelled = [{**item, "msg": "全撤回退成功"} for item in
                             [{k: r.get(k) for k in ("合同编号", "证券代码", "证券名称",
                                                      "操作", "委托价格", "委托数量")}
                              for r in targets]]
                failed = []

    # 复核: 重读委托表确认
    _, _, remain, popup = read_table_fast(app_el, "委托")
    print(json.dumps({"ok": not failed, "cancelled": cancelled, "failed": failed,
                      "remaining": len(remain), "popup": popup,
                      "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)},
                     ensure_ascii=False))


ACCOUNT_NAMES = {"A股": "A股", "real": "A股",
                 "模拟": "模拟", "sim": "模拟", "mock": "模拟"}

HELP_TEXT = """\
同花顺 Mac 客户端 GUI 自动交易工具 (ths_trade.py)
基于 macOS Accessibility API 的纯后台 GUI 自动化, 不依赖屏幕坐标.

══════════════════════════════════════════════════════════════════
运行方式
══════════════════════════════════════════════════════════════════
  # 项目内 (pyobjc 已在 pyproject.toml 依赖中, 推荐):
  uv run python scripts/ths_trade.py <command> [options]

  # 脱离项目独立运行 (需临时拉取 pyobjc):
  uv run --with pyobjc python scripts/ths_trade.py <command> [options]

══════════════════════════════════════════════════════════════════
前置条件
══════════════════════════════════════════════════════════════════
  1. 同花顺 Mac 客户端 (cn.com.10jqka.macstock) 已启动并显示主窗口
  2. 运行脚本的终端 App 已在:
     系统设置 -> 隐私与安全性 -> 辅助功能 中勾选
  3. .env 配置 (项目根目录, 可选):
     THS_USER=交易帐户     # 自动登录用 (不配则需手动登录)
     THS_PASS=交易密码     # 自动登录用
     THS_BARK_KEY=Bark密钥 # 下单失败推送到 iPhone (不配则静默跳过)

══════════════════════════════════════════════════════════════════
命令一览
══════════════════════════════════════════════════════════════════
  交易类:
    buy   <code> <qty> [--price P] [--dry-run]    买入证券
    sell  <code> <qty> [--price P] [--dry-run]    卖出证券
    cancel (--contract C | --code X | --all)      撤销委托

  查询类:
    positions                                       持仓表 (高频快速, ~200ms, 不切账户)
    orders                                          委托表 (今日) (高频快速, ~200ms)
    trades                                          成交表 (高频快速, ~200ms)
    funds                                           资金明细 (高频快速, ~200ms)
    query_order [--code X | --contract C]           委托状态推断 (filled/cancelled/...)

  其他:
    login                                          触发/检查自动登录
    switch-account {real|sim|A股|模拟}              登录检查 + 账户切换 (低频维护, 推荐定时循环)
    help                                           显示本帮助

══════════════════════════════════════════════════════════════════
通用选项 (适用于多数命令, 但 positions/orders/trades/funds 四条高频快速查询除外)
══════════════════════════════════════════════════════════════════
  --account {A股,real,模拟,sim,mock}  执行前切换到指定账户, 缺省用当前账户
  --no-login                          跳过自动登录检测 (默认未登录时自动登录)
  --keyboard                          激活同花顺到前台, 用键盘输入 (AX 失效时备用)
  --timeout FLOAT                     等弹窗超时秒数 (默认 5.0, 仅 buy/sell/cancel)

注意: positions/orders/trades/funds 为高频优化路径, 不接受 --account/--no-login/--keyboard.
     若需要切换账户或保持登录状态, 请单独调用 switch-account 子命令 (推荐放定时循环里).

输出: 所有命令均输出一行 JSON, 字段含义见各命令详情.

══════════════════════════════════════════════════════════════════
buy / sell — 买入 / 卖出
══════════════════════════════════════════════════════════════════
语法:
  ths_trade.py buy  <code> <qty> [--price P] [--dry-run] [--keyboard] [--timeout T] [--no-login]
  ths_trade.py sell <code> <qty> [--price P] [--dry-run] [--keyboard] [--timeout T] [--no-login]

参数:
  code    证券代码, 如 601899 (A股) / 513120 (ETF)
  qty     委托数量 (股), 正整数; A股需 100 的倍数, ETF 需 100 的倍数

选项:
  --price P      限价; 缺省用代码联动带出的对手价 (买一/卖一)
  --dry-run      只填单不提交, 用于测试填写是否正确
  --keyboard     用键盘输入代码 (需同花顺在前台); AX 联动失效时的备用路径
  --timeout T    等委托确认框/结果框超时秒数 (默认 5.0)
  --no-login     跳过自动登录

输出 JSON:
  {
    "ok": true|false,            # 委托是否被券商受理 (无弹窗=成功)
    "side": "buy"|"sell",
    "fill": {                    # 实际填入面板的值
      "code": "601899",
      "price": "34.65",          # 联动对手价 或 --price 指定值
      "qty": "100"
    },
    "steps": { ... },            # 各步耗时 (毫秒)
    "result_text": "委托已提交",  # 券商返回弹窗文本; null=无弹窗(成功)
    "error": "..."               # 仅 ok=false 时有
  }

注意:
  - 代码联动是关键: 填代码后同花顺识别市场并带出对手价; 联动失败则提交必报
    "市场代码不允许为空", 脚本会自动重试一次联动
  - 本命令只表示"委托是否被券商受理", 是否成交需后续用 query_order 或 orders 查询
  - T+1: A股当日买入不能当日卖出; ETF (如 513120) 支持 T+0
  - 失败推送: 券商明确拒绝/状态未知时自动 Bark 推送 (需配 THS_BARK_KEY)

示例:
  uv run python scripts/ths_trade.py buy 601899 100
  uv run python scripts/ths_trade.py buy 601899 100 --price 34.65
  uv run python scripts/ths_trade.py sell 601899 100
  uv run python scripts/ths_trade.py buy 513120 100 --dry-run

══════════════════════════════════════════════════════════════════
positions — 持仓查询
══════════════════════════════════════════════════════════════════
语法:
  ths_trade.py positions

特性: 高频优化路径. 直接用 AXTable 语义节点读表, 内部 ~200ms, 不做登录判断 / 不切换账户 / 不扫描弹窗.
     如需切换账户或保持已登录状态, 请用 switch-account 子命令 (推荐单独跑在定时循环里).

输出 JSON:
  {
    "ok": true,
    "table": "positions",
    "account": "",                    # 高频路径恒为空串, 账户信息由 switch-account 维护
    "count": 3,
    "columns": ["证券代码","证券名称","持仓数量","可用数量","成本价","当前价","盈亏",...],
    "rows": [ {"证券代码":"601899","证券名称":"紫金矿业","持仓数量":"200",...}, ... ],
    "popup": [],                      # 高频路径恒为空数组
    "elapsed_ms": 210.5
  }

注意: rows 中值均为字符串, 数值字段需自行 float() 转换.

══════════════════════════════════════════════════════════════════
orders — 委托查询 (今日)
══════════════════════════════════════════════════════════════════
语法:
  ths_trade.py orders

特性: 高频优化路径. 同 positions, 不做登录判断 / 不切换账户 / 不扫描弹窗.

输出: 同 positions 结构, table="orders".
列: 委托日期/委托时间/证券代码/证券名称/操作/备注/委托数量/撤销数量/
    委托价格/成交价格/合同编号/委托属性
注意: 委托表无显式"状态"列, 建议用 query_order 获取已推断好的状态.

══════════════════════════════════════════════════════════════════
trades — 成交查询
══════════════════════════════════════════════════════════════════
语法:
  ths_trade.py trades

特性: 高频优化路径. 同 positions, 不做登录判断 / 不切换账户 / 不扫描弹窗.
输出: 同 positions 结构, table="trades".

══════════════════════════════════════════════════════════════════
funds — 资金明细查询
══════════════════════════════════════════════════════════════════
语法:
  ths_trade.py funds

特性: 高频优化路径. 同 positions, 不做登录判断 / 不切换账户 / 不扫描弹窗.
输出: 同 positions 结构, table="funds".

══════════════════════════════════════════════════════════════════
query_order — 委托状态查询 (带状态推断)
══════════════════════════════════════════════════════════════════
语法:
  ths_trade.py query_order [--code X | --contract C] [--account A] [--keyboard] [--no-login]

与 orders 命令的关系:
  两者读的是同花顺界面上的同一张『委托』表, 走同一份 AXTable 语义快速路径 (~400ms).
  区别仅在于 query_order 在原始数据之上额外做了:
    1. --code / --contract 过滤 (都不指定则返回今日全部委托)
    2. 状态推断 (见下), 并输出 status_map: 合同编号 -> {status, filled_qty, avg_price}
  如果只需要原始委托表 rows/columns 而不需要状态推断, 直接用 orders 命令即可.

性能: 表读 ~400ms (与 orders 同速), 过滤和状态推断额外开销 < 1ms, 通常可忽略.
      本命令仍保留 --account / --no-login / --keyboard (支持按需切换账户 / 跳过登录检查 /
      键盘输入模式), 所以适合在单笔下单后 "一次性" 查成交或被轮询调用 (轮询场景建议加
      --no-login 跳过登录检查, 再配合 switch-account 在定时循环里兜底保持登录).

选项 (二选一, 缺省返回全部今日委托):
  --code X       按证券代码过滤
  --contract C   按合同编号精确匹配

状态推断规则 (从 备注+成交价格+撤销数量+委托属性 推断):
  filled      成交价>0 且 撤销量=0 (或备注含"已成")
  cancelled   撤销量>=委托量 (或备注含"撤")
  rejected    备注含"废单"/"拒绝"
  partial     成交价>0 且 撤销量>0 (部成部撤)
  pending     其余 (已报待成交)

输出 JSON:
  {
    "ok": true,
    "columns": [...],
    "orders": [...],            # 匹配的委托行 (原始字典)
    "count": 1,
    "status_map": {             # 合同编号 -> 状态
      "1140009957": {
        "status": "filled",     # filled/cancelled/rejected/partial/pending
        "filled_qty": 100,
        "avg_price": 34.65
      }
    },
    "popup": [],
    "note": "",                 # 空表时返回 "今日无委托"
    "elapsed_ms": 400.5
  }

示例:
  uv run python scripts/ths_trade.py query_order --code 601899 --no-login   # 轮询推荐: 加 --no-login 更快
  uv run python scripts/ths_trade.py query_order --contract 1140009957

══════════════════════════════════════════════════════════════════
cancel — 撤单
══════════════════════════════════════════════════════════════════
语法:
  ths_trade.py cancel (--contract C | --code X | --all) [--account A] [--timeout T] [--no-login]

选项 (三选一):
  --contract C   按合同编号撤指定一笔
  --code X       撤该证券代码的全部可撤委托
  --all          撤全部可撤委托 (走"全撤"按钮, 比双击行更可靠)
  --timeout T    等确认框超时秒数 (默认 5.0)

机制:
  - 单笔: 双击委托行 -> 点确认 -> 读结果; 失败自动回退"全撤"按钮
  - 全部: 点"全撤" -> 点确认 -> 循环关结果弹窗
  - 撤完自动重读委托表复核剩余数
  - 需要 同花顺在前台 (双击依赖全局合成点击), 脚本会自动激活窗口

输出 JSON:
  {
    "ok": true|false,           # failed 为空则 true
    "cancelled": [ {"合同编号":...,"证券代码":...,"msg":"撤单指令已提交"} ],
    "failed":     [ {"合同编号":...,"msg":"找不到委托行: ..."} ],
    "remaining": 0,             # 复核后剩余可撤委托数
    "popup": [],
    "elapsed_ms": 3456.7
  }

示例:
  uv run python scripts/ths_trade.py cancel --contract 1140009957
  uv run python scripts/ths_trade.py cancel --code 601899
  uv run python scripts/ths_trade.py cancel --all

══════════════════════════════════════════════════════════════════
login — 登录
══════════════════════════════════════════════════════════════════
语法:
  ths_trade.py login [--no-login]

行为: 检测登录状态, 未登录则用 THS_USER/THS_PASS 自动登录.
      所有交易/查询命令执行前会自动调用登录检测, 通常无需单独执行.

输出 JSON:
  {"ok": true, "msg": "已登录"}         # 已登录
  {"ok": true, "msg": "登录成功"}       # 刚登录成功
  {"ok": false, "msg": "登录失败: ..."}

══════════════════════════════════════════════════════════════════
switch-account — 登录检查 + 账户切换 (低频维护)
══════════════════════════════════════════════════════════════════
语法:
  ths_trade.py switch-account {real|sim|A股|模拟} [--timeout TIMEOUT]

位置参数:
  real / A股     切换到真实交易账户 ("A股" 账户标签)
  sim / 模拟     切换到模拟交易账户 ("模拟" 账户标签)

选项:
  --timeout FLOAT    每一步 (找登录框/登录/切tab/读账户名) 的超时秒数, 默认 20.0

依赖环境变量 (和 login 命令一致, 项目根 .env 加载):
  THS_USER=交易帐号     # 未登录时才需要, 已登录状态可省
  THS_PASS=交易密码

设计用途: 低频维护命令, 推荐放到定时调度循环 (例如每 30s 跑一次), 确保同花顺客户端始终
         处于"已登录 + 正确账户"的状态. 把耗时的登录 + 账户切换 (~1.2s) 剥离到独立
         路径, 让 positions/orders/trades/funds 四条高频查询可以只做最快速的表读
         (~200ms), 不会因为每次额外判断登录 / 切换账户而变慢.

执行流程:
  1) 加载 .env 中的 THS_USER / THS_PASS
  2) 检测是否有"立即登录"按钮: 有 → 未登录, 自动调用登录逻辑 (已登录则跳过)
  3) 将参数 {real|sim|A股|模拟} 映射为交易面板上的账户标签 (A股 / 模拟)
  4) 找到对应的账户 tab 按钮并点击
  5) 读一次账户文本框, 确认切换是否成功

输出 JSON:
  {
    "ok": true|false,
    "target": "real",                      # 调用入参 (real/sim/A股/模拟)
    "mapped_tab": "A股",                   # 实际点击的 tab 名 (A股 | 模拟)
    "login_performed": false,              # 本次是否真正执行了登录
    "account_before": "",                  # 切换前读到的账户名 (可能为空=首次)
    "account_after":  "王伟",              # 切换后读到的账户名 (成功时应 != "")
    "msg": "已切换到 A股",                 # 人类可读描述
    "elapsed_ms": 1298.7
  }

典型 JSON 返回示例:
  # 已登录, 已是目标账户 (无副作用)
  {"ok":true,"target":"real","mapped_tab":"A股","login_performed":false,
   "account_before":"王伟","account_after":"王伟","msg":"已是 A股 账户",
   "elapsed_ms":320.4}

  # 已登录, 切换 sim -> real
  {"ok":true,"target":"real","mapped_tab":"A股","login_performed":false,
   "account_before":"模拟账户A","account_after":"王伟","msg":"已切换到 A股",
   "elapsed_ms":980.1}

  # 未登录, THS_USER/THS_PASS 登录成功并切到 sim
  {"ok":true,"target":"sim","mapped_tab":"模拟","login_performed":true,
   "account_before":"","account_after":"模拟账户A","msg":"登录成功, 已切换到 模拟",
   "elapsed_ms":3450.6}

  # 失败: 找不到 THS_USER / THS_PASS 环境变量
  {"ok":false,"target":"sim","mapped_tab":"模拟","login_performed":false,
   "msg":"需要 .env 中设置 THS_USER/THS_PASS 才能自动登录"}

  # 失败: 找不到目标 tab
  {"ok":false,"target":"sim","mapped_tab":"模拟","login_performed":false,
   "msg":"找不到账户 tab: 模拟"}

定时运行示例 (bash / cron / launchd 场景, 每 30s 保证是真实账户):
  while true; do
    uv run python scripts/ths_trade.py switch-account real >/dev/null 2>&1
    sleep 30
  done

注意:
  * 本命令是"幂等"的: 已登录且已在目标账户时也可以安全重复调用, 不会产生副作用.
  * 高频查询 (positions/orders/trades/funds) 并不依赖本命令, 但如果同花顺掉线或跳到了
    别的账户, 查询命令也不会自动切回; 必须靠外部定时循环跑 switch-account 来兜底.
  * buy / sell / cancel / query_order 这些低频的交易/查询命令本身仍会自动登录和切
    账户 (通过 --account 参数), 不受影响.

示例:
  uv run python scripts/ths_trade.py switch-account real
  uv run python scripts/ths_trade.py switch-account sim
  uv run python scripts/ths_trade.py switch-account A股  --timeout 30
  uv run python scripts/ths_trade.py switch-account 模拟

══════════════════════════════════════════════════════════════════
常见问题
══════════════════════════════════════════════════════════════════
Q: 报 "同花顺未运行"?          A: 先启动同花顺 Mac 客户端.
Q: 报 "读不到同花顺窗口"?       A: AX 瞬时故障 (已内置5次重试); 仍失败则检查辅助功能权限.
Q: buy 报 "代码联动失败"?       A: 面板异常/AX失效. 尝试加 --keyboard, 或重启同花顺.
Q: 下单后如何确认是否成交?     A: query_order --contract <合同编号>, 或 orders 看委托表.
Q: 如何切换模拟盘/真实盘?      A: 推荐用独立子命令: switch-account sim (或 real).
                              positions/orders/trades/funds 不支持 --account.
                              buy/sell/cancel/query_order 仍支持 --account 参数.
Q: 高频查询报空表/错数据?      A: 先跑 switch-account real 或 switch-account sim, 确认同花顺
                              停留在正确账户和已登录状态.
Q: rows 的值为什么都是字符串?  A: AX 读到的表格单元格均为文本, 数值需自行 float() 转换.
"""


def print_help():
    print(HELP_TEXT)


def main():
    # 帮助: 无参数 / help / --help / -h
    if len(sys.argv) <= 1 or sys.argv[1] in ("help", "--help", "-h", "h"):
        print_help()
        return

    if len(sys.argv) > 1 and sys.argv[1] in TABLE_TABS:
        # 高频查询命令: 极简参数 (不做登录/账户/弹窗判断, ~200ms 典型)
        # 登录状态 & 账户切换用独立的 switch-account 命令 (周期性执行)
        table = sys.argv.pop(1)
        ap = argparse.ArgumentParser(
            prog=f"ths_trade.py {table}",
            description=f"高速查询{TABLE_TABS[table]}表 (~200ms)."
                        f" 不检查登录 / 不切账户 / 不扫弹窗, 这些请用 switch-account 命令."
                        f" (运行 `help` 查看完整命令文档与输出 JSON 结构)",
            formatter_class=argparse.RawDescriptionHelpFormatter)
        args = ap.parse_args()
        args.table = table
        cmd_read_table(args)
        return

    if len(sys.argv) > 1 and sys.argv[1] == "switch-account":
        sys.argv.pop(1)
        ap = argparse.ArgumentParser(
            prog="ths_trade.py switch-account {real|sim|A股|模拟}",
            description="低频维护命令: 检查登录 (未登录则用 THS_USER/THS_PASS 自动登录), "
                        "然后切换到指定账户 (A股/真实 或 模拟). "
                        "设计用于定期调度循环里 (例如每 30s 运行一次), 保障同花顺客户端停留在"
                        "正确的账户 & 登录状态, 不影响高频查询 API. "
                        "(运行 `help` 查看完整命令文档与输出 JSON 结构)",
            formatter_class=argparse.RawDescriptionHelpFormatter)
        ap.add_argument("target",
                        choices=["real", "sim", "A股", "模拟"],
                        help="real/A=真实账户, sim/模拟=模拟账户")
        ap.add_argument("--timeout", type=float, default=25.0,
                        help="登录+切换合计超时秒数 (默认 25.0, 含登录最长 20s)")
        args = ap.parse_args()
        cmd_switch_account(args)
        return

    if len(sys.argv) > 1 and sys.argv[1] == "login":
        ap = argparse.ArgumentParser(
            prog="ths_trade.py login",
            description="检测同花顺登录状态, 未登录则用 THS_USER/THS_PASS 自动登录. "
                        "所有交易/查询命令执行前会自动调用登录检测, 通常无需单独执行. "
                        "(运行 `help` 查看完整命令文档)")
        add_login_arg(ap)
        args = ap.parse_args(sys.argv[2:])
        app, pid, app_el = find_app()
        msg = ensure_login(app_el)
        print(json.dumps({"ok": msg is None, "msg": msg or "已登录"},
                         ensure_ascii=False))
        return

    if len(sys.argv) > 1 and sys.argv[1] == "cancel":
        sys.argv.pop(1)
        ap = argparse.ArgumentParser(
            prog="ths_trade.py cancel",
            description="撤销委托. 单笔双击委托行, 全部走\"全撤\"按钮; 双击失败自动回退\"全撤\". "
                        "撤完自动复核剩余可撤委托数. 需同花顺在前台(双击依赖全局点击). "
                        "(运行 `help` 查看完整命令文档与输出 JSON 结构)",
            formatter_class=argparse.RawDescriptionHelpFormatter)
        g = ap.add_mutually_exclusive_group()
        g.add_argument("--contract", default=None, help="按合同编号撤指定一笔")
        g.add_argument("--code", default=None, help="撤该证券代码的全部可撤委托")
        g.add_argument("--all", action="store_true", help="撤销全部可撤委托 (走\"全撤\"按钮)")
        ap.add_argument("--account", default=None,
                        help="切换账户: A股(真实)/real 或 模拟/sim; 缺省用当前账户")
        ap.add_argument("--timeout", type=float, default=5.0,
                        help="等确认框超时秒数 (默认 5.0)")
        add_login_arg(ap)
        args = ap.parse_args()
        cmd_cancel(args)
        return

    if len(sys.argv) > 1 and sys.argv[1] == "query_order":
        sys.argv.pop(1)
        ap = argparse.ArgumentParser(
            prog="ths_trade.py query_order",
            description="查询委托状态并推断 filled/cancelled/rejected/partial/pending. "
                        "从 备注+成交价格+撤销数量+委托属性 推断. "
                        "(运行 `help` 查看完整命令文档与输出 JSON 结构)",
            formatter_class=argparse.RawDescriptionHelpFormatter)
        g = ap.add_mutually_exclusive_group()
        g.add_argument("--contract", default=None, help="按合同编号查单笔")
        g.add_argument("--code", default=None, help="按证券代码查全部今日委托")
        ap.add_argument("--account", default=None,
                        help="切换账户: A股(真实)/real 或 模拟/sim; 缺省用当前账户")
        ap.add_argument("--keyboard", action="store_true",
                        help="激活同花顺到前台 (默认纯后台)")
        add_login_arg(ap)
        args = ap.parse_args()
        cmd_query_order(args)
        return

    ap = argparse.ArgumentParser(
        prog="ths_trade.py buy|sell",
        description="买入/卖出证券. 填代码->联动带出对手价->填价格/数量->提交->确认->读结果. "
                    "代码联动是关键步骤, 失败会自动重试一次. "
                    "本命令只表示委托是否被券商受理, 是否成交需用 query_order 查询. "
                    "(运行 `help` 查看完整命令文档与输出 JSON 结构)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("side", choices=["buy", "sell"], help="buy=买入, sell=卖出")
    ap.add_argument("code", help="证券代码, 如 601899 (A股) / 513120 (ETF)")
    ap.add_argument("qty", type=int, help="委托数量 (股), 正整数; A股/ETF 需 100 的倍数")
    ap.add_argument("--price", default=None, help="限价; 缺省用联动带出的对手价 (买一/卖一)")
    ap.add_argument("--dry-run", action="store_true", help="只填单不提交, 用于测试")
    ap.add_argument("--keyboard", action="store_true",
                    help="代码用键盘输入 (需前台); AX 联动失效时的备用路径")
    ap.add_argument("--timeout", type=float, default=5.0, help="等弹窗超时秒数 (默认 5.0)")
    ap.add_argument("--account", default=None,
                    help="切换账户: A股(真实)/real 或 模拟/sim; 缺省用当前账户")
    ap.add_argument("--bark-on-reject", action="store_true",
                    help="券商明确拒绝时也通过 Bark 推手机 (默认仅'状态未知'才强制推)")
    add_login_arg(ap)
    args = ap.parse_args()

    steps = {}
    t0 = time.perf_counter()

    app, pid, app_el = find_app(activate=args.keyboard)
    win = main_window(app_el)
    if win is None:
        raise SystemExit("找不到同花顺主窗口")
    maybe_login(app_el, no_login=args.no_login)

    # 切换账户 (--account real/sim)
    if args.account:
        tab_name = ACCOUNT_NAMES.get(args.account.lower(), args.account)
        switch_account(app_el, tab_name)
        win = main_window(app_el)

    # 清理残留弹窗 (上次失败可能留下警告框, 挡住代码框导致联动失败)
    _close_all_dialogs(app_el)

    # 切到买入/卖出面板 (同花顺会记住上次的面板, 不显式切换可能停在卖出面板)
    tab_name = "买入" if args.side == "buy" else "卖出"
    tab = _fast_find_button(win, tab_name)
    if tab is not None:
        AXUIElementPerformAction(tab, "AXPress")
        time.sleep(0.5)
        win = main_window(app_el)

    code_f, price_f, qty_f = _fast_scan_fields(win)
    submit_title = "确定买入" if args.side == "buy" else "确定卖出"

    # 1. 代码: 清空+聚焦+写值触发联动 (识别市场/带出对手价)
    # 联动失败则重试一次, 仍失败则中止 (市场代码为空, 提交必败)
    linked = fill_code(code_f, price_f, args.code, keyboard=args.keyboard)
    if not linked:
        steps["link_retry"] = "首次联动失败, 重试中..."
        time.sleep(0.5)
        # 重新扫描面板 (防止元素失效)
        win = main_window(app_el)
        code_f, price_f, qty_f = _fast_scan_fields(win)
        linked = fill_code(code_f, price_f, args.code, keyboard=args.keyboard)
    steps["type_code_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    if not linked:
        print(json.dumps({"ok": False, "error": "代码联动失败, 价格未跳出, 市场代码为空; 请检查同花顺面板状态或重试",
                          "side": args.side, "fill": {"code": args.code}, "steps": steps},
                         ensure_ascii=False))
        return

    # 联动成功后等 UI 完全提交价格/市场代码, 再操作后续字段
    time.sleep(0.5)

    # 2. 价格: 代码联动已带出对手价, 只有 --price 才覆盖
    t1 = time.perf_counter()
    if args.price:
        set_text(price_f, args.price)
        time.sleep(0.2)
    steps["price_ms"] = round((time.perf_counter() - t1) * 1000, 1)

    # 3. 数量
    t1 = time.perf_counter()
    set_text(qty_f, str(args.qty))
    time.sleep(0.2)
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
    btn = _fast_find_button(win, submit_title)
    if btn is None:
        raise SystemExit(f"找不到 {submit_title} 按钮")
    t1 = time.perf_counter()
    AXUIElementPerformAction(btn, "AXPress")
    steps["submit_press_ms"] = round((time.perf_counter() - t1) * 1000, 1)

    # 5. 委托确认框 (含取消) -> 校验代码 -> 点确认
    # 确认框一定得等到 (正常流程 0.1-1.2s 出), 用 args.timeout 兜底
    t1 = time.perf_counter()
    dlg = wait_dialog(app_el, has_cancel=True, timeout=args.timeout)
    steps["confirm_dialog_wait_ms"] = round((time.perf_counter() - t1) * 1000, 1)
    if dlg is None:
        warn = wait_dialog(app_el, has_cancel=False, timeout=1.0, early_ok_after=0.8)
        result = warn[1] if warn else None
        print(json.dumps({"ok": False, "side": args.side, "fill": fill,
                          "steps": steps,
                          "result_text": result or "未出现委托确认框"}, ensure_ascii=False))
        # 已点提交但未等到确认框: 订单命运未知, 可能资金/持仓已变动, 无法挽回 → 立即通知
        notify_bark(f"⚠️ {args.side}单状态未知 {args.code}",
                    f"{args.side} {args.code} x{args.qty} @{fill['price'] or fill.get('price')}\n"
                    f"{result or '未出现委托确认框, 请立即人工核查'}")
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
    # 成功路径通常 无弹窗 (直接跳下一步) 或 "委托已提交" 类提示一闪而过;
    # 失败 (余额不足/废单/拒绝等) 的警告弹窗通常 0.2~0.8s 就弹出.
    # 采用 early_ok_after 策略: 1.5s 内没出结果框即判委托成功, 不再等满 timeout.
    # 原逻辑等满 5s 才判"无弹窗=成功", 成功路径这里白白多等 4 秒.
    ERROR_KEYWORDS = ("警告", "错误", "失败", "关闭", "非交易", "禁止",
                      "无效", "不足", "超过", "不允许", "未成交", "拒绝")
    SUCCESS_KEYWORDS = ("已提交", "成功", "受理", "委托已")

    RESULT_WAIT_EARLY_OK = 1.5   # 此秒数内无结果框 → 委托成功 (不白等)
    RESULT_WAIT_TIMEOUT  = 2.0   # 兜底最大等待 (early_ok_after 之后也不再拉长)

    t1 = time.perf_counter()
    res = wait_dialog(app_el, has_cancel=False,
                      timeout=RESULT_WAIT_TIMEOUT,
                      early_ok_after=RESULT_WAIT_EARLY_OK)
    steps["result_wait_ms"] = round((time.perf_counter() - t1) * 1000, 1)
    result_text = None
    ok = True
    if res:
        btns2, texts2 = res
        result_text = " | ".join(texts2)
        # 弹出了结果框 → 按内容判断: 含失败词且无成功词 → ok=False
        rt_joined = result_text.replace(" ", "")
        if any(kw in rt_joined for kw in ERROR_KEYWORDS) and not any(
                kw in rt_joined for kw in SUCCESS_KEYWORDS):
            ok = False
        if "确认" in btns2:  # 关掉结果框, 留干净面板给下一次
            AXUIElementPerformAction(btns2["确认"], "AXPress")
    # else: 没有任何警告/错误弹窗 → 默认视为委托成功 (无弹窗=成功)

    # 7. 收尾: 额外对话框清理 (夜市委托提示/收盘挂单提示等)
    #    仅在"委托成功/状态不明/结果框无内容"时才有必要跑.
    #    若 ok=false 且已经有结果文本 (明确拒绝类失败), 就跳过, 避免白等 timeout.
    if not (ok is False and result_text):
        INFO_KEYWORDS = ("夜市委托", "收盘", "委托已提交", "委托已受理", "请稍后",
                         "次日", "下一交易日", "清算") + SUCCESS_KEYWORDS
        # 大部分成功场景不会弹额外提示框; 夜市/收盘提示若弹也会在 0.4s 内出现.
        # 用 early_ok 逻辑: 0.4s 内没扫到 INFO 类对话框就立即放行, 不再等满 1.2s.
        INFO_WAIT_EARLY_OK = 0.4  # 夜市/收盘提示弹窗会在 0.4s 内出现, 未出即判无弹窗

        def _check_info_dialog():
            for _, btns, texts in dialogs_with_buttons(app_el):
                if "确认" not in btns:
                    continue
                joined = "".join(texts).replace(" ", "")
                has_info = any(kw in joined for kw in INFO_KEYWORDS)
                has_err  = any(kw in joined for kw in ERROR_KEYWORDS)
                if has_info and not has_err:
                    return (btns, texts)
            return None

        t1 = time.perf_counter()
        # early_ok_after=0.4s → 0.4s 内没 INFO 类对话框冒出来就直接放行, 不白等
        extra = poll(_check_info_dialog, INFO_WAIT_EARLY_OK, interval=0.07)
        if extra is None:
            # 0.4s 内没出 → 判无额外提示, 立即返回 (绝大多数成功路径走这里)
            steps["extra_dialog_ms"] = round((time.perf_counter() - t1) * 1000, 1)
        else:
            # 0.4s 内命中了 → 点确认关掉, 并记录耗时
            btns3, texts3 = extra
            extra_text = " | ".join(texts3)
            result_text = (result_text + " | " + extra_text) if result_text else extra_text
            if "确认" in btns3:
                AXUIElementPerformAction(btns3["确认"], "AXPress")
            steps["extra_dialog_ms"] = round((time.perf_counter() - t1) * 1000, 1)
    else:
        steps["extra_dialog_ms"] = 0.0  # 明确失败时跳过, 不浪费时间

    # 券商明确拒绝类失败 Bark: 默认节流不开, 仅传 --bark-on-reject 时才发手机.
    # "状态未知"(点完提交没等到确认框)属于严重问题, 每次都强制通知, 不受此开关影响.
    if not ok and result_text and args.bark_on_reject:
        notify_bark(f"⚠️ {args.side}单失败 {args.code}",
                    f"{args.side} {args.code} x{args.qty} @{fill['price'] or fill.get('price')}\n"
                    f"{result_text}")

    print(json.dumps({"ok": ok, "side": args.side, "fill": fill,
                      "steps": steps, "result_text": result_text},
                     ensure_ascii=False))


if __name__ == "__main__":
    load_dotenv()
    main()

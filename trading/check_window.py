#!/usr/bin/env python3
"""检测同花顺主窗口是否可读. 退出码 0=OK, 1=找不到窗口, 2=异常."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from trading import ths_trade as t

try:
    app, pid, app_el = t.find_app(activate=False)
    print(f"PID: {pid}")
    win = t.main_window(app_el)
    f = t.get_frame(win)
    role = t.ax_get(win, "AXRole")
    kids = t.ax_get(win, "AXChildren")
    print(f"OK role={role} frame={f} children={len(kids) if kids else 0}")
    sys.exit(0)
except SystemExit as e:
    print(f"FAIL: {e}")
    sys.exit(1)
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")
    sys.exit(2)

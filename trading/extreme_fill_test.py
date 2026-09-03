#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同花顺填表极限测试: 遍历全部 A 股代码, 填表买入 100 股(不提交), 验证代码联动与价格带入.

设计要点:
  * 一次性初始化同花顺 App / 登录 / 切买入面板, 之后只循环 fill_code + 填数量
  * 全程 dry-run: 绝不点击"确定买入", 不触发任何委托确认框
  * 每只股票单独 try/except, 单步失败不中断整体测试
  * 周期性写 JSON 快照, 支持 --resume 断点续跑
  * 实时输出进度条 + ETA, 结束输出完整统计报告

用法:
  # 全量遍历(预计 ~1.5h / 5000 只, 可 Ctrl+C 随时中断后 --resume)
  uv run python scripts/extreme_fill_test.py

  # 先跑 50 只快速验证
  uv run python scripts/extreme_fill_test.py --limit 50

  # 断点续跑 (读上次快照继续)
  uv run python scripts/extreme_fill_test.py --resume

  # 切换模拟账户
  uv run python scripts/extreme_fill_test.py --account sim

  # 跳过同花顺登录检测
  uv run python scripts/extreme_fill_test.py --no-login

输出:
  scripts/results/extreme_fill_YYYYMMDD_HHMMSS.json
  ── 最后一次写入的快照, 可用 --resume 指定该文件续跑
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

import ths_trade as t
from ApplicationServices import AXUIElementPerformAction

# ──────────────────────────────────── 配置 ────────────────────────────────────

QTY = 100                           # 固定买 100 股
SNAPSHOT_EVERY = 50                 # 每 N 只写一次快照
RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


# ──────────────────────────────── 股票代码列表 ────────────────────────────────

def get_all_a_stock_codes() -> list[tuple[str, str]]:
    """获取全部 A 股代码列表. 返回 [(代码, 名称), ...] (代码为 6 位纯数字字符串).

    优先级:
      1) eltdx (通达信官方行情源, 稳定无封 IP)
      2) akshare.stock_info_a_code_name (东方财富, 备用)
    两者均失败则抛 SystemExit.
    """
    # 1. eltdx 通道
    try:
        from eltdx import TdxClient
        client = TdxClient(timeout=10)
        client.connect()
        rows = list(client.codes.latest_stock_list())
        if rows:
            # r.exchange = "sh"/"sz"; r.code 已经是 6 位数字; r.name = 股票名
            result = [(r.code, getattr(r, "name", "")) for r in rows]
            # 过滤: 只保留 6 位纯数字 (剔除北交所 8/4 开头 8 位、债券等)
            result = [(c, n) for c, n in result if len(c) == 6 and c.isdigit()]
            # 去重保序
            seen = set()
            uniq = []
            for c, n in result:
                if c not in seen:
                    seen.add(c)
                    uniq.append((c, n))
            if uniq:
                print(f"[数据源] eltdx: 拿到 {len(uniq)} 只 A 股代码", flush=True)
                return uniq
    except Exception as exc:
        print(f"[数据源] eltdx 失败: {exc!r}, 尝试 akshare…", flush=True)

    # 2. akshare 通道
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        if df is not None and len(df) > 0:
            # akshare 返回列: code, name
            pairs = list(zip(df["code"].astype(str).tolist(),
                             df["name"].astype(str).tolist()))
            pairs = [(c, n) for c, n in pairs if len(c) == 6 and c.isdigit()]
            if pairs:
                print(f"[数据源] akshare: 拿到 {len(pairs)} 只 A 股代码", flush=True)
                return pairs
    except Exception as exc:
        print(f"[数据源] akshare 失败: {exc!r}", flush=True)

    raise SystemExit("两个数据源都拿不到 A 股代码列表, 请检查网络或手动指定 --sample")


# ──────────────────────────────── 结果快照持久化 ────────────────────────────────

def _snapshot_path() -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return RESULTS_DIR / f"extreme_fill_{stamp}.json"


def save_snapshot(path: Path, results: list[dict], meta: dict) -> None:
    payload = {
        "saved_at": dt.datetime.now().isoformat(timespec="seconds"),
        "meta": meta,
        "count": len(results),
        "results": results,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_snapshot(path: Path) -> tuple[list[dict], dict]:
    with open(path, encoding="utf-8") as f:
        p = json.load(f)
    return p["results"], p.get("meta", {})


# ──────────────────────────────── 单只股票 dry-run 填表 ─────────────────────────

def dry_fill_one(app_el, code: str, qty: int,
                 retry_link: int = 1) -> tuple[bool, str, dict, float]:
    """对单只代码执行: 切买入 → fill_code 等联动 → 填数量 → 读面板值.

    绝不点击"确定买入". 返回 (ok, 错误描述或"ok", fill 字典, 总耗时 ms).
    fill 字典 = {code, price, qty, code_raw, price_raw, qty_raw}  (raw = 读 AX 原值).
    """
    t0 = time.perf_counter()
    try:
        win = t.main_window(app_el)
    except Exception as exc:
        return False, f"main_window 异常: {exc!r}", {}, (time.perf_counter() - t0) * 1000

    # 清残留弹窗 (上一只失败可能留警告框, 挡住输入框)
    try:
        t._close_all_dialogs(app_el)
    except Exception:
        pass

    # 切买入 tab (同花顺会记住上次面板, 但我们确保在买入)
    try:
        tab = t.find_button(win, "买入")
        if tab is not None:
            AXUIElementPerformAction(tab, "AXPress")
            time.sleep(0.3)
            win = t.main_window(app_el)
    except Exception:
        pass

    # 扫描三个输入框 (代码 / 价格 / 数量)
    try:
        code_f, price_f, qty_f = t.scan_fields(win)
    except SystemExit as exc:
        return False, f"scan_fields 失败: {exc}", {}, (time.perf_counter() - t0) * 1000
    except Exception as exc:
        return False, f"scan_fields 异常: {exc!r}", {}, (time.perf_counter() - t0) * 1000

    # fill_code 联动 (核心步骤). 失败重试一次
    linked = False
    link_attempts = 0
    for attempt in range(1 + retry_link):
        link_attempts = attempt + 1
        try:
            linked = t.fill_code(code_f, price_f, code)
        except Exception as exc:
            linked = False
            last_err = f"fill_code 抛出 {type(exc).__name__}: {exc}"
        if linked:
            break
        time.sleep(0.3)
        # 重试前重新扫描面板 (元素可能已失效)
        try:
            win = t.main_window(app_el)
            code_f, price_f, qty_f = t.scan_fields(win)
        except Exception:
            pass

    if not linked:
        msg = locals().get("last_err", "联动超时(价格未跳出)/市场代码为空")
        return (False, f"代码联动失败({link_attempts}次): {msg}",
                {}, (time.perf_counter() - t0) * 1000)

    # 联动成功 → 等 0.5s 让 UI 完全写入对手价和市场代码 (按 ths_trade.py 规范)
    time.sleep(0.5)

    # 填数量 (不覆盖价格, 价格是联动出来的对手价)
    try:
        t.set_text(qty_f, str(qty))
        time.sleep(0.2)
    except Exception as exc:
        return (False, f"填数量抛出: {exc!r}",
                {}, (time.perf_counter() - t0) * 1000)

    # 读面板当前值
    code_raw = t.field_value(code_f)
    price_raw = t.field_value(price_f)
    qty_raw = t.field_value(qty_f)

    # ── 验证逻辑 ──
    # 1) 代码框必须是目标代码 (同花顺有时会把旧代码残留部分数字)
    #    接受"包含"判断: 例如填 601899, 面板可能显示为 "601899" 或含空白
    issues = []
    if code not in (code_raw or "").replace(" ", ""):
        issues.append(f"代码不匹配: 期望{code}, 实际{code_raw!r}")

    # 2) 价格必须是合法正数 (联动失败时可能为空 / "--" / "0.00" / 文本)
    price_val: float | None = None
    if price_raw and price_raw.strip():
        s = price_raw.strip().replace(",", "")
        try:
            price_val = float(s)
            if price_val <= 0:
                issues.append(f"价格≤0: {price_raw!r}")
        except ValueError:
            issues.append(f"价格非数字: {price_raw!r}")
    else:
        issues.append("价格为空")

    # 3) 数量必须等于 QTY (容忍同花顺自动进位/舍入造成差 1 股的极端情况,
    #    但我们 QTY=100 是一手整数, 严格判断即可)
    qty_val: float | None = None
    if qty_raw and qty_raw.strip():
        try:
            qty_val = int(float(qty_raw.strip().replace(",", "")))
        except ValueError:
            issues.append(f"数量非数字: {qty_raw!r}")
    else:
        issues.append("数量为空")
    if qty_val is not None and qty_val != qty:
        issues.append(f"数量不匹配: 期望{qty}, 实际{qty_val}")

    fill = {
        "code": code,
        "code_raw": code_raw,
        "price": price_val,
        "price_raw": price_raw,
        "qty": qty_val,
        "qty_raw": qty_raw,
    }

    if issues:
        return False, "; ".join(issues), fill, (time.perf_counter() - t0) * 1000
    return True, "ok", fill, (time.perf_counter() - t0) * 1000


# ──────────────────────────────── 进度条 ────────────────────────────────

class Progress:
    def __init__(self, total: int, start_idx: int = 0,
                 bar_width: int = 28) -> None:
        self.total = total
        self.idx = start_idx
        self.t0 = time.perf_counter()
        self.bar_w = bar_width
        # 记录开始时已完成数, 用于估算整体速度
        self.done_at_start = start_idx

    def step(self, extra: str = "") -> None:
        self.idx += 1
        n = self.idx
        done = n - self.done_at_start
        elapsed = time.perf_counter() - self.t0
        rate = done / elapsed if elapsed > 0 else 0
        remain = self.total - n
        eta_s = remain / rate if rate > 0 else 0
        pct = n / self.total if self.total else 1.0
        filled = int(pct * self.bar_w)
        bar = "█" * filled + "░" * (self.bar_w - filled)
        eta = dt.timedelta(seconds=int(eta_s))
        sys.stderr.write(
            f"\r  [{bar}] {n}/{self.total} {pct*100:5.1f}%  "
            f"{rate:.2f}股/s  ETA {eta}  {extra[:36]:<36}"
        )
        sys.stderr.flush()

    def close(self) -> None:
        sys.stderr.write("\n")
        sys.stderr.flush()


# ──────────────────────────────── 主流程 ────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="同花顺 dry-run 极限测试: 全 A 股填表验证代码联动与价格带入 (绝不委托)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--limit", type=int, default=None,
                    help="只测前 N 只 (用于快速验证, 默认全量)")
    ap.add_argument("--offset", type=int, default=0,
                    help="从第几只开始 (0-based, 不用 resume 时手工断点)")
    ap.add_argument("--resume", type=str, default=None, nargs="?", const="__AUTO__",
                    help="从快照文件续跑. 不带参数时自动加载最新的快照文件.")
    ap.add_argument("--account", choices=["A股", "real", "模拟", "sim", "mock"],
                    default=None, help="先切换账户再测, 默认用同花顺当前账户")
    ap.add_argument("--keyboard", action="store_true",
                    help="(保留) 代码输入走键盘. 本脚本是 dry-run 批量测试, 不推荐开启")
    ap.add_argument("--no-login", action="store_true",
                    help="跳过自动登录检测 (默认自动调 THS_USER/THS_PASS 登录)")
    ap.add_argument("--retry-link", type=int, default=1,
                    help="联动失败自动重试次数 (不含首次)")
    ap.add_argument("--sample", type=str, default=None,
                    help="跳过网络拿代码, 用这个逗号分隔的代码列表快速自测. 例: 601899,513120,000001")
    ap.add_argument("--report-failures", type=int, default=30,
                    help="报告末尾打印前 N 条失败样例详情")
    args = ap.parse_args()

    # ── 1. 准备股票代码 ──
    if args.sample:
        codes = [(c.strip(), "") for c in args.sample.split(",") if c.strip()]
        print(f"[数据源] 使用 --sample: {len(codes)} 只", flush=True)
    else:
        codes = get_all_a_stock_codes()

    if args.limit:
        codes = codes[:args.limit]
    total_count = len(codes)
    print(f"[计划] 共 {total_count} 只待测试, offset={args.offset}", flush=True)

    # ── 2. 加载续跑快照 ──
    snapshot_path: Path = _snapshot_path()
    done_map: dict[str, dict] = {}   # code -> 已有的 result dict (跳过)
    meta_base: dict = {}
    results: list[dict] = []

    if args.resume:
        if args.resume == "__AUTO__":
            # 找修改时间最新的快照
            snaps = sorted(RESULTS_DIR.glob("extreme_fill_*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            if not snaps:
                raise SystemExit("--resume 不带参数但找不到任何历史快照")
            snap_file = snaps[0]
        else:
            snap_file = Path(args.resume)
            if not snap_file.exists():
                raise SystemExit(f"--resume 文件不存在: {snap_file}")
        loaded, meta_base = load_snapshot(snap_file)
        for r in loaded:
            done_map[r["code"]] = r
        results = loaded
        print(f"[续跑] 加载 {snap_file.name}: 已完成 {len(done_map)} 只, 继续处理剩余",
              flush=True)
        # 续跑时快照文件沿用上次的 (不要在续跑时改快照文件名, 否则断点断链)
        snapshot_path = snap_file

    # offset 切片 (续跑 + offset 同时用时, offset 在剩余里再切)
    remaining = [(c, n) for c, n in codes if c not in done_map]
    if args.offset:
        remaining = remaining[args.offset:]
    print(f"[计划] 实际待跑 {len(remaining)} 只", flush=True)

    # ── 3. 初始化同花顺 / 登录 / 切账户 ──
    print("[环境] 连接同花顺…", flush=True)
    app, pid, app_el = t.find_app(activate=False)
    print(f"[环境] 同花顺 PID={pid}", flush=True)
    t.load_dotenv()

    if not args.no_login:
        msg = t.ensure_login(app_el)
        if msg:
            print(f"[环境] ⚠️  登录问题: {msg}. 仍尝试继续, 但联动价格可能为空.",
                  flush=True)
        else:
            print("[环境] 登录 ok", flush=True)

    if args.account:
        tab_name = t.ACCOUNT_NAMES.get(args.account.lower(), args.account)
        print(f"[环境] 切换账户 → {tab_name}", flush=True)
        t.switch_account(app_el, tab_name)

    # 确保买入面板可见 (首次)
    win = t.main_window(app_el)
    tab = t.find_button(win, "买入")
    if tab is not None:
        AXUIElementPerformAction(tab, "AXPress")
        time.sleep(0.5)
    t._close_all_dialogs(app_el)
    print("[环境] 初始化完成, 开始填表测试", flush=True)

    # ── 4. 循环遍历 ──
    meta = {
        "qty": QTY,
        "retry_link": args.retry_link,
        "account": args.account,
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
        **meta_base,   # 保留续跑的元信息
    }
    prog = Progress(total_count, start_idx=len(done_map))
    fail_counter: dict[str, int] = {}   # 失败原因归类 → 计数

    last_save_time = time.time()

    try:
        for pos, (code, name) in enumerate(remaining):
            try:
                ok, msg, fill, elapsed = dry_fill_one(
                    app_el, code, QTY, retry_link=args.retry_link,
                )
            except Exception as exc:
                ok, msg, fill, elapsed = (
                    False, f"未捕获异常: {type(exc).__name__}: {exc}", {}, 0.0,
                )

            if not ok:
                # 归大类 (取第一个分号前的错误开头, 太细不好统计)
                head = msg.split(":", 1)[0][:40]
                fail_counter[head] = fail_counter.get(head, 0) + 1

            result = {
                "idx": len(results),
                "code": code,
                "name": name or "",
                "ok": ok,
                "msg": msg,
                "elapsed_ms": round(elapsed, 1),
                "fill": fill,
                "ts": dt.datetime.now().isoformat(timespec="seconds"),
            }
            results.append(result)
            done_map[code] = result

            # 实时进度提示 (前几只/异常 会把失败信息高亮到 extra 上)
            extra = "" if ok else f"✗ {code} {msg[:28]}"
            if not extra and pos < 5:
                extra = f"✓ {code} P={fill.get('price_raw')}"
            prog.step(extra=extra)

            # 周期性写快照 + 失败样本打印
            n_done = len(results)
            if n_done % SNAPSHOT_EVERY == 0:
                save_snapshot(snapshot_path, results, meta)
                last_save_time = time.time()
                # 每 50 只也 stderr 打一行失败统计
                if fail_counter:
                    sys.stderr.write("\n")
                    sys.stderr.write(
                        "  ── 失败分类 Top5: "
                        + ", ".join(f"{k}×{v}" for k, v in
                                    sorted(fail_counter.items(),
                                           key=lambda x: -x[1])[:5])
                        + "\n"
                    )
                    sys.stderr.flush()

    except KeyboardInterrupt:
        prog.close()
        print("\n[中断] Ctrl+C, 保存当前快照…", flush=True)
        save_snapshot(snapshot_path, results, meta)
        # 中断仍走报告
    else:
        prog.close()

    # ── 5. 完整保存 + 报告 ──
    meta["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
    meta["duration_s"] = round(
        time.perf_counter() - (prog.t0 - (prog.done_at_start / max(prog.idx - prog.done_at_start, 1) * (time.perf_counter() - prog.t0))),
        1,
    ) if prog.idx > prog.done_at_start else 0
    # 上面 duration 计算有点绕, 简单算总耗时: 用结果里首尾时间差 或 直接用测试时间
    save_snapshot(snapshot_path, results, meta)
    print(f"[保存] 完整结果 → {snapshot_path}", flush=True)

    _print_report(results, fail_counter, topn=args.report_failures)


def _print_report(results: list[dict], fail_counter: dict[str, int],
                  topn: int) -> None:
    total = len(results)
    succ = sum(1 for r in results if r["ok"])
    fail = total - succ
    pct = succ / total * 100 if total else 0.0

    # 耗时统计
    times = [r["elapsed_ms"] for r in results if r["elapsed_ms"]]
    avg_ms = round(sum(times) / len(times), 1) if times else 0.0
    max_ms = round(max(times), 1) if times else 0.0
    min_ms = round(min(times), 1) if times else 0.0

    # 价格分布 (仅成功样本)
    prices = [r["fill"]["price"] for r in results
              if r["ok"] and r["fill"].get("price") is not None]
    price_stats = ""
    if prices:
        price_stats = (f"  成功样例价格范围: min={min(prices):.3f}  "
                       f"max={max(prices):.3f}  样本数={len(prices)}")

    sep = "═" * 68
    print()
    print(sep)
    print("  同花顺填表极限测试报告")
    print(sep)
    print(f"  总样本数     : {total}")
    print(f"  成功 / 失败  : {succ} / {fail}   成功率 {pct:.1f}%")
    print(f"  单股平均耗时 : {avg_ms} ms  (最快 {min_ms} / 最慢 {max_ms})")
    if price_stats:
        print(price_stats)
    print(sep)

    if fail_counter:
        print("  失败原因分类:")
        for k, v in sorted(fail_counter.items(), key=lambda x: -x[1]):
            print(f"    × {v:>5}  {k}")
        print(sep)

    if fail:
        failures = [r for r in results if not r["ok"]]
        print(f"  失败样例 (前 {min(topn, len(failures))} 条):")
        print(f"  {'序号':>5}  {'代码':<7} {'名称':<10}  {'耗时ms':>7}  详情")
        for r in failures[:topn]:
            print(f"  {r['idx']:>5}  {r['code']:<7} {(r['name'] or '')[:10]:<10}  "
                  f"{r['elapsed_ms']:>7.0f}  {r['msg']}")
        print(sep)

    # 价格异常的成功案例 (理论上不应该有, 但找 0 价格 / 超大价格)
    weird = [r for r in results if r["ok"] and r["fill"].get("price") is not None
             and (r["fill"]["price"] < 0.01 or r["fill"]["price"] > 100_000)]
    if weird:
        print(f"  ⚠️  价格可疑的成功样本 ({len(weird)} 条, 前 5):")
        for r in weird[:5]:
            f = r["fill"]
            print(f"    {r['code']} {r['name']:<10}  price={f['price_raw']!r}  "
                  f"parsed={f['price']}")
        print(sep)

    # 代码不匹配的 (严重)
    code_mismatch = [r for r in results
                     if r.get("fill", {}).get("code_raw")
                     and r["code"] not in str(r["fill"]["code_raw"]).replace(" ", "")]
    if code_mismatch:
        print(f"  ⚠️  代码框与目标不一致的样本 ({len(code_mismatch)} 条, 前 10):")
        for r in code_mismatch[:10]:
            f = r["fill"]
            print(f"    期望={r['code']}  实际={f['code_raw']!r}  ok={r['ok']}")
        print(sep)

    print("  报告结束")
    print(sep)


if __name__ == "__main__":
    main()

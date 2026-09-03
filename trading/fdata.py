#!/usr/bin/env python3
"""fdata — 统一金融数据命令行接口.

首选 `quote` 子命令: 按代码自动路由数据源, 以统一 JSON 格式返回实时价格
(股票/ETF/指数/期货/期权同一结构, 见 quote 子命令帮助).

数据源选型 (2026-08 实测结论, 详见 datasource_test/REPORT.md):
- 股票/ETF/指数 快照+K线+列表: eltdx (通达信7709协议, 稳定不限频)
- 商品/金融期货 实时: tqsdk (快期websocket, 免费账户含五档)
- 期货全合约列表: akshare (新浪 futures_zh_realtime)
- 商品期权: tqsdk (query_options + 平值附近合约快照)
- ETF期权实时: 新浪 CON_OP_ 批量直连
- ETF期权合约列表/Greeks/IV/全球快讯/中证权重: akshare (低频调用, 无封禁风险)

所有输出均为 JSON (stdout), 日志/警告走 stderr。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import time
import warnings

warnings.filterwarnings("ignore")


def _patch_mini_racer() -> None:
    """akshare 的部分新浪接口依赖 py_mini_racer, 其 0.6.0 在 macOS arm64 二进制已损坏
    (import 成功但首次 eval 时 dlsym 失败); 探测后用维护中的 mini-racer 垫兼容模块."""
    try:
        import py_mini_racer

        py_mini_racer.MiniRacer().eval("1+1")
        return  # 正常则不动
    except Exception:  # noqa: BLE001
        pass
    from mini_racer import MiniRacer

    import types

    mod = types.ModuleType("py_mini_racer")
    mod.MiniRacer = MiniRacer
    mod.py_mini_racer = mod
    sys.modules["py_mini_racer"] = mod
    sys.modules["py_mini_racer.py_mini_racer"] = mod


_patch_mini_racer()

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REAL_STDOUT = sys.stdout  # tqsdk 会向 stdout 打印横幅/日志, JSON 始终走真实 stdout

# 名称映射表磁盘缓存 (约8800条, 拉取需3-4s; 名称极少变化, 7天刷新一次足够)
_NAMES_CACHE = os.path.expanduser("~/.cache/fdata/names.json")
_NAMES_TTL = 7 * 86400


def out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str), file=_REAL_STDOUT)


def die(msg: str) -> None:
    print(json.dumps({"error": msg}, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)


def _nan(x):
    """nan -> None, 保证 JSON 干净."""
    return None if x != x else x


# ================================================================ 统一格式 ====
# 统一实时价格结构 (所有品种一致):
# {
#   "code": "sh600519",            # 规范化完整代码
#   "type": "stock",               # stock|etf|index|futures|option
#   "name": "贵州茅台",            # 证券/合约名称
#   "exchange": "sh",              # eltdx: sh/sz; tqsdk: SHFE/DCE/...; 新浪期权: sse
#   "option": null,                # 仅期权非 null: {underlying, strike, class, expire}
#   "source": "eltdx",
#   "volume_unit": "手",           # eltdx/tqsdk: 手; ETF期权: 张
#   "quote": {
#     "time": "2026-08-28 15:00:00",
#     "last": 1708.0, "pre_close": 1712.0,
#     "change": -4.0, "change_pct": -0.234,   # 均为相对昨收 pre_close
#     "open": 1712.0, "high": 1721.0, "low": 1701.0,
#     "upper_limit": 1883.2, "lower_limit": 1540.8,
#     "volume": 21534, "amount": 3664580000.0,
#     "open_interest": null,       # 仅期货/期权有
#     "pre_settle": null,          # 仅期货/期权有 (涨跌停/交易所涨跌幅基于它)
#     "bids": [[1708.0, 12], ...], # 买一..买五 [价格, 量], 按价格降序, 量纲见 volume_unit
#     "asks": [[1709.0, 5], ...]   # 卖一..卖五, 按价格升序; 指数无五档为 []
#   }
# }


def _unified(code, dtype, name, exchange, source, volume_unit, quote, option=None, fund=None):
    return {
        "code": code,
        "type": dtype,
        "name": name,
        "exchange": exchange,
        "option": option,
        "fund": fund,
        "source": source,
        "volume_unit": volume_unit,
        "quote": quote,
    }


def _levels_desc(levels, ref):
    """[(price, volume)] -> 有效买档降序 (过滤指数等量纲异常值)."""
    ok = [(lv.price, lv.volume) for lv in levels if lv.price and 0 < lv.price < ref * 3]
    return sorted(ok, key=lambda t: -t[0])


def _levels_asc(levels, ref):
    ok = [(lv.price, lv.volume) for lv in levels if lv.price and 0 < lv.price < ref * 3]
    return sorted(ok, key=lambda t: t[0])


# ==================================================================== eltdx ====

def _norm_code(code: str) -> str:
    """自动补交易所前缀: 60/68→sh, 0/3/1(2,5,6,8)→sz.

    注意 6 位纯数字无法区分 sh000001(上证指数) 与 000001(平安银行),
    指数请显式写前缀 sh000001 / sz399001。
    """
    if len(code) == 6 and code.isdigit():
        if code.startswith(("60", "68")):
            return "sh" + code
        if code.startswith(("0", "3", "12", "15", "16", "18")):
            return "sz" + code
    return code.lower()


def _tdx_type(exch: str, code: str) -> str:
    if (exch == "sh" and code.startswith("000")) or (exch == "sz" and code.startswith("399")):
        return "index"
    if code.startswith(("51", "56", "58", "11", "12", "15", "16", "18")):
        return "etf"
    return "stock"


def _tdx():
    from eltdx import TdxClient

    client = TdxClient(timeout=5)
    client.connect()
    return client


def _tdx_tables(client) -> dict:
    """三类证券表 {stocks/etfs/indices: {sh600519: 名称}}, 磁盘缓存 TTL 7天."""

    def _fetch() -> dict:
        tables = {}
        try:
            tables["stocks"] = {f"{r.exchange}{r.code}": r.name for r in client.codes.latest_stock_list()}
        except Exception:  # noqa: BLE001
            tables["stocks"] = {}
        tables["etfs"] = {}
        tables["indices"] = {}
        for mk in ("sh", "sz"):
            for getter, key in (("etfs", "etfs"), ("indices", "indices")):
                try:
                    for r in getattr(client.codes, getter)(mk):
                        tables[key][f"{r.exchange}{r.code}"] = r.name
                except Exception:  # noqa: BLE001
                    pass
        return tables

    # 缓存新鲜 -> 直接用
    try:
        with open(_NAMES_CACHE, encoding="utf-8") as f:
            cached = json.load(f)
        if time.time() - cached.get("updated", 0) < _NAMES_TTL:
            return cached["tables"]
    except Exception:  # noqa: BLE001
        cached = None

    tables = _fetch()
    if any(tables.values()):  # 拉取成功才写缓存
        try:
            os.makedirs(os.path.dirname(_NAMES_CACHE), exist_ok=True)
            with open(_NAMES_CACHE, "w", encoding="utf-8") as f:
                json.dump({"updated": time.time(), "tables": tables}, f, ensure_ascii=False)
        except OSError:
            pass
        return tables
    if cached:  # 拉取失败退回旧缓存
        return cached["tables"]
    return {"stocks": {}, "etfs": {}, "indices": {}}


def _tdx_names(client) -> dict:
    """sh600519 -> 名称 的映射, 由三类证券表合并."""
    tables = _tdx_tables(client)
    return {**tables["stocks"], **tables["etfs"], **tables["indices"]}


def _eltdx_rows(codes, client) -> list:
    """codes: 已带前缀的代码列表 -> 统一结构列表."""
    names = _tdx_names(client)
    snaps = client.quotes.get_snapshots(codes)
    rows = []
    for q in snaps:
        exch, body = q.exchange, q.code
        full = f"{exch}{body}"
        name = names.get(full)
        change = q.last_price - q.pre_close_price if (q.last_price and q.pre_close_price) else None
        quote = {
            "time": None,  # eltdx 快照不提供行情时间戳
            "last": q.last_price,
            "pre_close": q.pre_close_price,
            "change": change,
            "change_pct": round(change / q.pre_close_price * 100, 3)
            if change is not None and q.pre_close_price
            else None,
            "open": q.open_price,
            "high": q.high_price,
            "low": q.low_price,
            "upper_limit": None,
            "lower_limit": None,
            "volume": q.total_hand,
            "amount": q.amount,
            "open_interest": None,
            "pre_settle": None,
            "bids": _levels_desc(q.buy_levels, q.last_price or 1),
            "asks": _levels_asc(q.sell_levels, q.last_price or 1),
        }
        rows.append(_unified(full, _tdx_type(exch, body), name, exch, "eltdx", "手", quote))
    return rows


def cmd_snapshot(args) -> None:
    codes = [_norm_code(c) for c in args.codes]
    client = _tdx()
    try:
        rows = _eltdx_rows(codes, client)
        out({"source": "eltdx", "count": len(rows), "data": rows})
    finally:
        client.close()


def _kline_bars(client, code: str, period: str, kind: str,
                adjust=None, limit=None) -> list:
    """从 eltdx client 取 K 线 bars (统一结构, 升序). CLI 与 serve 共用."""
    kwargs = {"period": period, "kind": kind}
    if adjust not in (None, "none"):
        kwargs["adjust"] = adjust
    if limit:
        kwargs["count"] = limit
    else:
        kwargs["all_pages"] = True
    series = client.bars.get(code, **kwargs)
    bars = [
        {
            "date": b.time.isoformat(sep=" ", timespec="seconds"),
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume_lots,
            "amount": b.amount,
        }
        for b in series.bars
    ]
    bars.sort(key=lambda r: r["date"])  # eltdx all_pages 为最新在前, 统一升序输出
    return bars


def cmd_kline(args) -> None:
    code = _norm_code(args.code)
    kind = args.kind
    if kind == "auto":  # 自动判别: 期货(au/rb/IF等)优先, 再指数, 再股票
        if _resolve_fut(code):
            kind = "futures"
        elif code[:4] in ("sh00", "sz39"):
            kind = "index"
        else:
            kind = "stock"
    # 期货走 tqsdk kline_serial (eltdx 不覆盖期货, 会报 invalid code)
    if kind == "futures":
        r = _resolve_fut(code)
        if not r:
            out({"source": "tqsdk", "code": code, "kind": "futures",
                 "count": 0, "data": []})
            return
        dur = _PERIOD_DUR.get(args.period, 86400)
        bars = _tq_kline_rows(r[2], dur, args.limit)
        out({"source": "tqsdk", "code": code, "kind": "futures",
             "count": len(bars), "data": bars})
        return
    client = _tdx()
    try:
        bars = _kline_bars(client, code, args.period, kind,
                           args.adjust, args.limit)
        out({"source": "eltdx", "code": code, "kind": kind,
             "count": len(bars), "data": bars})
    finally:
        client.close()


def cmd_serve(args) -> None:
    """长连接数据服务器: 常驻单个 eltdx client, 复用连接避免每次初始化开销.

    协议: 标准 TCP, 每行一个 JSON 请求 -> 每行一个 JSON 响应 (和 CLI 同构数据).
      请求: {"op": "quote", "code": "sh601899"}
            {"op": "kline", "code": "sh601899", "period": "1m",
             "kind": "stock", "adjust": null, "limit": 640}
      响应: {"ok": true, "result": ...}  或  {"ok": false, "error": "..."}
    并发安全: 用全局锁串行化对 eltdx client 的访问; 断线自动重连.

    覆盖全数据源: 除内建 quote/kline (eltdx 长连接) 外,
    任意子命令通过 {\"op\": \"cli\", \"argv\": [子命令, ...], \"timeout\": n}
    透传 fdata 自身 CLI 获取 (期货/基金/期权/新闻等), 数据与命令行完全一致.
    """
    import asyncio  # noqa: PLC0415
    import threading  # noqa: PLC0415
    from eltdx.exceptions import ConnectionClosedError  # noqa: PLC0415

    state = {"client": None, "io_lock": threading.Lock()}

    def _get_client():
        c = state["client"]
        if c is None:
            c = _tdx()
            state["client"] = c
        return c

    def _reset_client():
        c = state["client"]
        state["client"] = None
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    def _handle(req) -> dict:
        op = req.get("op")
        if op == "cli":
            # 通用子命令: 透传 fdata 自身 CLI, 与命令行数据完全一致 (期货/基金/期权/新闻等)
            argv = req.get("argv")
            if not isinstance(argv, list) or not argv:
                return {"ok": False, "error": "cli op needs argv: [subcommand, ...]"}
            try:
                r = subprocess.run(
                    [sys.executable, os.path.abspath(__file__),
                     *[str(a) for a in argv]],
                    capture_output=True, text=True,
                    timeout=int(req.get("timeout") or 180))
            except subprocess.TimeoutExpired as e:
                return {"ok": False, "error": f"cli {argv[0]} timeout: {e}"}
            if r.returncode != 0:
                return {"ok": False, "error": f"cli {argv[0]} failed: "
                        f"{r.stderr.strip()[:400]}"}
            try:
                return {"ok": True, "sub": argv[0], "result": json.loads(r.stdout)}
            except json.JSONDecodeError:
                return {"ok": False, "error": f"cli {argv[0]} bad output: "
                        f"{r.stdout[:300]}"}
        if op not in ("quote", "kline"):
            return {"ok": False, "error": f"unknown op: {op!r}"}
        with state["io_lock"]:  # 串行化 eltdx client 访问
            try:
                if op == "quote":
                    code = _norm_code(str(req.get("code", "")))
                    rows = _eltdx_rows([code], _get_client())
                    if not rows:
                        return {"ok": False, "error": "no quote data"}
                    return {"ok": True, "result": rows[0]}
                # kline
                code = _norm_code(str(req.get("code", "")))
                kind = req.get("kind") or "stock"
                if kind == "auto":
                    kind = "index" if code[:4] in ("sh00", "sz39") else "stock"
                bars = _kline_bars(_get_client(), code,
                                   req.get("period") or "day", kind,
                                   req.get("adjust"), req.get("limit"))
                return {"ok": True, "result": {"code": code, "kind": kind,
                                               "count": len(bars), "data": bars}}
            except ConnectionClosedError:
                _reset_client()  # 断线: 重置连接, 下次请求重连
                return {"ok": False, "error": "connection reset, will retry"}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def _svc(reader, writer):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                req = json.loads(line.decode("utf-8", "replace"))
                resp = await asyncio.to_thread(_handle, req)
                writer.write(json.dumps(resp, ensure_ascii=False).encode() + b"\n")
                await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _main():
        server = await asyncio.start_server(_svc, args.host, args.port)
        addrs = ", ".join(f"{a[0]}:{a[1]}" for a in
                          (s.getsockname() for s in server.sockets))
        print(f'fdata serve now listening on {addrs} '
              f'({"raw TCP line-JSON" if not args.ws else "WebSocket"})',
              file=sys.stderr)
        async with server:
            await server.serve_forever()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("fdata serve stopped", file=sys.stderr)


def cmd_list(args) -> None:
    client = _tdx()
    try:
        rows = _tdx_tables(client)[args.category]
        result = [
            {"code": code, "name": name}
            for code, name in rows.items()
            if not args.filter or args.filter.lower() in f"{code} {name}".lower()
        ]
        out({"source": "eltdx", "count": len(result), "data": result[: args.limit] if args.limit else result})
    finally:
        client.close()


# ==================================================================== tqsdk ====

_FUT_ALIAS = {
    # 上海期交所
    "cu": "KQ.m@SHFE.cu", "al": "KQ.m@SHFE.al", "zn": "KQ.m@SHFE.zn", "pb": "KQ.m@SHFE.pb",
    "ni": "KQ.m@SHFE.ni", "sn": "KQ.m@SHFE.sn", "au": "KQ.m@SHFE.au", "ag": "KQ.m@SHFE.ag",
    "rb": "KQ.m@SHFE.rb", "hc": "KQ.m@SHFE.hc", "ss": "KQ.m@SHFE.ss", "ao": "KQ.m@SHFE.ao",
    "fu": "KQ.m@SHFE.fu", "bu": "KQ.m@SHFE.bu", "ru": "KQ.m@SHFE.ru", "sp": "KQ.m@SHFE.sp",
    # 上期能源
    "sc": "KQ.m@INE.sc", "lu": "KQ.m@INE.lu", "nr": "KQ.m@INE.nr", "bc": "KQ.m@INE.bc",
    # 大商所
    "m": "KQ.m@DCE.m", "y": "KQ.m@DCE.y", "a": "KQ.m@DCE.a", "b": "KQ.m@DCE.b",
    "c": "KQ.m@DCE.c", "cs": "KQ.m@DCE.cs", "p": "KQ.m@DCE.p", "jd": "KQ.m@DCE.jd",
    "l": "KQ.m@DCE.l", "v": "KQ.m@DCE.v", "pp": "KQ.m@DCE.pp", "eg": "KQ.m@DCE.eg",
    "eb": "KQ.m@DCE.eb", "pg": "KQ.m@DCE.pg", "lh": "KQ.m@DCE.lh", "jm": "KQ.m@DCE.jm",
    "j": "KQ.m@DCE.j", "i": "KQ.m@DCE.i", "rr": "KQ.m@DCE.rr",
    # 郑商所
    "sr": "KQ.m@CZCE.SR", "ta": "KQ.m@CZCE.TA", "ma": "KQ.m@CZCE.MA", "cf": "KQ.m@CZCE.CF",
    "fg": "KQ.m@CZCE.FG", "sa": "KQ.m@CZCE.SA", "ur": "KQ.m@CZCE.UR", "pf": "KQ.m@CZCE.PF",
    "pk": "KQ.m@CZCE.PK", "ap": "KQ.m@CZCE.AP", "cj": "KQ.m@CZCE.CJ", "rm": "KQ.m@CZCE.RM",
    "oi": "KQ.m@CZCE.OI", "wh": "KQ.m@CZCE.WH",
    # 中金所
    "if": "KQ.m@CFFEX.IF", "ih": "KQ.m@CFFEX.IH", "ic": "KQ.m@CFFEX.IC", "im": "KQ.m@CFFEX.IM",
    "t": "KQ.m@CFFEX.T", "tf": "KQ.m@CFFEX.TF", "ts": "KQ.m@CFFEX.TS", "tl": "KQ.m@CFFEX.TL",
    # 广期所
    "si": "KQ.m@GFEX.si", "lc": "KQ.m@GFEX.lc",
}

# 期权合约尾部形态: SHFE.rb2610C3100 / CFFEX.IO2612C4500 / DCE.m2609-C-3100
_OPT_PATTERNS = (
    re.compile(r"^[A-Za-z]+\d+(?:[CP])\d+(?:\.\d+)?$"),
    re.compile(r"^[A-Za-z0-9]+-[CP]-\d+$"),
)


def _is_option_code(code: str) -> bool:
    return any(p.match(code) for p in _OPT_PATTERNS)


# 品种小写 -> (交易所, 交易所口径的品种代码), 由 _FUT_ALIAS 派生
_FUT_EXCH = {}
for _v in _FUT_ALIAS.values():
    _exch, _prod = _v.split("@")[1].split(".")
    _FUT_EXCH[_prod.lower()] = (_exch, _prod)


def _resolve_fut(code: str):
    """短期货代码 -> (交易所, sina nf_ 符号, tqsdk 符号).

    IF -> CFFEX IF0;  IF2612 -> CFFEX.IF2612;  rb2610 -> SHFE.rb2610
    """
    m = re.fullmatch(r"([A-Za-z]+)(\d{0,4})", code)
    if not m or m.group(1).lower() not in _FUT_EXCH:
        return None
    prod_raw, digits = m.group(1), m.group(2)
    exch, prod = _FUT_EXCH[prod_raw.lower()]
    # 新浪合约月份统一 4 位 (SR2609); 交易所习惯的 3 位写法 (SR609) 自动补 2
    nf_digits = "2" + digits if len(digits) == 3 else digits
    nf_sym = prod.upper() + (nf_digits or "0")  # sina: IF0/RB2610/SR2609
    tq_sym = f"{exch}.{prod}{digits}" if digits else f"KQ.m@{exch}.{prod}"
    return exch, nf_sym, tq_sym


def _tq_symbol(sym: str) -> str:
    """rb/IF/IF0/IF2612 -> tqsdk 完整代码; 已含交易所前缀的原样透传."""
    s = sym.strip()
    if "@" in s or "." in s:
        return s
    r = _resolve_fut(s)
    return r[2] if r else s


@contextlib.contextmanager
def _hush():
    """tqsdk 会向 stdout 打横幅/日志、TQSIM 账户信息, 全部静音 (异常仍向外抛)."""
    devnull = open(os.devnull, "w")
    so, se = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = devnull
    try:
        yield
    finally:
        sys.stdout, sys.stderr = so, se
        devnull.close()


def _tq_api():
    """tqsdk 客户端: 凭据从环境变量 TQ_USER/TQ_PASS 读取 (自动加载项目 .env)."""
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(REPO, ".env"))
    except ImportError:
        pass
    user = os.environ.get("TQ_USER")
    pwd = os.environ.get("TQ_PASS")
    if not user or not pwd:
        raise RuntimeError(
            "tqsdk 凭据缺失: 请在项目 .env 或环境变量中设置 TQ_USER / TQ_PASS (快期免费账户)"
        )
    from tqsdk import TqApi, TqAuth

    return TqApi(auth=TqAuth(user, pwd))


def _tq_levels(prices, vols, ref):
    out = []
    for p, v in zip(prices, vols):
        p = _nan(p)
        if p and 0 < p < ref * 3:
            out.append((p, v))
    return out


def _tq_rows(symbols, api, wait_s: float = 10) -> list:
    quotes = [api.get_quote(s) for s in symbols]
    api.wait_update(deadline=time.time() + wait_s)
    rows = []
    for q in quotes:
        last = _nan(q.last_price)
        ref = last or _nan(q.pre_close) or 1
        bids = _tq_levels(
            [q.bid_price1, q.bid_price2, q.bid_price3, q.bid_price4, q.bid_price5],
            [q.bid_volume1, q.bid_volume2, q.bid_volume3, q.bid_volume4, q.bid_volume5],
            ref,
        )
        asks = _tq_levels(
            [q.ask_price1, q.ask_price2, q.ask_price3, q.ask_price4, q.ask_price5],
            [q.ask_volume1, q.ask_volume2, q.ask_volume3, q.ask_volume4, q.ask_volume5],
            ref,
        )
        pre_close = _nan(q.pre_close)
        change = last - pre_close if (last is not None and pre_close) else None
        is_opt = _is_option_code(q.instrument_id)
        option = None
        if is_opt or q.strike_price == q.strike_price and q.strike_price:
            option = {
                "underlying": q.underlying_symbol or None,
                "strike": _nan(q.strike_price) if q.strike_price == q.strike_price else None,
                "class": q.option_class if q.option_class else None,
                "expire": None,
            }
        quote = {
            "time": q.datetime[:19] if q.datetime else None,
            "last": last,
            "pre_close": pre_close,
            "change": change,
            "change_pct": round(change / pre_close * 100, 3) if change is not None and pre_close else None,
            "open": _nan(q.open),
            "high": _nan(q.highest),
            "low": _nan(q.lowest),
            "upper_limit": _nan(q.upper_limit),
            "lower_limit": _nan(q.lower_limit),
            "volume": q.volume,
            "amount": _nan(q.amount),
            "open_interest": q.open_interest,
            "pre_settle": _nan(q.pre_settlement),
            "bids": bids,
            "asks": asks,
        }
        rows.append(
            _unified(
                q.instrument_id,
                "option" if is_opt else "futures",
                q.instrument_name,
                q.instrument_id.split("@")[-1].split(".")[0] if "@" in q.instrument_id else q.exchange_id,
                "tqsdk",
                "手",
                quote,
                option,
            )
        )
    return rows


# ---- 期货 K线 (tqsdk kline_serial, 凭据从 .env 读 TQ_USER/TQ_PASS) ----
# period 字符串 -> tqsdk 持续秒数 (day=86400, 5m=300, ...)
_PERIOD_DUR = {
    "day": 86400, "1d": 86400,
    "1w": 604800, "week": 604800,
    "60m": 3600, "1h": 3600,
    "30m": 1800, "15m": 900, "5m": 300, "1m": 60,
}


def _tq_kline_rows(tq_sym: str, dur_sec: int, limit: int | None) -> list:
    """tqsdk kline_serial -> 统一 bars (升序).

    日线 dur_sec=86400, 5m=300, 15m=900, 30m=1800, 60m=3600, 1w=604800.
    返回 [{date,open,high,low,close,volume,amount}] 与 eltdx kline 同构,
    date 格式 'YYYY-MM-DD HH:MM:SS+08:00' (eltdx 同口径).
    """
    import pandas as pd  # noqa: PLC0415
    from datetime import timezone, timedelta  # noqa: PLC0415
    tz = timezone(timedelta(hours=8))
    # tqsdk data_length 上限 8964; None/0 = 取满 (用上限值)
    n = min(max(int(limit), 1) if limit else 8964, 8964)
    with _hush():
        api = _tq_api()
        try:
            kl = api.get_kline_serial(tq_sym, dur_sec, data_length=n)
            api.wait_update(deadline=time.time() + 10)  # 超时返回False, 避免非主力合约无限阻塞
        finally:
            api.close()
    if kl is None or len(kl) == 0:
        return []  # 无数据 -> 降级路径抛 serve 原错, 不静默成功
    bars = []
    for r in kl.to_dict("records"):
        ts = r.get("datetime")
        # data_length 大于实际历史时, tqsdk 前面补 datetime=0 的空槽 (NaN OHLC),
        # 解析出来是 1970-01-01, 必须在源头丢弃, 否则 JSON 序列化 NaN 直接 500
        try:
            if not ts or float(ts) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        dt = pd.to_datetime(ts, unit="ns", utc=True, errors="coerce")
        if pd.isna(dt):
            continue  # 跳过未完成/无效 bar
        dt = dt.tz_convert(tz)
        bars.append({
            "date": dt.isoformat(sep=" ", timespec="seconds"),
            "open": float(r.get("open") or 0),
            "high": float(r.get("high") or 0),
            "low": float(r.get("low") or 0),
            "close": float(r.get("close") or 0),
            "volume": float(r.get("volume") or 0),
            "amount": float(r.get("amount") or 0) or 0.0,
        })
    bars.sort(key=lambda x: x["date"])
    return bars


# ---- 期货实时 (新浪 nf_ 直连, 具体合约与主连均支持, 一次 HTTP 批量) ----
# 商品(CF)布局: 0名称 1时间 2开 3高 4低 5昨收 6买价 7卖价 8最新 9均价 10昨结
#               11买量 12卖量 13持仓 14成交量 15交易所(沪/连/郑/能) 16品种 17日期
# 金融(CFFEX)布局: 0开 1高 2低 3最新 4成交量 5成交额 6持仓 7最新 8- 9涨停 10跌停
#               13昨结 14昨收 16买价 17买量 26卖价 27卖量 38日期 39时间 40乘数 尾部名称
_EXCH_TAG = {"沪": "SHFE", "连": "DCE", "郑": "CZCE", "能": "INE", "广": "GFEX"}


def _sina_fut_rows(symbols) -> list:
    """symbols: rb/IF0/IF2612/SR601 (自动解析主连/合约) -> 统一结构列表."""
    import requests

    nf_syms = []
    for s in symbols:
        r = _resolve_fut(s)
        nf_syms.append(r[1] if r else s.upper())
    url = "https://hq.sinajs.cn/list=" + ",".join(f"nf_{s}" for s in nf_syms)
    r = _http().get(url, timeout=5)
    r.encoding = "gbk"
    if r.status_code != 200:
        raise RuntimeError(f"sina http {r.status_code}")
    rows = []
    for line in r.text.splitlines():
        if "nf_" not in line or '="' not in line:
            continue
        sym = line.split("nf_")[1].split("=")[0]
        f = line.split('="', 1)[1].rstrip('";').split(",")
        if not f or not f[0]:
            continue
        try:
            is_cffex = _is_number(f[0])
            if is_cffex:  # 金融期货 (日期/时间下标不固定, 按格式定位)
                date_i = next(i for i, x in enumerate(f) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", x))
                last, pre_close = float(f[3]), float(f[14])
                change = last - pre_close if (last and pre_close) else None
                quote = {
                    "time": f"{f[date_i]} {f[date_i + 1]}",
                    "last": last,
                    "pre_close": pre_close,
                    "change": change,
                    "change_pct": round(change / pre_close * 100, 3) if change else None,
                    "open": float(f[0]),
                    "high": float(f[1]),
                    "low": float(f[2]),
                    "upper_limit": float(f[9]),
                    "lower_limit": float(f[10]),
                    "volume": float(f[4]),
                    "amount": float(f[5]),
                    "open_interest": float(f[6]),
                    "pre_settle": float(f[13]),
                    "bids": [[float(f[16]), float(f[17])]] if float(f[16]) else [],
                    "asks": [[float(f[26]), float(f[27])]] if float(f[26]) else [],
                }
                name = f[-1]
                exch = "CFFEX"
            else:  # 商品期货
                last, pre_close = float(f[8]), float(f[10])
                change = last - pre_close if (last and pre_close) else None
                hh, mm, ss = f[1][:2], f[1][2:4], f[1][4:6]
                quote = {
                    "time": f"{f[17]} {hh}:{mm}:{ss}",
                    "last": last,
                    "pre_close": pre_close,  # 商品期货此处为昨结算, 与 tqsdk 口径一致
                    "change": change,
                    "change_pct": round(change / pre_close * 100, 3) if change else None,
                    "open": float(f[2]),
                    "high": float(f[3]),
                    "low": float(f[4]),
                    "upper_limit": None,
                    "lower_limit": None,
                    "volume": float(f[14]),
                    "amount": None,
                    "open_interest": float(f[13]),
                    "pre_settle": float(f[10]),
                    "bids": [[float(f[6]), float(f[11])]] if float(f[6]) else [],
                    "asks": [[float(f[7]), float(f[12])]] if float(f[7]) else [],
                }
                name = f[0]
                exch = _EXCH_TAG.get(f[15], None)
            rows.append(_unified(sym, "futures", name, exch, "sina-nf", "手", quote))
        except (ValueError, IndexError):
            rows.append(_unified(sym, "futures", None, None, "sina-nf", "手", {"raw": line[:200]}))
    return rows


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def cmd_futures(args) -> None:
    if args.tq:
        syms = [_tq_symbol(s) for s in args.symbols]
        with _hush():
            api = _tq_api()
            try:
                rows = _tq_rows(syms, api)
            finally:
                api.close()
        out({"source": "tqsdk", "count": len(rows), "data": rows})
        return
    # 默认新浪快通道 (一次 HTTP, ~200ms), 失败自动落到 tqsdk
    try:
        rows = _sina_fut_rows(args.symbols)
        if rows:
            out({"source": "sina-nf", "count": len(rows), "data": rows})
            return
    except Exception:  # noqa: BLE001
        pass
    syms = [_tq_symbol(s) for s in args.symbols]
    with _hush():
        api = _tq_api()
        try:
            rows = _tq_rows(syms, api)
        finally:
            api.close()
    out({"source": "tqsdk", "count": len(rows), "data": rows})


def cmd_copt(args) -> None:
    """商品期权: 找标的平值附近活跃行权价的看涨/看跌合约并取快照."""
    with _hush():
        api = _tq_api()
        try:
            und = api.get_quote(args.underlying)
            api.wait_update(deadline=time.time() + 10)
            ref = und.last_price
            result = {"source": "tqsdk", "underlying": _tq_rows([args.underlying], api)[0], "options": []}
            for opt_class in ("CALL", "PUT"):
                codes = api.query_options(args.underlying, option_class=opt_class)
                # 从合约代码解析行权价, 取平值附近 +-N 档
                strikes = []
                for c in codes:
                    m = re.search(r"(\d+(?:\.\d+)?)(?:[CP])?$", c)
                    if m:
                        strikes.append((float(m.group(1)), c))
                strikes.sort(key=lambda t: abs(t[0] - ref))
                picked = sorted(c for _, c in strikes[: args.n])
                if picked:
                    for row in _tq_rows(picked, api):
                        if row["quote"]["last"] is not None:  # 跳过无成交的深度合约
                            result["options"].append(row)
        finally:
            api.close()
    out(result)


# ------------------------------------------------------- akshare / sina ----

def _df_records(df) -> list:
    return json.loads(df.to_json(orient="records", force_ascii=False))


def cmd_futcontracts(args) -> None:
    import akshare as ak

    df = ak.futures_zh_realtime(symbol=args.product)
    out({"source": "akshare-sina", "product": args.product, "count": len(df), "data": _df_records(df)})


def cmd_futspot(args) -> None:
    import akshare as ak

    market = "CFF" if args.cff else "CF"
    df = ak.futures_zh_spot(symbol=args.symbol.upper(), market=market, adjust="0")
    out({"source": "akshare-sina", "data": _df_records(df)})


# ---- ETF 期权 (新浪 CON_OP_) ----
# 字段序依据 akshare option_sse_spot_price_sina 源码, 并经实盘交叉验证:
# 0买量 1买价 2最新 3卖价 4卖量 5持仓 6涨跌幅(vs昨结) 7行权价 8昨收 9开
# 10涨停 11跌停 12-21卖五档(价五..价一/量五..量一) 22-31买五档(价一..价五)
# 32时间 36标的 37合约简称 38振幅 39最高 40最低 41成交量 42成交额
# 43M 44昨结算 45C/P 46到期日
_SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}


_HTTP = None


def _http():
    """进程内共享 requests.Session (复用 TCP/TLS 连接, watch 常驻轮询必需)."""
    global _HTTP
    if _HTTP is None:
        import requests

        _HTTP = requests.Session()
        _HTTP.headers.update(_SINA_HEADERS)
    return _HTTP


def _con_op_rows(codes) -> list:
    url = "https://hq.sinajs.cn/list=" + ",".join(f"CON_OP_{c}" for c in codes)
    r = _http().get(url, timeout=5)
    r.encoding = "gbk"
    if r.status_code != 200:
        raise RuntimeError(f"sina http {r.status_code}")
    rows = []
    for line in r.text.splitlines():
        if 'CON_OP_' not in line or '="' not in line:
            continue
        code = line.split("CON_OP_")[1].split("=")[0]
        f = line.split('="', 1)[1].rstrip('";').split(",")
        if not f or not f[0]:
            continue

        def pair(i):
            try:
                return [float(f[i]), float(f[i + 1])]
            except (ValueError, IndexError):
                return None

        try:
            pre_close = float(f[8])
            last = float(f[2])
            change = last - pre_close if (last and pre_close) else None
            quote = {
                "time": f[32],
                "last": last,
                "pre_close": pre_close,
                "change": change,
                "change_pct": round(change / pre_close * 100, 3) if change is not None else None,
                "open": float(f[9]),
                "high": float(f[39]),
                "low": float(f[40]),
                "upper_limit": float(f[10]),
                "lower_limit": float(f[11]),
                "volume": float(f[41]),
                "amount": float(f[42]),
                "open_interest": float(f[5]),
                "pre_settle": float(f[44]) if len(f) > 44 else None,
                # 交易所口径的涨跌幅基于昨结算, 单独给出供参考
                "settle_change_pct": float(f[6]),
                "bids": [pair(22 + 2 * i) for i in range(5)],
                "asks": [pair(20 - 2 * i) for i in range(5)],
            }
            option = {
                "underlying": f[36],
                "strike": float(f[7]),
                "class": f[45],  # C=认购, P=认沽
                "expire": f[46] if len(f) > 46 else None,
            }
            rows.append(_unified(code, "option", f[37], "sse", "sina-CON_OP", "张", quote, option))
        except (ValueError, IndexError):
            rows.append(_unified(code, "option", None, "sse", "sina-CON_OP", "张", {"raw": line[:200]}))
    return rows


# ---- 公募基金净值 (东财基金接口, 非 push2, 低频安全) ----
_FUND_HEADERS = {
    "Referer": "https://fund.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}


def _fund_name(code: str):
    """基金名称: 东财 pingzhongdata JS 里的 fS_name."""
    import requests

    try:
        r = requests.get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
                         headers=_FUND_HEADERS, timeout=5)
        m = re.search(r'fS_name\s*=\s*"([^"]+)"', r.text)
        return m.group(1) if m else None
    except Exception:  # noqa: BLE001
        return None


def _fund_nav_rows(codes) -> list:
    """codes: 6位基金代码 -> 最新净值统一结构 (type=fund)."""
    import akshare as ak

    rows = []
    for code in codes:
        name = _fund_name(code)
        df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
        if df.empty:
            raise ValueError(f"基金 {code} 无单位净值数据 (货币基金请用 7日年化口径, 暂不支持)")
        tail = df.tail(2)
        last_nav = float(tail.iloc[-1]["单位净值"])
        pre_nav = float(tail.iloc[-2]["单位净值"]) if len(tail) > 1 else None
        change = round(last_nav - pre_nav, 4) if pre_nav else None
        fund_info = {}
        try:
            df2 = ak.fund_open_fund_info_em(symbol=code, indicator="累计净值走势")
            if not df2.empty:
                a_last = float(df2.iloc[-1]["累计净值"])
                a_pre = float(df2.iloc[-2]["累计净值"]) if len(df2) > 1 else None
                fund_info = {
                    "accum_nav": a_last,
                    "accum_change_pct": round((a_last / a_pre - 1) * 100, 3) if a_pre else None,
                }
        except Exception:  # noqa: BLE001
            pass
        quote = {
            "time": str(tail.iloc[-1]["净值日期"]),
            "last": last_nav,
            "pre_close": pre_nav,
            "change": change,
            "change_pct": float(tail.iloc[-1]["日增长率"]),
            "open": None,
            "high": None,
            "low": None,
            "upper_limit": None,
            "lower_limit": None,
            "volume": None,
            "amount": None,
            "open_interest": None,
            "pre_settle": None,
            "bids": [],
            "asks": [],
        }
        rows.append(_unified(f"{code}.of", "fund", name, "of", "eastmoney-fund", None, quote,
                             fund=fund_info or None))
    return rows


def cmd_doctor(args) -> None:
    """自检全部数据源通道: 逐项连通性+正确性测试, 输出 JSON 报告."""
    import akshare as ak

    checks = []

    def check(name, fn, skip_reason=None):
        if skip_reason:
            checks.append({"check": name, "status": "skip", "ms": 0, "detail": skip_reason})
            return
        t0 = time.time()
        try:
            detail = fn()
            checks.append({"check": name, "status": "pass", "ms": round((time.time() - t0) * 1000),
                           "detail": str(detail)[:200]})
        except Exception as e:  # noqa: BLE001
            checks.append({"check": name, "status": "fail", "ms": round((time.time() - t0) * 1000),
                           "detail": f"{type(e).__name__}: {e}"[:200]})

    def _tdx_snapshot():
        client = _tdx()
        try:
            rows = _eltdx_rows(["sh000001", "sz159915"], client)
            assert rows[0]["quote"]["last"] > 0 and rows[1]["name"]
            return f"sh000001={rows[0]['quote']['last']} {rows[1]['name']}={rows[1]['quote']['last']}"
        finally:
            client.close()

    def _tdx_kline():
        client = _tdx()
        try:
            series = client.bars.get("sh000001", period="day", kind="index", count=3)
            assert len(series.bars) == 3
            return f"3 bars, close={series.bars[-1].close}"
        finally:
            client.close()

    def _tdx_names():
        client = _tdx()
        try:
            tables = _tdx_tables(client)
            n = sum(len(v) for v in tables.values())
            assert n > 5000
            return f"{n} 个证券名称 (缓存 {'~/.cache/fdata/names.json' if os.path.exists(_NAMES_CACHE) else '未建'})"
        finally:
            client.close()

    def _sina_fut():
        rows = _sina_fut_rows(["rb", "IF"])
        assert rows[0]["quote"]["last"] > 0 and rows[1]["quote"]["last"] > 0
        return f"rb={rows[0]['quote']['last']} IF={rows[1]['quote']['last']}"

    def _sina_etfopt():
        rows = _con_op_rows(["10011255"])
        assert rows[0]["quote"]["last"] >= 0 and rows[0]["name"]
        return f"{rows[0]['name']} last={rows[0]['quote']['last']}"

    def _fund_nav():
        rows = _fund_nav_rows(["004075"])
        assert rows[0]["quote"]["last"] > 0
        return f"{rows[0]['name']} nav={rows[0]['quote']['last']} ({rows[0]['quote']['time']})"

    def _news():
        df = ak.stock_info_global_sina()
        assert len(df) > 0
        return f"{len(df)} 条"

    def _weight():
        df = ak.index_stock_cons_weight_csindex(symbol="000300")
        assert len(df) > 100
        return f"{len(df)} 只成分"

    def _has_tq_creds() -> bool:
        try:
            from dotenv import load_dotenv

            load_dotenv(os.path.join(REPO, ".env"))
        except ImportError:
            pass
        return bool(os.environ.get("TQ_USER") and os.environ.get("TQ_PASS"))

    no_cred = "未配置 TQ_USER/TQ_PASS (在项目 .env 或环境变量中设置)"
    tq = os.environ.get("TQ_USER") or (_has_tq_creds() and os.environ.get("TQ_USER"))

    def _tq_fut():
        with _hush():
            api = _tq_api()
            try:
                rows = _tq_rows(["KQ.m@SHFE.rb"], api)
                assert rows[0]["quote"]["last"] and rows[0]["quote"]["last"] > 0
                return f"rb={rows[0]['quote']['last']} (五档 bids={len(rows[0]['quote']['bids'])})"
            finally:
                api.close()

    def _tq_copt():
        with _hush():
            api = _tq_api()
            try:
                und = api.get_quote("SHFE.rb2610")
                api.wait_update(deadline=time.time() + 10)
                codes = api.query_options("SHFE.rb2610", option_class="CALL")
                assert len(codes) > 0
                return f"{len(codes)} 个看涨合约"
            finally:
                api.close()

    check("eltdx 快照 (股票/ETF/指数)", _tdx_snapshot)
    check("eltdx K线", _tdx_kline)
    check("eltdx 证券名称表/缓存", _tdx_names)
    check("新浪期货 nf_ (商品+金融)", _sina_fut)
    check("新浪 ETF期权 CON_OP_", _sina_etfopt)
    check("东财基金净值", _fund_nav)
    check("akshare 全球快讯", _news)
    check("akshare 中证成分权重", _weight)
    check("tqsdk 期货 (五档)", _tq_fut, skip_reason=no_cred if not tq else None)
    check("tqsdk 商品期权", _tq_copt, skip_reason=no_cred if not tq else None)

    n_fail = sum(1 for c in checks if c["status"] == "fail")
    n_skip = sum(1 for c in checks if c["status"] == "skip")
    out({"ok": n_fail == 0, "passed": len(checks) - n_fail - n_skip,
         "failed": n_fail, "skipped": n_skip, "checks": checks})
    sys.exit(1 if n_fail else 0)


def cmd_quote(args) -> None:
    """统一实时价格入口: 按代码形态路由到 eltdx/新浪期货/新浪期权, tqsdk 兜底."""
    buckets = {"tdx": [], "sina_fut": [], "tq": [], "sina_opt": [], "fund": []}
    for raw in args.codes:
        c = raw.strip()
        m_fund = re.fullmatch(r"(?:of:|)(\d{6})\.of", c.lower()) or re.fullmatch(r"of:(\d{6})", c.lower())
        if m_fund:
            buckets["fund"].append(m_fund.group(1))
        elif len(c) == 8 and c.isdigit():
            buckets["sina_opt"].append(c)
        elif re.fullmatch(r"[a-zA-Z]{2}\d{6}", c) or (len(c) == 6 and c.isdigit()):
            buckets["tdx"].append(_norm_code(c))
        elif _is_option_code(c):
            buckets["tq"].append(_tq_symbol(c))
        elif "@" in c or "." in c:
            buckets["tq"].append(_tq_symbol(c))
        elif re.fullmatch(r"[A-Za-z]+\d{0,4}", c):
            buckets["sina_fut"].append(c)  # rb / IF0 / IF2612 / SR601
        else:
            buckets["tq"].append(c)  # 交给 tqsdk 报错

    data, errors = [], []
    if buckets["tdx"]:
        client = _tdx()
        try:
            data.extend(_eltdx_rows(buckets["tdx"], client))
        except Exception as e:  # noqa: BLE001
            errors.append({"codes": buckets["tdx"], "error": f"{type(e).__name__}: {e}"})
        finally:
            client.close()
    if buckets["fund"]:
        for code in buckets["fund"]:
            try:
                data.extend(_fund_nav_rows([code]))
            except Exception as e:  # noqa: BLE001
                errors.append({"codes": [code], "error": f"{type(e).__name__}: {e}"})
    if buckets["sina_fut"]:
        try:
            data.extend(_sina_fut_rows(buckets["sina_fut"]))
        except Exception as e:  # noqa: BLE001
            # 新浪失败 -> tqsdk 兜底
            try:
                syms = [_tq_symbol(s) for s in buckets["sina_fut"]]
                with _hush():
                    api = _tq_api()
                    try:
                        data.extend(_tq_rows(syms, api))
                    finally:
                        api.close()
            except Exception as e2:  # noqa: BLE001
                errors.append({"codes": buckets["sina_fut"], "error": f"sina: {e}; tqsdk: {e2}"})
    if buckets["sina_opt"]:
        try:
            data.extend(_con_op_rows(buckets["sina_opt"]))
        except Exception as e:  # noqa: BLE001
            errors.append({"codes": buckets["sina_opt"], "error": f"{type(e).__name__}: {e}"})
    if buckets["tq"]:
        with _hush():
            api = _tq_api()
            try:
                data.extend(_tq_rows(buckets["tq"], api))
            except Exception as e:  # noqa: BLE001
                errors.append({"codes": buckets["tq"], "error": f"{type(e).__name__}: {e}"})
            finally:
                api.close()

    out({"ok": not errors and bool(data), "count": len(data), "data": data, "errors": errors})


def cmd_etfopt(args) -> None:
    rows = _con_op_rows([c.strip().upper() for c in args.codes if c.strip()])
    out({"source": "sina-CON_OP", "count": len(rows), "data": rows})


def cmd_etfcodes(args) -> None:
    """上交所 ETF 期权合约列表, 看涨/看跌各调一次 (akshare 1.18 单表返回)."""
    import akshare as ak

    result = {}
    for cls, key in (("看涨期权", "calls"), ("看跌期权", "puts")):
        df = ak.option_sse_codes_sina(symbol=cls, trade_date=args.month, underlying=args.etf)
        result[key] = df["期权代码"].astype(str).tolist() if not df.empty else []
    result.update({"source": "akshare-sina", "count": len(result["calls"]) + len(result["puts"])})
    out(result)


def cmd_greeks(args) -> None:
    import akshare as ak

    df = ak.option_sse_greeks_sina(symbol=args.code)
    out({"source": "akshare-sina", "data": _df_records(df)})


def cmd_nav(args) -> None:
    """基金历史净值: 单位净值+累计净值按日期合并."""
    import akshare as ak

    df = ak.fund_open_fund_info_em(symbol=args.code, indicator="单位净值走势")
    if df.empty:
        die(f"基金 {args.code} 无单位净值数据 (货币基金暂不支持)")
    df["净值日期"] = df["净值日期"].astype(str).str[:10]
    df2 = ak.fund_open_fund_info_em(symbol=args.code, indicator="累计净值走势")
    if not df2.empty:
        df2["净值日期"] = df2["净值日期"].astype(str).str[:10]
        df = df.merge(df2, on="净值日期", how="left")
    if args.limit:
        df = df.tail(args.limit)
    out({"source": "eastmoney-fund", "code": args.code, "name": _fund_name(args.code),
         "count": len(df), "data": _df_records(df)})


def cmd_news(args) -> None:
    import akshare as ak

    df = ak.stock_info_global_sina()
    if args.limit:
        df = df.head(args.limit)
    out({"source": "akshare-sina", "count": len(df), "data": _df_records(df)})


def cmd_weight(args) -> None:
    import akshare as ak

    df = ak.index_stock_cons_weight_csindex(symbol=args.index)
    if args.limit:
        df = df.head(args.limit)
    out({"source": "akshare-csindex", "count": len(df), "data": _df_records(df)})


# ------------------------------------------------------------------ watch ----

def cmd_watch(args) -> None:
    """常驻轮询: 长连接复用, 每个 tick 调用策略文件的 on_tick(quotes) 回调."""
    import importlib.util

    strategy = None
    if args.strategy:
        spec = importlib.util.spec_from_file_location("fdata_strategy", args.strategy)
        strategy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(strategy)
        if not hasattr(strategy, "on_tick"):
            die(f"策略文件缺少 on_tick(quotes) 函数: {args.strategy}")

    # 路由 (基金净值是 T-1 数据, 不支持 tick)
    buckets = {"tdx": [], "sina_fut": [], "tq": [], "sina_opt": []}
    for raw in args.codes:
        c = raw.strip()
        if re.fullmatch(r"(?:of:|)(\d{6})\.of", c.lower()) or re.fullmatch(r"of:(\d{6})", c.lower()):
            die("基金净值是 T-1 数据, 不支持 tick 轮询")
        if len(c) == 8 and c.isdigit():
            buckets["sina_opt"].append(c)
        elif re.fullmatch(r"[a-zA-Z]{2}\d{6}", c) or (len(c) == 6 and c.isdigit()):
            buckets["tdx"].append(_norm_code(c))
        elif _is_option_code(c) or "@" in c or "." in c:
            buckets["tq"].append(_tq_symbol(c))
        elif re.fullmatch(r"[A-Za-z]+\d{0,4}", c):
            buckets["sina_fut"].append(c)
        else:
            die(f"无法识别的代码: {c}")
    if not any(buckets.values()):
        die("请提供至少一个代码")

    client = None
    api = None
    log_f = open(args.log, "a", encoding="utf-8") if args.log else None
    n_cycles = n_signals = 0
    t_start_all = time.time()

    def _fetch_tdx():
        nonlocal client
        if client is None:
            client = _tdx()
        return _eltdx_rows(buckets["tdx"], client)

    def _fetch_tq():
        nonlocal api
        if api is None:
            with _hush():
                api = _tq_api()
        return _tq_rows(buckets["tq"], api, wait_s=args.interval)

    try:
        cycle = 0
        while args.cycles <= 0 or cycle < args.cycles:
            cycle += 1
            t0 = time.time()
            quotes, feed_errors = [], []
            for name, fetch in (
                ("eltdx", lambda: _fetch_tdx()),
                ("sina-fut", lambda: _sina_fut_rows(buckets["sina_fut"])),
                ("sina-etfopt", lambda: _con_op_rows(buckets["sina_opt"])),
                ("tqsdk", lambda: _fetch_tq()),
            ):
                if not {
                    "eltdx": buckets["tdx"],
                    "sina-fut": buckets["sina_fut"],
                    "sina-etfopt": buckets["sina_opt"],
                    "tqsdk": buckets["tq"],
                }[name]:
                    continue
                try:
                    quotes.extend(fetch())
                except Exception as e:  # noqa: BLE001
                    feed_errors.append({"source": name, "error": f"{type(e).__name__}: {e}"})
                    if name == "eltdx":  # 连接可能失效, 下轮重建
                        try:
                            client.close()
                        except Exception:  # noqa: BLE001
                            pass
                        client = None

            signals = []
            if strategy:
                try:
                    import inspect

                    if len(inspect.signature(strategy.on_tick).parameters) >= 2:
                        signals = strategy.on_tick(quotes, feed_errors) or []
                    else:
                        signals = strategy.on_tick(quotes) or []
                except Exception as e:  # noqa: BLE001
                    feed_errors.append({"source": "strategy", "error": f"{type(e).__name__}: {e}"})
            n_cycles += 1
            n_signals += len(signals)
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            for sig in signals:
                line = json.dumps({"ts": ts, "signal": sig}, ensure_ascii=False, default=str)
                print(f"[SIG] {line}", file=_REAL_STDOUT, flush=True)
                if log_f:
                    log_f.write(line + "\n")
                    log_f.flush()
            if feed_errors:
                print(f"[ERR] {ts} {json.dumps(feed_errors, ensure_ascii=False)}",
                      file=sys.stderr, flush=True)
            if args.verbose or args.cycles == 1:
                prices = {q["code"]: q["quote"]["last"] for q in quotes}
                print(f"[TICK] {ts} #{cycle} {round((time.time()-t0)*1000)}ms {prices}"
                      f" signals={len(signals)} errors={len(feed_errors)}",
                      file=_REAL_STDOUT, flush=True)
            rest = args.interval - (time.time() - t0)
            if rest > 0:
                time.sleep(rest)
    except KeyboardInterrupt:
        pass
    finally:
        if client:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        if log_f:
            log_f.close()
        summary = {"cycles": n_cycles, "signals": n_signals,
                   "elapsed_s": round(time.time() - t_start_all, 1)}
        print(f"[DONE] {json.dumps(summary, ensure_ascii=False)}", file=_REAL_STDOUT, flush=True)


# ------------------------------------------------------------------ main ----

HELP = argparse.RawDescriptionHelpFormatter


def main() -> None:
    p = argparse.ArgumentParser(
        prog="fdata",
        formatter_class=HELP,
        description="统一金融数据 CLI (JSON 输出). 查实时价格首选 quote 子命令.",
        epilog="""\
快速上手:
  fdata.py quote sh000001 600519 rb 10011255     # 任意品种统一实时价格 (推荐)
  fdata.py kline sh000001 --kind index --limit 60
  fdata.py news --limit 20

代码路由规则 (quote):
  6位数字    -> 股票/ETF/指数  (eltdx; 指数须写前缀 sh000001/sz399001)
  004075.of 或 of:004075 -> 公募基金净值 (东财基金)
  8位数字    -> ETF期权合约    (新浪 CON_OP_, 如 10011255)
  rb/IF/IF0/IF2612 -> 期货主连/具体合约 (新浪 nf_, 失败落 tqsdk)
  SHFE.rb2610 / KQ.m@SHFE.rb -> 期货具体合约/主力连续 (tqsdk)
  SHFE.rb2610C3100 等含 C/P 行权价 -> 期权 (tqsdk)
""",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser(
        "quote",
        formatter_class=HELP,
        help="【推荐】任意品种统一实时价格 (自动路由 eltdx/tqsdk/新浪)",
        description="""\
按代码自动判断品种与数据源, 以统一 JSON 结构返回实时价格。
股票/ETF/指数走 eltdx, 期货/商品期权走 tqsdk, ETF期权走新浪 CON_OP_。
一批代码会按数据源分组批量请求 (tqsdk 单连接, 新浪单 HTTP)。

统一输出结构 (每个元素):
  code          规范化完整代码
  type          stock | etf | index | futures | option
  name          证券/合约名称
  exchange      sh/sz | SHFE/DCE/CZCE/CFFEX/INE/GFEX | sse
  option        仅期权: {underlying, strike, class(C/P), expire}, 其余 null
  source        eltdx | tqsdk | sina-CON_OP
  volume_unit   "手" (eltdx/tqsdk) 或 "张" (ETF期权)
  quote.time    行情时间 (eltdx 快照不提供, 为 null)
  quote.last / pre_close / change / change_pct   # change(_pct) 统一相对昨收
  quote.open / high / low / upper_limit / lower_limit
  quote.volume / amount / open_interest / pre_settle
  quote.bids / asks    五档 [[价格, 量]...], 买档降序卖档升序; 指数为 []

注意: 闭市日返回上一交易日收盘快照; 期货涨跌停基于 pre_settle 而非 pre_close。
盘中 last=0 表示该品种当日尚无成交 (如集合竞价前), 此时 change/change_pct 为 null,
策略里请先判断 last>0 或 change_pct is not None。
""",
        epilog="""\
示例:
  # 股票/ETF/指数/期货/期权 混合查询, 一次调用:
  %(prog)s sh000001 sz159915 600519 rb IF SHFE.rb2610 10011255

  # 返回 (节选, 每个 code 一个元素):
  [
   {
    "code": "sh000001", "type": "index", "name": "上证指数",
    "exchange": "sh", "option": null, "source": "eltdx", "volume_unit": "手",
    "quote": {
      "time": null, "last": 3952.18, "pre_close": 3956.57,
      "change": -4.39, "change_pct": -0.111,
      "open": 3950.24, "high": 3970.31, "low": 3947.8,
      "upper_limit": null, "lower_limit": null,
      "volume": 510581645, "amount": 970365140992.0,
      "open_interest": null, "pre_settle": null,
      "bids": [], "asks": []
    }
   },
   {
    "code": "KQ.m@SHFE.rb", "type": "futures", "name": "螺纹主连",
    "exchange": "SHFE", "option": null, "source": "tqsdk", "volume_unit": "手",
    "quote": {
      "time": "2026-08-28 22:59:59", "last": 3130.0, "pre_close": 3112.0,
      "change": 18.0, "change_pct": 0.578,
      "open": 3115.0, "high": 3132.0, "low": 3115.0,
      "upper_limit": 3264.0, "lower_limit": 2953.0,
      "volume": 285599, "amount": 970365140992.0,
      "open_interest": 1051925, "pre_settle": 3112.0,
      "bids": [[3130.0, 141]], "asks": [[3131.0, 24]]
    }
   },
   {
    "code": "10011255", "type": "option", "name": "50ETF购9月2650",
    "exchange": "sse", "source": "sina-CON_OP", "volume_unit": "张",
    "option": {"underlying": "510050", "strike": 2.65, "class": "C", "expire": "2026-09-23"},
    "quote": {
      "time": "2026-08-28 14:54:43", "last": 0.3762, "pre_close": 0.3811,
      "change": -0.0049, "change_pct": -1.286,
      "open": 0.378, "high": 0.3839, "low": 0.3762,
      "upper_limit": 0.694, "lower_limit": 0.086,
      "volume": 96.0, "amount": 363550.0,
      "open_interest": 1625.0, "pre_settle": 0.39, "settle_change_pct": -3.54,
      "bids": [[0.3775, 1.0], [0.37, 1.0], ...], "asks": [[0.3821, 1.0], ...]
    }
   }
  ]

外层: {"ok": true, "count": 3, "data": [...], "errors": []}
部分代码失败不影响其余: 失败项进入 errors: [{"codes": [...], "error": "..."}]
""",
    )
    sp.add_argument("codes", nargs="+", help="任意混合: sh000001 600519 rb IF2612 10011255 004075.of")
    sp.set_defaults(fn=cmd_quote)

    sp = sub.add_parser(
        "doctor",
        formatter_class=HELP,
        help="自检全部数据源通道 (连通性+正确性, JSON 报告)",
        description="""\
逐项测试 fdata 依赖的所有数据源: eltdx(股票/ETF/指数)、新浪期货 nf_、
新浪 ETF期权 CON_OP_、东财基金净值、akshare(快讯/权重)、tqsdk(期货/商品期权,
未配置 TQ_USER/TQ_PASS 时标记 skip 而非 fail)。

退出码: 0 = 全部通过(或跳过), 1 = 有失败项。适合 cron/部署前自检。
""",
        epilog="""\
示例:
  %(prog)s

返回:
  {"ok": true, "passed": 10, "failed": 0, "skipped": 0,
   "checks": [{"check": "eltdx 快照 (股票/ETF/指数)", "status": "pass",
               "ms": 480, "detail": "sh000001=3952.18 ..."}, ...]}
""",
    )
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser(
        "serve",
        formatter_class=HELP,
        help="长连接数据服务器 (常驻 eltdx client, 复用连接, 减少初始化开销)",
        description="""\
启动常驻数据服务器: 进程内保持单个 eltdx client, 连接复用, 避免每次调用重建的
~50-100ms 初始化开销 (适合每秒 tick / 高频轮询场景). CLI 各命令保持原样, 不变化.

协议: 标准 TCP, 每行一个 JSON 请求, 每行一个 JSON 响应 (数据结构和 CLI 同构).
  请求: {"op": "quote", "code": "600519"}
        {"op": "kline", "code": "600519", "period": "1m",
         "kind": "stock", "adjust": null, "limit": 640}
  响应: {"ok": true, "result": ...} 或 {"ok": false, "error": "..."}
并发安全: 内部串行化 eltdx 访问; 断线自动重连.

供 Python 侧使用 strategy/fdata_client.py, 或手工 nc/printf 调试.
""",
        epilog="""\
示例:
  %(prog)s --port 9701                # 默认 127.0.0.1:9701
  FDATA_PORT=9702 %(prog)s            # 端口由环境变量覆盖
""",
    )
    sp.add_argument("--host", default=os.environ.get("FDATA_HOST", "127.0.0.1"))
    sp.add_argument("--port", type=int, default=int(os.environ.get("FDATA_PORT", "9701")))
    sp.add_argument("--ws", action="store_true",
                    help="(预留) 输出提示为 WebSocket; 当前为原始 TCP line-JSON")
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser(
        "watch",
        formatter_class=HELP,
        help="常驻 tick 轮询: 长连接复用, 每个 tick 调用策略 on_tick 回调",
        description="""\
实盘信号监控的推荐入口。进程常驻, 所有连接复用 (eltdx 长连接 / requests.Session /
tqsdk websocket), 无 CLI 每次启动的进程与握手开销; 每隔 --interval 秒拉一轮
watchlist 最新价 (统一结构), 调用策略文件的 on_tick 回调, 返回的信号打印为
[SIG] JSON 行并可追加到 --log JSONL 文件。

策略文件约定 (普通 .py 文件, 无需注册):
    def on_tick(quotes, feed_errors=None):
        # quotes: 与 quote 命令相同的统一结构列表 (含 name/exchange/quote/bids...)
        # feed_errors: 本轮数据源错误列表 (可省略此参数)
        signals = []
        for q in quotes:
            if q["code"] == "rb" and q["quote"]["last"] > 3200:
                signals.append({"symbol": "rb", "event": "breakout",
                                "price": q["quote"]["last"]})
        return signals          # 返回 list[dict], 空/None 表示无信号

注意:
- 新浪通道 (期货/ETF期权) 请保持 --interval >= 1 秒, 过高频有封禁风险
- 基金净值是 T-1 数据, 不支持 tick
- tqsdk 代码需要 .env 配置 TQ_USER/TQ_PASS; 仅 eltdx 代码则无需任何凭据
""",
        epilog="""\
示例:
  # 监控 ETF+期货+期权混合, 1秒一轮, 突破告警策略, 信号落盘:
  %(prog)s sz159915 rb IF2612 10011255 --interval 1 \\
      --strategy my_alert.py --log signals.jsonl --verbose

  # 只监控 A股/ETF (纯 eltdx, 无需任何凭据), 可到 0.2s 间隔:
  %(prog)s 600519 sz159915 sh000001 --interval 0.2

  # 单轮调试 (跑一轮就退出):
  %(prog)s rb --cycles 1 --verbose

# my_alert.py 示例 (突破+涨跌幅告警):
def on_tick(quotes):
    out = []
    for q in quotes:
        qd = q["quote"]
        if q["type"] == "futures" and qd["last"] and q["code"] == "IF2612" and qd["last"] > 4550:
            out.append({"symbol": q["code"], "event": "breakout", "price": qd["last"]})
        if qd["change_pct"] is not None and abs(qd["change_pct"]) >= 1.5:
            out.append({"symbol": q["code"], "event": "big_move", "pct": qd["change_pct"]})
    return out

每轮周期 = max(数据耗时, --interval); [TICK] 行 (--verbose) 显示单轮耗时,
通常 eltdx ~50ms、新浪每源 ~100-200ms, 1 秒间隔余量充足。
""",
    )
    sp.add_argument("codes", nargs="+",
                    help="任意混合 (同 quote): sh000001 600519 rb IF2612 10011255; 基金不支持")
    sp.add_argument("--interval", type=float, default=1.0,
                    help="轮询间隔秒 (默认1.0; 新浪通道勿低于1s)")
    sp.add_argument("--strategy", default=None, help="策略 .py 文件路径, 需定义 on_tick(quotes)")
    sp.add_argument("--log", default=None, help="信号 JSONL 落盘文件 (追加写)")
    sp.add_argument("--cycles", type=int, default=0, help="轮数, 0=无限 (默认0; 1=单轮调试)")
    sp.add_argument("--verbose", action="store_true", help="每轮打印 [TICK] 价格摘要")
    sp.set_defaults(fn=cmd_watch)

    sp = sub.add_parser(
        "snapshot",
        formatter_class=HELP,
        help="股票/ETF/指数实时快照 (eltdx, 输出统一结构)",
        description="eltdx 通道的 A股/ETF/指数批量快照, 输出与 quote 相同的统一结构, 但仅限 sh/sz 代码。",
        epilog="""\
示例:
  %(prog)s sh000001 sz159915 600519
  %(prog)s 600519 000001            # 6位纯数字自动补前缀(60/68→sh, 其余→sz)
                                    # ⚠️ 指数必须显式写前缀: sh000001/sz399001
返回: {"source": "eltdx", "count": 3, "data": [统一结构...]}  (结构见 quote 帮助)
""",
    )
    sp.add_argument("codes", nargs="+", help="如 sh000001 000001 sz159915")
    sp.set_defaults(fn=cmd_snapshot)

    sp = sub.add_parser(
        "kline",
        formatter_class=HELP,
        help="股票/ETF/指数K线 (eltdx)",
        description="""\
eltdx 通道 K 线, 支持日线/分钟线与前复权。K线按时间升序输出。
volume 单位: 手 (指数为成交量手数, 股票/ETF 为手; 乘 100 得股/份)。
""",
        epilog="""\
示例:
  %(prog)s sh000001 --kind index --limit 60     # 上证指数近60日
  %(prog)s sz159915 --adjust qfq --limit 30     # 创业板ETF 前复权
  %(prog)s 600519 --period 30m --limit 100      # 茅台30分钟线
  %(prog)s sh000001 --kind index --limit 0      # 全部历史(1990至今)

返回:
  {"source": "eltdx", "code": "sh000001", "kind": "index", "count": 60,
   "data": [{"date": "2026-08-28 15:00:00+08:00", "open": 3950.2, "high": 3970.3,
             "low": 3947.8, "close": 3952.18, "volume": 4869314.24, "amount": 9.5e+11},
            ...]}   # 升序
""",
    )
    sp.add_argument("code")
    sp.add_argument("--period", default="day", help="K线周期: day/30m/5m/... (默认 day)")
    sp.add_argument("--kind", default="auto", choices=["auto", "index", "stock", "futures"],
                    help="指数 index; 股票/ETF stock; 期货 futures(au/rb/IF等, 走tqsdk); auto 自动判别")
    sp.add_argument("--adjust", default="none", choices=["none", "qfq"], help="前复权 (默认不复权)")
    sp.add_argument("--limit", type=int, default=30, help="根数, 0=全部历史 (默认30)")
    sp.set_defaults(fn=cmd_kline)

    sp = sub.add_parser(
        "list",
        formatter_class=HELP,
        help="证券列表: 代码+名称 (eltdx)",
        description="查代码/名称用。返回 [{code, name}], code 可直接用于 quote/snapshot/kline。",
        epilog="""\
示例:
  %(prog)s etfs --filter 创业板 --limit 10
  %(prog)s stocks --filter 茅台
  %(prog)s indices --limit 20

返回:
  {"source": "eltdx", "count": 3,
   "data": [{"code": "sz159915", "name": "创业板ETF易方达"}, ...]}
""",
    )
    sp.add_argument("category", choices=["stocks", "etfs", "indices"])
    sp.add_argument("--filter", default=None, help="代码或名称包含匹配, 如 茅台/159/510")
    sp.add_argument("--limit", type=int, default=0)
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser(
        "futures",
        formatter_class=HELP,
        help="期货实时快照 (默认新浪快通道, --tq 走 tqsdk 五档; 统一结构)",
        description="""\
期货实时, 输出与 quote 相同的统一结构。
默认走新浪 nf_ 通道: 一次 HTTP 批量, ~200ms, 支持具体合约 (IF2612/RB2610),
一档买卖盘; 失败自动落到 tqsdk。
--tq 强制走 tqsdk 快期 websocket: 五档盘口, 但每次握手 2-4 秒, 适合长驻场景。
凭据在项目 .env 或环境变量 TQ_USER / TQ_PASS (快期免费账户)。
""",
        epilog="""\
示例:
  %(prog)s rb IF cu m          # 品种别名 -> 主力连续 (自动映射交易所)
  %(prog)s IF2612 rb2610       # 具体合约 (短代码自动解析交易所)
  %(prog)s SHFE.rb2610 --tq    # tqsdk 通道 (五档)

返回: {"source": "sina-nf", "count": 4, "data": [统一结构...]}  (结构见 quote 帮助)
""",
    )
    sp.add_argument("symbols", nargs="+", help="品种别名(rb/IF)或合约代码(IF2612/SHFE.rb2610)")
    sp.add_argument("--tq", action="store_true",
                    help="强制走 tqsdk (五档盘口, 较慢); 默认新浪快通道, 失败自动落 tqsdk")
    sp.set_defaults(fn=cmd_futures)

    sp = sub.add_parser(
        "copt",
        formatter_class=HELP,
        help="商品期权平值附近合约快照 (tqsdk, 输出统一结构)",
        description="""\
按标的价格找平值附近活跃行权价的看涨/看跌合约并取快照。
深度实值/虚值无成交合约 (last=nan) 自动跳过。
""",
        epilog="""\
示例:
  %(prog)s SHFE.rb2610 --n 4

返回:
  {"source": "tqsdk",
   "underlying": {统一结构, type=futures},
   "options": [{统一结构, type=option, option.strike/class...}, ...]}
""",
    )
    sp.add_argument("underlying", help="期货标的, 如 SHFE.rb2610")
    sp.add_argument("--n", type=int, default=4, help="每方向(涨/跌)取最近行权价档数 (默认4)")
    sp.set_defaults(fn=cmd_copt)

    sp = sub.add_parser(
        "futcontracts",
        formatter_class=HELP,
        help="期货品种全部合约列表 (akshare/新浪)",
        description="新浪通道一次返回某品种全部合约的实时量价 (含各月合约与主连)。",
        epilog="""\
示例:
  %(prog)s 螺纹钢
  %(prog)s 上证50股指期货

返回:
  {"source": "akshare-sina", "product": "螺纹钢", "count": 13,
   "data": [{"symbol": "RB0", "name": "螺纹钢连续", "trade": 3178.0,
             "presettlement": 3151.0, "volume": 221502, "position": 1152964, ...},
            ...]}   # akshare futures_zh_realtime 原始列
""",
    )
    sp.add_argument("product", help="中文品种名, 如 螺纹钢/上证50股指期货")
    sp.set_defaults(fn=cmd_futcontracts)

    sp = sub.add_parser(
        "futspot",
        formatter_class=HELP,
        help="期货实时 (akshare/新浪, tqsdk 的备用通道)",
        description="""\
新浪 HTTP 通道期货实时快照, 作为 tqsdk 不可用时的备用。
注意: 此接口依赖 py_mini_racer (arm64 已由脚本内 shim 修复)。
""",
        epilog="""\
示例:
  %(prog)s RB0                 # 商品期货 (market=CF)
  %(prog)s IF0 --cff           # 中金所品种必须加 --cff

返回:
  {"source": "akshare-sina", "data": [{"symbol": "沪深300指数期货连续",
   "time": "15:00:00", "open": 4591.8, "current_price": 4593.6, ...}]}
   # akshare futures_zh_spot 原始列
""",
    )
    sp.add_argument("symbol", help="如 RB0/IF0/RB2610")
    sp.add_argument("--cff", action="store_true", help="中金所品种加此开关")
    sp.set_defaults(fn=cmd_futspot)

    sp = sub.add_parser(
        "etfopt",
        formatter_class=HELP,
        help="ETF期权批量实时 (新浪 CON_OP_, 输出统一结构)",
        description="""\
新浪 CON_OP_ 接口一次 HTTP 批量拉取几十只 ETF 期权合约实时,
输出与 quote 相同的统一结构 (volume_unit=张)。
settle_change_pct 字段为交易所口径涨跌幅 (基于昨结算);
change_pct 统一为相对昨收。
""",
        epilog="""\
示例:
  %(prog)s 10011255 10011257        # 合约代码先用 etfcodes 获取

返回: {"source": "sina-CON_OP", "count": 2, "data": [统一结构...]}  (结构见 quote 帮助)
""",
    )
    sp.add_argument("codes", nargs="+", help="期权合约代码, 如 10011255")
    sp.set_defaults(fn=cmd_etfopt)

    sp = sub.add_parser(
        "etfcodes",
        formatter_class=HELP,
        help="上交所ETF期权合约列表 (akshare/新浪)",
        description="上交所指定标的+到期月份的期权合约代码表 (看涨/看跌)。",
        epilog="""\
示例:
  %(prog)s --month 2609 --etf 510050

返回:
  {"source": "akshare-sina", "calls": ["10011255", ...], "puts": ["10011257", ...],
   "count": 28}
注意: 目前仅上交所 (510050/510300); 深市合约表未封装。
""",
    )
    sp.add_argument("--month", required=True, help="到期月份, 如 2609")
    sp.add_argument("--etf", default="510050", help="标的: 510050(50ETF) 或 510300(300ETF)")
    sp.set_defaults(fn=cmd_etfcodes)

    sp = sub.add_parser(
        "greeks",
        formatter_class=HELP,
        help="期权希腊字母/隐含波动率 (akshare/新浪)",
        description="单只上交所 ETF 期权的 Delta/Gamma/Vega/Theta/隐含波动率 (新浪)。",
        epilog="""\
示例:
  %(prog)s 10011255

返回:
  {"source": "akshare-sina", "data": [{"字段": "Delta", "值": "0.9959"},
   {"字段": "Gamma", "值": "0.0754"}, {"字段": "隐含波动率", "值": "..."}, ...]}
   # 字段/值两列的表格结构; 深度虚值合约部分字段可能为空
""",
    )
    sp.add_argument("code", help="期权合约代码, 如 10011255")
    sp.set_defaults(fn=cmd_greeks)

    sp = sub.add_parser(
        "nav",
        formatter_class=HELP,
        help="公募基金历史净值 (akshare/东财基金)",
        description="""\
开放式基金的单位净值+累计净值历史 (按日期升序, 含日增长率)。
最新一期净值也可直接用 quote: %(prog)s 004075.of
注意: 货币基金 (万份收益/7日年化口径) 暂不支持; 场内 ETF/LOF 直接用 quote/kline。
""",
        epilog="""\
示例:
  %(prog)s 004075 --limit 10       # 最近10个净值日
  %(prog)s 004075                  # 成立以来全部

返回:
  {"source": "eastmoney-fund", "code": "004075", "name": "交银医药创新股票A",
   "count": 10,
   "data": [{"净值日期": "2026-08-28", "单位净值": 2.8986, "日增长率": -1.56,
             "累计净值": 2.8986}, ...]}
""",
    )
    sp.add_argument("code", help="6位基金代码, 如 004075")
    sp.add_argument("--limit", type=int, default=20, help="返回条数, 0=全部历史")
    sp.set_defaults(fn=cmd_nav)

    sp = sub.add_parser(
        "news",
        formatter_class=HELP,
        help="全球财经快讯 (akshare/新浪)",
        description="新浪全球财经快讯滚动流, 每条含 时间/内容。",
        epilog="""\
示例:
  %(prog)s --limit 20

返回:
  {"source": "akshare-sina", "count": 20,
   "data": [{"时间": "2026-08-30 17:52:00", "内容": "【...】..."}, ...]}
""",
    )
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(fn=cmd_news)

    sp = sub.add_parser(
        "weight",
        formatter_class=HELP,
        help="中证指数成分权重 (akshare/中证官网)",
        description="中证指数官网成分股权重表 (含沪深300/中证500/中证1000/行业指数)。",
        epilog="""\
示例:
  %(prog)s 000300 --limit 10

返回:
  {"source": "akshare-csindex", "count": 300,
   "data": [{"日期": 1785456000000, "指数代码": "000300", "指数名称": "沪深300",
             "成分券代码": "000001", "成分券名称": "平安银行", "权重": 0.8, ...},
            ...]}   # 日期为毫秒时间戳, 权重单位 %
""",
    )
    sp.add_argument("index", help="指数代码, 如 000300/000905/932000")
    sp.add_argument("--limit", type=int, default=0)
    sp.set_defaults(fn=cmd_weight)

    args = p.parse_args()
    try:
        args.fn(args)
    except SystemExit:
        raise  # doctor 用退出码 0/1 表达自检结果
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        die(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()

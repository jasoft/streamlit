#!/usr/bin/env python3
"""fdata 数据源统一客户端.

优先走 `fdata serve` 长连接 (进程内常驻 eltdx client, 连接复用, 无每次初始化开销);
服务器不可用时自动回退到 fdata CLI (subprocess), 保证功能不被单点卡死.

返回结构与 fdata JSON 同构; quote/kline 均归一化到简单结构.
串行客户端单例, 线程安全由调用方 (backend asyncio.to_thread / 条件单 asyncio.to_thread)
保证单协程内调用, 连接复用 + 断线自动重连.

数据来源标识:
  - 服务器模式: source = "eltdx(serve)";  复用同一 TCP 连接
  - CLI 回退:   source = "eltdx(cli)";   每次 subprocess
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FDATA_CLI = REPO / "scripts" / "fdata.py"
_HOST = os.environ.get("FDATA_HOST", "127.0.0.1")
_PORT = int(os.environ.get("FDATA_PORT", "9701"))
_TIMEOUT = float(os.environ.get("FDATA_TIMEOUT", "8"))
_MAX_LINE = int(os.environ.get("FDATA_MAXLINE", 16 << 20))  # 16MB 响应上限

# 防雪崩熔断与限频状态
_last_fallback_warn_time: float = 0.0
_last_cli_call_time: float = 0.0
_cli_call_history: list[float] = []


def _throttle_cli_fallback(action: str) -> None:
    """CLI 回退模式下的防雪崩节流器.

    当 fdata serve 未启动时, 高频轮询 (如 1s tick) 会频繁创建独立 Python 解释器,
    导致严重的系统 CPU 调度风暴 (Load Average 飙升).
    本函数提供:
      1) 15s 频控告警输出
      2) 最小调用间隔平滑节流
      3) 高频突发时的强制背压 (Backpressure)
    """
    global _last_fallback_warn_time, _last_cli_call_time, _cli_call_history
    now = time.time()

    # 1. 周期性告警 (15s 一次, 避免刷屏)
    if now - _last_fallback_warn_time > 15.0:
        _last_fallback_warn_time = now
        print(
            f"[fdata_client] ⚠️ fdata serve (:{_PORT}) 未启动，正在回退到 CLI 模式 ({action})。\n"
            f"             单次 CLI 启动冷开销较大，高频调用建议启动常驻服务:\n"
            f"             uv run python scripts/fdata.py serve --port {_PORT}",
            file=sys.stderr,
            flush=True,
        )

    # 2. 最小调用间隔节流 (至少间隔 100ms)
    elapsed = now - _last_cli_call_time
    if elapsed < 0.1:
        time.sleep(0.1 - elapsed)
        now = time.time()
    _last_cli_call_time = now

    # 3. 滑动窗口限频 (过去 3s 内超过 6 次 CLI 调用则强制背压 300ms)
    _cli_call_history = [t for t in _cli_call_history if now - t < 3.0]
    _cli_call_history.append(now)
    if len(_cli_call_history) > 6:
        time.sleep(0.3)


class ServerUnavailable(Exception):
    """fdata serve 不可用 (未启动/断线), 调用方应回退到 CLI."""


class FdataClient:
    """TCP line-JSON 客户端: 连接复用 + 断线重连."""

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._buf: bytes = b""   # 跨请求复用: 一次 recv 可能多读, 保留供下一行用
        self.source = "unset"

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._buf = b""

    def _recv_line(self) -> str:
        # 直接用 recv 循环读取直到遇到换行符, 避免 socket.makefile() 的
        # BufferedReader 在响应超过 8192 字节时被内部缓冲截断的 bug.
        sock = self._sock
        if sock is None:
            raise ServerUnavailable("socket not connected")
        while b"\n" not in self._buf:
            try:
                chunk = sock.recv(65536)
            except (OSError, socket.timeout) as e:
                raise ServerUnavailable(f"recv failed: {e}") from None
            if not chunk:
                raise ServerUnavailable("empty reply from server")
            self._buf += chunk
            if len(self._buf) > _MAX_LINE:
                raise ServerUnavailable(f"reply too large: {len(self._buf)} bytes")
        line, _, self._buf = self._buf.partition(b"\n")
        return line.strip().decode("utf-8", "replace")

    def request(self, req: dict) -> dict:
        """请求服务器, 失败抛 ServerUnavailable (会重置连接)."""
        if self._sock is None:
            try:
                self._sock = socket.create_connection((_HOST, _PORT), _TIMEOUT)
                self._sock.settimeout(_TIMEOUT)
                self.source = "eltdx(serve)"
            except OSError as e:
                raise ServerUnavailable(f"connect {_HOST}:{_PORT} failed: {e}") from None
        try:
            self._sock.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode())
            return json.loads(self._recv_line())
        except (OSError, socket.timeout, ServerUnavailable):
            self.close()
            raise ServerUnavailable("server gone")
        except (json.JSONDecodeError, ValueError):
            # 响应被截断/损坏: 连接已进入不一致状态, 重置后让上层走 CLI 回退
            self.close()
            raise ServerUnavailable("server returned non-JSON")


_client = FdataClient()


def quote(code: str) -> dict | None:
    """实时快照 -> 平铺 dict {code,last,pre_close,open,high,low,volume,amount,change_pct}.

    与 CLI `quote` 命令同口径, 自动路由所有品种:
      - 股票/ETF/指数 (eltdx 前缀或 6 位数字): 走服务器 eltdx 长连接(高频路径)
      - 期货 (rb/IF0)、8位期权、基金 (004075.of)、外盘 (@/.): 自动降级委托 CLI quote
        路由到对应数据源, 对调用方透明.

    无成交或无数据时 last 可能为 None, 返回该 dict (勿误判).
    """
    resp = _request({"op": "quote", "code": code}, ["quote", code])
    q = _flat_quote(resp)
    if q is not None:
        return q
    # 非 eltdx 类型 (期货/基金/期权/外盘): 委托 CLI quote 自动路由, 结构与 CLI 一致
    return _flat_quote(cli(["quote", code]))


def kline(code: str, period: str = "day", kind: str = "stock",
          adjust: str | None = None, limit: int | None = None) -> list[dict]:
    """K 线 bars 列表 (升序), 每条 {date,open,high,low,close,volume,amount}."""
    req: dict = {"op": "kline", "code": code, "period": period,
                 "kind": kind, "limit": limit}
    if adjust:
        req["adjust"] = adjust
    cli_cmd = ["kline", code, "--period", period, "--kind", kind]
    if adjust:
        cli_cmd += ["--adjust", adjust]
    # CLI 默认 --limit 30; 统一语义: limit 为空 => 全历史 (与 serve 缺失 limit 一致), 传 --limit 0
    cli_cmd += ["--limit", str(limit if limit else 0)]
    resp = _request(req, cli_cmd)
    if not resp:
        return []
    # serve: {"ok": True, "result": {"code","kind","count","data":[...]}}
    # serve ok=False: {"ok": False, "error": "..."}
    # CLI fallback: {"data":[...]}  或 {"result": {"data":[...]}}
    if resp.get("ok") is False and resp.get("error"):
        raise RuntimeError(f"fdata kline {code} {period} failed: {resp['error']}")
    if resp.get("ok"):
        result = resp.get("result") or {}
        if isinstance(result, dict) and "data" in result:
            return result["data"]
    # CLI fallback 路径: 直接有 data key, 或 result.data
    if "data" in resp and isinstance(resp["data"], list):
        return resp["data"]
    result = resp.get("result")
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return []


def is_server_available() -> bool:
    """快速探测 fdata serve 是否在线."""
    try:
        s = socket.create_connection((_HOST, _PORT), timeout=0.5)
        s.close()
        return True
    except OSError:
        return False


def cli(argv: list[str], timeout: int = 180) -> dict:
    """通用子命令 (期货/基金/期权/新闻等): 与 fdata CLI 输出完全一致.

    服务器模式 -> 透传 fdata 自身 CLI; 服务器不可用 -> 本地 subprocess 回退.
    argv 如 ["futures", "rb"] / ["news"] / ["na", "004075"].
    """
    argv = [str(a) for a in argv]
    req: dict = {"op": "cli", "argv": argv, "timeout": timeout}
    try:
        resp = _client.request(req)
    except ServerUnavailable:
        resp = None  # 走 CLI 回退
    if resp is not None:
        if resp.get("ok"):
            _client.source = f"{argv[0]}(serve)"
            return resp["result"]
        raise RuntimeError(f"fdata server cli {argv[0]} failed: {resp.get('error')}")
    _throttle_cli_fallback(f"cli {argv[0] if argv else ''}")
    r = subprocess.run([sys.executable, str(FDATA_CLI), *argv],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"fdata CLI failed (src={argv[0]}): {r.stderr.strip()[:300]}")
    _client.source = f"{argv[0]}(cli)"
    return json.loads(r.stdout)


def _request(req: dict, cli: list[str]) -> dict | None:
    try:
        return _client.request(req)
    except ServerUnavailable:
        pass  # 回退 CLI
    _throttle_cli_fallback(f"{cli[0]} {cli[1] if len(cli) > 1 else ''}")
    r = subprocess.run([sys.executable, str(FDATA_CLI), *cli],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"fdata CLI failed (src={cli[0]}): {r.stderr.strip()[:300]}")
    _client.source = "eltdx(cli)"
    return json.loads(r.stdout)


def _flat_quote(resp: dict | None) -> dict | None:
    if not resp:
        return None
    # serve: {"ok": true, "result": {code,type,name,quote:{...}}}
    if resp.get("ok") and isinstance(resp.get("result"), dict):
        r = resp["result"]
        q = r.get("quote", {})
        return {"code": r.get("code"), "type": r.get("type"), "name": r.get("name"),
                "last": q.get("last"), "pre_close": q.get("pre_close"),
                "open": q.get("open"), "high": q.get("high"), "low": q.get("low"),
                "volume": q.get("volume"), "amount": q.get("amount"),
                "change_pct": q.get("change_pct")}
    # CLI: {"data":[{code,type,name,quote:{...}}]}
    data = resp.get("data") or (resp.get("result") or {}).get("data", [])
    if data:
        item = data[0]
        q = item.get("quote", {})
        return {"code": item.get("code"), "type": item.get("type"), "name": item.get("name"),
                "last": q.get("last"), "pre_close": q.get("pre_close"),
                "open": q.get("open"), "high": q.get("high"), "low": q.get("low"),
                "volume": q.get("volume"), "amount": q.get("amount"),
                "change_pct": q.get("change_pct")}
    return None
#!/usr/bin/env python3
"""fdata 数据源统一客户端.

走 `fdata serve` 长连接 (进程内常驻 eltdx client, 连接复用, 无每次初始化开销);
serve 不可用直接抛 ServerUnavailable, 不回退 CLI (CLI 每次 subprocess 冷开销大, 仅供 Agent 用).

返回结构与 fdata JSON 同构; quote/kline 均归一化到简单结构.
串行客户端单例, 线程安全由调用方 (backend asyncio.to_thread / 条件单 asyncio.to_thread)
保证单协程内调用, 连接复用 + 断线自动重连.

数据来源标识:
  - eltdx 类型: source = "eltdx(serve)";  复用同一 TCP 连接
  - 非 eltdx 透传: source = "<argv[0]>(serve)";  走 serve 的 cli op
"""
from __future__ import annotations

import json
import os
import socket
import threading

_HOST = os.environ.get("FDATA_HOST", "127.0.0.1")
_PORT = int(os.environ.get("FDATA_PORT", "9701"))
_TIMEOUT = float(os.environ.get("FDATA_TIMEOUT", "8"))
_MAX_LINE = int(os.environ.get("FDATA_MAXLINE", 16 << 20))  # 16MB 响应上限


class ServerUnavailable(Exception):
    """fdata serve 不可用 (未启动/断线); 直接抛错, 不回退 CLI (CLI 开销大仅供 Agent 用)."""


class FdataClient:
    """TCP line-JSON 客户端: 连接复用 + 断线重连 + 线程锁.

    全局单例被 backend 多线程 (asyncio.to_thread) 和 tick 循环并发调用,
    单个 socket 的 send/recv 必须串行, 否则响应 buffer 互相污染导致
    "server returned non-JSON" / "server gone" 假阳性.
    """

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._buf: bytes = b""   # 跨请求复用: 一次 recv 可能多读, 保留供下一行用
        self._lock = threading.Lock()
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
        """请求服务器, 失败抛 ServerUnavailable (会重置连接).

        全程持锁: sendall + recv 必须原子, 否则多线程交叉 send 会使
        对应 recv 读到别条请求的响应, 导致 non-JSON / buffer 污染.
        """
        with self._lock:
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
                # 响应被截断/损坏: 连接已进入不一致状态, 重置后下条请求自动重连
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
    resp = _request({"op": "quote", "code": code})
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
    resp = _request(req)
    if not resp:
        return []
    # serve: {"ok": True, "result": {"code","kind","count","data":[...]}}
    # serve ok=False: {"ok": False, "error": "..."}
    if resp.get("ok") is False and resp.get("error"):
        raise RuntimeError(f"fdata kline {code} {period} failed: {resp['error']}")
    if resp.get("ok"):
        result = resp.get("result") or {}
        if isinstance(result, dict) and "data" in result:
            return result["data"]
    # 兜底: 直接有 data key, 或 result.data (serve cli op 透传格式)
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
    """通用子命令 (期货/基金/期权/新闻等): 走 serve 的 cli op 透传, 与 fdata CLI 输出一致.

    serve 不可用直接抛 ServerUnavailable (不回退本地 subprocess, CLI 开销大仅供 Agent 用).
    argv 如 ["futures", "rb"] / ["news"] / ["na", "004075"].
    """
    argv = [str(a) for a in argv]
    req: dict = {"op": "cli", "argv": argv, "timeout": timeout}
    resp = _client.request(req)
    if resp.get("ok"):
        _client.source = f"{argv[0]}(serve)"
        return resp["result"]
    raise RuntimeError(f"fdata server cli {argv[0]} failed: {resp.get('error')}")


def _request(req: dict) -> dict:
    """请求 serve, 不可用直接抛 ServerUnavailable (不回退 CLI, CLI 开销大仅供 Agent 用)."""
    return _client.request(req)


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
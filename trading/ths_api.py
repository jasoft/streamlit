#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同花顺交易 HTTP API (ths_trade.py 的 RESTful 包装, 自带 Swagger 文档)

定位: 让任意 HTTP 客户端 (curl / 前端 / 策略进程 / 手机快捷指令) 都能调用
ths_trade.py 的全部子命令, 不必直接跑 CLI. 每个请求 fork 一个
`python trading/ths_trade.py <cmd>` 子进程, 复用与 backend/main.py /api/positions
相同的 subprocess 模式 — 与 CLI 的 AX 实现完全隔离, CLI 崩溃/改版不影响 API 进程.

并发控制: 同花顺交易面板是单一有状态 GUI (买卖面板共用一套输入框, tab 会互切),
并发调用必然互相踩踏. 因此所有端点共用一把全局 asyncio.Lock 串行执行;
请求多时排队 (FastAPI 默认无锁竞争上限, 客户端侧请自行控频).

启动:
  uv run uvicorn trading.ths_api:app --host 127.0.0.0 --port 8010
  # 或
  uv run python trading/ths_api.py            # 端口读 THS_API_PORT, 默认 8010

Swagger: http://127.0.0.1:8010/docs   (ReDoc: /redoc, OpenAPI JSON: /openapi.json)

前置条件 (与 ths_trade.py 相同):
  1. 同花顺 Mac 客户端已启动并显示主窗口
  2. 运行本服务的终端 App 已在 系统设置 -> 隐私与安全性 -> 辅助功能 中勾选
  3. .env 配置 THS_USER / THS_PASS (自动登录) 与 THS_BARK_KEY (可选推送)

安全提示: 交易类端点会真实下单. 默认绑定 127.0.0.1 仅本机可访问;
  如需局域网访问请自行加鉴权 (反向代理 / API Key 中间件).

端点一览:
  GET    /health                        服务与同花顺客户端存活检查
  POST   /session/login                 检测/触发自动登录
  POST   /session/account               切换账户 (real/sim)
  GET    /positions                     持仓表 (~200ms 高频路径)
  GET    /orders                        今日委托表 (原始行列)
  GET    /orders/status                 委托状态推断 (filled/cancelled/...)
  GET    /trades                        成交表
  GET    /funds                         资金明细
  POST   /orders                        下单 (buy/sell, 支持 dry_run)
  DELETE /orders/{id}                   撤单 (合同编号 / 证券代码 / all)
"""
import asyncio
import json
import os
import subprocess
import sys

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THS_TRADE = os.path.join(REPO_ROOT, "trading", "ths_trade.py")

app = FastAPI(
    title="THS Trade HTTP API",
    description=(
        "同花顺 Mac 客户端 GUI 自动交易的 RESTful 包装。\n\n"
        "所有端点内部调用 `trading/ths_trade.py` 子进程并回传其 JSON 输出; "
        "GUI 操作被全局锁串行化, 并发请求会排队。\n\n"
        "⚠️ POST /orders 与 DELETE /orders/{id} 会**真实下单/撤单**, "
        "测试请先传 `dry_run=true` 或切到模拟账户 "
        "(`POST /session/account {\"target\": \"sim\"}`)。"
    ),
    version="1.0.0",
)

# 全局串行锁: 同花顺面板单一有状态, 买卖/查询/切账户互斥
_gui_lock = asyncio.Lock()

# 单次子进程超时秒数 (cancel/login/switch-account 最慢 ~25s, 留足余量)
CMD_TIMEOUT = float(os.environ.get("THS_API_TIMEOUT", "90"))


# ---------------------------------------------------------------------------
# 子进程执行
# ---------------------------------------------------------------------------

async def run_ths(*cli_args: str) -> dict:
    """跑一条 ths_trade.py 子命令, 返回其 JSON 输出.

    错误映射: 参数错 -> 400; THS 未运行/窗口读不到 -> 503;
    超时 -> 504; 输出非 JSON -> 502 (附 stderr 尾部).
    """
    cmd = [sys.executable, THS_TRADE, *cli_args]
    async with _gui_lock:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=REPO_ROOT,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as e:
            raise HTTPException(503, f"无法启动子进程: {e}")
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=CMD_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise HTTPException(504, f"ths_trade 执行超时 (>{CMD_TIMEOUT:.0f}s): {' '.join(cli_args)}")

    stdout, stderr = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    if proc.returncode == 0:
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            raise HTTPException(502, detail={
                "error": "ths_trade 输出不是合法 JSON",
                "stdout_tail": stdout[-500:], "stderr_tail": stderr[-500:]})
    # argparse 用法错误 exit 2; SystemExit(str) exit 1 (信息在 stderr)
    detail = {"exit_code": proc.returncode,
              "stderr_tail": stderr[-500:], "stdout_tail": stdout[-500:]}
    if proc.returncode == 2:
        raise HTTPException(400, detail=detail)
    raise HTTPException(503, detail=detail)


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class PlaceOrderRequest(BaseModel):
    """下单请求 (等价于 CLI buy/sell). 提交后仅表示券商受理, 成交与否用
    GET /orders/status 轮询确认."""
    side: str = Field(..., description="买卖方向", json_schema_extra={"examples": ["buy"]})
    code: str = Field(..., min_length=6, max_length=6,
                      description="6 位证券代码, 如 601899 (A股) / 513120 (ETF)",
                      json_schema_extra={"examples": ["601899"]})
    qty: int = Field(..., gt=0, description="委托数量 (股), 100 的倍数")
    price: float | None = Field(None, gt=0,
                                description="限价; 缺省用代码联动带出的对手价 (买一/卖一)")
    dry_run: bool = Field(False, description="只填单不提交, 用于测试联动与填单")
    account: str | None = Field(None, description="执行前切换账户: real/A股 或 sim/模拟")
    keyboard: bool = Field(False, description="AX 联动失效时的键盘输入备用路径 (需同花顺前台)")
    timeout: float = Field(5.0, gt=0, description="等确认框/结果框超时秒数")
    bark_on_reject: bool = Field(False, description="券商明确拒绝时也 Bark 推手机")


class SwitchAccountRequest(BaseModel):
    """账户切换请求 (等价于 CLI switch-account). 幂等: 已在目标账户时无副作用."""
    target: str = Field(..., description="real/A股=真实账户, sim/模拟=模拟账户",
                        json_schema_extra={"examples": ["real"]})
    timeout: float = Field(25.0, gt=0, description="登录+切换合计超时秒数")


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------

@app.get("/health", tags=["系统"])
async def health():
    """服务存活 + 同花顺客户端是否在运行 (只查进程, 不做 AX 操作, 无需锁)."""
    ths_running = False
    try:
        import AppKit
        ths_running = bool(AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(
            "cn.com.10jqka.macstock"))
    except Exception:
        pass  # AppKit 不可用时不影响 /health 本身
    return {"ok": True, "ths_running": ths_running,
            "hint": None if ths_running else "同花顺未运行, 请先启动客户端"}


# ---------------------------------------------------------------------------
# 会话 (登录 / 账户)
# ---------------------------------------------------------------------------

@app.post("/session/login", tags=["会话"])
async def login():
    """检测登录状态, 未登录则用 .env 的 THS_USER/THS_PASS 自动登录.

    交易/查询端点执行前已自动做登录检测, 本端点一般只在开机初始化或掉线后手动调用.
    返回: `{ok, msg}`.
    """
    return await run_ths("login")


@app.post("/session/account", tags=["会话"])
async def switch_account(body: SwitchAccountRequest):
    """登录检查 + 切换到指定账户 (低频维护, 幂等).

    高频查询端点 (positions/orders/trades/funds) 不切账户; 保持正确账户
    靠本端点定时调用 (如每 30s 一次). 返回
    `{ok, target, mapped_tab, login_performed, account_before, account_after, msg, elapsed_ms}`.
    """
    return await run_ths("switch-account", body.target, "--timeout", str(body.timeout))


# ---------------------------------------------------------------------------
# 查询 (高频快速路径, ~200ms, 不做登录判断/不切账户/不扫弹窗)
# ---------------------------------------------------------------------------

async def _read_table(table: str):
    data = await run_ths(table)
    return {**data, "account_hint": "高频路径不返回账户名; 用 POST /session/account 维护"}

for _table, _cn, _desc in [
    ("positions", "持仓", "持仓表 (证券代码/名称/持仓数量/可用数量/成本价/当前价/盈亏...)"),
    ("orders", "委托", "今日委托表原始行列 (无显式状态列, 需状态请用 GET /orders/status)"),
    ("trades", "成交", "今日成交表"),
    ("funds", "资金明细", "资金明细表"),
]:
    @app.get(f"/{_table}", tags=["查询"], name=f"查询{_cn}",
             description=f"{_desc}. 高频优化路径 (~200ms): 不检查登录 / 不切账户 / 不扫弹窗. "
                         f"rows 中数值均为字符串, 需自行 float() 转换.")
    async def _endpoint(t=_table):
        return await _read_table(t)


@app.get("/orders/status", tags=["查询"])
async def order_status(code: str | None = None, contract: str | None = None,
                       account: str | None = None):
    """委托状态查询 (带推断): filled / cancelled / rejected / partial / pending.

    读同一张委托表 (~400ms), 在原始数据之上按 备注+成交价格+撤销数量+委托属性 推断状态,
    输出 `status_map: {合同编号: {status, filled_qty, avg_price}}`.

    - `code`: 按证券代码过滤今日全部委托
    - `contract`: 按合同编号精确查单笔 (下单后轮询成交用这个)
    - 都不传则返回今日全部委托及其状态
    - 轮询场景建议不传 account (省去切账户耗时), 账户正确性交给 /session/account 定时兜底
    """
    args = ["query_order"]
    if contract:
        args += ["--contract", contract]
    elif code:
        args += ["--code", code]
    if account:
        args += ["--account", account]
    return await run_ths(*args)


# ---------------------------------------------------------------------------
# 交易 (真实下单!)
# ---------------------------------------------------------------------------

@app.post("/orders", tags=["交易"], status_code=201)
async def place_order(body: PlaceOrderRequest):
    """买入/卖出证券. 填代码 → 联动带出对手价 → 填价格/数量 → 提交 → 确认 → 读结果.

    返回的 `ok=true` 仅表示**委托被券商受理** (无失败弹窗), 是否成交需用
    GET /orders/status?contract=<合同编号> 轮询; `result_text` 为券商返回弹窗文本
    (null = 无弹窗即成功), `fill` 为实际填入面板的值, `steps` 为各步耗时 ms.

    失败场景: 代码联动失败 (面板异常/AX 失效), 确认框代码不符, 券商拒绝
    (余额不足/废单等, 见 result_text). 点完提交但未等到确认框时订单命运未知,
    CLI 会自动 Bark 推送 (需配 THS_BARK_KEY).
    """
    if body.side not in ("buy", "sell"):
        raise HTTPException(400, f"side 必须是 buy 或 sell, 得到 {body.side!r}")
    args = [body.side, body.code, str(body.qty)]
    if body.price is not None:
        args += ["--price", str(body.price)]
    if body.dry_run:
        args.append("--dry-run")
    if body.keyboard:
        args.append("--keyboard")
    if body.account:
        args += ["--account", body.account]
    if body.bark_on_reject:
        args.append("--bark-on-reject")
    args += ["--timeout", str(body.timeout)]
    return await run_ths(*args)


@app.delete("/orders/{ident}", tags=["交易"])
async def cancel_order(ident: str, account: str | None = None,
                       timeout: float = 5.0):
    """撤销委托. `ident` 三种取值:

    - 合同编号: 撤指定一笔 (如 `1140009957`)
    - 证券代码: 撤该代码的全部可撤委托 (如 `601899`)
    - `all`: 撤全部可撤委托 (走"全撤"按钮, 最可靠)

    单笔走双击委托行 (需同花顺前台, 脚本会自动激活窗口), 失败自动回退"全撤".
    撤完自动重读委托表复核. 返回 `{ok, cancelled, failed, remaining}`.
    """
    if ident == "all":
        args = ["cancel", "--all"]
    elif ident.isdigit() and len(ident) == 6:
        args = ["cancel", "--code", ident]
    else:
        args = ["cancel", "--contract", ident]
    if account:
        args += ["--account", account]
    args += ["--timeout", str(timeout)]
    return await run_ths(*args)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("THS_API_HOST", "127.0.0.0"),
                port=int(os.environ.get("THS_API_PORT", "8010")))

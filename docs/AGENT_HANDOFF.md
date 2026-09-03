# 项目交接文档 — 量化交易系统

> 生成于 2026-09-02。本文档汇总项目全部协作历史与决策结论，供下一个 Agent 无缝接手。
> **上手路径**：先看「一、项目演进史」定位当前阶段 → 读「二、当前架构」理解六大子系统 → 查「九、常见坑速查」对应问题 → 查「八、硬约束 13 条」确认不违规。
> **完整详细手册见项目根目录 `SKILL.md`（863 行 17 章）**，本文件是精简交接版 + 演进脉络。

---

## 一、项目是什么 & 演进史

项目路径: `/Users/weiwang/Projects/streamlit`。从 Streamlit 行情面板演进为**完整的个人量化交易全链路**（六大子系统）。用户场景：A 股/ETF 个人量化，低频日线策略 + 日内做 T 探索，实盘账户国泰海通（同花顺 Mac 客户端）+ 模拟练习账户。

演进里程碑（按时间倒序，最新优先）：

**阶段 7 — 前端重写 + 后端 API 化（2026-09-01 ~ 09-02，当前主架构）**
- 前端从 Streamlit 实盘页 → **Next.js 15 + ECharts 6** 重写，路由 `/charts` / `/backtest` / `/config`；根路径 `/` 重定向到 `/charts`（旧 `/live` 实盘策略页面已删除）
- 后端新建 `backend/main.py`（FastAPI）：REST `/api/*` + WebSocket `/ws/market`（每秒tick推送）+ `/ws/mock_stream`（流式测试逐根推bar）
- 图表会话持久化到 `backend/charts.db`（SQLite），跨浏览器/设备共享
- 前端新增全局数字格式化 `frontend/src/lib/fmt.ts`（股价三位小数/金额千分位/百分比两位 + 参数统计中英文转换）
- 启动脚本 `dev.sh` 一键起后端+前端（Ctrl-C 同停），支持 BACKEND_PORT/FRONTEND_PORT 自定义

**阶段 6 — ths_trade 高速查询优化 + 账户切换拆分（2026-09-02）**
- positions/orders/trades/funds 从 ~1.3s 优化到 ~220ms（6x）：深度限制 DFS 直接 AXTable 语义定位 + 30ms 轮询 AXRow 稳定性替代固定 sleep(0.5)
- 高频命令和低频操作彻底分离：查询命令**无 flag 零冗余检查**（去掉 --account/--no-login/登录校验），新增独立 `switch-account real/sim` 子命令负责切账户+自动登录，放周期性调度不影响 API 性能
- 下单失败 Bark 通知：券商拒单结果弹窗（含"警告/错误/失败/不足/不允许/拒绝"）或提交后状态未知超时 → 通过 Bark key `THS_BARK_KEY` 立即推送到用户手机

**阶段 5 — fdata 长连接 + 统一客户端（2026-09-02）**
- fdata.py 新增 `serve --port 9701` 子命令：TCP line-JSON 协议，进程内常驻单个 eltdx client 连接复用，全局锁并发安全，自动断线重连
- 新建 `strategy/fdata_client.py` 统一客户端：优先走 serve 长连接（source="eltdx(serve)"），失败自动回退 subprocess CLI（"eltdx(cli)"），调用方完全无感
- `quote()` 自动路由全部品种：股票/ETF/指数 → serve 高频长连接；期货/基金/期权/外盘 → 自动降级 CLI quote 路由
- 多客户端并发支持：asyncio.start_server 每连接独立协程；非 eltdx 类型通用 `cli` 请求类型透传 fdata 自身 CLI，字节级一致

**阶段 4 — 条件单引擎（2026-09-02）**
- `trading/condition_orders.py`：多条件单 `asyncio.gather` 异步并发，每个单用独立状态机
- 状态：`WATCH`（开盘前3分钟内跌破阈值买）→ `ARMED`（等待反弹卖）→ `DONE`（结束）
- 行情入口：统一走 `fdata_client.quote(last/pre_close)`；同花顺行情连接未恢复时保持 WATCH 不下单
- 记账复用 runtime 层（Portfolio + Broker + ctx.submit_order），支持 SimulatedBroker / LiveBroker

**阶段 3 — runtime 三层重构 + vectorbt 回测（2026-08-31 ~ 09-01）**
- `strategy/runtime/` 三层架构：`Portfolio`（现金/持仓target与qty分离/订单/state.json）+ `Context`（策略看到的世界）+ `Broker`（Backtest/Simulated/Live 三种实现）
- 订单状态机：`pending_submit → submitted → partial_filled → filled | cancelled | rejected`，轮询模型监控
- `strategy/backtest/vbt_adapter.py` 取代原 `engine.py` 成为主回测路径：strategy.signal → entries/exits shift(1) + price=open → vbt.Portfolio（收盘出信号次日开盘成交，无未来函数）
- Strategy 基类双接口：`signal(df, params)`（向量化，给 vbt 回测/优化）+ `on_bar(bar, ctx)`（事件驱动，给实盘），默认 on_bar 自动桥接 signal；`target_position` 是旧名保留别名

**阶段 2 — ths_trade 自动交易闭环（2026-08-30）**
- macOS AX API 纯后台驱动同花顺 Mac 客户端，代码框聚焦→填值联动带出对手价，绝不激活窗口前台（防反自动化封禁 -25212）
- 子命令：buy/sell/cancel/positions/orders/trades/funds/login + --dry-run，JSON 输出
- 自动登录：.env THS_USER/THS_PASS，断连后抢按钮（轮询 0.05s）

**阶段 1 — 数据源迁移 + strategy 初建 + Streamlit 看板（2026-08-30）**
- fdata.py 统一 CLI：股票/ETF/指数 → eltdx 通达信 7709；期货 → tqsdk/新浪 nf_；ETF 期权 → 新浪 CON_OP_；基金 → 东财
- strategy/ 雏形：target_position + engine.py 自研回测 + trader.py 实盘 + dashboard.py（Streamlit 三页看板）
- stockview/：旧 Streamlit 行情 Dashboard（保留，非主流程），市场面板/五因子信号/资金流向/行业权重/拥挤度等

---

## 二、当前架构（六大子系统，9/2 最新）

```
┌───────────────────────────────────────────────────────────────────┐
│  Next.js 前端 (:3001)        后端 rewrite 结果, 替代 Streamlit 看板  │
│  /charts 多图会话 + 策略挂载 + 流式测试 (KLineChart/IntradayChart)  │
│  /backtest 批量回测 + 参数优化       /config 策略参数配置            │
│  ECharts 6 canvas setOption 增量更新 (无闪烁)                       │
│  fmt.ts 统一格式化 / api.ts REST / ws.ts MarketWs+MockStreamWs     │
└────────────┬───────────────────────────────────────┬───────────────┘
     rewrite │ /api/* /ws/*                          │ next.config.ts
             ▼                                       ▼
┌───────────────────────────────────────────────────────────────────┐
│  FastAPI 后端 (:8000)    backend/main.py (15+ REST + 2 WebSocket) │
│  全部阻塞调用 asyncio.to_thread() 包装, 绝不卡事件循环              │
│  /api/backtest + vbt_adapter  │  /api/kline /api/intraday         │
│  /api/positions (ths_trade)   │  /api/charts/sessions (SQLite)    │
│  /ws/market 每秒 tick (默认 sz159915, sh510300, sh000001)         │
│  /ws/mock_stream 流式逐根 bar + 实时策略 + paper 成交              │
└────────┬──────────────────────┬──────────────────────┬────────────┘
         │ 取数                  │ 策略                 │ 下单
         ▼                       ▼                      ▼
┌──────────────────┐  ┌──────────────────┐  ┌────────────────────────┐
│ fdata 数据网关    │  │ strategy 策略层   │  │ ths_trade 同花顺交易    │
│ trading/fdata.py  │  │ base.py 双接口   │  │ trading/ths_trade.py    │
│  serve模式 9701   │  │ runtime/ 三层    │  │ AX API 后台 ~220ms 查询 │
│  TCP 长连接复用   │  │ vbt_adapter.py   │  │ switch-account 切账户   │
│  CLI 自动回退     │  │ 零注册自动发现    │  │ Bark 通知下单失败      │
│ fdata_client.py   │  │ config.json 配置 │  │ buy/sell/cancel/…      │
└──────────────────┘  └──────────────────┘  └────────────────────────┘
         ▲                                               ▲
         └──────────── condition_orders.py ─────────────┘
                    条件单引擎: WATCH → ARMED → DONE
```

---

## 三、快速启动

```bash
cd /Users/weiwang/Projects/streamlit
uv sync                         # 装 Python 依赖 (首次)
cd frontend && npm install       # 装前端依赖 (首次)

# 终端1: 启 fdata 长连接 (可选, 不启自动走 CLI 慢些)
uv run python trading/fdata.py serve --port 9701

# 终端2: 一键启前后端
./dev.sh          # 后端 :8000 + 前端 :3001, Ctrl-C 同停
# 访问 http://localhost:3001 → /charts
```

验证：
```bash
curl http://localhost:8000/api/health                              # 健康
curl "http://localhost:8000/api/kline?symbol=sz159915&tf=day&limit=3" # K线
uv run python trading/fdata.py quote sz159915                      # 快照
uv run python trading/ths_trade.py positions                        # 同花顺持仓 (需开着)
uv run python trading/condition_orders.py --poll 5                 # 条件单模拟
```

---

## 四、目录结构速查（9/2）

```
/Users/weiwang/Projects/streamlit/
├── SKILL.md                  ★ 863 行完整手册 (给 AI Agent 接手用)
├── dev.sh                    一键启 FastAPI(:8000) + Next.js(:3001), Ctrl-C 同停
├── pyproject.toml            Python 依赖 (uv sync)
├── Dockerfile / nginx.conf / deploy.sh / start.sh   生产部署 (远期, 未闭环)
│
├── frontend/                 ★ Next.js 15 前端 (核心 UI)
│   ├── next.config.ts        rewrite: /api/*→:8000/api/*, /ws/*→:8000/ws/*
│   └── src/
│       ├── app/page.tsx      根 / → 重定向 /charts
│       ├── app/charts/page.tsx   多图会话 + 策略挂载 + 流式测试 + 账户快照
│       ├── app/backtest/page.tsx 批量回测 + 参数网格
│       ├── app/config/page.tsx   策略参数配置 (写 config.json)
│       ├── components/KLineChart.tsx     ECharts K线 (拖拽缩放/双轨日期marker)
│       ├── components/IntradayChart.tsx  分时 (白价格线+黄VWAP+染色量柱)
│       ├── components/ParamForm.tsx       schema 驱动参数表单
│       ├── lib/api.ts  REST 封装
│       ├── lib/ws.ts   MarketWs (tick推送, 自动重连) + MockStreamWs (流式)
│       └── lib/fmt.ts  ★ 全局格式化: fmtPrice/fmtMoney/fmtPct/fmtNum/paramCn/statCn/formatStat
│
├── backend/                  ★ FastAPI 后端
│   ├── main.py               全部 REST + WebSocket 端点 (~600 行)
│   ├── store.py              图会话 SQLite 持久化 (charts/sessions)
│   └── charts.db             SQLite 数据库
│
├── strategy/                 ★ 策略框架
│   ├── base.py               Strategy 基类: signal() 向量化 + on_bar() 事件驱动 双接口
│   ├── registry.py           策略自动发现 (零注册)
│   ├── config.py             config.json 读写
│   ├── manager.py            策略进程 start/stop/status CLI
│   ├── fdata_client.py       ★★ 统一取数入口 (serve 优先/CLI 回退/自动路由全品种)
│   ├── trader.py             旧实盘执行器 (取数入口已迁 fdata_client)
│   ├── mock_market.py        Mock 市场 (流式测试逐根 advance_bar)
│   │
│   ├── backtest/vbt_adapter.py  ★ vectorbt 回测适配器 (主回测路径, 取代 engine.py)
│   │                           entries/exits shift(1) + price=open → 次日开盘成交
│   ├── runtime/              ★ 策略运行时三层
│   │   ├── portfolio.py      Portfolio: 现金/持仓(qty实 vs target意图分离)/订单/state.json
│   │   ├── ctx.py            Context: 策略看到的世界 (history/qty_for/submit_order)
│   │   ├── broker.py         三种 Broker: Backtest(待次日open) / Simulated(立即fill) / Live(ths_trade+轮询)
│   │   └── runner.py         Runner.run_live 事件驱动单轮执行
│   │
│   └── strategies/           放进去自动注册
│       ├── ma20_trend.py     MA20 趋势 (日线)
│       ├── sma_cross.py      SMA 金叉死叉 (日线)
│       ├── intraday_t.py     日内做 T (5m, 参数待调优)
│       └── tick_buy_sell.py  tick 级策略模板
│
├── scripts/
│   ├── fdata.py              ★★ 统一金融数据 CLI + TCP serve 模式 (12+ 子命令)
│   ├── ths_trade.py          ★★ 同花顺 Mac AX 自动交易 (高频查询~220ms, switch-account独立)
│   ├── condition_orders.py   ★ 条件单引擎 (WATCH→ARMED→DONE, 开盘3分钟窗口)
│   ├── check_window.py / stress_test.py / extreme_fill_test.py  调试/压测脚本
│   └── hs300_industry_weight.py / example_tick_strategy.py / test_realtime_rate.py
│
├── stockview/                旧 Streamlit 行情仪表盘 (保留, 非主流程)
│   └── app.py / etf_signal.py / fund_flow.py / options.py / ...  20+ 分析模块
│
├── nextjs/                   独立实验项目 (资金流向页, 非主系统, 忽略)
├── tests/                    单测 (test_strategy_system.py 等)
├── datasource_test/          2026-08-30 数据源选型实测 REPORT.md + 脚本 + 结果
├── skills/financial-data/    旧 skill 参考 (已被 SKILL.md 取代)
└── docs/
    ├── AGENT_HANDOFF.md      本文件 (精简交接版 + 演进脉络)
    └── quant-framework-report.md   vectorbt/backtrader 调研报告 (参考)
```

---

## 五、数据层要点（fdata）

**规则：一切行情/K线只走 strategy/fdata_client.py，不许直连 eltdx/akshare。**

```python
from strategy import fdata_client

# 实时快照: 自动路由所有品种 (股票/ETF/指数→serve; 期货/期权/基金→CLI回退)
q = fdata_client.quote("601899")   # → {code, name, type, last, pre_close, change_pct, source, ...}

# K线: limit=None → 全历史 (传 --limit 0)
bars = fdata_client.kline("sz159915", period="5m", kind="stock", adjust="qfq")

# 通用 CLI 透传
result = fdata_client.cli(["futures", "rb2610", "--tq"])
```

启动长连接服务器（推荐，快 5-10 倍）：
```bash
uv run python trading/fdata.py serve --port 9701
```

fdata_client 内部：先连 127.0.0.1:9701 → 失败自动回退 subprocess CLI。source 字段标 "eltdx(serve)" / "eltdx(cli)"。

数据源选型（别换，踩过坑的结论）：

| 品类 | 稳定方案 | 别用 |
|---|---|---|
| 股票/ETF/指数 快照+K线 | **eltdx**（通达信7709，`TdxClient(timeout=5)`） | 东财 push2 / pytdx/mootdx（7727端口已死） |
| 商品/金融期货实时 | tqsdk（免费账户五档） | eltdx 不覆盖期货 |
| ETF 期权实时 | 新浪 CON_OP_ 批量直连 | tqsdk（付费墙） |
| 商品期权实时 | tqsdk | — |
| 基金净值 | 东财 fund 域名 | — |
| 全球快讯/中证权重/Greeks | akshare（低频） | — |

eltdx 关键要点：指数 K 线 `kind="index"`；分钟线上限 800，用 `limit=0` 全量后本地截断；`timeout=5` 不激进。

---

## 六、交易层要点（ths_trade.py + condition_orders.py）

### ths_trade 子命令（高频 vs 低频彻底分离）

```bash
# ★ 高频查询 (~220ms, 零 flag, 零检查) — 给 /api/positions 等用
uv run python trading/ths_trade.py positions   # 持仓
uv run python trading/ths_trade.py orders      # 委托
uv run python trading/ths_trade.py trades      # 成交
uv run python trading/ths_trade.py funds       # 资金明细

# ★ 低频切账户 / 登录 (独立子命令, 单独调度, 不影响高频)
uv run python trading/ths_trade.py switch-account real   # → A股实盘
uv run python trading/ths_trade.py switch-account sim    # → 模拟练习
# 功能: 读 .env THS_USER/THS_PASS, 未登录自动登录 → 切 tab → 校验

# 下单 (不给 --price 就用联动带出对手价, 不许清空价格框!)
uv run python trading/ths_trade.py buy 601899 100 --dry-run
uv run python trading/ths_trade.py sell 601899 100 --price 35.00

# 撤单 (优先"全撤"按钮方式; 双击行不可靠时自动回退)
uv run python trading/ths_trade.py cancel --all
```

**AX 核心机制（违反就废单/封接口）**：代码框先 `AXFocused=True` 再填值才触发联动；**绝不激活同花顺窗口到前台**（触发 -25212 封禁，重启客户端恢复）。

**下单失败 Bark 通知必发的两种场景**：
1. 券商结果弹窗含失败词（警告/错误/失败/不足/不允许/拒绝）
2. 点了确认但等不到对话框（状态未知超时）
- 从 `.env` 读 `THS_BARK_KEY`；无 key / 网络失败 → 静默跳过，不影响交易主流程
- 通知内容必须带：方向+代码+数量+价格 + 券商原始返回

### 条件单引擎

编辑 `trading/condition_orders.py` 顶部列表：
```python
CONDITION_ORDERS = [{
    "id": "co_601899_gap",
    "symbol": "601899",
    "trigger_gap_pct": -4.0,    # 相对昨收跌4% → 触发买入
    "buy_qty": 1000,
    "sell_rally_pct": 1.0,      # 买入价反弹+1% → 卖出
}]
```

状态机：
```
  WATCH: 盯盘. 仅开盘 09:30 后的前 3 分钟内判定; 最新价/昨收 ≤ trigger_gap_pct
    → 买入 buy_qty (等待成交确认) → ARMED
  ARMED: 盯盘. 现价 ≥ 买入价 × (1 + sell_rally_pct)
    → 卖出 → DONE
  买入被拒/撤销 → 终止本单 (打印提示)
```

跨交易日自动重置。行情取 `fdata_client.quote()`，同花顺行情未恢复 → 保持 WATCH 不下单。

运行：
```bash
uv run python trading/condition_orders.py --poll 5          # 模拟 (安全)
uv run python trading/condition_orders.py --live --poll 5   # 真实下单 (ths_trade)
```

---

## 七、策略系统要点

**策略 = 写 signal() 一次，回测/优化/实盘全复用，禁止 if backtest 分支。**

```python
# strategy/strategies/my_strategy.py (放进去自动注册)
from strategy.base import Strategy, INT
import pandas as pd

class MyStrategy(Strategy):
    NAME = "my_strategy"
    TITLE = "我的策略"
    TIMEFRAME = "day"          # "day"/"5m"/"15m"/...
    PARAMS = {"window": {"type": INT, "default": 20, "min": 5, "max": 120}}
    SYMBOLS = ["sz159915"]

    def signal(self, df: pd.DataFrame, params: dict) -> pd.Series:
        """返回 0/1 目标仓位序列. 写这一次就够了."""
        ma = df["close"].rolling(int(params["window"])).mean()
        return (df["close"] > ma).astype(int).where(ma.notna(), 0)
```

基类默认 `on_bar()` 自动桥接：取历史 → 调 signal → 看最后一根 target → target 变了就 `ctx.submit_order()`。复杂策略（状态机/追踪止损）可自行覆盖 `async def on_bar(bar, ctx)`，但仍要写 signal 供快速回测。

**运行时三层**（runtime/）：
- **Portfolio**：内存态 + state.json 落盘。两个核心概念严格分离：`target`（策略意图，submit_order 立即更新，防重复下单）vs `qty`（实际持仓，apply_fill 后才更新）
- **Context**：策略看到的世界。`history()` 取 bars、`qty_for()` 算整手、`submit_order()` 订单意图层（不直接碰 ths_trade）
- **Broker**：BacktestBroker（submit 入队 pending → 下根 bar.open flush 成交）/ SimulatedBroker（立即 fill）/ LiveBroker（调 ths_trade + 异步 poll 成交流）

**回测口径（vbt_adapter.py）**：entries/exits 布尔序列 `shift(1)` + vbt `price=open` = 收盘出信号 t → 次日开盘 t+1 成交（无未来函数）。佣金万 1（ETF 无印花税）。回测 `/api/backtest` 与 vbt_adapter 用的就是这个口径。

---

## 八、硬约束（任何情况下不得违反，出生产事故的根源）

1. **成交口径统一**：收盘信号 → 次日开盘成交。绝不允许信号当日 close 成交（未来函数）
2. **策略信号统一 target position 0/1**，不是买卖动作。策略里**禁止 `if backtest/live` 判断**
3. **所有阻塞调用必须 `asyncio.to_thread()` 包**：eltdx TCP、subprocess、ths_trade AX、文件 IO。漏了就全后端卡住 10-30s + WS 中断
4. **同花顺交易全程 AX 后台，绝不激活前台**（触发反自动化保护 -25212 封禁 AX）
5. **高频查询 vs 切账户彻底分离**：positions/orders/trades/funds 无 flag 零检查；切账户/登录用独立 `switch-account` 子命令（单独调度）
6. **一切取数只走 `strategy/fdata_client.py`**，不许直连 eltdx / subprocess 调 akshare
7. **fdata kline limit 为空 = 全历史 = 传 `--limit 0`**（eltdx 分钟线上限 800，CLI 默认 limit 30 不够）
8. **同花顺下单必须显式传价格参数**（避免依赖行情联动带出，联动挂了就下单失败）
9. **下单失败 Bark 必发**：券商拒单弹窗 + 提交后状态未知超时
10. **Next.js 根路径 `/` 必须重定向到 `/charts`**，禁止跳转已删除的 `/live`
11. **前端数字格式化全走 fmt.ts**：股价<10 三位小数、≥10 两位小数；百分比最多两位；金额千分位两位
12. **React StrictMode 下 ECharts dispose 必须置 ref = null**（StrictMode mount→unmount→mount 双执行导致实例泄漏闪烁）
13. **Docker 远期架构**：下单 ths_trade 必须在宿主机跑（同花顺 GUI），后端容器走 Webhook → 宿主机轻量 agent，不要把 ths_trade 塞容器

---

## 九、常见坑速查（80% 问题在这里能找到修复方向）

### 前端图表

| 现象 | 原因 | 修复 |
|---|---|---|
| 挂载策略 500 报错 | pandas date 列/索引冲突；numba pyobject；uvicorn --reload 旧代码 | vbt_adapter.py: pop(date)→设index → pd.to_numeric → astype float64；重启 uvicorn |
| K线买卖 marker 不显示 | bars 数量不足；日期格式不匹配（分钟级/日级双轨） | limit=0 取全历史；KLineChart 双轨日期匹配（精确 + startsWith） |
| 分时图均价线预先画完整 | 流式测试时 VWAP 计算长度超过 bars 长度 | IntradayChart VWAP 数组与 bars 同步推进，不提前计算末尾 |
| ECharts 报 "series undefined" | merge 模式传空对象 series | setOption 前过滤 data 为空的 series，确保每个都有 type/name/data |
| StrictMode 下图闪烁/消失 | dispose 后 ref 未置 null，二次 mount 用旧实例 | chartRef.current?.dispose(); chartRef.current = null; |
| 分时数据 5 秒才更新 | 用了 REST 轮询 /api/intraday，没走 WebSocket | 确认 ws.ts MarketWs 订阅 /ws/market tick 推送 |

### 后端 API

| 现象 | 原因 | 修复 |
|---|---|---|
| API 响应 10-30s + WS 中断 | 阻塞调用没包 asyncio.to_thread | 全文搜 subprocess/eltdx/ths_trade 调用，确认被 to_thread 包 |
| /api/positions 空/报错 | 同花顺未运行/未打开交易界面/未登录 | 手动打开同花顺 → 交易下单界面 → CLI `ths_trade.py positions` 自测 |
| /api/intraday bars 为空 | 非交易时段没自动回退上一交易日 | fetch_intraday_1m 拉 640 根 1m bars，当日本地分钟数=0 时自动取上一完整交易日 |
| /ws/mock_stream 连不上 | next.config.ts rewrite 缺 mock_stream 规则；ws.ts URL 没走 rewrite | 检查 next.config.ts 三条 rewrite：/api/backend、/ws/market、/ws/mock_stream |

### 数据层

| 现象 | 原因 | 修复 |
|---|---|---|
| quote 返回 None/空 | fdata serve 未起 + CLI 也不通；同花顺行情连接挂了 | CLI 自测 `fdata.py quote sz159915`；确认同花顺客户端能刷实时价 |
| eltdx 连接超时/拒连 | 7709 主站不稳；timeout 太短 | TdxClient timeout=5；fdata_client 自动回退 CLI，一般无感 |
| 分钟 K 线只返回 800 根 | eltdx 分钟线上限 800 | fdata_client.kline limit=None → 内部传 `--limit 0` 全量 |
| 指数 K 线报 invalid kline date | 缺 kind="index" | kline() 里 kind 正确传 "index" |

### 交易层

| 现象 | 原因 | 修复 |
|---|---|---|
| 报"市场代码不允许为空" | 代码框没聚焦就填值，不触发联动 | fill_code：先 AXFocused=True → 再填值 → 0.2s 等联动 |
| AX API 返回 -25212（禁用） | 脚本把同花顺激活到前台，触发反自动化保护 | 全程不许 activate()/前台化；重启同花顺恢复 |
| 连续下单后代码联动失灵 | 同花顺连续下单导致面板退化，联动停止响应 | 联动失败 → 中止提交（避免废单）；重启同花顺客户端 |
| 撤单双击不行 → 全撤按钮 | AXRow 双击不可靠 | cancel 优先走"全撤"按钮方式；按钮匹配含"确认/确定/是" |

---

## 十、当前已知未完成项 / 待办

1. **Docker 生产部署完整闭环**：宿主机 ths_trade agent + 容器 Webhook 下单通道尚未实现（Dockerfile/nginx.conf 已写，但 ths_trade 宿主机 agent 是 TODO）
2. **intraday_t 策略参数调优**：回测 -1.9% vs 买入持有 +5.1%（两市缩量 ~1.97 万亿环境），vol_expand/min_amount_yi 阈值需重调
3. **条件单实盘完整闭环验证**：同花顺行情稳定后需跑一次 WATCH→ARMED→DONE 真实下单全流程
4. **ths_trade cancel 自动双击路径实盘验证**：人工同结构手动撤单成功过，但脚本自动路径未跑真实单
5. **nextjs/ 独立项目去向确认**：资金流向实验页，是否合并入主 frontend/
6. **stockview/ 旧 Streamlit 迁移确认**：市场面板/五因子信号/资金流向/行业权重等功能是否全部迁到 Next.js 还是保留双入口

---

## 十一、给下一个 Agent 的操作约定

- **中文沟通**。用户偏好简洁直接，讨厌多余操作
- **生产安全 > 一切**：下单类功能默认 dry-run，真实下单需用户反复确认。Bark 通知两个失败场景不能省
- **不要瞎创新架构**：当前六大子系统结构是多轮迭代踩坑稳定下来的。不要说"我建议换框架重写"
- **改代码前读 SKILL.md 和文件头注释**：每个核心文件顶部 30 行都是设计决策和踩坑记录，读了就不踩第二次
- **查行情走 fdata_client，下单走 ths_trade（或 LiveBroker），回测走 vbt_adapter**，不要直连底层绕路径
- **运行任何项目内 Python 都用 `uv run python xxx`**（项目 venv），不要系统 Python / 其他 venv
- **diff 尽量小**：尤其高频命令（ths_trade 查询），多加一个 tree scan 就慢 800ms，优化要抠到极致

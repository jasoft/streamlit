# 量化交易系统

个人 A 股/ETF 全链路量化交易平台。从行情数据 → 策略回测 → 图表可视化 → 自动下单 → 条件单一站式闭环。

> **项目路径**: `/Users/weiwang/Projects/streamlit`
> **给 AI Agent 接手**: 根目录 `SKILL.md`（863 行完整手册）+ `docs/AGENT_HANDOFF.md`（精简交接版 + 演进脉络），必读。

---

## 快速开始

```bash
# 1. 安装依赖 (首次)
uv sync                         # Python
cd frontend && npm install       # Next.js 前端
cd ..

# 2. 启 fdata 长连接服务器 (可选, 快 5-10 倍, 没启自动走 CLI)
uv run python scripts/fdata.py serve --port 9701

# 3. 一键启后端 + 前端
./dev.sh          # FastAPI :8000 + Next.js :3001, Ctrl-C 同停
# 访问 http://localhost:3001 → 自动跳 /charts 图会话页
```

验证系统正常：
```bash
curl http://localhost:8000/api/health                                # 后端健康
curl "http://localhost:8000/api/kline?symbol=sz159915&tf=day&limit=3" # K线
uv run python scripts/fdata.py quote sz159915                        # 实时快照
uv run python scripts/condition_orders.py --poll 5                   # 条件单 (模拟)
```

自定义端口：
```bash
BACKEND_PORT=8001 FRONTEND_PORT=3002 ./dev.sh
```

---

## 架构概览

```
用户浏览器 (Next.js 前端 :3001)
  ├─ /charts     多图会话 + 策略挂载 + 流式测试    (KLineChart / IntradayChart, ECharts 6)
  ├─ /backtest   批量回测 + 参数网格优化
  └─ /config     策略参数在线配置
        │
        ▼  next.config.ts rewrite (/api/*, /ws/*)
        │
FastAPI 后端 (:8000, backend/main.py)
  ├─ REST: /api/backtest, /api/kline, /api/intraday, /api/quote,
  │        /api/positions, /api/charts/sessions, /api/strategies, ...
  ├─ WS  : /ws/market       (每秒 tick 推送, 前端增量更新无闪烁)
  │        /ws/mock_stream  (流式测试逐根推 bar + 实时策略 + paper 成交)
  └─ 全部阻塞调用用 asyncio.to_thread() 包装, 绝不卡事件循环
        │
        ├── strategy/fdata_client.py    ──→  fdata.py 数据网关
        │     (serve优先/CLI回退/自动路由)      (eltdx 7709 + tqsdk + 新浪 + 东财)
        │
        ├── strategy/runtime/* + vbt_adapter.py
        │     (Portfolio/Context/Broker 三层, 写一次 strategy.signal() → 回测/实盘全复用)
        │
        └── scripts/ths_trade.py + condition_orders.py
              (同花顺 Mac AX API 自动交易, 条件单 WATCH→ARMED→DONE)
```

---

## 子系统说明

### ① Next.js 前端（frontend/）— 当前主 UI

- **图会话页 `/charts`**：多图管理（K线/分时切换）+ 选策略挂载回测（自动生成买卖点 marker）+ 流式逐根推 bar 测试策略 + 同花顺账户快照（自动刷新）
- **回测页 `/backtest`**：多选策略 × 多标的 × 参数网格 → 批量跑 vectorbt 回测，比较收益/回撤/胜率
- **配置页 `/config`**：按策略 schema 自动生成参数表单，写 `strategy/config.json`
- **核心前端库**：ECharts 6（canvas 只初始化一次，`setOption` 增量更新避免闪烁）、自定义 fmt.ts 全局数字格式化、ws.ts 单例 WebSocket 管理（自动重连 + 断连回退）

### ② FastAPI 后端（backend/）

- `backend/main.py`：15+ REST 端点 + 2 WebSocket 端点，全部阻塞调用 `asyncio.to_thread()` 封装
- `backend/store.py`：图会话 SQLite 持久化，跨浏览器/设备/刷新恢复
- 前端通过 `next.config.ts` rewrite 同域访问，无 CORS 问题

### ③ 策略框架（strategy/）— Write Once, Run Anywhere

- **双接口 Strategy 基类**：写一个 `signal(df, params)` 返回 0/1 目标仓位序列 → vectorbt 回测、参数优化、实盘 on_bar 事件驱动 全部自动复用，策略中**禁止 `if backtest/live` 判断**
- **runtime/ 三层架构**：
  - `Portfolio`：内存态 + state.json 落盘，严格分离 `target`（意图，submit_order 立即更新防重复）vs `qty`（实际持仓，fill 后更新）
  - `Context`：策略看到的世界（history / qty_for / submit_order）
  - `Broker`：BacktestBroker（次日开盘成交）/ SimulatedBroker（立即 fill）/ LiveBroker（同花顺真实下单 + 异步轮询成交流）
- **回测口径（vbt_adapter.py）**：收盘出信号 → 次日开盘成交（entries/exits shift(1) + price=open），无未来函数，与实盘执行一致
- **零注册自动发现**：策略放 `strategy/strategies/` 即被 registry 发现，无需手动注册

### ④ 数据网关（scripts/fdata.py + strategy/fdata_client.py）

**一切行情/K线只走这里，禁止直连底层。**

- **双模式**：
  - **TCP 长连接 serve 模式**（推荐）：`uv run python scripts/fdata.py serve --port 9701`，进程内常驻单个 eltdx client，连接复用省初始化开销，全局锁并发安全，自动断线重连
  - **CLI 模式**（回退）：subprocess 调 fdata.py，每次新建进程。serve 不可达时客户端**自动无感回退**
- **统一客户端**（fdata_client.py）：
  - `quote(code)`：自动路由股票/ETF/指数（serve 长连接）、期货/基金/期权/外盘（CLI quote 路由）
  - `kline(symbol, period, kind, adjust, limit=None)`：`limit=None` = 全历史 = `--limit 0`
  - `cli(argv)`：通用 CLI 透传，字节级一致
- **数据源选型（踩坑结论）**：
  - 股票/ETF/指数 → **eltdx 通达信 7709 协议**（`TdxClient(timeout=5)`）
  - 商品/金融期货实时 → tqsdk
  - ETF 期权实时 → 新浪 CON_OP_ 批量直连
  - 商品期权实时 → tqsdk
  - 基金净值 → 东财 fund 域名
  - 低频全球快讯/Greeks/中证权重 → akshare

### ⑤ 同花顺自动交易（scripts/ths_trade.py）

macOS AX API 纯后台驱动同花顺 Mac 客户端，**绝不激活窗口前台**（触发反自动化保护导致 AX API 封禁 -25212）。

- **高频 vs 低频彻底拆分**（~220ms 查询，6x 提速）：
  - **高频查询**（给 `/api/positions` 等 API 用）：`positions / orders / trades / funds`，无 flag、零冗余检查、直接 AXTable 语义定位
  - **低频切账户/登录**（独立调度）：`switch-account real|sim`，读 `.env THS_USER/THS_PASS`，自动登录 + 切 tab + 校验
  - **下单**：`buy / sell [--price N] [--dry-run]`，聚焦→填值触发联动带出对手价（用户明确不清空价格框，除非 `--price`）
  - **撤单**：`cancel [--contract N|--code N|--all]`，优先"全撤"按钮方式，双击行不可靠自动回退
- **下单失败 Bark 推送**：券商拒单弹窗（含失败词）或提交后状态未知超时 → 立即 Bark 到用户手机（`THS_BARK_KEY`），带方向/代码/数量/价格 + 券商原始返回

### ⑥ 条件单引擎（scripts/condition_orders.py）

多条件单 `asyncio.gather` 异步并发，每个单用独立状态机：

```
  WATCH: 仅开盘 09:30 后前 3 分钟内，最新价/昨收 ≤ trigger_gap_pct
    → 买入 buy_qty (等待成交确认) → ARMED
  ARMED: 现价 ≥ 买入价 × (1 + sell_rally_pct)
    → 卖出 → DONE
  买入被拒/撤销 → 终止本单
```

- 行情统一走 `fdata_client.quote(last/pre_close)`，同花顺行情连接未恢复时保持 WATCH 不下单
- 记账复用 runtime 层（Portfolio + Broker + ctx.submit_order）
- 支持模拟模式（默认）和真实下单模式（`--live`）
- `--poll N` 秒设置行情刷新间隔

---

## 目录结构

```
streamlit/
├── SKILL.md                  ★ AI Agent 接手完整手册 (863 行 17 章)
├── README.md                 本文件
├── dev.sh                    一键启 FastAPI + Next.js, Ctrl-C 同停
├── pyproject.toml            Python 依赖 (uv sync 安装)
├── Dockerfile / nginx.conf / deploy.sh / start.sh   生产部署 (远期, 未闭环)
│
├── frontend/                 Next.js 15 前端 (主 UI)
├── backend/                  FastAPI 后端 (REST + WebSocket + SQLite)
├── strategy/                 策略框架 (双接口 + runtime三层 + vbt回测)
├── scripts/
│   ├── fdata.py              统一金融数据 CLI + TCP serve 长连接
│   ├── ths_trade.py          同花顺 Mac AX 自动交易
│   ├── condition_orders.py   条件单引擎
│   └── check_window.py / stress_test.py ...   调试/压测
│
├── stockview/                旧 Streamlit 行情仪表盘 (保留, 非主流程)
├── nextjs/                   独立实验项目 (资金流向, 非主系统)
├── tests/                    单测
├── datasource_test/          2026-08-30 数据源选型实测报告
├── skills/financial-data/    旧 skill 参考
└── docs/
    ├── AGENT_HANDOFF.md      精简交接版 + 演进脉络 (给下一个 Agent)
    └── quant-framework-report.md   vectorbt/backtrader 调研报告
```

---

## 常用命令速查

```bash
# ===== 启动 =====
./dev.sh                                                  # 一键启前后端
uv run python scripts/fdata.py serve --port 9701         # fdata 长连接

# ===== 数据 =====
uv run python scripts/fdata.py quote sz159915            # 实时快照
uv run python scripts/fdata.py kline sz159915 --period 5m --limit 0   # 全量 5m K线
uv run python scripts/fdata.py doctor                    # 10项数据源自检

# ===== 交易 =====
uv run python scripts/ths_trade.py positions             # 查持仓 (~220ms)
uv run python scripts/ths_trade.py switch-account sim    # 切到模拟账户
uv run python scripts/ths_trade.py buy 601899 100 --dry-run    # 模拟填单
uv run python scripts/ths_trade.py cancel --all          # 全撤

# ===== 条件单 =====
uv run python scripts/condition_orders.py --poll 5       # 模拟模式
uv run python scripts/condition_orders.py --live --poll 5    # 实盘

# ===== 回测 (挂载策略时前端实际调用的接口) =====
curl -X POST http://localhost:8000/api/backtest \
  -H "Content-Type: application/json" \
  -d '{"strategy":"sma_cross","symbol":"sh510300","params":{"fast":10,"slow":30}}'

# ===== 单元测试 =====
uv run python -m unittest tests.test_strategy_system
```

---

## 配置文件

### `.env` (项目根目录, git 未提交)

```bash
# 同花顺登录
THS_USER=交易账号
THS_PASS=交易密码

# 下单失败 Bark 推送 (手机通知)
THS_BARK_KEY=https://api.day.app/xxxxx/

# tqsdk (期货/商品期权实时)
TQ_USER=xxx
TQ_PASS=xxx

# fdata serve (可选, 有默认值)
FDATA_HOST=127.0.0.1
FDATA_PORT=9701
FDATA_TIMEOUT=8
```

### `strategy/config.json` (策略配置, 可通过 /config 页面在线编辑)

```json
{
  "strategies": {
    "ma20_trend": {
      "enabled": true,
      "symbols": ["sz159915"],
      "params": {"window": 20},
      "live": {
        "dry_run": true,
        "poll_seconds": 60,
        "execute_time": "14:55",
        "cash_per_symbol": 10000
      }
    }
  }
}
```

⚠️ `live.dry_run=true` 是默认安全值，改为 false 才会真实下单。**永远不要默认设 false**。

---

## 13 条硬约束（任何情况下不得违反）

1. 成交口径统一：收盘信号 → 次日开盘成交，绝不允许信号当日 close 成交（未来函数）
2. 策略信号统一 target position 0/1，不是买卖动作；策略里禁止 `if backtest/live` 判断
3. 所有阻塞调用必须 `asyncio.to_thread()` 包：eltdx TCP、subprocess、ths_trade AX、文件 IO
4. 同花顺交易全程 AX 后台操作，绝不激活窗口前台（触发 -25212 封禁 AX）
5. 高频查询与切账户/登录彻底分离：positions/orders/trades/funds 无 flag 零检查
6. 一切取数只走 `strategy/fdata_client.py`，不许直连 eltdx / subprocess 调 akshare
7. fdata kline limit 为空 = 全历史 = 传 `--limit 0`
8. 同花顺下单必须显式传价格参数（避免依赖行情联动带出）
9. 下单失败 Bark 必发：券商拒单弹窗 + 提交后状态未知超时
10. Next.js 根路径 `/` 必须重定向到 `/charts`，禁止跳转已删除的 `/live`
11. 前端数字格式化全走 `frontend/src/lib/fmt.ts` 统一函数
12. React StrictMode 下 ECharts dispose 后必须置 ref = null
13. Docker 远期：ths_trade 放宿主机，后端容器走 Webhook，不要塞容器

---

## 更多文档

- **SKILL.md**（项目根目录）：863 行 17 章完整手册，给 AI Agent 接手用
- **docs/AGENT_HANDOFF.md**：精简交接版 + 项目演进脉络（7 个阶段里程碑）
- **docs/quant-framework-report.md**：vectorbt / backtrader 开源量化框架调研报告
- **datasource_test/REPORT.md**：2026-08-30 10 类金融数据源实测选型报告

---

## 当前待办

1. Docker 生产部署完整闭环（宿主机 ths_trade agent + 容器 Webhook）
2. intraday_t 日内策略参数调优（当前两市缩量环境下回测 -1.9%）
3. 条件单实盘 WATCH→ARMED→DONE 完整闭环验证
4. ths_trade cancel 自动双击路径实盘验证（人工手动同结构验证过但脚本未跑真单）
5. nextjs/ 独立实验项目去向确认
6. stockview/ 旧 Streamlit 是否全部迁到 Next.js 或保留双入口

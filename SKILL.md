# Quant Trading System — SKILL 文档

> **项目路径**: `/Users/weiwang/Projects/streamlit`
> **接手必读**: 本项目是一个完整的 A 股/ETF 个人量化交易系统，包含 Next.js 前端、FastAPI 后端、策略框架、数据网关、同花顺 GUI 自动化下单、条件单引擎六大子系统。**请先完整阅读「硬约束」「架构」「常见坑」三节再动手**，违反任何一条都会出生产级故障。

***

## 一、什么时候用这个 Skill

用户提到以下任何一项时激活：

- 改前端页面（图会话 /charts、回测 /backtest、配置 /config）

- 改后端 API、WebSocket 推送、回测逻辑

- 加/改策略、策略回测、参数优化

- 查行情/K线/分时数据（fdata 数据源）

- 同花顺自动交易（下单/撤单/查持仓）

- 条件单引擎、开盘买入/反弹卖出逻辑

- 项目启动、部署、调试

- Bug 排查（尤其是图表不显示、下单失败、回测报错）

***

## 二、架构总览

```
┌──────────────────────────────────────────────────────────────────────┐
│                        用户浏览器 (Next.js 前端)                        │
│  /charts 图会话  │  /backtest 回测  │  /config 配置                     │
│  ECharts 6  K线/分时 │  WebSocket 实时tick │  REST API 调用              │
└────────────┬─────────────────────────────┬───────────────────────────┘
             │ /api/* 和 /ws/*             │  next.config.ts rewrite
             ▼                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    FastAPI 后端 (:8000)                                │
│  REST: /api/backtest, /api/kline, /api/intraday, /api/positions, ... │
│  WS:   /ws/market (每秒tick推送) │ /ws/mock_stream (流式测试)          │
│  全部阻塞调用用 asyncio.to_thread() 包装，绝不卡事件循环                │
└────────┬──────────────────────┬──────────────────────┬───────────────┘
         │                      │                      │
         ▼                      ▼                      ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐
│  fdata 数据层     │  │  strategy 策略层 │  │  ths_trade 同花顺交易层   │
│  fdata serve      │  │  base.py 双接口  │  │  macOS AX API 后台操作   │
│  TCP长连接        │  │  vbt_adapter     │  │  buy/sell/positions/... │
│  eltdx(7709)      │  │  runtime/*       │  │  Bark 通知下单失败      │
│  CLI fallback     │  │  state.json 记账 │  │  switch-account 切账户  │
└──────────────────┘  └──────────────────┘  └──────────────────────────┘
        ▲                                            ▲
        │                                            │
        └─────────────── 条件单引擎 ─────────────────┘
                  scripts/condition_orders.py
            WATCH → 开盘跌破阈值买 → ARMED → 反弹卖 → DONE
```

### 关键设计原则（不可违背）

1. **Write Once, Run Anywhere**: 策略只写一次 `signal()`，vectorbt 回测、参数优化、实盘 on\_bar 全都复用，策略里**绝不出现** **`if backtest: ... else: ...`**
2. **成交口径统一**: 收盘出信号 → 次日开盘成交（回测 vbt\_adapter shift+1，实盘 flush\_pending，无未来函数）
3. **目标仓位幂等**: 所有策略信号统一为 `target position` 0/1（仓位比例），不是"买/卖动作"，避免重复下单
4. **阻塞必异步化**: 所有同步阻塞调用（eltdx TCP、subprocess、同花顺 AX）**必须用** **`asyncio.to_thread()`** **包装**，否则事件循环卡死 10-30 秒
5. **后台不抢前台**: 同花顺交易全程 AX 后台操作，**绝不激活窗口到前台**（会触发反自动化保护导致 AX API 永久禁用，错误码 -25212）
6. **高频与低频分离**: 查持仓/委托/成交/资金（高频）和切账户/登录（低频）拆成不同子命令，高频路径零冗余检查
7. **数据源统一入口**: **一切行情/K线数据只走** **`strategy/fdata_client.py`**，内部优先 fdata serve 长连接，失败自动回退 CLI，调用方无感

***

## 三、目录结构与关键文件速查

```
/Users/weiwang/Projects/streamlit/
├── dev.sh                      ★ 一键启动脚本: ./dev.sh (Ctrl-C 停止)
├── pyproject.toml              Python 依赖管理 (uv sync 安装)
├── Dockerfile                  生产部署镜像 (Ubuntu 24.04 + Nginx)
├── nginx.conf                  Nginx 反代配置
├── deploy.sh / start.sh        部署脚本
│
├── frontend/                   ★ Next.js 15 前端 (:3001)
│   ├── next.config.ts          /api/* → localhost:8000, /ws/* 转发
│   └── src/
│       ├── app/
│       │   ├── page.tsx        根路径 → 重定向 /charts
│       │   ├── charts/page.tsx ★ 图会话主页 (多图+策略挂载+流式测试)
│       │   ├── backtest/page.tsx   批量回测
│       │   └── config/page.tsx     策略参数配置
│       ├── components/
│       │   ├── KLineChart.tsx      ★ K线图 (ECharts 6, 拖拽缩放, marker双轨匹配)
│       │   ├── IntradayChart.tsx   ★ 分时图 (白价线+黄VWAP线+染色量柱)
│       │   └── ParamForm.tsx       参数表单 (schema 驱动自动生成控件)
│       └── lib/
│           ├── api.ts          REST API 封装
│           ├── ws.ts           ★ WebSocket 管理 (MarketWs + MockStreamWs)
│           └── fmt.ts          ★ 全局数字格式化 + 中英文转换
│
├── backend/                    ★ FastAPI 后端 (:8000)
│   ├── main.py                 ★ 全部 REST + WebSocket 端点
│   ├── store.py                图会话 SQLite 持久化 (charts.db)
│   └── charts.db               SQLite 数据库文件
│
├── strategy/                   ★ 策略框架核心
│   ├── base.py                 ★ Strategy 基类 (signal/on_bar 双接口)
│   ├── registry.py             策略自动发现 (零注册, 扫描 strategies/)
│   ├── config.py               config.json 读写
│   ├── manager.py              策略进程启停 (start/stop/status)
│   ├── runner.py               策略定时运行 (轮询评估)
│   ├── run_live.py             实盘入口
│   ├── engine.py               自研回测引擎(事件驱动, 已被 vbt_adapter 取代)
│   ├── dashboard.py            Streamlit 看板(旧实盘管理页)
│   ├── mock_market.py          Mock 市场 (流式测试逐根 bar 推进)
│   ├── trader.py               旧实盘执行器 (取数入口已迁 fdata_client)
│   ├── fdata_client.py         ★★★ 统一数据客户端 (serve优先/CLI回退/自动路由)
│   │
│   ├── backtest/
│   │   └── vbt_adapter.py      ★ vectorbt 回测适配器 (核心回测逻辑)
│   │
│   ├── runtime/                ★ 策略运行时 (订单意图 → 成交)
│   │   ├── portfolio.py        Portfolio: 现金/持仓/订单 内存态 + state.json
│   │   ├── ctx.py              Context: 策略看到的"世界"
│   │   ├── broker.py           ★ 三种 Broker: Backtest/Simulated/Live
│   │   └── runner.py           Runner.run_live (事件驱动实盘单轮)
│   │
│   └── strategies/             策略目录(放进去自动注册)
│       ├── ma20_trend.py       MA20 趋势跟踪 (默认示例策略)
│       ├── sma_cross.py        SMA 金叉死叉 (默认示例策略)
│       ├── intraday_t.py       日内做T (5分钟, 参数待调优)
│       └── tick_buy_sell.py    tick级策略模板
│
├── scripts/
│   ├── fdata.py                ★★★ 统一金融数据 CLI + TCP长连接服务器
│   ├── ths_trade.py            ★★★ 同花顺 Mac GUI 自动交易 (AX API)
│   ├── condition_orders.py     ★ 条件单引擎 (WATCH→ARMED→DONE状态机)
│   ├── check_window.py         调试: 同花顺 AX 窗口树检查
│   ├── stress_test.py          下单压测脚本
│   ├── extreme_fill_test.py    极端填单测试
│   └── example_tick_strategy.py tick策略示例
│
├── stockview/                  旧 Streamlit 行情仪表盘(保留, 非主流程)
│   ├── app.py                  Streamlit 入口
│   ├── etf_signal.py           创业板ETF五因子信号面板
│   ├── fund_flow.py / hs300_industry.py / options.py / ...
│   └── tdx_source.py           旧通达信数据封装(已被fdata取代)
│
├── nextjs/                     另一个独立 Next.js 项目(资金流向实验,非主流程)
├── skills/financial-data/      fdata 旧 skill 文件(参考用)
├── datasource_test/            数据源选型实测报告 REPORT.md
├── docs/
│   ├── AGENT_HANDOFF.md        ★ 旧版交接文档(含早期决策记录)
│   └── quant-framework-report.md  量化框架调研报告(vectorbt/backtrader)
└── tests/
    ├── test_strategy_system.py  策略系统单元测试
    └── test_etf_signal.py ...   stockview 测试
```

***

## 四、快速启动

### 4.1 本地开发（推荐，日常使用）

```bash
cd /Users/weiwang/Projects/streamlit

# 0. 安装依赖（首次）
uv sync                       # 装 Python 依赖
cd frontend && npm install    # 装前端依赖

# 1. 先启动 fdata 长连接服务器（可选，不启动则自动走 CLI，速度慢些）
uv run python scripts/fdata.py serve --port 9701

# 2. 另开终端，一键启动前后端
./dev.sh
# 自动开: 后端 localhost:8000 + 前端 localhost:3001
# 访问 http://localhost:3001 → 自动跳 /charts
# Ctrl-C 同时停止两个进程
```

`dev.sh` 支持自定义端口:

```bash
BACKEND_PORT=8001 FRONTEND_PORT=3002 ./dev.sh
```

### 4.2 验证各系统正常

```bash
# 后端健康检查
curl http://localhost:8000/api/health

# 拉一条K线
curl "http://localhost:8000/api/kline?symbol=sz159915&tf=day&limit=3"

# 回测（挂载策略用的接口）
curl -X POST http://localhost:8000/api/backtest \
  -H "Content-Type: application/json" \
  -d '{"strategy":"ma20_trend","symbol":"sz159915","params":{"window":20}}'

# fdata CLI 直接取数
uv run python scripts/fdata.py quote sz159915

# 查同花顺持仓（需同花顺运行并打开交易界面）
uv run python scripts/ths_trade.py positions

# 条件单（模拟模式, 安全）
uv run python scripts/condition_orders.py --poll 5
```

***

## 五、前端系统详解

### 5.1 路由与页面

| 路径          | 页面                  | 功能                                    |
| ----------- | ------------------- | ------------------------------------- |
| `/`         | `page.tsx`          | 根路径 → 重定向到 `/charts`（已删除的 /live 绝不跳转） |
| `/charts`   | `charts/page.tsx`   | ★ 核心页：多图会话管理 + 策略挂载 + 流式测试 + 账户快照     |
| `/backtest` | `backtest/page.tsx` | 批量回测多策略多标的 + 参数网格                     |
| `/config`   | `config/page.tsx`   | 策略参数在线配置（写 config.json）               |

### 5.2 图表组件（KLineChart + IntradayChart）

**核心约束（常见Bug来源）**：

- **ECharts 6 canvas 只初始化一次**，后续用 `setOption` 增量更新，React StrictMode 下 useEffect 会 mount→unmount→mount，dispose 时**必须重置 ref** 为 null，否则实例泄漏 + 重渲染闪烁

- **merge 模式下不要传空 series**：ECharts merge 时空对象会创建无 type/name 的新 series，直接报错

- **K 线图 marker 双轨日期匹配**：分钟级 `date==="2026-09-02 14:55:00"` 精确匹配实盘/流式 marker；日级 `date.startsWith("2026-09-02")` 模糊匹配日线回测 marker，两种都要支持

- **Tooltip 从数据数组索引取值**：不要用 ECharts 的 `p.data`（会错位），从渲染闭包实际保存的 `ohlc`/`ma`/`vols` 数组按下标取

### 5.3 分时图（IntradayChart）

- **显示完整交易时段**：09:31–11:30 / 13:01–15:00，哪怕当前只有上午数据，也要画出下午的空区域（时间格子占满，不预先填充价格）

- **三条线/元素**：价格白线（收盘价）、均价黄线 VWAP = 累计成交额/累计成交量、成交量柱（按涨跌染色红/绿）

- **均价线实时绘制**：黄线随时间推进同步绘制，**不预先画完整直线**（流式测试尤其注意）

- **盘后自动取上一交易日**：当日分钟数=0（非交易时段）时，自动取上一个完整交易日的分时数据

### 5.4 WebSocket 管理（ws.ts）

```
MarketWs (单例)
  └─ 连接 /ws/market?symbols=sz159915,...
     └─ 每秒推送 {type:"tick", snapshot, bars:[最后1-2根增量]}
        前端 appendData 增量更新, 整页不重渲染 → 无闪烁
     └─ 自动重连（断开 1.5s 重连）

MockStreamWs (一次性连接, 流式测试用)
  └─ 连接 /ws/mock_stream?symbol=...&strategy=ma20_trend&tf=5m&speed=1x&params=...
     └─ 首条 {type:"info", pre_close:xx}   (初始化昨收参考轴)
     └─ 逐根 {type:"bar", bar, orders, markers, snapshot, target}
     └─ 停止/断开即停, 不自动重连 (停止后自动返回上一视图)
```

### 5.5 全局数字格式化（fmt.ts）

**必须使用这些函数，禁止直接 toFixed()**，保证全站一致：

```typescript
fmtPrice(3.567)   // → "3.567"  股价<10元三位小数
fmtPrice(12.3)    // → "12.3"   股价≥10元两位小数, 去尾零
fmtMoney(123456)  // → "123,456" 金额, 最多两位小数, 千分位
fmtPct(0.0523)    // → "5.23%"   比率→百分比乘100
fmtPct(5.23, false) // → "5.23%" 已是百分数, 不乘
fmtNum(123.456)   // → "123.46"  通用数字, 最多两位小数

paramCn("window") // → "均线窗口"  参数英→中
statCn("total_return") // → "总收益" 统计英→中
formatStat("total_return", 2.023) // → "202.3%" 自动识别类型+格式化
```

### 5.6 图会话持久化（charts/page.tsx + store.py）

- 新建图表 → 选代码+周期（day/5m/分时）→ 拉 K 线

- 选策略 → 调 `/api/strategies/{name}/schema` 拿参数 schema → 自动生成参数控件

- 点「挂载策略」→ 调 `/api/backtest` 跑回测 → 在 K 线上画买卖 markers

- 图表元数据（不含 bars/markers 大数据）**防抖 800ms 存后端 SQLite** → 跨浏览器/设备/刷新会话恢复

- 流式测试：保存当前状态到 `preStreamStateRef` → 逐根推 bar + 实时跑策略 → 结束后还原原状态 → 自动切回原视图

***

## 六、后端系统详解（backend/main.py）

### 6.1 REST API 全集

| 方法   | 路径                                | 作用                         | 关键参数                                      |
| ---- | --------------------------------- | -------------------------- | ----------------------------------------- |
| GET  | `/api/health`                     | 健康检查                       | —                                         |
| GET  | `/api/charts/sessions`            | 读图会话列表                     | —                                         |
| PUT  | `/api/charts/sessions`            | 保存图会话                      | body: list\[{id,symbol,tf,...}]           |
| GET  | `/api/strategies`                 | 策略列表+运行状态                  | —                                         |
| GET  | `/api/config`                     | 读 config.json              | —                                         |
| PUT  | `/api/config`                     | 写 config.json              | body: 完整 config                           |
| GET  | `/api/strategies/{name}/schema`   | 策略参数 schema                | —                                         |
| POST | `/api/backtest`                   | ★ vectorbt 回测（挂载策略用）       | strategy, symbol, params, tf, qfq, cash   |
| POST | `/api/optimize`                   | 参数网格搜索                     | strategy, symbol, param\_grid             |
| POST | `/api/charts/run`                 | 图会话跑一轮事件驱动                 | strategy, symbol, params, mode, dry\_run  |
| GET  | `/api/kline`                      | K线 OHLCV 数据                | symbol, tf(day/5m/15m/30m/1h), qfq, limit |
| GET  | `/api/intraday/{symbol}`          | 当日分时 1m + VWAP + MACD + 昨收 | —                                         |
| GET  | `/api/quote/{symbol}`             | 实时快照                       | —                                         |
| GET  | `/api/positions`                  | 同花顺实际持仓（subprocess）        | —                                         |
| GET  | `/api/evals/{name}`               | 策略评估记录流水                   | tail=N                                    |
| POST | `/api/strategies/{name}/start`    | 启动策略进程                     | —                                         |
| POST | `/api/strategies/{name}/stop`     | 停止策略进程                     | —                                         |
| POST | `/api/strategies/{name}/run-once` | 立刻 dry-run 跑一轮             | —                                         |

### 6.2 WebSocket 端点

**`/ws/market`** — 实盘分时 tick 推送

- Query: `?symbols=sz159915,sh510300`（逗号分隔）

- 连接后自动扩展全局 tick 循环覆盖的 symbol 列表（无需重启）

- 每秒推送最后 1-2 根 bar（新 bar + 实时更新的当前 bar）+ 快照

- VWAP/MACD 每次重算最后一根（前端已有历史，只发增量）

- 前端走 MarketWs 单例，自动重连

**`/ws/mock_stream`** — 流式测试

- Query: `?symbol=sz159915&strategy=ma20_trend&tf=5m&speed=1x&params={...}`

- speed 映射: 1x=5s, 2x=2.5s, 5x=1s, 10x=0.5s, 20x=0.25s

- 先推 init 消息: `{type:"info", pre_close, msg}` → 前端初始化分时参考轴

- 按 speed 间隔逐根 advance\_bar → 调 strategy.signal → target 变化则 paper 成交 → 推 bar+orders+markers+snapshot

- 成交口径简化为"当根收盘出信号 → 当根 close 成交"（仅供演示，真实回测是次日开盘）

### 6.3 后端硬约束

```python
# ★★★ 所有阻塞调用必须这样写，绝对不能直接调用！
df = await asyncio.to_thread(_fetch, symbol, qfq, tf, limit)
result = await asyncio.to_thread(vbt_adapter.backtest, strat, df, params, cash)
r = await asyncio.to_thread(subprocess.run, cmd, ...)
```

如果忘了加 `asyncio.to_thread()`，当 eltdx 或 ths\_trade 阻塞 10 秒时，整个后端所有 API 全部卡住 10 秒，WebSocket tick 中断。

***

## 七、策略系统详解

### 7.1 Strategy 基类双接口设计

写一个策略 = 继承 `Strategy` 类 + 实现 `signal()`。通常不用写 `on_bar()`。

```python
# strategy/strategies/my_strategy.py
import pandas as pd
from strategy.base import Strategy, INT, FLOAT

class MyStrategy(Strategy):
    NAME = "my_strategy"               # 唯一ID (英文, 文件名一致)
    TITLE = "我的策略"                  # 中文显示名
    TIMEFRAME = "day"                   # 默认周期: day/5m/15m/30m/60m
    TRIGGER_ON_CLOSE = True             # True=收盘触发, False=tick触发
    LOOKBACK = 3000                     # on_bar 取历史 bar 数
    SYMBOLS = ["sz159915"]              # 默认标的列表
    
    PARAMS = {                          # 参数 schema (前端自动生成控件)
        "window": {"type": INT, "default": 20, "min": 5, "max": 120},
        "threshold": {"type": FLOAT, "default": 0.02, "min": 0, "max": 0.1},
    }
    
    def signal(self, df: pd.DataFrame, params: dict) -> pd.Series:
        """★ 核心: 写一次, 回测/优化/实盘全部复用.
        返回 0/1 目标仓位序列, index 与 df 对齐.
        1 = 满仓持有, 0 = 空仓. 支持小数吗? 回测支持, 实盘 broker 目前只处理 0/1.
        """
        w = int(params["window"])
        ma = df["close"].rolling(w).mean()
        # 收盘>均线且涨跌幅超阈值 → 持有
        return ((df["close"] > ma) & (df["close"].pct_change() > params["threshold"])).astype(int).where(ma.notna(), 0)
```

放进去 `strategy/strategies/` 即被 `registry.discover()` 自动发现，零注册。

### 7.2 signal() → on\_bar() 自动桥接

基类默认 `on_bar()` 实现（你通常不用重写）：

```python
async def on_bar(bar, ctx):
    df = ctx.history()           # 取历史 bars
    tgt = signal(df, ctx.params).iloc[-1]  # 最后一根的目标仓位
    if tgt == 1 and ctx.target == 0:       # 0→1, 开仓
        await ctx.submit_order("buy", ctx.qty_for(), ctx._last_price)
    elif tgt == 0 and ctx.target == 1 and ctx.position > 0:  # 1→0, 平仓
        await ctx.submit_order("sell", ctx.position, ctx._last_price)
```

### 7.3 策略运行时三层（runtime/）

```
Portfolio (内存态 + state.json 落盘)
  ├─ cash: 可用现金
  ├─ positions: {symbol: Position(qty 实际持仓, target 目标仓位, avg_price)}
  └─ orders: {order_id: Order(status, filled_qty, avg_fill_price, ...)}

Context (策略看到的"世界")
  ├─ history(n) → DataFrame     取历史K线
  ├─ position / target / cash   当前状态
  ├─ qty_for() → int            按 cash_per_symbol 算整手份数(100向下取整)
  └─ submit_order(side, qty, price)  → 订单意图层

Broker (执行器, 三种实现)
  ├─ BacktestBroker: submit 入 pending, 下根 bar.open flush → fill
  ├─ SimulatedBroker: submit 立即 fill(信号价), 不调 ths_trade
  └─ LiveBroker: submit → 调 ths_trade → 异步轮询 query_order → fill/reject
```

### 7.4 订单状态机（portfolio.py + broker.py）

```
pending_submit → submitted → partial_filled → filled
                                      ↘ cancelled → rejected
```

- **submit\_order 立即更新 target**：portfolio.register\_order 先把意图落地，策略下一轮 on\_bar 不会重复下单（即使实盘要等成交回报）

- **实际持仓 qty 只在 apply\_fill 后变更**：`ctx.position` 反映真实成交，`ctx.target` 反映策略意图，两个概念严格分离

- **拒单回滚 target**：apply\_reject 把 target 恢复到下单前，避免策略以为有仓位而卡住

### 7.5 vbt\_adapter.py 回测核心

**成交口径**: 收盘出信号 → 次日开盘成交（无未来函数）

- 实现方式: entries/exits boolean 序列 `shift(1)`，vbt `price=open` 成交

- 手续费: ETF 万1，无印花税（A股 ETF，用户默认）

**常见坑（已修复，但改代码时容易再犯）**：

1. **pandas Date 歧义**：`df["date"]` 既是列名又想设成 index → `ValueError: Date is both column and index`。修复：`dates = df.pop("date")` 先弹再设 index
2. **numba pyobject 报错**：OHLCV 列含 object dtype → vectorbt 底层 numba 报 `non-precise type array(pyobject, 1d, C)`。修复：所有数字列先 `pd.to_numeric(df[c], errors="coerce")`，`astype(np.float64)` 传给 vbt，entries/exits/price/close 全转原生类型
3. **bars 数量不足导致 marker 不显示**：`--limit 0` 取全历史，本地截断到 3000 根，保证 marker 区间覆盖

***

## 八、fdata 数据系统详解（★ 一切行情只走这里）

### 8.1 双模式架构

```
调用方 → strategy/fdata_client.py
          │
          ├─ 优先: TCP 连接 127.0.0.1:9701 (fdata serve 长连接模式)
          │   └─ 进程内常驻单个 eltdx client, 全局锁串行访问
          │   └─ 连接复用, 无每次初始化开销, 自动断线重连
          │   └─ source = "eltdx(serve)"
          │
          └─ 回退: subprocess 调 scripts/fdata.py CLI
              └─ 每次新建进程, 初始化 eltdx (~200ms 额外开销)
              └─ source = "eltdx(cli)"
```

启动长连接服务器（**建议开着**，取数快 5-10 倍）：

```bash
uv run python scripts/fdata.py serve --port 9701
```

服务器挂了 / 没启动 → 客户端自动回退 CLI，功能一样，只是慢点。调用方**完全无感，不需要任何判断**。

### 8.2 客户端 API（fdata\_client.py）

```python
from strategy import fdata_client

# ★ 统一实时快照: 自动路由全部品种
# 股票/ETF/指数 → eltdx serve 长连接(高频路径)
# 期货/期权/基金/外盘 → 自动降级 CLI quote 路由对应数据源
q = fdata_client.quote("601899")   # 或 sz159915, rb2610, sh510300购2700, 004075.of ...
# 返回: {code, name, type, last, pre_close, open, high, low, volume, amount, change_pct, source}
# 全部字段归一化平铺 dict

# K线 (升序 bars 列表)
bars = fdata_client.kline("sz159915", period="5m", kind="stock", adjust="qfq", limit=None)
# limit=None → 全历史 (传 --limit 0, eltdx 分钟线上限800会自动分页/回退CLI拿全量)
# 返回: [{date, open, high, low, close, volume, amount}, ...]

# 通用 CLI 透传 (期货/基金/期权/新闻等非高频命令)
result = fdata_client.cli(["futures", "rb2610", "--tq"])   # 与 CLI 输出字节级一致
result = fdata_client.cli(["etfopt", "510300"])
```

### 8.3 quote() 路由规则（对调用方透明，不用记）

- `sh*` / `sz*` / 6位数字（60/68→sh，0/3/1\*→sz）→ eltdx（股票/ETF/指数）

- `rb*` / `IF*` / `IC*` / 2位字母开头 → CLI `quote` → tqsdk 或新浪 `nf_`

- 8位期权代码（`100*` / `900*`）→ CLI `quote` → 新浪 CON\_OP\_

- `*.of` 基金代码 → CLI `quote` → 东财 fund

- `@*` / `*.*` 外盘 → CLI `quote` → akshare

### 8.4 fdata.py 服务器协议（TCP line-JSON）

每请求一行 JSON，响应一行 JSON：

```json
→ {"op": "quote", "code": "sz159915"}
← {"ok": true, "result": {...统一结构...}}

→ {"op": "kline", "code": "sz159915", "period": "5m", "kind": "stock", "adjust": "qfq", "limit": 3000}
← {"ok": true, "result": {"code":"sz159915", "count": 3000, "data":[...]}}

→ {"op": "cli", "argv": ["futures", "rb2610", "--tq"]}   ← 通用透传, 非 eltdx 类型
← {与 fdata CLI 字节级一致的 JSON}
```

并发安全：进程内一个全局锁 `_ELTDX_LOCK`，所有 eltdx 访问串行化（eltdx 自身是非线程安全的 C 扩展）；asyncio.start\_server 为每个客户端连接创建独立协程，连接之间并发。

### 8.5 数据源选型（fdata 内已封装，不要自行换源）

| 品类               | 稳定方案                         | 不要用（已踩坑）                                 |
| ---------------- | ---------------------------- | ---------------------------------------- |
| 股票/ETF/指数 快照+K线  | **eltdx** (通达信 7709 协议)      | 东财 push2 (限频封禁), pytdx/mootdx (7727端口已死) |
| 商品/金融期货实时        | tqsdk (快期 websocket, 免费账户五档) | eltdx 不覆盖期货                              |
| ETF 期权实时         | 新浪 CON\_OP\_ 批量直连            | tqsdk (ETF期权付费墙)                         |
| 商品期权实时           | tqsdk                        | —                                        |
| 基金净值/快照          | 东财 fund 域名 (非 push2)         | —                                        |
| 全球快讯/中证权重/Greeks | akshare (低频安全调用)             | —                                        |

eltdx 要点：

- `kind="index"` 取指数 K 线，否则报 `invalid kline date`

- `adjust="qfq"` 前复权

- 分钟线上限 800，用 `limit=0` 取全量后本地截断

- `TdxClient(timeout=5)`，3s 太激进偶发超时，7709 主站偶尔拒连

***

## 九、同花顺自动交易系统（ths\_trade.py）

### 9.1 子命令全集

```bash
# 下单 (--dry-run 只填不提交, 安全测试用; --price 指定限价; 不给则用联动带出的对手价)
uv run python scripts/ths_trade.py buy 601899 100
uv run python scripts/ths_trade.py buy 601899 100 --dry-run
uv run python scripts/ths_trade.py buy 601899 100 --price 34.65
uv run python scripts/ths_trade.py sell 601899 100

# ★ 高频查询 (已极致优化 ~220ms, 6x 提速)
uv run python scripts/ths_trade.py positions   # 持仓
uv run python scripts/ths_trade.py orders      # 委托 (默认今天)
uv run python scripts/ths_trade.py trades      # 成交
uv run python scripts/ths_trade.py funds       # 资金明细
# ↑↑↑ 注意: 高频命令**无 flag**！--account/--no-login 都已移除,
# 切账户/登录用下面的 switch-account

# ★ 低频切账户 (独立子命令, 不要在高频循环里调用)
uv run python scripts/ths_trade.py switch-account real   # A股实盘
uv run python scripts/ths_trade.py switch-account sim    # 模拟练习
# 功能: 读 .env THS_USER/THS_PASS, 未登录自动登录 → 切 A股/模拟 tab → 校验切换成功
# 输出: {target, mapped_tab, login_ok, account_names} JSON

# 撤单
uv run python scripts/ths_trade.py cancel --contract 1140009957
uv run python scripts/ths_trade.py cancel --code 601899
uv run python scripts/ths_trade.py cancel --all          # 优先"全撤"按钮方式

# 其他
uv run python scripts/ths_trade.py login       # 单独触发登录
uv run python scripts/ths_trade.py help        # 完整自解释帮助
```

### 9.2 高频查询极致优化原理（\~220ms/次，vs 原 \~1.3s）

| 优化点      | 原实现                     | 优化后                                                                      | 效果            |
| -------- | ----------------------- | ------------------------------------------------------------------------ | ------------- |
| 表格定位     | 整棵 AX 树遍历 5 次 → \~864ms | 深度限制 DFS，聚焦 AXScrollArea/AXGroup，面积>10万px²、宽度>400px 容器 → 直接 AXTable 语义定位 | \~30ms 定位     |
| Tab 切换等待 | 固定 sleep(0.5)           | 30ms 间隔轮询 AXRow 稳定性                                                      | 实际等待 10-100ms |
| 单元格读值    | 几何坐标聚类 y 聚行 x 映列        | 直接 `AXRows[i].AXChildren` 递归                                             | 0 ms 额外开销     |
| 冗余检查     | 登录检查/账户校验/弹窗扫描 每轮都做     | **全部移除**，高频路径零检查                                                         | 省 \~300ms     |
| 弹窗扫描     | 每次全窗口扫                  | 懒加载，正常查询路径完全跳过                                                           | 省 \~200ms     |

**拆分原则（必须遵守）**：高频命令（positions/orders/trades/funds）只管查表，不管切账户/登录；账户切换/登录完全由独立的 `switch-account` 子命令处理（放周期性调度里，不影响 API 性能）。

### 9.3 下单链路与 Bark 通知

**下单失败必须通知用户的两种场景**（Bark 推送）：

1. **券商明确拒绝**：结果弹窗含「警告/错误/失败/不足/不允许/拒绝」等失败词 → 立即 Bark
2. **状态未知**：点了确认但等不到券商对话框/返回结果 → 超时后 Bark

Bark 实现（ths\_trade.py `notify_bark()`）：

- 从 `.env` 环境变量 `THS_BARK_KEY` 读取密钥

- 无 key / 网络失败 → 静默跳过，绝不影响交易主流程

- 通知内容必须包含：方向(buy/sell)、代码、数量、价格、**券商原始返回文本**

### 9.4 同花顺代码联动机制（AX 核心坑）

```
错误做法 ❌: AXSetValue(code_field, "601899") → 直接填值不聚焦
结果: 同花顺不触发联动 → 市场为空 → 提交报"市场代码不允许为空"

正确做法 ✓: 1. AXSetAttributeValue(code_field, "AXFocused", True)  # 先聚焦
           2. AXSetAttributeValue(code_field, "AXValue", "601899") # 再填值
           3. time.sleep(0.2)  # 等联动带出对手价和市场代码
           4. 不! 要! 清! 空! 价! 格! 框! (用户明确要求保留联动出的对手价)
```

联动失败处理：填单前先清理残留弹窗，联动失败自动重试一次；重试后仍失败 → 中止提交（避免废单），**因为连续下单会触发同花顺代码联动停止响应的面板退化问题，必须重启客户端才能恢复**。

### 9.5 main\_window 获取校验

获取同花顺主窗口必须校验元素：

1. 枚举 `AXWindows` → 过滤 `role == "AXWindow"` 且有有效 frame 且有子元素
2. 5 次重试取面积最大者
3. 仍无效 → 走 `AXMainWindow` / `AXFocusedWindow` 兜底，同样校验

AX API 有瞬时故障会返回 `role=AXApplication` 的错误元素，不校验会导致后续所有操作报错。

### 9.6 权限要求（macOS）

1. 运行脚本的终端 App（iTerm2 / Terminal / VS Code）必须在 **系统设置 → 隐私与安全性 → 辅助功能** 中勾选
2. Karabiner-Elements 16.x 升级后，如果脚本通过 `shell_command` 触发并需要辅助功能权限：

   - 不要加脚本本身

   - **必须加** **`/Library/Application Support/org.pqrs/Karabiner-Elements/Karabiner-Console-User-Server.app`**（TCC 按责任进程/父进程归因）

***

## 十、条件单引擎（condition\_orders.py）

### 10.1 状态机（每个条件单独立盯一个标的）

```
  最新价相对昨收 ≤ trigger_gap_pct(负数, 如 -4%)
  且仅在开盘后前 open_window_min(默认3) 分钟内
         │
         ▼
  ┌─────────────┐   买入成交(等待确认)   ┌─────────────┐
  │   WATCH     │ ───────────────────→ │    ARMED    │
  │  (盯盘等跌) │                       │ (等反弹卖)  │
  └─────────────┘                       └──────┬──────┘
         ▲                                    │
         │ 跨日重置 buy_locked                 │ 现价 ≥ 买入价 × (1 + sell_rally_pct)
         │                                    │
         │                              ┌─────▼─────┐
         └──────────────────────────────│   DONE    │
           买入被拒/撤销 → 终止本单     │  (结束)   │
                                        └───────────┘
```

- **买入窗口限制**：只在 `09:30` 开盘后的前 3 分钟内判定"跌破阈值买入"，超时当日 `buy_locked=True`，不再买入（等待下一个交易日）

- **交易时段**：`09:30–11:30 / 13:00–15:00`，非交易时段不判定但保持循环（周一到周五 0-4，周六日跳过）

- **行情来源**：`fdata_client.quote()` → `last` / `pre_close` → 算涨跌幅（`(last-pre_close)/pre_close*100`）

- **同花顺行情服务器连接未恢复**：quote 返回 last=None → 引擎保持 WATCH，不执行任何下单

### 10.2 配置方式

编辑 `scripts/condition_orders.py` 顶部 `CONDITION_ORDERS` 列表：

```python
CONDITION_ORDERS = [
    {
        "id": "co_601899_gap",          # 唯一ID
        "symbol": "601899",             # 标的 (6位数字自动补前缀)
        "trigger_gap_pct": -4.0,        # 相对昨收跌4%触发买入 (负数=低开)
        "buy_qty": 1000,                # 买入 1000 股 (100整数倍)
        "sell_rally_pct": 1.0,          # 反弹达到买入价+1%卖出
    },
]
```

### 10.3 运行

```bash
# 模拟成交模式 (安全, 默认, 不碰同花顺)
uv run python scripts/condition_orders.py --poll 5

# 真实下单 (LiveBroker → ths_trade.py, 需同花顺正常运行)
uv run python scripts/condition_orders.py --live --poll 5
```

- 多个条件单 `asyncio.gather` 并发执行，每单独立协程、独立 Context

- 行情拉取用 `asyncio.to_thread`，不阻塞事件循环

- 记账复用 runtime 层（共享一个 Portfolio + Broker），与回测/实盘同口径

***

## 十一、常见 Bug 排查速查

### 前端图表类

| 现象                            | 可能原因                                                     | 修复方向                                                                                                           |
| ----------------------------- | -------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| 图会话挂载策略报错 500                 | pandas date 列/索引冲突；numba pyobject；uvicorn --reload 旧代码   | 查 backend/main.py `/api/backtest` → vbt\_adapter.py 三处修复：pop date → pd.to\_numeric → astype float64；重启 uvicorn |
| K线 marker 不显示                 | bars 数量不够（limit 太小）；日期格式不匹配（分钟级/日级双轨）                    | 传 `limit=0` 取全历史；KLineChart 双轨日期匹配代码是否齐全                                                                       |
| 分时图均价线预先画完整直线                 | 流式测试时 VWAP 计算方式不对，把最后值提前画了                               | 检查 IntradayChart VWAP 数组长度是否与 bars 一致，只同步推进                                                                    |
| ECharts 报错 "series undefined" | merge 模式传了空 series 对象（无 type/name）                       | setOption merge 前过滤掉空 data 的 series                                                                            |
| React StrictMode 下图表闪烁/消失     | useEffect 双执行，dispose 后 ref 没置 null                      | dispose 里 `chartRef.current?.dispose(); chartRef.current = null`                                               |
| 5 秒轮询模式数据还是卡                  | 旧代码用 REST 轮询 /api/intraday → 已换 WebSocket /ws/market 每秒推 | 检查是否走了 MarketWs（ws.ts），不是 REST 轮询                                                                              |

### 后端 API 类

| 现象                            | 可能原因                                                                                     | 修复方向                                                                 |
| ----------------------------- | ---------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| API 响应 10-30 秒卡住、WebSocket 中断 | 某个阻塞调用忘了加 `asyncio.to_thread()`                                                          | 全文搜索 `subprocess.run` / `eltdx` / `ths_trade` 调用处，看是否被 to\_thread 包裹 |
| `/api/positions` 返回空/报错       | 同花顺未运行 / 未打开交易界面 / 未登录                                                                   | 先手动打开同花顺 → 交易下单界面 → 确保正常显示持仓；用 ths\_trade.py CLI 自测                  |
| `/api/intraday` 返回 bars 空     | 非交易时段（应自动取上一交易日）；fdata 连接失败                                                              | 检查 fetch\_intraday\_1m 逻辑（是否 640 根 1m bars 回退上一交易日）                  |
| `/ws/mock_stream` 连不上         | next.config.ts rewrite 是否配置了 `/ws/mock_stream → localhost:8000`；前端 ws.ts URL 是否走 rewrite | 检查 next.config.ts 三条 rewrite 规则是否齐全                                  |

### 数据层类

| 现象                         | 可能原因                                 | 修复方向                                                            |
| -------------------------- | ------------------------------------ | --------------------------------------------------------------- |
| fdata quote 返回 None/空      | fdata serve 未启动 + CLI 路径也不通；同花顺行情未恢复 | 单独跑 `uv run python scripts/fdata.py quote sz159915` 调试；启动 serve |
| eltdx 连接超时 / 拒绝连接          | 7709 主站不稳定；timeout 太短（<5s）           | TdxClient timeout=5；fdata\_client 自动回退 CLI，一般无感                 |
| 分钟 K 线只有 800 根             | eltdx 分钟线上限 800                      | 传 `limit=0`（fdata\_client 默认处理，不要显式传 limit=800）                 |
| 指数 K 线报 invalid kline date | 没传 kind="index"                      | fdata\_client.kline kind 参数正确传 "index"                          |

### 交易层类

| 现象                          | 可能原因                       | 修复方向                                              |
| --------------------------- | -------------------------- | ------------------------------------------------- |
| 报"市场代码不允许为空"                | 代码框没聚焦就填值，不触发联动            | fill\_code 必须先 AXSetValue(AXFocused, True) 再填值    |
| 报 AX API -25212（禁用）         | 脚本把同花顺激活到前台 → 触发反自动化保护     | **所有交易操作绝不能调 activate()/前台化**；重启同花顺恢复             |
| 下单后查不到持仓/委托（实际成功）           | 同花顺连续下单导致面板退化，联动停止响应       | 实现"联动失败→中止提交"机制；重启同花顺客户端                          |
| 连续撤单不行 → 全撤按钮               | 双击 AXRow 不可靠               | cancel 优先走"全撤"按钮方式；撤单按钮匹配含"确认/确定/是"               |
| switch-account 成功但高频查询还是旧账户 | 高频命令和切账户是两个独立进程，同花顺窗口状态不共享 | switch-account 必须在查询进程启动前完成；或查询命令本身带 tab 切换（但会变慢） |

***

## 十二、硬约束清单（任何情况下都不得违反）

1. **成交口径统一**: 收盘出信号 → 次日开盘成交。回测 vbt\_adapter shift(1)+price=open；实盘 BacktestBroker flush\_pending(下根 open)。任何时候不能让"当日 close 信号当日 close 成交"（未来函数）
2. **策略信号统一 target position 0/1**，不是买卖动作。策略代码中**禁止出现** **`if backtest/if live`** **分支判断**，保持 Write Once Run Anywhere
3. **所有阻塞调用必须** **`asyncio.to_thread()`** **包装**：eltdx TCP、subprocess、ths\_trade AX、文件 IO
4. **同花顺交易全程 AX 后台操作，绝不激活前台**（会触发反自动化封禁）
5. **高频查询命令与低频切账户/登录彻底分离**：positions/orders/trades/funds 无 flag 零检查；切账户用独立 switch-account 子命令
6. **取数只走 strategy/fdata\_client.py**，不得直连 eltdx / subprocess 调 akshare
7. **fdata kline limit 为空 = 全历史 = 传 --limit 0**（CLI 与 serve 语义统一）
8. **同花顺下单必须显式传价格参数**（避免依赖行情联动带出，联动挂了会下单失败）
9. **下单失败 Bark 通知两种场景必发**：券商拒单结果弹窗 + 提交后状态未知超时
10. **根路径** **`/`** **必须重定向到** **`/charts`**，禁止跳转已删除的 `/live` 页面
11. **前端全局数字格式化必须用 fmt.ts 统一函数**：价格<10三位小数，≥10两位；百分比最多两位；金额千分位两位
12. **React StrictMode 下 ECharts dispose 必须置 ref = null**，否则 StrictMode 双执行后实例泄漏
13. **Docker 远期架构**：前端+后端同容器运行，但下单(ths\_trade)必须宿主机跑，容器走 Webhook → 宿主机 agent；charts.db 挂数据卷

***

## 十三、部署架构

### 当前状态（本地开发）

```
本机 macOS
  ├─ fdata serve (TCP 9701, 可选)
  ├─ FastAPI backend: 0.0.0.0:8000 (uvicorn)
  ├─ Next.js frontend: localhost:3001 (next dev)
  ├─ 同花顺 Mac 客户端 (GUI, 交易用)
  └─ 条件单引擎: python condition_orders.py (独立进程)
```

### 远期 Docker 部署（架构已定，暂未完整实施）

```
Docker 容器 (Ubuntu 24.04)
  ├─ Nginx: 80 端口, /api/* → FastAPI 8000, / → Next.js 静态文件
  ├─ FastAPI: 127.0.0.1:8000 (uvicorn)
  ├─ Next.js: next build → 静态文件 (next start 或 standalone)
  ├─ charts.db: /data 目录, 挂载宿主机数据卷持久化
  └─ fdata serve: 127.0.0.1:9701

宿主机 macOS
  ├─ 同花顺 Mac 客户端 (必须本机 GUI)
  └─ 轻量 agent: 接收容器 Webhook 下单指令 → 调 ths_trade.py 操作同花顺
```

Dockerfile + start.sh + nginx.conf 已存在于项目根目录，但 ths\_trade 宿主机 agent 尚未实现。

***

## 十四、环境变量与配置文件

### `.env`（项目根目录，gitignore 未提交）

```bash
# 同花顺登录凭据
THS_USER=你的交易账号
THS_PASS=你的交易密码

# Bark 推送密钥 (下单失败通知)
THS_BARK_KEY=https://api.day.app/xxxxx/

# tqsdk 账号 (期货/商品期权实时)
TQ_USER=xxx
TQ_PASS=xxx

# fdata serve 配置 (可选, 默认值如下)
FDATA_HOST=127.0.0.1
FDATA_PORT=9701
FDATA_TIMEOUT=8
```

### `strategy/config.json`（策略配置）

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

注意：`live.dry_run=true` 是默认安全默认值，改 false 才会真实下单。**永远不要默认设 false**。

### `state/*.state.json`（Portfolio 内存态落盘）

由 `Portfolio.save()` 写入，格式：`{cash, positions:{symbol:{qty, avg_price, target, ...}}, orders:{...}}`。不要手动编辑。

***

## 十五、命令速查表

```bash
# ===== 启动 =====
./dev.sh                                                  # 一键启前后端
uv run python scripts/fdata.py serve --port 9701         # 启 fdata 长连接

# ===== 数据 =====
uv run python scripts/fdata.py quote sz159915            # 实时快照
uv run python scripts/fdata.py kline sz159915 --period 5m --limit 0  # 全量 5m K线
uv run python scripts/fdata.py futures rb2610 --tq       # 期货实时
uv run python scripts/fdata.py doctor                    # 10项数据源自检

# ===== 交易 =====
uv run python scripts/ths_trade.py buy 601899 100 --dry-run   # 模拟填单
uv run python scripts/ths_trade.py positions                 # 查持仓 (~220ms)
uv run python scripts/ths_trade.py switch-account sim        # 切到模拟账户
uv run python scripts/ths_trade.py cancel --all              # 全撤

# ===== 条件单 =====
uv run python scripts/condition_orders.py --poll 5      # 模拟模式
uv run python scripts/condition_orders.py --live --poll 5  # 实盘

# ===== 回测 =====
curl -X POST http://localhost:8000/api/backtest \
  -H "Content-Type: application/json" \
  -d '{"strategy":"sma_cross","symbol":"sh510300","params":{"fast":10,"slow":30}}'

# ===== 测试 =====
uv run python -m unittest tests.test_strategy_system     # 策略系统单测
```

***

## 十六、给接手 AI 的特别提示

1. **中文沟通**：用户偏好中文
2. **不要瞎创新**：用户喜欢"不要多余操作、最直接代码、最少改动"。用户明确说过不要清空价格框、不要做多余检查
3. **diff 要小**：高频命令优化要追求极致性能，比如去掉一个多余的 tree scan 就能省 800ms
4. **生产安全 > 一切**：下单类功能默认 dry-run，真实下单必须**反复确认**用户意图。Bark 通知失败场景不能省
5. **不要重写架构**：当前分模块（前端/后端/策略/数据/交易/条件单）是多轮迭代出来的稳定结构，不要说"我建议用 xx 框架重写"
6. **报错优先查「常见 Bug 排查」和「硬约束清单」**，80% 的问题都在里面有答案
7. **改代码前先读对应文件前 30 行注释**：每个文件顶部都有详尽的设计说明和踩坑记录
8. **uv run python ...**：任何项目 Python 脚本执行都用项目 venv（uv run），不要系统 Python
9. **实盘策略页面已删除**：不要恢复 /live 路由或相关导航链接
10. **图表左右拖动和缩放交互**：K线图和分时图的交互状态必须独立（缩放一个不影响另一个）

***

## 十七、待办与已知未完成项

1. **Docker 部署完整闭环**：宿主机 ths\_trade agent + Webhook 未实现
2. **intraday\_t 策略参数调优**：回测表现 -1.9% vs 买入持有 +5.1%（两市缩量环境），vol\_expand/min\_amount\_yi 阈值需重调
3. **条件单实盘全面验证**：同花顺行情连接稳定后需跑一次完整 WATCH→ARMED→DONE 实盘闭环
4. **撤单自动双击路径未实盘验证**：人工测过同结构撤单成功，但脚本自动路径未跑真实单
5. **nextjs/ 目录独立项目**：资金流向实验页，需确认是否合并入主 frontend
6. **stockview/ 旧 Streamlit**：功能保留但非主流程，确认是否需要迁移到 Next.js

***

> 文档版本: v1.0 | 生成日期: 2026-09-02 | 基于项目状态: 前端 vNext.js15/ECharts6, 后端 FastAPI, 策略 vbt\_adapter, fdata serve 模式, ths\_trade 高速查询优化版


# strategy/ — 策略框架

Write Once, Run Anywhere：**策略只写一个 `signal(df, params)` 返回 0/1 目标仓位序列**，vectorbt 回测、参数优化、实盘 on_bar 事件驱动 全部自动复用，策略中禁止 `if backtest/live` 判断。

> 完整手册见项目根目录 `SKILL.md` 第七章；精简交接见 `docs/AGENT_HANDOFF.md`。

---

## 架构总览

```
strategy/strategies/*.py        ★ 写这一个 (放进去自动注册, 零注册)
    │
    │ Strategy 基类 (base.py) 双接口:
    │   ├─ signal(df, params) → pd.Series [0/1]   向量化, 给 vbt_adapter 回测/优化
    │   └─ on_bar(bar, ctx)    async              事件驱动, 给实盘 (默认桥接 signal, 通常不用重写)
    │
    ├── backtest/vbt_adapter.py   ★ 主回测路径: signal → entries/exits shift(1) + vbt price=open
    │                              收盘出信号 → 次日开盘成交 (无未来函数)
    │
    └── runtime/                  ★ 策略运行时三层
        ├── portfolio.py          Portfolio: 内存态 + state.json 落盘
        │   严格分离: target (策略意图, submit_order 立即更新, 防重复下单)
        │           vs qty (实际持仓, apply_fill 后才更新)
        ├── ctx.py                Context: 策略看到的世界: history/qty_for/submit_order
        └── broker.py             三种 Broker 实现
            ├─ BacktestBroker     submit→pending, 下根 bar.open flush → fill (默认回测)
            ├─ SimulatedBroker    submit→立即 fill (信号价) → 条件单/流式测试模拟
            └─ LiveBroker         submit→调 ths_trade→异步轮询 query_order→fill/reject  (实盘)

┌──────────────────────────────────────────────────┐
│ 统一取数入口: strategy/fdata_client.py            │
│  fdata_client.quote(code)     自动路由所有品种     │
│  fdata_client.kline(...)      limit=None→全历史    │
│  fdata_client.cli(argv)       通用 CLI 透传        │
│  内部: fdata serve 长连接优先 → 失败自动回退 CLI    │
└──────────────────────────────────────────────────┘
```

---

## 快速上手

### 1. 写一个策略

在 `strategy/strategies/` 下放一个 py 文件，定义 `Strategy` 子类，**保存即被发现**：

```python
# strategy/strategies/rsi_contrarian.py
import pandas as pd
from strategy.base import Strategy, INT, FLOAT

class RsiContrarian(Strategy):
    NAME = "rsi_contrarian"             # 唯一 ID, 与文件名一致
    TITLE = "RSI 反转"                  # 中文显示名
    TIMEFRAME = "day"                   # day / 5m / 15m / 30m / 60m
    LOOKBACK = 500                      # on_bar 取历史 bar 数
    SYMBOLS = ["sz159915"]              # 默认标的列表
    
    PARAMS = {
        "window":    {"type": INT,   "default": 14,  "min": 5,   "max": 60},
        "oversold":  {"type": INT,   "default": 25,  "min": 5,   "max": 40},
        "overbought":{"type": INT,   "default": 75,  "min": 60,  "max": 95},
    }
    
    def signal(self, df: pd.DataFrame, params: dict) -> pd.Series:
        """返回 0/1 目标仓位序列, 写这一次就够了.
        基类默认 on_bar() 会取最后一根 target → submit_order.
        """
        w  = int(params["window"])
        os_ = int(params["oversold"])
        ob_ = int(params["overbought"])
        
        delta = df["close"].diff()
        gain = delta.clip(lower=0).rolling(w).mean()
        loss = (-delta.clip(upper=0)).rolling(w).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - 100 / (1 + rs)
        
        # RSI < 超卖 → 持有; RSI > 超买 → 空仓
        pos = pd.Series(0, index=df.index)
        pos[rsi < os_] = 1
        pos[rsi > ob_] = 0
        # RSI 中间区间保持前值 (ffill)
        pos = pos.replace(0, np.nan).ffill().fillna(0).astype(int)
        return pos.where(rsi.notna(), 0)
```

### 2. 回测 (两种方式)

**方式 A** — 图会话前端挂载（给用户看可视化）：
访问 http://localhost:3001/charts → 新建 sz159915 日K 图 → 策略下拉选 `rsi_contrarian` → 调参数 → 点「挂载策略」→ K 线自动画买卖点 marker + 统计。

**方式 B** — 后端 REST 直接调：
```bash
curl -X POST http://localhost:8000/api/backtest \
  -H "Content-Type: application/json" \
  -d '{"strategy":"rsi_contrarian","symbol":"sz159915","params":{"window":14,"oversold":25,"overbought":75}}'
```

### 3. 参数优化

```bash
curl -X POST http://localhost:8000/api/optimize \
  -H "Content-Type: application/json" \
  -d '{
    "strategy": "rsi_contrarian",
    "symbol": "sz159915",
    "param_grid": {
      "window": [7, 14, 21],
      "oversold": [20, 25, 30],
      "overbought": [70, 75, 80]
    }
  }'
```

### 4. 运行时

```python
from strategy.runtime.portfolio import Portfolio
from strategy.runtime.broker import SimulatedBroker
from strategy.runtime.ctx import Context
from strategy.registry import Registry

# 1. 构建运行时
reg = Registry("strategy/strategies")
strat = reg.get("rsi_contrarian")
pf = Portfolio(symbols=["sz159915"], cash=100000, state_path="state/test.state.json")
broker = SimulatedBroker(pf)      # 或 LiveBroker(pf) → 真下 ths_trade
ctx = Context(strategy=strat, portfolio=pf, broker=broker, symbol="sz159915", params={...})

# 2. 事件驱动单轮 (每根 bar 进来调用一次)
await strat.on_bar(bar, ctx)
broker.flush_pending(next_bar_open_price)   # BacktestBroker 用, 模拟次日开盘成交

# 3. 落盘 (通常在策略跑完后)
pf.save()
```

---

## 运行时核心概念

### Portfolio：target vs qty 严格分离

这是防止**重复下单**的关键机制：

```python
pf.positions["sz159915"] = Position(
    qty     = 0,           # 实际持仓数. 只有 apply_fill() 后会变
    target  = 0,           # 策略意图仓位 (0/1 比例). submit_order 立即变
    avg_price = 0.0,
    filled_cost = 0.0,
    realized_pnl = 0.0,
)
```

- `ctx.submit_order("buy", qty, price)` → 调 `portfolio.register_order(order_id, ...)`：
  - 先把 `target` 设为 1（立即生效，策略下一轮 on_bar 不重复提交）
  - 但 `qty` 仍然是 0
- 真实成交后 `portfolio.apply_fill(order_id, fill_qty, fill_price)`：
  - `qty += fill_qty`（实际仓位更新）
- 拒单 `portfolio.apply_reject(order_id, reason)`：
  - `target` 回滚到下单前（没成交就不该有意图）

### 订单状态机

```
pending_submit (刚注册, broker 还没提交)
      ↓ submit()
submitted (已交给交易所/同花顺受理)
      ↓ partial fill
partial_filled (部分成交, 填剩余继续轮询)
      ↓ 全部成交
filled (结束)
      ↓ 用户撤 / 券商拒
cancelled → rejected
```

LiveBroker 轮询 ths_trade `query_order`，从 `备注` + `成交价格` + `撤销数量` 推断状态迁移。

### 订单意图层 vs 底层 Broker

策略只管调 `ctx.submit_order(side, qty, price)`，**不直接碰 ths_trade / 不关心 broker 实现**：
- 回测 → BacktestBroker，下一根 bar.open 成交
- 流式测试 → SimulatedBroker，信号价立即 fill
- 实盘 → LiveBroker，调 ths_trade + 异步轮询成交流

策略代码完全一样，只换 broker 对象 → Write Once, Run Anywhere。

---

## 回测口径（vbt_adapter.py）

**一句话：收盘出信号 → 次日开盘成交。**

```python
positions = signal(df, params)    # t 日收盘后, 基于 t 日 close 算出信号
entries = (positions == 1) & (positions.shift(1) != 1)
exits   = (positions == 0) & (positions.shift(1) != 0)

# entries/exits t 日 True → t+1 日 open 成交
entries_t1 = entries.shift(1).fillna(False)
exits_t1   = exits.shift(1).fillna(False)

import vectorbt as vbt
pf = vbt.Portfolio.from_signals(
    close   = df["close"],
    entries = entries_t1,
    exits   = exits_t1,
    price   = df["open"],     # ★ 以 open 成交, 不是 close
    fees    = 0.0001,         # ETF 佣金万 1, 无印花税
    freq    = freq,
)
```

MA20 回测基准（sz159915, 2011-12 ~ 2026-08）：总收益 +202.2% / 年化 7.8% / 最大回撤 -57.1% / 215 笔 / 胜率 24.7%。震荡市拖累（2016 -25%、2018 -22%），改进方向：波动率过滤、均线缓冲带、与五因子共振。

---

## 内置策略

| 策略文件 | NAME | 说明 | 默认标的 |
|---|---|---|---|
| ma20_trend.py | `ma20_trend` | 日线收盘 > MA20 持有，< MA20 空仓 | sz159915 |
| sma_cross.py | `sma_cross` | 快慢 SMA 金叉 / 死叉（日线） | sh510300 |
| intraday_t.py | `intraday_t` | 5m 日内做 T：10:00 后两市量能预估定方向，放量先买后卖缩量先卖后买 | sz159915 |
| tick_buy_sell.py | `tick_buy_sell` | tick 级策略模板（参考用） | — |

**intraday_t 未调优**：2026-03~08 回测 -1.9% vs 买入持有 +5.1%（两市持续缩量 ~1.97 万亿，2 万亿阈值下策略多处于先卖后买持有态）。vol_expand/min_amount_yi 阈值需重调后再启实盘。

---

## 实盘配置（strategy/config.json）

```json
{
  "strategies": {
    "ma20_trend": {
      "enabled": true,
      "symbols": ["sz159915"],
      "params": {"window": 20},
      "live": {
        "dry_run": true,              # ★ 默认安全值, true=只记账不下单, 改 false 才真下
        "poll_seconds": 60,           # 评估轮询间隔
        "execute_time": "14:55",      # 每日执行时间 (日线策略: 收盘前)
        "cash_per_symbol": 10000,     # 每标的分配资金
        "qfq": true                   # 前复权
      }
    }
  }
}
```

在线编辑：http://localhost:3001/config

---

## 单元测试

```bash
uv run python -m unittest tests.test_strategy_system
```

覆盖：Portfolio 注册订单 / target vs qty 分离 / apply_fill / apply_reject / BacktestBroker flush_pending / signal → on_bar 桥接。

---

## 关键文件索引

| 文件 | 作用 |
|---|---|
| `base.py` | Strategy 基类双接口 / PARAMS schema / 默认 on_bar 桥接 |
| `registry.py` | 策略零注册自动发现（importlib 扫描 strategies/*.py） |
| `config.py` | config.json 读写 + 默认值填充 |
| `manager.py` | 策略进程 start/stop/status CLI（独立进程 + 心跳） |
| `runner.py` | `run_live()` 事件驱动实盘单轮 |
| `fdata_client.py` | ★ 统一取数入口，serve 优先/CLI 回退/自动路由全品种 |
| `mock_market.py` | Mock 市场（流式测试逐根 advance_bar） |
| `trader.py` | 旧实盘执行器（取数入口已迁 fdata_client） |
| `backtest/vbt_adapter.py` | ★ 主回测路径（vectorbt + 次日开盘成交口径） |
| `runtime/portfolio.py` | Portfolio + 订单状态机 + state.json 落盘 |
| `runtime/ctx.py` | Context 订单意图层（submit_order / history / qty_for） |
| `runtime/broker.py` | 三种 Broker 实现 |
| `runtime/runner.py` | Runner.run_live 事件驱动单轮 |
| `strategies/*.py` | 策略代码（放进去自动注册） |

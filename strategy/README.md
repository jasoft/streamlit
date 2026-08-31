# strategy — 自动化交易系统

**策略(信号) → 回测引擎 / 实盘执行 → 同花顺下单** 的最小完整闭环。
策略只写信号逻辑，与回测/执行完全解耦；每个策略可独立启动/停止（独立常驻进程），
可并行多个，接入正式下单系统 `scripts/ths_trade.py`。

```
strategy/strategies/*.py   策略 (只实现 target_position: 日K -> 0/1 目标仓位, 零注册自动发现)
        │
        ├── engine.py      回测引擎 (次日开盘成交/佣金万1/整手100份)
        │        └── dashboard.py 回测页: 多选策略+实时调参+图表
        │
        └── trader.py      实盘执行 (信号->份数->ths_trade 下单->state.json 记账)
                 ├── runner.py    每策略一个常驻进程, 交易日 execute_time 自动跑
                 ├── manager.py   策略管理器: start/stop/status (独立进程+心跳)
                 ├── run_live.py  cron 入口: 全部启用策略跑一轮 (默认 dry-run)
                 └── dashboard.py 实盘管理页: 启动/停止/立即执行/对账
config.json   每策略: enabled/symbols/params/cash_per_symbol/live(dry_run,execute_time,qfq)
state/        运行时: pid/心跳/日志/应有仓位记账
```

## 快速上手

```bash
# 看板 (回测 / 实盘策略管理 / 配置 三页)
uv run streamlit run strategy/dashboard.py

# 策略管理器 CLI
uv run python strategy/manager.py list
uv run python strategy/manager.py start ma20_trend
uv run python strategy/manager.py stop  ma20_trend
uv run python strategy/manager.py status

# 定时任务 (cron, 交易日 14:55)
uv run python strategy/run_live.py            # dry-run
uv run python strategy/run_live.py --execute  # 真实下单

# 单策略 CLI 回测 (逻辑同看板)
uv run python strategy/ma20_backtest.py --qfq
```

## 内置策略

| 策略 | 规则 | 默认标的 |
|---|---|---|
| `ma20_trend` | 收盘>MA20 持有, 收盘<MA20 空仓 (日线) | sz159915 创业板ETF |
| `sma_cross` | 快线上穿慢线持有, 下穿空仓 (日线) | sh510300 沪深300ETF |
| `intraday_t` | 日内做T (5m线): 10:00 后按两市量能预估定方向——放量(>近N日均×1.05 且≥2万亿)先买后卖, 缩量/变化不大/<2万亿先卖后买(需底仓); 买入=恐慌放量杀跌(RSI6≤25 且 5m量≥1.8×均量 且价在VWAP下), 回升到VWAP±0.3%止盈; 冲高≥1.5%后20分钟未创新高+RSI死叉→卖出, 恐慌或回VWAP回补; 14:50 强制了结 | sz159915 创业板ETF |

日内策略在策略类上声明 `TIMEFRAME = "5m"`, 回测/实盘/评估自动切换数据周期; 实时量能预估
复用 `stockview/main.py` 的 `get_estimate_amount` 与近N日均额。

**加新策略**：在 `strategy/strategies/` 放一个 py 文件，定义 `Strategy` 子类
（NAME/TITLE/PARAMS schema/SYMBOLS + `target_position(df, params)`），保存即被看板与管理器发现。

## MA20 回测基准（sz159915, 2011-12 ~ 2026-08）

总收益 +202.2% / 年化 7.8% / 最大回撤 -57.1% (2015-06→2019-01) / 215 笔 / 胜率 24.7%，
买入持有 +332.0%。震荡市 (2016 -25%, 2018 -22%) 连续止损是主要拖累，改进方向：
波动率过滤、均线缓冲带、与 etf_signal.py 五因子共振。

## 注意

- 回测口径：收盘出信号、次日开盘成交（无未来函数）；实盘在 execute_time 用准收盘数据出信号下单，与回测口径基本一致。
- 实盘进程盘中 (9:25-11:30 / 13:00-15:05) 每 `live.poll_seconds`(默认60s) 取数评估一轮：
  **每次取数都输出一条处理结果**（价格/MA/目标仓位/是否触发警报），
  写入 `state/{name}.evals.jsonl` 流水 + 心跳 + stdout 日志；到 execute_time 当天下单一次。
- 实盘页实时面板（运行中每 60s 自动刷新）：日内 5m 分时图（叠加 MA20/昨收/最新价）、
  近 120 日 K 线（叠加均线与当前应有仓位标记）、最新评估指标、处理结果流水表。
- 默认 dry-run（只填单不提交）；真实下单需同花顺客户端在运行，且看板/CLI 显式 `--execute` 或关掉 live.dry_run。
- state.json 记"策略应有仓位"，与同花顺实际持仓（实盘页「读取持仓」）只做人工对账，不自动纠偏。
- 159915 无分红，不复权=前复权；其他标的请开 `qfq`（走 fdata）。
- 测试：`uv run python -m unittest tests.test_strategy_system`

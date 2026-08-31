# 项目交接文档 — streamlit 量化/行情项目

> 生成于 2026-08-31。本文档汇总了用户与 AI Agent 在本项目中的全部协作历史、决策结论、踩坑记录与当前工作区状态，供下一个 Agent 无缝接手。**接手前请完整阅读，尤其是"数据源避坑"和"同花顺 GUI 自动化"两节——里面每一条都是实测踩出来的，违反会直接导致功能失效或被封 IP。**

---

## 一、项目是什么 & 演进史

项目位于 `/Users/weiwang/Projects/streamlit`（git 仓库，分支 main，共 120 个提交），从最早一个 Streamlit A股行情可视化面板（2024 年 "first commit"）逐步演进为一条**完整的个人量化交易链路**：

1. **stockview/** — Streamlit 市场行情 Dashboard（首页决策仪表盘、ETF 五因子信号、期权、拥挤度、行业资金流等页面），Docker + Nginx 反代部署过远程服务器。
2. **数据源迁移**（2026-08-30）— 因 akshare 新浪/东财 HTTP 源频繁被限频封禁，行情类数据迁移到 **eltdx**（通达信协议），并封装成统一 CLI **fdata**。
3. **交易链路**（2026-08-30）— miniqmt 不可用，改用 GUI 自动化驱动**同花顺 Mac 客户端**下单，脚本 `scripts/ths_trade.py`。
4. **strategy/** 自动化交易系统（2026-08-30 建成）— 策略零注册/回测/实盘启停/接 ths_trade 下单，默认 dry-run。
5. **开源量化框架调研**（2026-08-30）— 见 `docs/quant-framework-report.md`，结论：vectorbt（参数扫描）+ backtrader（事件驱动复现）组合，尚未引入。

用户目标场景：A 股/ETF 个人量化，低频日线策略为主 + 日内做 T 探索，实盘账户为**国泰海通**（同花顺客户端），另有"模拟练习"模拟账户。

## 二、当前项目结构与文件状态

```
stockview/            Streamlit 应用主体
  main.py (~1044行)   首页决策仪表盘（市场状态评分、估算两市成交额 get_estimate_amount、迷你分时图等）
  etf_signal.py       创业板ETF(159915) 五因子买卖信号面板（快照+日K，已迁 eltdx）
  options.py          ETF 期权 T 型报价/希腊字母/IV（akshare 新浪通道）
  congestion.py / fund_flow.py / hs300_industry.py / index_*.py / if_im_strategy.py
  db_helper.py        SQLite 结构化存储（market_data.db，市场面板历史趋势）
  tdx_source.py       通达信(eltdx)数据源封装
  debug_*.py / check_names.py / test_*.html   调试脚本（未提交）

scripts/
  fdata.py (~1661行)  ★ 统一金融数据 CLI，12+ 子命令，详见第三节
  ths_trade.py        ★ 同花顺 Mac GUI 自动交易，详见第四节
  example_tick_strategy.py / test_realtime_rate.py   watch 子命令示例与压测

strategy/             自动化交易系统，详见第五节
datasource_test/      2026-08-30 数据源实测报告 REPORT.md + 测试脚本 + results*.json
docs/
  quant-framework-report.md   开源量化框架调研报告（vectorbt/backtrader 实测）
  AGENT_HANDOFF.md            本文档
tests/test_strategy_system.py   strategy 系统单测（uv run python -m unittest tests.test_strategy_system）
market_data.db        SQLite 行情/面板历史库
```

**工作区未提交内容（git status）**：`strategy/`、`scripts/ths_trade.py`、`datasource_test/`、`docs/`、`tests/test_strategy_system.py`、`scripts/fdata.py` 的 watch 等新增（+223 行 diff）均**未提交**；pyproject.toml/uv.lock 有改动（tqsdk、pyobjc 等新依赖）。market_data.db 在仓库目录下。**注意：.env 已从 git untrack 并 gitignore，但历史提交中 .env 里的 TUSHARE_TOKEN/IWENCAI_API_KEY 仍在 git 历史里，若仓库公开建议轮换。**

## 三、数据源体系（核心决策 + 避坑）

### 3.1 选型总表（2026-08-30 实测，报告在 datasource_test/REPORT.md）

| 品类 | 稳定方案 | 不要用 |
|---|---|---|
| 股票/ETF/指数 快照+K线 | **eltdx**（通达信 7709 协议） | 东财 push2（对本机 IP 限频封禁，efinance/qstock 全挂，efinance 包内还夹带 TickFlow 推广） |
| 商品/金融期货实时 | akshare 新浪底层（`futures_zh_spot`，~110ms，10/10）或 tqsdk | pytdx/mootdx 扩展行情（7727 端口已死，官方自认失效）；**eltdx 永远不覆盖期货期权** |
| ETF 期权实时 | 新浪 `hq.sinajs.cn/list=CON_OP_代码1,代码2,...` 批量直连（akshare 未封装批量版） | tqsdk（ETF 期权是付费墙，免费账户被拒） |
| 商品期权实时 | tqsdk 免费快期账户（用户账号 **soj** 已注册，websocket 批量订阅五档，10/10） | 深度实值合约 last=nan 属正常，按活跃行权价过滤 |
| 基金净值/快照 | 东财 fund 域名（非 push2，可低频安全用） | |
| akshare 保留的低频调用 | `option_value_analysis_em`（期权 Greeks/IV）、`stock_info_global_sina`（全球快讯）、`index_stock_cons_weight_csindex`（中证成分权重） | 这三个 TDX 协议覆盖不了，必须留 akshare |

### 3.2 eltdx 实用要点（v3.0.9，https://github.com/electkismet/eltdx）

- **指数 K 线必须 `kind="index"`**：`client.bars.get("sh000001", period="day", kind="index")`，否则报 `ProtocolError: invalid kline date`。指数 bar 还带 `up_count/down_count`（涨跌家数）。
- **`all_pages=True` 返回 newest-first**——从 akshare 迁移时必须按时间升序重排，否则信号计算反向。
- 前复权：`client.bars.get("sz159915", period="day", adjust="qfq")`。
- 快照是 `QuoteSnapshot` 对象（`last_price/pre_close_price/change_pct/buy_levels/sell_levels/amount`）；K 线是 `KlineSeries`，bar 列表在 `.bars` 里（不能对 series 本身 len()）。
- 批量：`get_snapshots([codes...])` 一次请求多代码，尽量批量以压低请求数。
- Client：`with TdxClient(timeout=3) as client:`，自动测速 43 个内置 TDX 主机，保留最快 2 个 × 4 TCP 连接，30s 心跳。实测 10 req/s 持续无 throttling、p50 ~23ms、900+ 请求 100% 成功（用户需求只要 1 req/s）。
- 数据可回溯数十年（上证指数日K到 1993，8714 根，0.7s）。
- **分钟线分页上限 800**：limit>800 直接报错——用 `--limit 0` 取全量后本地截断（5m 全历史 ≈2 年/2.4 万根）。盘中取数含当日实时未完成 bar。
- License 限制：README 仅限个人学习/非商用。

### 3.3 fdata CLI（scripts/fdata.py）★ 查行情一律先走它

用户与 Agent 已按反馈两轮重构，**stdout 纯 JSON、日志全静音**，封装为 skill `fdata`（`.agents/skills/fdata/SKILL.md`）。

- **首选子命令 `quote CODE`**：按代码形态自动路由，股票/期货/期权返回**同一统一价格结构**（code/type/name/exchange/option/source/volume_unit/quote{last, pre_close, change_pct, open, high, low, 涨跌停, volume, amount, open_interest, pre_settle, bids, asks}），change_pct 统一相对昨收。
- **期货默认走新浪 `nf_` 直连**（~200ms，支持 IF2612/rb2610 短合约自动解析交易所、郑商所 3 位月份自动扩 4 位）；tqsdk 兜底 / `futures --tq` 强制（有五档但握手 2-4s）。商品/金融期货是两套字段布局。
- 其余子命令：`snapshot/kline/list`（eltdx）、`futures/copt`（tqsdk）、`etfopt/etfcodes/greeks`（新浪）、`futcontracts/futspot/news/weight`（akshare）、`nav/quote fund`（东财：`quote 004075.of` 最新净值、`nav 004075` 历史净值；货币基金不支持）、`doctor`（10 项数据源自检，退出码 0/1，tqsdk 凭据缺失时 skip 而非 fail）、`watch`（实盘 tick 监控，2026-08-31 加）。每个子命令 `--help` 含用法/示例/返回结构。
- **分钟K线**：`kline 159915 --period 5m --limit 0`（同上 800 上限规则）。
- **`watch` 子命令**：常驻进程长连接复用（eltdx / requests.Session / tqsdk websocket），首轮 ~600ms 后每轮 50-100ms。用法 `watch CODE... --interval 1 --strategy xx.py --log s.jsonl`；策略定义 `on_tick(quotes[, feed_errors]) -> list[dict]`，quotes 即 quote 统一结构；新浪源间隔勿 <1s；盘中 last=0 表示无成交且 change_pct=null；示例见 `scripts/example_tick_strategy.py`。**watch 与 strategy/ 的日K目标仓位系统是两个范式**。
- 实现注意：py_mini_racer shim 必须在 import akshare 之前执行；tqsdk 调用必须包 `_hush()` 静音；CON_OP_/nf_ 字段映射见函数注释。akshare 锁 1.18.37。
- 证券名称表有 7 天磁盘缓存 `~/.cache/fdata/names.json`（拉取要 3-4s，用户确认一周一刷即可），命中后单次 ~0.5s；删该文件强制刷新。
- 运行方式：`.venv/bin/python scripts/fdata.py <cmd>` 或 `uv run`。

## 四、同花顺 Mac GUI 自动交易（scripts/ths_trade.py）★ 每条都是实测踩坑

背景：miniqmt 不可用，改用 GUI 自动化驱动同花顺 Mac 客户端（bundle id `cn.com.10jqka.macstock`）。脚本支持 buy/sell/cancel/positions/orders/trades/funds/login + `--dry-run` + 每步耗时打点，JSON 输出。

- **代码框联动是纯 AX 的**：先 AX set `focused=True` 再 AX set value 就会触发联动（识别市场/带对手价/名称）——完全后台、无需激活窗口、无需键盘事件。直接 set value 不聚焦则不联动，提交报"市场代码不允许为空"。价格/数量直接 AX set value。
- **联动带出对手价后千万不要清空价格框**——只有显式 `--price` 才覆盖（用户明确要求过）。
- 键盘/鼠标事件走 osascript System Events（CGEventPostToPid 同花顺不收），必须逐字 keystroke+delay；已降级为 `--keyboard` 备用路径（需前台）。
- **委托确认框是 attached sheet，不在 AXWindows 枚举里**，要从 `AXFocusedWindow` 拿。读弹窗按窗口面积过滤只扫小窗口（遍历主窗口 1000 元素一轮好几秒）。
- 实测速度（纯 AX 后台路径）：填单 ~0.5s + 提交确认 ~0.7s ≈ 全流程 ~1.3s。委托成功受理**不弹结果框**（result_text=null），以可用资金冻结判断是否受理（模拟户 20万→196533.89 = 冻结了 34.65×100）。
- **定位已全部去坐标化**（2026-08-30 窗口被移动后旧坐标全失效，用户明确要求）：输入框用"代码/价格/数量"文字标签锚定（标签右侧同行、中心 y 差<15 的 textfield；工具栏搜索框 y~37 旁无标签天然排除）；功能 tab 用 title+AXEnabled 区分（"持仓"有两个，表头那个是 disabled）；账户名锚定"添加"按钮左侧同行最近的 statictext；表格列头 = 功能 tab 下方区域内带 title 的 enabled 按钮按 y 聚类（±15px）取最大组，右界 = 最右列头+110；行聚类 12px、列映射容差 10px、真数据行首列对齐第一个列头。这些相对布局量可保留。
- **撤单**：`cancel --contract 编号 | --code 代码 | --all`。唯一入口 = **双击委托行**（行无 AXPress/AXShowMenu），弹"撤单委托"确认框后自动点确认、再自动复核剩余。双击用 System Events 两次 click at（间隔 0.08s），坐标从 AXRow frame 运行时取，**必须激活同花顺前台**。已人工验证真实撤单成功（601899 卖 100@34.64）；脚本自动双击路径结构相同但未跑过真实单。
- 查询命令：positions/orders/trades/funds，委托/成交/资金明细默认只查"今天"。orders 行解析已在真实委托上验证 ✓。"订单待报"状态 = 非交易时间排队，开盘才报交易所。持仓表坑：账户名限 x<260（否则抓到顶部行情条涨幅）、表格区域限右界（右侧是自选股列表）、真数据行首列需与列头对齐（否则抓到落在表格区域的警告弹窗，弹窗文本单独放 popup 字段返回）。查当前账户 ~1.8s，带切换 ~3-5s。
- **账户切换**：`--account A股|real|模拟|sim`；切换 = 点左侧 y<100 的 "A股"/"模拟" tab（AXPress），等左侧账户名（x<260, y 90~110，如"王伟"/"模拟练习"）变化。
- **解释器/依赖**：需 `pyobjc-framework-Cocoa` + `pyobjc-framework-ApplicationServices`（已入 pyproject.toml，带 `sys_platform == 'darwin'` 标记）。**必须 `uv run python scripts/ths_trade.py ...`**（strategy/trader.py 用 sys.executable 调用即项目 venv）；不要再依赖 `~/.mano/venv`（无关环境）。AppKit 能加载 ≠ 能读持仓——若同花顺停在行情页/登录页（交易界面未打开），AX 树里没有 tab，会报"找不到 持仓 tab"，需先打开交易下单界面（这是 GUI 状态问题非代码问题）。
- **自动登录/断线重连（2026-08-31 已真实闭环验证 ✓）**：凭据在项目根 `.env`（THS_USER/THS_PASS），`load_dotenv()` 启动加载（setdefault 不覆盖已有变量，不打印内容）；所有命令入口默认 `ensure_login`（主窗口出现"立即登录"按钮 = 未登录），`login` 子命令单独触发，`--no-login` 跳过。登录窗字段用"交易帐户/交易密码"标签锚定，密码框 AX set value 可用（无需键盘路径）；成功判定 = 登录窗消失且主窗口无"立即登录"按钮。坑：同花顺退出后用本地保存凭据 1-2 秒内自动重连，想测登录流程必须在退出后零间隔抢按钮（轮询 0.05s）。
- 模拟账户非交易时间也接受委托并冻结资金；真实国泰海通账户验证过拒单路径（资金不足）。
- 买入参考价可用 `fdata quote 601899` 取。

## 五、strategy/ 自动化交易系统（2026-08-30 建成）

用户明确要求：策略独立于回测系统、可启停、可并行、接正式下单；**一切下单默认 dry-run 防误操作**。

### 架构

```
strategy/strategies/*.py   策略：只实现 Strategy.target_position(df, params) -> 0/1 序列 (base.py)
                           放进去即被 registry 自动发现，零注册
  ├─ engine.py             回测引擎：收盘出信号、次日开盘成交、佣金万1、整手100份（无未来函数）
  │    └─ dashboard.py 回测页：多选策略 + 侧栏按 PARAMS schema 动态生成控件实时调参
  └─ trader.py             实盘执行：信号→固定金额/价格//100*100 份数→ths_trade 下单→state/{name}.state.json 记账
       ├─ runner.py        每策略独立常驻进程，盘中 9:25-11:30/13:00-15:05 每 poll_seconds(默认60s) 评估一轮，
       │                   每次取数写 evals.jsonl 流水 + 心跳 + stdout；execute_time(14:55) 当天下单一次
       ├─ manager.py       start/stop/status CLI（独立进程 + 心跳）
       └─ run_live.py      cron 入口，默认 dry-run，--execute 才真实下单
config.json                每策略: enabled/symbols/params/cash_per_symbol/live(dry_run, execute_time, qfq)
state/                     运行时 pid/心跳/日志/应有仓位记账
```

看板：`uv run streamlit run strategy/dashboard.py` 三页——回测 / 实盘策略管理（启停、立即跑一轮、对账）/ 配置（写 config.json）。Streamlit 1.43 的 `st.fragment(run_every=...)` 做自动刷新（实盘页 60s）。

### 内置策略

| 策略 | 规则 | 标的 |
|---|---|---|
| ma20_trend | 收盘>MA20 持有，<MA20 空仓（日线） | sz159915 |
| sma_cross | 快慢线上/下穿（日线） | sh510300 |
| intraday_t | 日内做T（5m线，TIMEFRAME 属性切换周期）：10:00 后按两市量能预估定方向——放量(>近N日均×1.05 且≥2万亿)先买后卖，缩量/<2万亿先卖后买(需底仓)；买入=恐慌放量杀跌(RSI6≤25 且 5m量≥1.8×均量 且价在VWAP下)，回升 VWAP±0.3% 止盈；冲高≥1.5%后20分钟未创新高+RSI死叉→卖出；14:50 强制了结。实时量能预估复用 stockview/main.py 的 get_estimate_amount | sz159915 |

**intraday_t 参数未调优**：2026-03~08 真实回测 -1.9% vs 买入持有 +5.1%（样本期两市持续缩量 ~1.97万亿，压在 2万亿阈值下，策略多处于"先卖后买"持有态）。下一步：在看板侧栏对比 vol_expand/min_amount_yi 后再考虑启用实盘。相关历史研究在 strategy/intraday/（STRATEGY_REPORT.md、analyze_and_backtest.py）和 strategy/t0_intraday/。

### MA20 回测基准（sz159915, 2011-12 ~ 2026-08）

总收益 +202.2% / 年化 7.8% / 最大回撤 -57.1%（2015-06→2019-01）/ 215 笔 / 胜率 24.7%；买入持有 +332.0%。震荡市（2016 -25%、2018 -22%）连续止损是主要拖累，改进方向：波动率过滤、均线缓冲带、与 etf_signal.py 五因子共振。

### 关键约定与坑

- 改回测逻辑动 engine.py，跑 `uv run python -m unittest tests.test_strategy_system` 保真；加策略只写 strategies/*.py；实盘行为调 config.json live 段。
- state.json 记"应有仓位"，与同花顺实际持仓只**人工对账**不自动纠偏；看板/runner 经项目 venv 调 ths_trade.py（须 `uv run`）。
- eltdx 分钟线分页上限 800（用 limit 0 全量后本地截断）；plotly 多面板需唯一 key（StreamlitDuplicateElementId）；PARAMS 混有字符串常量（gate/exit_time）时配置页/侧栏需过滤。
- 159915 无分红不复权=前复权；其他标的请开 qfq（走 fdata）。

## 六、开源量化框架调研结论（docs/quant-framework-report.md，2026-08-30）

推荐组合：**vectorbt（参数扫描/批量回测）+ backtrader（事件驱动精写策略）**，数据走现有 fdata，信号输出后接现有 ths_trade 下单。两层回测互验（vbt 快筛参数，backtrader 逐日复现，收益对不上 = 有未来函数）。本地实测：backtrader SMA(10/30) +62.6% vs 买入持有 +111.3%（500根日K 样本）；vectorbt RSI 19 组参数扫描秒级，胜率 80%/PF 2.86（RSI window=6 的 +36.8% 明显过拟合嫌疑，数字仅验证框架可用性）。踩坑：vectorbt 须钉 `plotly<6`；`vbt.settings.array_wrapper["freq"]="d"`；backtrader 必须 addsizer（默认 size=1 股）。未引入：qlib（重，ML 时再看）、vnpy（与 THS 方案重叠）、QUANTAXIS（数据层冲突）、zipline（停更）。**A股口径**：ETF 佣金万1无印花税、T+1、100份整手，框架不默认，需在信号层自约束。落地架构尚未实施。

## 七、已知问题 / 待办 / 下一步

1. **strategy/、ths_trade.py、fdata.py 新增、docs/、datasource_test/ 等大量文件未提交**——考虑按功能分批 commit。
2. .env 历史泄露（TUSHARE_TOKEN/IWENCAI_API_KEY 在 git 历史里），仓库若公开需轮换。
3. intraday_t 参数调优（vol_expand/min_amount_yi 侧栏对比）后再启实盘。
4. 深市 ETF 期权 CON_OP_ 实时未在盘中验证过。
5. ths_trade.py cancel 的自动双击路径未跑过真实单（人工验证过同结构手动撤单）。
6. 回测改进方向：波动率过滤、均线缓冲带、五因子共振。
7. vectorbt/backtrader 组合落地架构（报告第四节）未实施。

## 八、给下一个 Agent 的操作约定

- 查行情/期货/期权数据：**先走 fdata CLI**（skill `fdata`），不要现调研数据源；不要建议 efinance/qstock/pytdx/mootdx，ETF 期权不要用 tqsdk。
- 跑任何项目代码用 `uv run`（项目 venv）；tqsdk 凭据在 .env 的 TQ_USER/TQ_PASS，同花顺凭据在 THS_USER/THS_PASS。
- 所有下单默认 dry-run；真实下单需用户显式确认且同花顺客户端在交易界面。
- 不要清空联动带出的价格框；不要在脚本里做多余清空操作（用户偏好：输入延迟尽量小）。
- 新浪源轮询间隔勿 <1s。
- 用户沟通语言为中文。

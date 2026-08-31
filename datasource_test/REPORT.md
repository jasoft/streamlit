# 金融数据源 GitHub 项目实测报告
## 商品期货 / 金融期货 / ETF 期权 / 商品期权 实时行情

- **测试时间**: 2026-08-30（周日）。所有"实时"接口返回的是周五 2026-08-28 收盘快照，测试结论基于**连通性、正确性、延迟、连续请求成功率**。
- **测试环境**: macOS arm64（Apple Silicon）、Python 3.12、家用宽带、无代理。
- **测试项目**: akshare 1.18.94、efinance、adata 1.4.9、qstock、pytdx、mootdx 0.11.7、tqsdk、eltdx 3.0.9（本地）。
- **测试脚本**: `datasource_test/test_all.py`（首轮全量）、`test_round2.py`（修正与补充）、`test_stability.py`（按 1 req/s 连续打 10 轮，模拟本项目轮询场景）。

---

## 一、结论速览

| 项目 | 商品期货 | 金融期货 | ETF 期权 | 商品期权 | 稳定性 | 一句话评价 |
|---|---|---|---|---|---|---|
| **akshare** | ✅ 强 | ✅ 强 | ✅ 强 | ❌ 无实时 | **10/10 全过，~100ms** | 本次测试唯一全面稳定可用 |
| efinance | ⚠️ 时好时坏 | ⚠️ | ❌ 不支持 | ❌ | **5 轮复测 0/5** | 走东财 push2，被限频封禁，且错误信息夹带推广 |
| qstock | ❌ | ❌ | ❌ | ❌ | 0/3 | 安装链烂（arm64 兼容炸），接口对上游结构变化零容错 |
| pytdx | ❌ | ❌ | ❌ | ❌ | — | 扩展行情(7727) 9 台服务器全部拒连，2021 年停更 |
| mootdx | ❌ | ❌ | ❌ | ❌ | — | 官方日志自认"扩展市场行情接口已经失效" |
| tqsdk（免费快期账户） | ✅ 强(含五档) | ✅ 强 | ❌ 付费墙 | ✅ 强(含五档) | websocket 单连接 10/10 | 期货+商品期权最佳免费源；ETF 期权需付费 |
| adata | ❌ | ❌ | ❌ | ❌ | — | 模块只有 stock/fund/bond/sentiment，无期货期权 |
| eltdx | ❌ | ❌ | 仅标的 ❌期权 | ❌ | 标的 10/10 | 只实现通达信标准行情(股票/ETF/指数)，无扩展行情 |

**最终推荐**：实时价格需求全部押 **akshare（新浪底层）**，其中 ETF 期权直接用新浪 `CON_OP_` 批量接口（akshare 封装的单合约版稳定，批量版一次 HTTP 可拉全部合约）。

---

## 二、分库详细结果

### 1. akshare —— 本次测试的大赢家 ✅

**商品期货**（底层新浪 `hq.sinajs.cn`，代码 `nf_` 前缀）：

| 接口 | 结果 | 延迟 | 备注 |
|---|---|---|---|
| `futures_zh_spot(symbol='RB0', market='CF')` | ✅ 10/10 | 117ms | 最新价 3178、买一卖一、持仓量齐全 |
| `futures_zh_spot(symbol='M0', market='CF')` | ✅ | ~105ms | |
| `futures_zh_realtime(symbol='螺纹钢')` | ✅ 3/3 | 137ms | 一次返回该品种全部 13 个合约 |
| `futures_display_main_sina()` | ✅ 3/3 | 7458ms | 主力合约总表 82 行，但很慢（页面抓取），适合低频刷新 |

**金融期货**（中金所走同一新浪通道，`market='CFF'`）：

| 接口 | 结果 | 延迟 |
|---|---|---|
| `futures_zh_spot(symbol='IF0', market='CFF')` | ✅ 10/10 | 107ms（IF0=4593.6） |
| `futures_zh_spot(symbol='IM0', market='CFF')` | ✅ | ~107ms（IM0=7666.6） |
| `futures_main_sina(symbol='IF0')` | ✅ | 212ms（分钟线，最新 bar 含当日） |

**ETF 期权**（新浪期权通道 `CON_OP_` 前缀）：

| 接口 | 结果 | 备注 |
|---|---|---|
| `option_sse_spot_price_sina('10011255')` | ✅ 5/5, 100ms | 买量/卖量/最新价/ Greeks 相关字段齐全 |
| `option_sse_codes_sina(看涨, 202609, 510050)` | ✅ | 当月合约代码表 14 行 |
| `option_sse_underlying_spot_price_sina('sh510050')` | ✅ | 标的行情 33 字段 |
| `option_current_day_sse()`（上交所官网） | ✅ 3/3, 181ms | 666 条当日合约静态表（**不是实时价**） |
| `option_current_day_szse()`（深交所官网） | ✅ 3/3, 406ms | 444 条深市期权合约静态表 |
| `option_current_em()`（东财） | ❌ | push2 被封，连接被掐断 |

**关键发现（本次最有价值的收获）**：新浪期权批量接口可用——
```
https://hq.sinajs.cn/list=CON_OP_10011255,CON_OP_10011256,CON_OP_10011257
```
一次 HTTP 请求返回多个合约完整快照（最新价、买卖五档、希腊字母相关字段、时间戳），实测 10/10、100ms。**上交所 ETF 期权全市场约 4000+ 合约，分批每请求 50 个即可高效轮询**。（akshare 未封装批量版，建议本项目直接调用；深市期权代码是否同样支持需盘中验证。）

**商品期权**：akshare 只有合约列表（`option_commodity_contract_sina`，返回月份合约）和日 K（`option_commodity_hist_sina`），**没有实时行情接口**。新浪直连测试了 `OPT_/OPT_o_/o_/nf_OPT_` 等格式全部返回空，新浪商品期权实时通道基本已废。这是本次测试发现的最大缺口（见第三节）。

### 2. efinance —— 不稳定，且包内夹带推广 ⚠️

- `ef.futures.get_realtime_quotes()`（东财 push2 `clist` 接口）：**第 1 次调用成功（1075 行全市场期货），此后 7 次连续失败**——push2 对本机 IP 从间歇拒绝变为持续拒绝，正是你之前被新浪/东财封禁的同一模式。
- 不支持期权。金融期货混在全量列表中（按市场列过滤）。
- 值得警惕：失败时 efinance 会打印 `网络连接异常，可尝试使用 TickFlow 获取更稳定的数据。→ https://tickflow.org?utm_source=efinance`——包内（`efinance/shared/tickflow_prompt.py`）被注入了推广跳转，说明维护状态可疑。

### 3. qstock —— 三重故障，不可用 ❌

1. **安装即炸**：依赖 py-mini-racer 0.6.0 在 macOS arm64 上二进制不兼容（V8 符号缺失），需手动换新 mini-racer 包 + 垫 shim 才能装上；pyfolio 依赖在 Python 3.12 也装不上（需 stub）。
2. **import 时硬依赖东财实时接口**取最新交易日，接口一断整个库无法导入（我打补丁绕过后才能继续测）。
3. **接口零容错**：`future_info()` 直接 KeyError（上游列结构变化没人修）；`realtime_data('期货')` 第 1 次返回 400 行，复测 3 次全部 KeyError。最后一次复测还暴露其自带 `func_set_timeout` 用 signal 实现、在子线程直接崩。

### 4. pytdx / mootdx —— 扩展行情已死 ❌

期货和期权在通达信协议里属于"扩展行情"（7727 端口，`TdxExHq_API`），与股票标准行情（7709）是两套通道：

- 标准行情 7709：正常（老服务器 `115.238.90.165` 等可连，市场 0 有 24132 只证券）——eltdx 走的就是这套，所以能工作。
- 扩展行情 7727：pytdx 内置和扫测的 **9 台服务器全部拒连**；mootdx 能握手但 `markets()` 返回空表，且其日志明说："**目前扩展市场行情接口已经失效, 后期有望修复**"（2026 年仍无修复）。
- 结论：TDX 免费扩展行情这一免费午餐已经结束，pytdx/mootdx 对期货期权**不可用**，也意味着 eltdx 按现有协议范围永远不会覆盖期货期权。

### 5. tqsdk —— 已用免费快期账户实测，期货+商品期权最佳 ✅

账户注册后实测通过（脚本 `test_tqsdk.py` / `test_tqsdk2.py`，结果 `results_tqsdk*.json`）。走 `wss://free-api.shinnytech.com` websocket 长连接，无 HTTP 限频封禁问题：

| 测试 | 结果 | 明细 |
|---|---|---|
| 商品期货主力（rb/cu/m/SR） | ✅ | rb=3130，含**五档买卖盘**（bid/ask_price1~5），夜盘时间戳正确（周五 22:59:59） |
| 金融期货主力（IF/IM） | ✅ | IF=4593.6 / IM=7666.6，与新浪通道数据一致 |
| 商品期权（query_options + get_quote） | ✅ | rb2610 全部 22 个看涨合约；活跃合约 C3100 last=47.5、买卖五档齐全、成交量/持仓量完整 |
| 商品期权批量订阅（5 合约一次 wait_update） | ✅ | 316ms 全部到位 |
| 单连接 10 轮轮询（1.6s/轮） | ✅ 10/10 | websocket 长连接复用，无被封风险 |
| **ETF 期权（上交所/深交所）** | ❌ **付费墙** | `您的账户不支持查看 SSE.10000969 / SZSE.90000097 的行情数据，需要购买后才能使用` |

**免费账户边界**：期货（商品+金融+期权）行情全开放且带五档盘口；股票期权（沪深 ETF 期权）行情是付费功能；希腊字母不在免费行情字段里（可用期权价+标的价格自行计算，或用 akshare 的 `option_sse_greeks_sina`）。

注意：`query_options` 返回全部行权价合约，深度实值/虚值合约无成交时 `last_price=nan`（市场现象，非接口问题），取数时要按行权价过滤活跃区间。

### 6. adata / eltdx —— 超出范围

- adata：只有 stock/fund/bond/sentiment 模块，**没有期货期权**。
- eltdx：纯标准行情实现（股票/ETF/指数），`get_snapshots` 对 510050 等标的 10/10 稳定（100ms 内），但无扩展行情，与期货期权无关。

---

## 三、商品期权实时行情的出路（缺口分析）

测试下来没有一个 GitHub 免费库能稳定拉商品期权实时价。可行路径按推荐排序：

1. **tqsdk + 免费快期账户（已实测可用）**：商品期权实时行情带五档盘口、成交量/持仓量，websocket 单连接批量订阅、无封禁风险。仅 ETF 期权被付费墙挡住。
2. **交易所官网盘后/盘中文件**：上期所 `option_quote.json`（本机测 HTTPS 连接失败，需换 `www.shfe.com.cn` 域名再试）、郑商所/大商所官网返回 412（反爬，需带正确 UA/Cookie 模拟浏览器会话）。延迟高（秒级~分钟级文件），不适合 tick 级但够用面板级。
3. **期货公司 CTP/柜台行情**（开户后免费， realtime 且带盘口，如 vnpy + CTP gateway），需要期货账户，工程量最大但最稳。
4. 付费 API（tqsdk 专业版、掘金、米筐等）。

---

## 四、对本项目的落地建议

你的 ETF 信号面板当前用 eltdx 拿 ETF/指数没有问题，但期货/期权部分建议：

| 商品期货 | 金融期货 | ETF 期权 | 商品期权 | 推荐源 |
|---|---|---|---|---|
| akshare(新浪) 或 tqsdk | akshare(新浪) 或 tqsdk | akshare/新浪 `CON_OP_` 批量（tqsdk 付费墙） | **tqsdk 免费账户**（唯一稳定免费源） | |

2. **商品期货 + 金融期货实时**：轻量轮询用 `ak.futures_zh_spot(symbol='RB0'/'IF0', market='CF'/'CFF')`（10/10 @ ~110ms）；需要五档盘口/批量订阅/事件驱动则用 tqsdk（websocket 长连接，实测 10/10）。
3. **ETF 期权实时**：直连新浪批量 `CON_OP_` 接口（10/10、100ms、一次多合约），合约代码表用 `option_sse_codes_sina` + 上交所/深交所官网合约日表（这两个官网接口也很稳）。深市期权代码是否支持 CON_OP_ 需盘中验证。
4. **商品期权实时**：用你注册的快期账户跑 tqsdk，`api.query_options('SHFE.rb2610')` 拿合约表 + `api.get_quote` 批量订阅活跃行权价，免费且带五档。
5. **避开一切东财 push2 依赖**（efinance、qstock、akshare 的 `*_em` 系列在你当前网络下都不可靠）。

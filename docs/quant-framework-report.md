# 开源量化框架调研报告（2026-08-30）

> 目标场景：A股/ETF 策略研究与回测。下单链路已打通（THS GUI 自动交易 ths_trade.py），
> 数据源已打通（fdata CLI / eltdx 通达信），缺的是「策略设计 → 信号 → 回测验证」这一环。
> 本报告调研了主流开源项目，并对两个最契合的框架做了本地实测（真实数据：创业板ETF 159915 前复权日K，500根）。

## 一、结论（TL;DR）

**推荐组合：vectorbt（参数扫描/批量回测）+ backtrader（事件驱动精写策略），数据走现有 fdata，信号输出后接现有 THS 下单脚本。**

| 用途 | 选型 | 理由 |
|---|---|---|
| 参数寻优 / 大批量策略筛选 | **vectorbt** | 向量化引擎，19 组参数扫描秒级完成；研究迭代快 |
| 逐日决策、贴近实盘逻辑的策略回测 | **backtrader** | 事件驱动，代码即策略逻辑，可直接对着写「每天收盘前判断 → 发信号 → 调下单脚本」 |
| AI/ML 因子选股（未来扩展） | qlib | 微软出品，因子→模型→回测全流程，但依赖重、需单独下载其二进制数据集 |
| 实盘交易平台（自建 broker/gateway） | vnpy | 国内期货 CTP 生态最强；股票实盘你已有 THS GUI 方案，不必引入 |

## 二、主流项目全景（GitHub 数据，2026-08-30 实查）

| 项目 | Stars | 维护状态 | 定位 | 对本项目适配度 |
|---|---|---|---|---|
| freqtrade/freqtrade | 53.8k | 活跃 | 加密货币交易机器人 | ✗ 仅币圈 |
| microsoft/qlib | 48.1k | 活跃 | AI 量化研究平台（因子/ML/RL） | △ 后期做 ML 选股再看 |
| vnpy/vnpy | 44.9k | 活跃 | 量化交易平台（CTP/期货为主） | △ 偏重，与 THS 下单方案重叠 |
| mementum/backtrader | 23.0k | **2024-08 起停更** | 事件驱动回测库 | ✓ 轻量成熟，够用 |
| quantopian/zipline | 20.1k | 停更（量化平台已关） | Quantopian 时代回测引擎 | ✗ 用 zipline-reloaded (1.9k) 替代 |
| polakowo/vectorbt | 8.9k | 活跃（OSS 版） | 向量化回测/参数优化 | ✓ 最契合快速研究 |
| yutiansut/QUANTAXIS | 11.1k | 半停更 | 全套本地量化（数据/回测/交易） | ✗ 太重，数据层与你重复 |
| pmorissette/bt | 3.0k | 活跃 | 组合再平衡回测 | △ 轮动策略可看 |
| fasiondog/hikyuu | 3.5k | 活跃 | C++内核系统化交易研究 | △ 国产，A股友好，可备选 |
| shidenggui/easytrader | 10.1k | 活跃 | 券商 GUI 自动交易 | — 与你的 ths_trade.py 同类，可参考实现 |

社区口碑（多平台对比文章共识）：backtrader 生态资料最丰富但停更；vectorbt 是公认的 backtrader 替代/互补；qlib 定位因子挖掘与 ML；vnpy 强在实盘通道。

## 三、本地实测（2026-08-30，macOS arm64，Python 3.12 + uv）

数据：`fdata kline 159915 --adjust qfq --limit 500`（创业板ETF，2024-08 ~ 2026-08，eltdx 通道）。

### 3.1 backtrader — 双均线 SMA(10/30)，万1佣金

```
初始资金 10万 -> 期末 162,597（总收益 +62.6%）
Sharpe 0.055   最大回撤 24.9%
同区间买入持有: +111.3%，最大回撤 32.6%
```

- 安装零障碍（`uv run --with backtrader`），Python 3.12 可跑（有若干无害的 SyntaxWarning）。
- 事件驱动结构清晰：`next()` 里写的就是「今天该不该发信号」，和实盘脚本的心智模型一致。
- 踩坑 1：默认下单 size=1 股，必须 `cerebro.addsizer(bt.sizers.PercentSizerInt, percents=95)`。
- 踩坑 2：停更导致文档站偶发失联，但社区示例极多，够查。
- 结论：**适合作为信号逻辑的最终表述层**（策略=代码，一眼可审）。

### 3.2 vectorbt — RSI 均值回归 + 参数扫描（19 组并行）

```
RSI(14) 30/70 均值回归: 胜率 80%，Profit Factor 2.86，Best/Worst Trade +8.4%/-5.3%
参数扫描 RSI 窗口 6~24: 最优 window=6（+36.8%），最差 window=9（-7.6%）
Sharpe 前5: window 24/23/21/18/6
同区间买入持有基准: +116.7%
```

- 19 组参数一次 `Portfolio.from_signals` 批量算完，速度是 backtrader 完全比不了的。
- 自带 `pf.stats()` 输出胜率/盈亏比/回撤/Sharpe 全套指标，省去手写绩效统计。
- 踩坑 1：开源版与 plotly>=6 不兼容（`scattermapbox` 被改名），须钉 `plotly<6`。
- 踩坑 2：不设时间频率时 Sharpe 报错，需 `vbt.settings.array_wrapper["freq"] = "d"`。
- 结论：**适合作为策略研究/参数寻优的主引擎**。

### 3.3 未实测项（理由说明）

- **qlib**：依赖重（numba/pyarrow 等），自带数据需从官方源下载数百 MB 二进制文件，且定位是 ML 因子研究——等需要机器学习选股时再单独评估。
- **vnpy**：面向 CTP 期货实盘的完整平台，安装即引入整套服务；你已有 THS GUI 下单 + fdata 数据，引入它只会重复。
- **QUANTAXIS**：自建数据仓库与你的 eltdx/fdata 体系冲突，且项目半停更。

## 四、建议的落地架构

```
fdata (eltdx/tqsdk/新浪)   ← 已有
        │ pandas DataFrame
        ▼
vectorbt 研究层   ← 参数扫描、策略筛选、绩效报告
        │ 选出的参数/规则
        ▼
backtrader 事件驱动复现 ← 与实盘等价的逐日决策流，防"回测幻觉"
        │ 每日信号 (buy/sell + 仓位)
        ▼
ths_trade.py (THS GUI 下单)  ← 已有
```

要点：
1. **两层回测互验**：vectorbt 快速筛参数，backtrader 用同一套参数逐日复现，两者收益对不上 = 有未来函数或口径差异。
2. **信号与下单解耦**：回测框架只产出「信号事件」（JSON），下单脚本只消费信号，策略换框架不影响交易链路。
3. **A股口径注意**：ETF 佣金万1无印花税、T+1、100份整手——backtrader/vbt 都不默认这些，佣金已可设置，T+1 需在信号层自己约束。
4. 本次实测样本只有 500 根日K、单标的、单策略，收益数字仅用于验证框架可用性，不构成策略有效性结论（如 RSI window=6 的 +36.8% 明显过拟合嫌疑）。

## 五、参考来源

- [GitHub - vnpy/vnpy](https://github.com/vnpy/vnpy) / [backtrader](https://github.com/mementum/backtrader) / [qlib](https://github.com/microsoft/qlib) / [vectorbt](https://github.com/polakowo/vectorbt) / [QUANTAXIS](https://github.com/yutiansut/QUANTAXIS) / [hikyuu](https://github.com/fasiondog/hikyuu) / [easytrader](https://github.com/shidenggui/easytrader)
- [量化开源项目对比 Backtrader/VectorBT/Zipline/vnpy/wtpy/qlib (CSDN)](https://blog.csdn.net/zhangyunchou2015/article/details/147185325)
- [量化回测框架大比拼 (知乎)](https://zhuanlan.zhihu.com/p/1995510498830091671)
- [VectorBT/Backtrader/Zipline 比较 (博客园)](https://www.cnblogs.com/hopesun/p/18815644)
- [2025年量化交易平台对比：付费与开源方案 (CSDN)](https://blog.csdn.net/m0_52307083/article/details/149396173)

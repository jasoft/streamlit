---
name: financial-data
description: "Comprehensive financial and market data tools: futures/options query, index query, market briefs, stock PDF reports, yfinance data, and news search. Consolidates 6 agent-created financial skills into a single class-level skill."
category: finance
---

# Financial Data Tools

All-in-one guide for agent-created financial data tools. Replaces six separate skills: `hithink-futures-query`, `hithink-zhishu-query`, `market-brief`, `stock-market-pdf-report`, `yfinance-data`, and `news-search`.

## Hithink Futures & Options Query
查询期货期权的行情、波动率、产销、会员持仓、会员榜单、行权等多种数据，支持自然语言问句输入，返回相关期货期权数据结果。

### Version: 1.0.0
Data source: **同花顺问财** (https://www.iwencai.com/unifiedwap/chat)

### Pre-use Setup
> Get API Key from https://www.iwencai.com/skillhub
> Set environment variable:
> - macOS/Linux: `export IWENCAI_API_KEY="your-api-key"`
> - Windows PowerShell: `$env:IWENCAI_API_KEY="your-api-key"`

### Core Workflow
1. **Receive Query**: Identify futures/options query type (market data, volatility, position, exercise)
2. **Query Rewrite**: Convert to standard financial terms (e.g., "沪铜今天怎么样" → "沪铜期货最新行情")
3. **API Call**: Use `scripts/cli_futures.py` or direct HTTP request with required headers:
   - `Authorization: Bearer <API_KEY>`
   - `X-Claw-Skill-Id: hithink-futures-query`
   - `X-Claw-Trace-Id: <64-char hex>`
4. **Empty Data Handling**: Retry up to 2 times with relaxed conditions
5. **Data Parsing**: Extract futures code, name, price, change%, position
6. **Response**: Emphasize **数据来源于同花顺问财**

### CLI Usage
```bash
python3 scripts/cli_futures.py --query "沪铜期货最新行情"
python3 scripts/cli_futures.py --query "50ETF期权隐含波动率" --page 2 --limit 20
```

---

## Hithink Index Query
查询上证指数、沪深300、创业板指、恒生指数、纳斯达克指数等指数行情数据，支持涨跌幅、成交量、点位等指标查询。

### Version: 1.0.0
Data source: **同花顺问财** (https://www.iwencai.com/unifiedwap/chat)

### Pre-use Setup
Same API Key as Futures Query: `IWENCAI_API_KEY` environment variable.

### Core Workflow
1. **Receive Query**: Identify index type (A-share, scale, overseas)
2. **Query Rewrite**: Convert to standard terms (e.g., "上证指数今天多少点" → "上证指数最新点位")
3. **API Call**: Use `scripts/cli_index.py` with headers:
   - `X-Claw-Skill-Id: hithink-zhishu-query`
4. **Batch Query Best Practice**: Split queries for completeness (e.g., separate precious metals from main indices)
5. **Response**: Emphasize **数据来源于同花顺问财**

### Common Query Examples
| User Query | Rewritten Query |
|------------|-----------------|
| 上证指数今天多少点 | 上证指数最新点位 |
| WTI原油期货今日涨跌幅 | 美原油主力合约涨跌幅 |
| 黄金价格 | 纽约金主力合约收盘价 |

### Pitfalls
1. **Broken symlink**: Install by copying directory, not symlink
2. **Commodity futures gaps**: Use alternative terms (e.g., "美原油主力合约涨跌幅" for WTI crude)

---

## Market Brief
获取 A 股、港股、黄金、白银、WTI 原油、美股主要指数等结构化行情简报。

### Recent Updates
- ✅ Replaced urllib with yfinance (no 429 rate limits)
- ✅ Fixed A-share data parsing (Sina Finance `s_` interface)
- ✅ Commodity data via Sina Finance (stable free source)

### Quick Start
```bash
# First use: install yfinance
cd ~/.hermes/skills/financial-data
uv venv .venv
source .venv/bin/activate
uv pip install yfinance

# Run
.venv/bin/python3 scripts/fetch_market_brief.py --format markdown
# Or with uv run
uv run --with yfinance python3 scripts/fetch_market_brief.py --format markdown
```

### Data Sources
| Category | Source | Status |
|----------|--------|--------|
| A-share/HK stocks | Sina Finance `hq.sinajs.cn` | ✅ Stable |
| Commodities | Sina Finance `hf_XAU/XAG/CL` | ✅ Stable |
| US indices | Yahoo Finance (yfinance) | ✅ No rate limits |

### Extend with Extra Assets
```bash
python3 scripts/fetch_market_brief.py --extra copper bitcoin brent
python3 scripts/fetch_market_brief.py --ticker AAPL TSLA 0700.HK
```

---

## Stock Market PDF Report
Generate professional A-share and Hong Kong stock market closing reports in PDF (single A4 page) using reportlab.

### Prerequisites
```bash
python3 -c "import reportlab; print('reportlab installed')"
# If not installed:
pip3 install reportlab
```

### Key Design Patterns
1. **Table styling**: Alternating row backgrounds, colored headers
2. **Color coding**: Red for negative changes, blue for index values
3. **Single page constraint**: Keep content concise, reduce font sizes if needed

### Common Data Sources
- Web search for: "A股 今日收盘 上证指数 深证成指"
- Extract data from search results

### Pitfalls
- **Environment mismatch**: execute_code may not access pip-installed packages; use system python3 directly
- **Path issues**: Verify home directory with `echo $HOME`

---

## yfinance Data
Fetch financial and market data using yfinance Python library (Yahoo Finance data).

### Step 1: Ensure yfinance Available
```python
import subprocess, sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "yfinance"])
```

### Step 2: Data Categories
| User Request | Data Category | Primary Method |
|--------------|----------------|----------------|
| Stock price, quote | Current price | `ticker.info` or `ticker.fast_info` |
| Price history, chart | Historical OHLCV | `ticker.history()` or `yf.download()` |
| Balance sheet | Financial statements | `ticker.balance_sheet` |
| Income statement | Financial statements | `ticker.income_stmt` |
| Dividends | Corporate actions | `ticker.dividends` |
| Options chain | Options data | `ticker.option_chain()` |
| Earnings, EPS | Analysis | `ticker.earnings_history` |
| Analyst targets | Analysis | `ticker.analyst_price_targets` |

### Step 3: Key Rules
1. Always wrap in try/except — Yahoo Finance may rate-limit
2. Use `yf.download()` for multi-ticker comparisons
3. For options, list expiration dates first with `ticker.options`
4. For quarterly data, use `ticker.quarterly_income_stmt`

### Valid Periods & Intervals
- Periods: `1d`, `5d`, `1mo`, `3mo`, `6mo`, `1y`, `2y`, `5y`, `10y`, `ytd`, `max`
- Intervals: `1m`, `2m`, `5m`, `15m`, `30m`, `60m`, `90m`, `1h`, `1d`, `5d`, `1wk`, `1mo`, `3mo`

---

## News Search
财经领域为主的资讯搜索引擎，覆盖官媒、主流财经媒体、垂直行业网站、知名上市公司/非上市公司官网等。

### Version: 1.0.0
Data source: **同花顺问财** (https://www.iwencai.com/unifiedwap/chat)

### Pre-use Setup
Same API Key as other Hithink skills: `IWENCAI_API_KEY` environment variable.

### Core Workflow
1. **Receive Query**: Identify news need (policy, industry, corporate)
2. **Query Splitting**: Break complex queries into simple sub-queries (e.g., "人工智能和芯片行业新闻" → ["人工智能最新动态", "芯片行业新闻"])
3. **API Call**: Use `/v1/comprehensive/search` endpoint with headers:
   - `X-Claw-Skill-Id: news-search`
   - `channels: ["news"]`
4. **Transparent Passthrough**: ⚠️ Must return API response **as-is** without modification (per 同花顺 OpenAPI 网关规范条件六)
5. **Data Source Labeling**: Must标注 **数据来源：同花顺问财**

### CLI Parameters
| Parameter | Required | Description |
|-----------|----------|-------------|
| `--query` / `-q` | Yes | Search keyword |
| `--output` / `-o` | No | Output file path |
| `--format` / `-f` | No | Output format (csv, json, text) |
| `--limit` / `-l` | No | Result count limit |

### Important Compliance Rule
**禁止行为**:
- Do not二次解析, 清洗, 重组 API 返回数据
- Do not wrap API response in custom structures
- Do not replace error responses with custom messages

**要求行为**:
- Directly return API raw response to LLM for processing
- Pass through error status codes and bodies unchanged

---

## Common References
- All Hithink-based skills require `IWENCAI_API_KEY` environment variable
- yfinance skills use Yahoo Finance data (educational/research purposes only)
- Market brief uses Sina Finance for A-share/commodity data, yfinance for US stocks
- Always标注数据来源 when using third-party financial data

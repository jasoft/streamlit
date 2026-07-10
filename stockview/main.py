import streamlit as st
from datetime import date, datetime
import pandas as pd
from stockview.log import logger
import pytz
import os
import sys
import requests
import json
import re

from dotenv import load_dotenv
load_dotenv()

from stockview.helpers import during_market_time, minutes_since_market_open
from streamlit_autorefresh import st_autorefresh
from stockview.index_spread import create_spread_chart
from stockview.index_comparison import render_index_comparison_page
from stockview.if_im_strategy import render_if_im_strategy_page
from stockview.akcache.rate_limiter import rate_limiter

# ============ 数据源配置 ============

# 腾讯行情 API
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
# 腾讯日线 API
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
# 新浪A股列表 API
SINA_STOCK_LIST_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
# 新浪分钟K线 API
SINA_MINUTE_URL = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_test=/CN_MarketDataService.getKLineData"

# 指数代码映射
# 注意：腾讯API不支持中证2000(932000)，使用中证500(000905)替代
INDEX_CODES = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sh000300": "沪深300",
    "sh000852": "中证1000",
    "sz399006": "创业板指",
    "sh000905": "中证500",
}


# ============ 缓存工具 ============

@rate_limiter
def _fetch_tencent_quotes_raw(symbols: str) -> dict:
    """从腾讯获取实时行情数据（原始函数）"""
    try:
        url = f"{TENCENT_QUOTE_URL}{symbols}"
        response = requests.get(url, timeout=10)
        result = {}
        for line in response.text.strip().split(";"):
            if "=" not in line:
                continue
            var_name = line.split("=")[0].strip()
            data = line.split("=")[1].strip('"').split("~")
            if len(data) < 40:
                continue
            code = data[2]
            result[code] = {
                "name": data[1],
                "code": code,
                "price": float(data[3]) if data[3] else 0,
                "pre_close": float(data[4]) if data[4] else 0,
                "open": float(data[5]) if data[5] else 0,
                "volume": float(data[6]) if data[6] else 0,
                "change": float(data[31]) if data[31] else 0,
                "change_pct": float(data[32]) if data[32] else 0,
                "high": float(data[33]) if data[33] else 0,
                "low": float(data[34]) if data[34] else 0,
                "amount": float(data[37]) if data[37] else 0,
                "turnover": float(data[38]) if data[38] else 0,
            }
        return result
    except Exception as e:
        logger.error(f"腾讯行情获取失败: {e}")
        return {}


@st.cache_data(ttl=180)
def fetch_tencent_quotes(symbols: str) -> dict:
    """从腾讯获取实时行情数据（带缓存）"""
    return _fetch_tencent_quotes_raw(symbols)


@rate_limiter
def _fetch_sina_stock_list_raw(page: int = 1, num: int = 80, sort: str = "amount", asc: int = 0) -> list:
    """从新浪获取A股列表数据（原始函数）"""
    try:
        params = {
            "page": page,
            "num": num,
            "sort": sort,
            "asc": asc,
            "node": "hs_a",
            "symbol": "",
            "_s_r_a": "init"
        }
        response = requests.get(SINA_STOCK_LIST_URL, params=params, timeout=15)
        return json.loads(response.text)
    except Exception as e:
        logger.error(f"新浪股票列表获取失败: {e}")
        return []


@st.cache_data(ttl=180)
def fetch_sina_stock_list(page: int = 1, num: int = 80, sort: str = "amount", asc: int = 0) -> list:
    """从新浪获取A股列表数据（带缓存）"""
    return _fetch_sina_stock_list_raw(page, num, sort, asc)


@st.cache_data(ttl=180)
def fetch_all_sina_stocks() -> pd.DataFrame:
    """获取所有A股数据"""
    all_data = []
    for page in range(1, 80):
        data = fetch_sina_stock_list(page=page, num=80, sort="symbol", asc=1)
        if not data:
            break
        all_data.extend(data)
    if not all_data:
        return pd.DataFrame()
    df = pd.DataFrame(all_data)
    # 确保数值列类型正确
    numeric_cols = ["trade", "pricechange", "changepercent", "buy", "sell", "settlement", "open", "high", "low"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


@rate_limiter
def _fetch_tencent_kline_raw(symbol: str, days: int = 250) -> pd.DataFrame:
    """从腾讯获取日K线数据（原始函数）"""
    try:
        params = {"param": f"{symbol},day,,,{days},qfq"}
        response = requests.get(TENCENT_KLINE_URL, params=params, timeout=15)
        data = response.json()
        if "data" not in data:
            return pd.DataFrame()
        for code, info in data["data"].items():
            kline = info.get("qfqday") or info.get("day", [])
            if not kline:
                continue
            df = pd.DataFrame(kline, columns=["date", "open", "close", "high", "low", "volume"])
            df["date"] = pd.to_datetime(df["date"])
            for col in ["open", "close", "high", "low", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df.set_index("date", inplace=True)
            return df
        return pd.DataFrame()
    except Exception as e:
        logger.error(f"腾讯K线获取失败 {symbol}: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=3600)
def fetch_tencent_kline(symbol: str, days: int = 250) -> pd.DataFrame:
    """从腾讯获取日K线数据（带缓存）"""
    return _fetch_tencent_kline_raw(symbol, days)


@rate_limiter
def _fetch_sina_minute_kline_raw(symbol: str, period: int = 15, count: int = 96) -> list:
    """从新浪获取分钟K线数据（原始函数）"""
    try:
        params = {
            "symbol": symbol,
            "scale": period,
            "ma": "no",
            "datalen": count
        }
        response = requests.get(SINA_MINUTE_URL, params=params, timeout=10)
        text = response.text
        start = text.find("(") + 1
        end = text.rfind(")")
        return json.loads(text[start:end])
    except Exception as e:
        logger.error(f"新浪分钟K线获取失败 {symbol}: {e}")
        return []


@st.cache_data(ttl=180)
def fetch_sina_minute_kline(symbol: str, period: int = 15, count: int = 96) -> list:
    """从新浪获取分钟K线数据（带缓存）"""
    return _fetch_sina_minute_kline_raw(symbol, period, count)


# ============ 业务逻辑函数 ============

def is_trade_date(check_date: date) -> bool:
    """判断是否是交易日（简化版：非周末）"""
    # 周一到周五为交易日（不考虑节假日）
    return check_date.weekday() < 5


@st.cache_data(ttl=42000)
def get_amount_curve(ndays: int) -> list:
    """获取成交量分时曲线"""
    logger.info(f"开始获取成交量曲线，天数：{ndays}")
    try:
        # 获取上证和深证的分钟数据
        sh_data = fetch_sina_minute_kline("sh000001", period=15, count=ndays * 16 + 16)
        sz_data = fetch_sina_minute_kline("sz399001", period=15, count=ndays * 16 + 16)

        if not sh_data or not sz_data:
            logger.warning("分钟数据获取失败，使用默认曲线")
            return [0.05, 0.08, 0.07, 0.06, 0.07, 0.08, 0.07, 0.06, 0.05, 0.07, 0.08, 0.07, 0.06, 0.07, 0.08, 0.07]

        # 合并计算
        df_sh = pd.DataFrame(sh_data)
        df_sz = pd.DataFrame(sz_data)
        df_sh["amount"] = pd.to_numeric(df_sh["amount"])
        df_sz["amount"] = pd.to_numeric(df_sz["amount"])

        # 取最近ndays天的数据（排除今天）
        today = datetime.now().strftime("%Y-%m-%d")
        df_sh = df_sh[~df_sh["day"].str.startswith(today)]
        df_sz = df_sz[~df_sz["day"].str.startswith(today)]

        df_sh = df_sh.tail(ndays * 16)
        df_sz = df_sz.tail(ndays * 16)

        # 计算每天每15分钟的成交额占比
        total_sh = df_sh["amount"].sum()
        total_sz = df_sz["amount"].sum()

        curve = []
        for i in range(16):
            sh_pct = df_sh.iloc[i::16]["amount"].sum() / total_sh if total_sh > 0 else 0
            sz_pct = df_sz.iloc[i::16]["amount"].sum() / total_sz if total_sz > 0 else 0
            curve.append((sh_pct + sz_pct) / 2)

        logger.info(f"成交量曲线: {[f'{x:.3f}' for x in curve]}")
        return curve
    except Exception as e:
        logger.error(f"获取成交量曲线失败: {e}")
        return [0.05, 0.08, 0.07, 0.06, 0.07, 0.08, 0.07, 0.06, 0.05, 0.07, 0.08, 0.07, 0.06, 0.07, 0.08, 0.07]


@st.cache_data(ttl=180)
def get_estimate_amount(minutes: int, vol: float = None) -> int:
    """估算成交量"""
    logger.info(f"估算成交量，已交易分钟数：{minutes}，当前成交量：{vol}")
    curve = get_amount_curve(3)
    df = pd.DataFrame(curve, columns=["amount"])
    t = min(minutes // 15, 15)
    remaining_minutes = minutes % 15

    try:
        a = df.iloc[0:t]["amount"].sum() + df.iloc[t]["amount"] * remaining_minutes / 15
    except (KeyError, IndexError):
        a = df.iloc[0:t]["amount"].sum()

    if a <= 0:
        return 0

    if not vol:
        quotes = fetch_tencent_quotes("sh000001,sz399001")
        vol = sum(q["amount"] for q in quotes.values())

    try:
        return int(vol / a) if vol > 0 else 0
    except ZeroDivisionError:
        return 0


@st.cache_data(ttl=3600)
def get_n_day_avg_amount(n: int) -> int:
    """获取最近n个交易日的平均成交额（使用新浪源历史数据）

    使用AKShare新浪源获取上证指数和深证成指的历史K线数据，
    通过实时成交额与成交量的比值来估算历史成交额。

    新浪源volume单位是手（100股），腾讯API volume单位是股。
    转换公式：股 = 手 / 100
    """
    logger.info(f"获取最近 {n} 个交易日的平均成交额")
    try:
        import akshare as ak

        # 先获取实时数据作为基准
        sh_amount, sz_amount = get_a_amount()
        sh_quotes = fetch_tencent_quotes("sh000001")
        sz_quotes = fetch_tencent_quotes("sz399001")

        # 获取实时成交量（股）
        sh_real_volume = sh_quotes.get("000001", {}).get("volume", 0)
        sz_real_volume = sz_quotes.get("399001", {}).get("volume", 0)

        if sh_real_volume == 0 or sz_real_volume == 0:
            logger.warning("实时成交量为0，返回实时成交额")
            return int(sh_amount + sz_amount)

        # 计算转换系数：成交额 / 成交量（元/股）
        sh_price_per_share = sh_amount / sh_real_volume if sh_real_volume > 0 else 0
        sz_price_per_share = sz_amount / sz_real_volume if sz_real_volume > 0 else 0

        # 新浪源获取历史数据
        sh_df = ak.stock_zh_index_daily(symbol="sh000001")
        sz_df = ak.stock_zh_index_daily(symbol="sz399001")

        if sh_df.empty or sz_df.empty:
            logger.warning("新浪源返回空数据，返回实时成交额")
            return int(sh_amount + sz_amount)

        # 取最近n个交易日
        sh_recent = sh_df.tail(n)
        sz_recent = sz_df.tail(n)

        # 新浪源volume单位是手（100股），需要除以100转换为股
        # 成交额 = 股数 * 每股价格
        sh_avg_amount = (sh_recent['volume'] / 100 * sh_price_per_share).mean()
        sz_avg_amount = (sz_recent['volume'] / 100 * sz_price_per_share).mean()

        total_avg = int(sh_avg_amount + sz_avg_amount)

        logger.info(f"最近{n}日平均成交额: {total_avg/1e8:.0f}亿 (基于新浪源历史数据)")
        return total_avg

    except Exception as e:
        logger.error(f"获取{n}日均值失败: {e}")
        # 降级到实时数据
        try:
            sh_amount, sz_amount = get_a_amount()
            return int(sh_amount + sz_amount)
        except:
            return 0


@st.cache_data(ttl=180)
def get_index_price(symbol: str) -> int:
    """获取指数价格"""
    try:
        # 转换代码格式
        if symbol.startswith("000") or symbol.startswith("60"):
            ts_symbol = f"sh{symbol}"
        else:
            ts_symbol = f"sz{symbol}"

        quotes = fetch_tencent_quotes(ts_symbol)
        code = symbol
        if code in quotes:
            return int(quotes[code]["price"])
        return 0
    except Exception as e:
        logger.error(f"获取指数 {symbol} 价格失败: {e}")
        return 0


@st.cache_data(ttl=180)
def get_index_amount(symbol: str) -> float:
    """获取指数成交额"""
    try:
        if symbol.startswith("000") or symbol.startswith("60"):
            ts_symbol = f"sh{symbol}"
        else:
            ts_symbol = f"sz{symbol}"

        quotes = fetch_tencent_quotes(ts_symbol)
        code = symbol
        if code in quotes:
            return quotes[code]["amount"] * 10000  # 万 -> 元
        return 0
    except Exception as e:
        logger.error(f"获取指数 {symbol} 成交额失败: {e}")
        return 0


@st.cache_data(ttl=180)
def get_a_amount() -> tuple[float, float]:
    """获取上证和深证成交额"""
    try:
        quotes = fetch_tencent_quotes("sh000001,sz399001")
        sh_amount = quotes.get("000001", {}).get("amount", 0) * 10000
        sz_amount = quotes.get("399001", {}).get("amount", 0) * 10000
        return sh_amount, sz_amount
    except Exception as e:
        logger.error(f"获取成交额失败: {e}")
        return 0, 0


@st.cache_data(ttl=180)
def get_all_stocks_data() -> pd.DataFrame:
    """获取所有A股数据"""
    return fetch_all_sina_stocks()


@st.cache_data(ttl=180)
def middle_price_change() -> float:
    """计算中位数涨幅"""
    df = get_all_stocks_data()
    if df.empty:
        return 0
    return df["changepercent"].median()


@st.cache_data(ttl=180)
def count_limit_up_stocks() -> int:
    """计算涨停板数量"""
    df = get_all_stocks_data()
    if df.empty:
        return 0
    # 30/68开头20%涨停，8开头30%涨停，其他10%涨停
    limit_up = df[
        ((df["code"].str.startswith(("30", "68"))) & (df["changepercent"] >= 19.9)) |
        ((df["code"].str.startswith("8")) & (df["changepercent"] >= 29)) |
        (~df["code"].str.startswith(("30", "68", "8")) & (df["changepercent"] >= 9.9))
    ]
    return len(limit_up)


@st.cache_data(ttl=180)
def count_limit_down_stocks() -> int:
    """计算跌停板数量"""
    df = get_all_stocks_data()
    if df.empty:
        return 0
    limit_down = df[
        ((df["code"].str.startswith(("30", "68"))) & (df["changepercent"] <= -19.9)) |
        ((df["code"].str.startswith("8")) & (df["changepercent"] <= -29)) |
        (~df["code"].str.startswith(("30", "68", "8")) & (df["changepercent"] <= -9.9))
    ]
    return len(limit_down)


@st.cache_data(ttl=180)
def stock_up_down_ratio() -> float:
    """计算上涨股票占比"""
    df = get_all_stocks_data()
    if df.empty:
        return 0
    up_count = len(df[df["changepercent"] >= 0])
    return (up_count / len(df)) * 100


@st.cache_data(ttl=180)
def top_n_stock_avg_price_change(n: int) -> tuple[float, float]:
    """计算前n%成交额股票的平均涨幅"""
    df = get_all_stocks_data()
    if df.empty:
        return 0, 0

    df_sorted = df.sort_values("amount", ascending=False)
    top_n = int(len(df) * n / 100)
    top_df = df_sorted.head(top_n)

    # 加权平均
    weighted_avg = (top_df["changepercent"] * top_df["mktcap"]).sum() / top_df["mktcap"].sum() if top_df["mktcap"].sum() > 0 else 0
    # 算术平均（去掉涨幅超过31%的）
    simple_avg = top_df[top_df["changepercent"] < 31]["changepercent"].mean()

    return weighted_avg, simple_avg


@st.cache_data(ttl=180)
def top_n_stock_amount_percent(n: int) -> float:
    """计算前n%股票成交额占比"""
    df = get_all_stocks_data()
    if df.empty:
        return 0

    df_sorted = df.sort_values("amount", ascending=False)
    top_n = int(len(df) * n / 100)
    total_amount = df["amount"].sum()

    if total_amount == 0:
        return 0

    return df_sorted.head(top_n)["amount"].sum() / total_amount


def predict_amount(current_amount: float, current_time: datetime) -> float:
    """预测成交额"""
    if not during_market_time(current_time):
        return current_amount
    elapsed_minutes = minutes_since_market_open(current_time)
    return get_estimate_amount(elapsed_minutes, current_amount)


@st.cache_data(ttl=180)
def get_top_n_popular_stocks(n: int) -> pd.DataFrame:
    """获取成交额前N的股票"""
    df = get_all_stocks_data()
    if df.empty:
        return None

    df_sorted = df.sort_values("amount", ascending=False).head(n)
    result = df_sorted[["code", "name", "trade", "changepercent", "amount", "mktcap", "turnoverratio"]].copy()
    result.columns = ["代码", "名称", "最新价", "涨跌幅", "成交额", "总市值", "换手率"]
    result["涨跌幅"] = result["涨跌幅"].apply(lambda x: f"{x:.2f}%")
    result["换手率"] = result["换手率"].apply(lambda x: f"{x:.2f}%")
    result["成交额"] = (result["成交额"].astype(float) / 1e8).apply(lambda x: f"{x:.0f}亿")
    result["总市值"] = (result["总市值"].astype(float) / 1e4).apply(lambda x: f"{x:.0f}亿")
    result["最新价"] = result["最新价"].apply(lambda x: f"{float(x):.2f}")
    result.set_index("名称", inplace=True)
    return result


@st.cache_data(ttl=180)
def calculate_top_n_stocks_avg_market_value(n: int) -> tuple[float, float, int]:
    """计算前N只股票的平均市值"""
    df = get_all_stocks_data()
    if df.empty:
        return 0, 0, 0

    df_sorted = df.sort_values("amount", ascending=False).head(n)
    total_mv = df_sorted["mktcap"].astype(float).sum() / 1e4  # 万 -> 亿
    avg_mv = df_sorted["mktcap"].astype(float).mean() / 1e4
    return avg_mv, total_mv, len(df_sorted)


# ============ Streamlit 页面 ============

def get_market_heat() -> dict:
    """获取市场热度数据"""
    logger.info("获取市场热度数据")

    sh_amount, sz_amount = get_a_amount()
    current_time = datetime.now()

    sh_pred = predict_amount(sh_amount, current_time)
    sz_pred = predict_amount(sz_amount, current_time)

    total_amount = sh_amount + sz_amount or 1
    total_pred = sh_pred + sz_pred

    cyb_amount = get_index_amount("399006")
    cyb_ratio = cyb_amount / total_amount * 100

    hs300_amount = get_index_amount("000300")
    hs300_ratio = hs300_amount / total_amount * 100

    zz1000_amount = get_index_amount("000852")
    zz1000_ratio = zz1000_amount / total_amount * 100

    # 注意：腾讯API不支持中证2000(932000)，使用中证500(000905)替代
    zz500_amount = get_index_amount("000905")
    zz500_ratio = zz500_amount / total_amount * 100

    avg_5_day = get_n_day_avg_amount(5)
    crowdedness = top_n_stock_amount_percent(5) * 100
    middle_price_change_value = middle_price_change()
    top5_weighted_avg_price_change, top5_avg_price_change = top_n_stock_avg_price_change(5)
    up_down_ratio = stock_up_down_ratio()
    limit_up_count = count_limit_up_stocks()
    limit_down_count = count_limit_down_stocks()
    avg_market_value, total_market_value, stocks_count = calculate_top_n_stocks_avg_market_value(10)
    top_stocks = get_top_n_popular_stocks(10)

    data = {
        "指标": [
            "上证成交额", "深证成交额", "创业板成交额", "当前总成交额",
            "创业板成交占总成交比例", "中证 1000 成交占总成交比例",
            "中证 500 成交占总成交比例", "沪深 300 成交占总成交比例",
            "预计今日总成交额", "5日均值", "交易拥挤度", "中位数股票涨幅",
            "前 5% 成交加权涨幅", "前 5% 成交算数涨幅", "股票上涨百分比",
            "涨停板股票数量", "跌停板股票数量",
            f"前{stocks_count}大成交额股票平均市值", f"前{stocks_count}大成交额股票活跃度",
        ],
        "数值": [
            int(sh_amount / 1e8), int(sz_amount / 1e8),
            int(cyb_amount / 1e8), int(total_amount / 1e8),
            round(cyb_ratio, 2), round(zz1000_ratio, 2),
            round(zz500_ratio, 2), round(hs300_ratio, 2),
            int(total_pred / 1e8) if is_trade_date(datetime.now(pytz.timezone("Asia/Shanghai")).date()) else None,
            int(avg_5_day / 1e8), round(crowdedness, 2),
            round(middle_price_change_value, 2),
            round(top5_weighted_avg_price_change, 2),
            round(top5_avg_price_change, 2),
            round(up_down_ratio, 2),
            limit_up_count, limit_down_count,
            int(avg_market_value), top_stocks,
        ],
    }
    return data


def color_negative_red(val):
    try:
        val = float(val.rstrip("%"))
    except ValueError:
        return ""
    color = "red" if val > 0 else "green"
    return f"color: {color}"


def get_progress_html(value_pct: float) -> str:
    """进度条HTML"""
    def get_color(pct):
        if pct <= 20: return "#90d4a2"
        elif pct <= 40: return "#27ae60"
        elif pct <= 60: return "#f1c40f"
        elif pct <= 80: return "#e67e22"
        else: return "#e74c3c"

    color = get_color(value_pct)
    return f"""
        <div style="width: 100%; background-color: #eee; border-radius: 3px; padding: 3px; box-sizing: border-box;">
            <div style="width: {value_pct}%; height: 20px; background-color: {color}; border-radius: 2px; transition: width 0.3s ease;"></div>
        </div>
    """


def streamlit_spread_chart():
    """收益差分析图表"""
    st.markdown("### 📈 指数40日收益差分析")
    fig, hs300_zz1000_spread, zz1000_dividend_spread = create_spread_chart()
    st.plotly_chart(fig, use_container_width=True)
    col1, col2 = st.columns(2)
    with col1:
        st.write(f"沪深300-中证1000收益差: {hs300_zz1000_spread:.2f}%")
    with col2:
        st.write(f"中证1000-红利指数收益差: {zz1000_dividend_spread:.2f}%")


def streamlit_app():
    current_time = datetime.now()
    is_trading = during_market_time(current_time)

    # 自动刷新：交易时间 3 分钟，非交易时间 10 分钟（避免永久冻结/触发反爬）
    if is_trading:
        st_autorefresh(interval=180000, key="data_refresh")
    else:
        st_autorefresh(interval=600000, key="data_refresh_offhours")

    if st.button("🔄 强制刷新", key="force_refresh"):
        st.cache_data.clear()
        st.rerun()

    tab1, tab2, tab3, tab4, tab5 = st.tabs(["💹 成交量与情绪", "🏢 龙头股分析", "📈 收益差分析", "📊 指数月度对比", "🎯 IF/IM策略"])

    with tab1:
        st.markdown("### 🎯 市场成交与情绪分析")
        try:
            data = get_market_heat()
        except Exception as e:
            logger.error(f"获取市场数据失败: {e}")
            st.error(f"获取数据失败: {e}")
            data = None

        if data is None:
            st.warning("暂无数据，请稍后刷新")
        else:
            metrics_col1, metrics_col2, metrics_col3, metrics_col4 = st.columns(4)

            with metrics_col1:
                avg_amount = data["数值"][9]
                pred_amount = data["数值"][8]
                if pred_amount is not None:
                    delta_vs_avg = pred_amount - avg_amount
                    st.metric("预估成交额", f"{pred_amount:,}亿",
                              delta=f"{delta_vs_avg:+,}亿 vs 5日均值",
                              delta_color="normal" if delta_vs_avg > 0 else "inverse")
                else:
                    st.metric("预估成交额", "休市", delta="非交易日", delta_color="off")

            with metrics_col2:
                st.metric("上涨占比", f"{data['数值'][14]:.1f}%")

            with metrics_col3:
                st.metric("涨停数量", str(data["数值"][15]),
                          delta=f"-跌停 {data['数值'][16]}", delta_color="inverse")

            with metrics_col4:
                middle_change = data["数值"][11]
                st.metric("中位数涨幅", f"{middle_change:.2f}%")

            col1, col2 = st.columns(2)
            with col1:
                st.markdown("""
                <style>
                .index-progress { margin-bottom: 1rem; }
                .index-progress .label { margin-bottom: 0.5rem; font-weight: 500; color: #333; }
                .index-progress .value { font-size: 0.9rem; color: #666; margin-top: 0.3rem; text-align: right; }
                </style>
                """, unsafe_allow_html=True)

                st.markdown("#### 💰 指数成交占比")
                total = data["数值"][3]
                indices = [
                    ("上证指数", data["数值"][0]), ("深证指数", data["数值"][1]),
                    ("创业板", data["数值"][2]),
                    ("中证1000", data["数值"][5] * total / 100),
                    ("中证500", data["数值"][6] * total / 100),
                    ("沪深300", data["数值"][7] * total / 100),
                ]

                for name, amount in indices:
                    st.markdown('<div class="index-progress">', unsafe_allow_html=True)
                    cols = st.columns([2, 8])
                    with cols[0]:
                        st.markdown(f'<div class="label">{name}</div>', unsafe_allow_html=True)
                    with cols[1]:
                        percentage = (amount / total) * 100
                        st.markdown(get_progress_html(percentage), unsafe_allow_html=True)
                        st.markdown(f'<div class="value">{percentage:.1f}%</div>', unsafe_allow_html=True)
                    st.markdown("</div>", unsafe_allow_html=True)

                cols = st.columns(2)
                with cols[0]:
                    st.info(f"**总成交额**: {data['数值'][3]} 亿")
                with cols[1]:
                    st.info(f"**5日均值**: {data['数值'][9]} 亿")

            with col2:
                st.markdown("#### 💡 情绪指标")
                for item, value in zip(data["指标"][10:17], data["数值"][10:17]):
                    suffix = "%" if ("百分比" in item or "涨幅" in item) else ""
                    st.metric(label=item, value=f"{value}{suffix}")

    with tab2:
        st.markdown("### 🔥 龙头股活跃度分析")
        try:
            data = get_market_heat()
        except Exception as e:
            logger.error(f"获取龙头股数据失败: {e}")
            st.error(f"获取数据失败: {e}")
            data = None

        if data is None:
            st.warning("暂无数据，请稍后刷新")
        else:
            st.info(f"#### 📊 {data['指标'][17]}\n{data['数值'][17]}")

            if data["数值"][18] is not None:
                col1, col2 = st.columns([2, 2])
                with col1:
                    sort_by = st.selectbox("排序依据", ["成交额", "涨跌幅", "换手率", "总市值"], index=0)

                df = data["数值"][18]
                if sort_by == "涨跌幅":
                    df = df.sort_values(by="涨跌幅", ascending=False, key=lambda x: x.str.rstrip("%").astype(float))
                elif sort_by in ["成交额", "总市值"]:
                    df = df.sort_values(by=sort_by, ascending=False, key=lambda x: x.str.rstrip("亿").astype(float))
                elif sort_by == "换手率":
                    df = df.sort_values(by="换手率", ascending=False, key=lambda x: x.str.rstrip("%").astype(float))

                styled_df = (
                    df.style.map(color_negative_red, subset=["涨跌幅"])
                    .set_properties(**{"background-color": "#f0f2f6", "color": "#1f2937", "font-size": "14px"})
                    .set_table_styles([{"selector": "th", "props": [("background-color", "#dfe3e8"), ("color", "#374151")]}])
                )
                st.dataframe(styled_df, use_container_width=True, height=400, hide_index=False)

    with tab3:
        streamlit_spread_chart()

    with tab4:
        render_index_comparison_page()

    with tab5:
        render_if_im_strategy_page()

    # 状态栏
    current_time = datetime.now()
    updated_at = current_time.astimezone(pytz.timezone("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    is_trading = during_market_time(current_time)
    status = "（非交易时间 - 已暂停刷新）" if not is_trading else f"（交易中 - 60秒自动刷新）"
    status_color = "#1f77b4" if is_trading else "#666"
    st.markdown(f"""
        <div style='padding: 10px; background-color: #f0f2f6; border-radius: 5px; font-size: 14px; color: {status_color}; text-align: center; display: flex; justify-content: space-between; align-items: center;'>
            <span>⏰ 数据更新时间: {updated_at}</span>
            <span style='color: {status_color}; font-weight: 500;'>{status}</span>
        </div>
    """, unsafe_allow_html=True)


if __name__ == "__main__":
    st.set_page_config("成交量预测", "📈", layout="wide", initial_sidebar_state="expanded")
    streamlit_app()

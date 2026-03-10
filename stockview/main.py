import streamlit as st
from datetime import date, datetime
import pandas as pd
from stockview.log import logger
import pytz

# from streamlit_autorefresh import st_autorefresh
import akshare
from stockview.akcache import CacheWrapper
from stockview.options import analyze_atm_options, find_primary_options
from stockview.helpers import during_market_time, minutes_since_market_open
from streamlit_autorefresh import st_autorefresh
from stockview.index_spread import create_spread_chart
import sys
import os

ak = CacheWrapper(akshare, cache_time=180)


@st.cache_data(ttl=60)
def is_trade_date(date):
    """
    判断是否是交易日。

    参数:
    date (datetime.date): 要检查的日期。

    返回:
    bool: 如果是交易日，则返回 True；否则返回 False。
    """
    try:
        stock_calendar = ak.tool_trade_date_hist_sina()
        stock_calendar_dates = pd.to_datetime(stock_calendar["trade_date"]).dt.date
        if date in set(stock_calendar_dates):
            logger.info(f"{date} 是交易日")
            return True
        else:
            logger.info(f"{date} 不是交易日")
            return False
    except Exception as e:
        logger.error(f"检查日期时发生错误：{str(e)}")
        raise


# 只需要每天执行一次，获取成交量分时比例
@st.cache_data(ttl=42000)
def get_amount_curve(ndays):
    """
    获取指定天数的成交量曲线。

    参数:
    ndays (int): 要获取的天数。

    返回:
    list: 包含每15分钟成交量百分比的列表。

    异常:
    如果在获取或处理数据时发生错误，将记录错误并抛出异常。

    功能描述:
    1. 从 akshare 获取上证和深证的分钟数据。
    2. 合并数据并计算总成交量。
    3. 过滤掉当天的数据，只保留指定天数的数据。
    4. 计算每15分钟的成交量占当天总成交量的百分比。
    5. 生成并返回成交量曲线。

    日志:
    - 记录获取数据的开始和成功信息。
    - 记录数据处理完成的信息。
    - 记录百分比数据计算成功的信息。
    - 记录生成成交量曲线的信息。
    - 记录任何发生的错误。
    """
    logger.info(f"开始获取成交量曲线，天数：{ndays}")
    try:
        stock_zh_a_minute_df_sh = ak.stock_zh_a_minute(
            symbol="sh000001", period="15", adjust="qfq"
        )
        stock_zh_a_minute_df_sz = ak.stock_zh_a_minute(
            symbol="sz399001", period="15", adjust="qfq"
        )
        logger.info("成功获取上证和深证分钟数据")

        df = pd.concat([stock_zh_a_minute_df_sh, stock_zh_a_minute_df_sz], axis=1)
        df2 = df.iloc[:, [0, 5, 11]].copy(deep=True)
        df2.columns = ["day", "amount_sh", "amount_sz"]
        df2["amount_sh"] = pd.to_numeric(df2["amount_sh"])
        df2["amount_sz"] = pd.to_numeric(df2["amount_sz"])
        df2["date"] = pd.to_datetime(df2["day"])  # format='%Y-%m-%d %H:%M:%S'
        df2["totalamount"] = df2.apply(
            lambda x: (x["amount_sh"] + x["amount_sz"]), axis=1
        )
        df2.drop(["day"], axis=1, inplace=True)
        df2 = df2[
            df2["date"] < datetime.combine(date.today(), datetime.min.time())
        ]  # 获取本日之前15分钟数据，当日不要。
        df2 = df2.tail(ndays * 16)  # 取n天的数据，一天16个数据。
        logger.info(f"数据处理完成，共{len(df2)}条记录")

        df_all = pd.DataFrame()
        for i in range(ndays):
            df_range = df2.iloc[i * 16 : (i + 1) * 16].copy()  # 创建副本
            day_amount = df_range.totalamount.sum()
            df_range.loc[:, "pct"] = df_range.apply(
                lambda x: (x["totalamount"] / day_amount), axis=1
            )
            df_all = pd.concat([df_all, df_range])
        df_all = df_all.reset_index()
        logger.info(f"成功计算{ndays}天的百分比数据")

        curve = []
        for j in range(16):
            curve.append(df_all[df_all.index % 16 - j == 0].pct.mean())

        logger.info(f"成功生成成交量曲线:{curve}")
        return curve
    except Exception as e:
        logger.error(f"获取成交量曲线时发生错误：{str(e)}")
        raise


@st.cache_data(ttl=180)
def get_estimate_amount(minutes, vol=None):
    """
    估算成交量。

    参数：
    minutes (int): 已交易的分钟数。
    vol (int, 可选): 指定的成交量。如果未提供，将自动获取。

    返回：
    int: 估算的成交量。如果发生错误，返回0。

    异常：
    KeyError: 当计算累计比例时发生键错误。
    ZeroDivisionError: 当估算成交量时发生除零错误。

    日志：
    - 记录开始估算成交量的信息。
    - 记录计算得到的累计比例。
    - 记录自动获取的成交量（如果未指定）。
    - 记录估算的成交量。
    - 记录计算累计比例时的键错误警告。
    - 记录估算成交量时的除零错误。
    """

    logger.info(f"开始估算成交量，已交易分钟数：{minutes}，指定成交量：{vol}")
    curve = get_amount_curve(3)
    df = pd.DataFrame(curve, columns=["amount"])
    t = minutes // 15
    if t > 15:
        t = 15
    remaining_minutes = minutes % 15

    try:
        a = df[0:t]["amount"].sum() + df.loc[t] * remaining_minutes / 15

        logger.info(f"计算得到的累计比例：{a}")
    except KeyError:
        a = df[0:t]["amount"].sum()
        logger.warning(f"计算累计比例时发生KeyError，使用备选方案：{a}")
        raise

    if not vol:
        total_amount = get_a_amount()
        vol = total_amount[0] + total_amount[1]
        logger.info(f"自动获取成交量：{vol}")
    try:
        estimated_amount = int(vol / a.iloc[0]) if vol > 0 else 0
        logger.info(f"估算的成交量：{estimated_amount}")
        return estimated_amount
    except ZeroDivisionError:
        logger.error("估算成交量时发生除零错误")
        return 0


@st.cache_data(ttl=180)
def get_n_day_avg_amount(n):
    """
    获取上证和深证指数最近 n 个交易日的平均成交额。

    参数:
        n (int): 要计算的交易日天数。

    返回:
        tuple: 包含上证和深证指数平均成交额的元组，格式为 (sh_avg_amount, sz_avg_amount)。
    """
    logger.info(f"开始获取最近 {n} 个交易日的平均成交额")
    try:
        stock_zh_a_daily_df_sh = ak.stock_zh_index_daily_em(symbol="sh000001")
        stock_zh_a_daily_df_sz = ak.stock_zh_index_daily_em(symbol="sz399001")
        logger.info("成功获取上证和深证每日数据")

        sh_amount = int(stock_zh_a_daily_df_sh["amount"].iloc[-6:-1].mean())
        sz_amount = int(stock_zh_a_daily_df_sz["amount"].iloc[-6:-1].mean())

        logger.info(
            f"最近 {n} 个交易日上证平均成交额: {sh_amount}, 深证平均成交额: {sz_amount}"
        )
        return sh_amount + sz_amount
    except Exception as e:
        logger.error(f"获取最近 {n} 个交易日的平均成交额时发生错误：{str(e)}")
        return 0, 0


@st.cache_data(ttl=180)
def get_index_price(symbol):
    try:
        index_data = ak.stock_zh_index_spot_em(symbol="沪深重要指数")
        index_value = int(index_data[index_data["代码"] == symbol]["最新价"].values[0])
        return index_value
    except Exception as e:
        exc_type, exc_obj, tb = sys.exc_info()
        fname = os.path.split(tb.tb_frame.f_code.co_filename)[1]
        logger.error(
            f"获取指数 {symbol} 当前价格时发生错误：文件 {fname} 行号 {tb.tb_lineno} 错误信息: {str(e)}"
        )
        return 0


def get_index_amount(symbol):
    try:
        df_sh = ak.stock_zh_index_spot_em(symbol="上证系列指数")
        df_sz = ak.stock_zh_index_spot_em(symbol="深证系列指数")
        df_csi = ak.stock_zh_index_spot_em(symbol="中证系列指数")
        df_vip = ak.stock_zh_index_spot_em(symbol="沪深重要指数")
        df = pd.concat([df_sh, df_sz, df_csi, df_vip], axis=0)

        index_value = int(df[df["代码"] == symbol]["成交额"].values[0])
        return index_value
    except Exception as e:
        logger.error(f"获取指数 {symbol} 当前成交额时发生错误：{str(e)}")
        raise


# 获取当前成交额
@st.cache_data(ttl=180)
def get_a_amount() -> tuple[float, float]:
    """
    获取上证和深证指数的成交量。

    该函数使用 akshare 库获取当前上证指数和深证指数的成交量。
    如果当前时间不在交易时间内，则将全局的 TTL 变量设置为 3600 秒。

    返回:
        tuple: 包含上证和深证指数成交量的元组，格式为 (sh_amount, sz_amount)。

    异常:
        KeyError: 如果在获取的数据中未找到预期的股票代码（上证为 "000001"，深证为 "399001"）。
    """

    try:
        logger.info("开始获取指数数据")
        spot_df = ak.stock_zh_index_spot_em(symbol="沪深重要指数")
    except Exception as e:
        logger.error(f"获取指数数据时发生错误：{str(e)}")
        return 0, 0

    # 检查是否存在对应的指数代码
    sh_mask = spot_df["代码"] == "000001"
    sz_mask = spot_df["代码"] == "399001"

    if not sh_mask.any() or not sz_mask.any():
        logger.error("未找到上证或深证指数数据")
        return 0, 0

    sh_amount = spot_df[sh_mask]["成交额"].iloc[0]  # 使用iloc[0]代替values[0]
    sz_amount = spot_df[sz_mask]["成交额"].iloc[0]  # 深证成交额
    logger.info(f"获取上证和深证指数的成交量: 上证 {sh_amount}, 深证 {sz_amount}")
    if pd.isna(sh_amount) or pd.isna(sz_amount):
        logger.error("获取的成交量数据包含 NaN 值")
        return 0, 0

    return sh_amount, sz_amount


@st.cache_data(ttl=180)
def middle_price_change():
    """
    计算所有股票的中位数涨幅。

    该函数获取A股的实时交易数据，并计算所有股票的中间涨幅（即按涨幅排序后位于中间的股票涨幅）。

    返回:
        float: 中间股票涨幅。如果数据为空，则返回0。
    """
    df = ak.stock_zh_a_spot_em()
    if df.empty:
        logger.info("实时行情数据为空，无法计算中间涨幅")
        return 0

    # 按涨幅排序
    df_sorted = df.sort_values("涨跌幅")

    # 计算中间位置
    middle_index = len(df_sorted) // 2

    # 获取中间涨幅
    middle_price_change = df_sorted.iloc[middle_index]["涨跌幅"]
    logger.info(f"中间股票涨幅为: {middle_price_change}")

    return middle_price_change


@st.cache_data(ttl=180)
def count_limit_up_stocks():
    """
    计算涨停板股票的数量。

    该函数获取A股的实时交易数据，并计算涨停板（涨幅达到10%或以上）的股票数量。

    返回:
        int: 涨停板股票的数量。如果数据为空，则返回0。
    """
    df = ak.stock_zh_a_spot_em()
    if df.empty:
        logger.info("实时行情数据为空，无法计算涨停板数量")
        return 0

    # 计算涨停板股票的数量，30 开头和 68 开头的是 20% 涨停，其他是 10% 涨停
    df["涨停板"] = df.apply(
        lambda row: (
            row["涨跌幅"] >= 19.9
            if row["代码"].startswith(("30", "68"))
            else row["涨跌幅"] >= 29
            if row["代码"].startswith("8")
            else row["涨跌幅"] >= 9.9
        ),
        axis=1,
    )
    limit_up_stocks = df[df["涨停板"] & ~df["代码"].str.startswith("8")].shape[0]
    logger.info(f"涨停板股票数量: {limit_up_stocks}")

    return limit_up_stocks


@st.cache_data(ttl=180)
def count_limit_down_stocks():
    """
    计算跌停板股票的数量。

    该函数获取A股的实时交易数据，并计算跌停板（跌幅达到10%或以上）的股票数量。

    返回:
        int: 跌停板股票的数量。如果数据为空，则返回0。
    """
    df = ak.stock_zh_a_spot_em()
    if df.empty:
        logger.info("实时行情数据为空，无法计算跌停板数量")
        return 0

    # 计算跌停板股票的数量，30 开头和 68 开头的是 20% 跌停，其他是 10% 跌停
    df["跌停板"] = df.apply(
        lambda row: (
            row["涨跌幅"] <= -19.9
            if row["代码"].startswith(("30", "68"))
            else row["涨跌幅"] <= -29
            if row["代码"].startswith("8")
            else row["涨跌幅"] <= -9.9
        ),
        axis=1,
    )
    limit_down_stocks = df[df["跌停板"] & ~df["代码"].str.startswith("8")].shape[0]
    logger.info(f"跌停板股票数量: {limit_down_stocks}")

    return limit_down_stocks


@st.cache_data(ttl=180)
def stock_up_down_ratio():
    """
    计算股票的涨跌比。

    该函数获取A股的实时交易数据，并计算上涨股票数量与下跌股票数量的比值。

    返回:
        float: 股票的涨跌比。如果数据为空，则返回0。
    """
    df = ak.stock_zh_a_spot_em()
    if df.empty:
        logger.info("实时行情数据为空，无法计算涨跌比")
        return 0

    # 计算股票总数
    num_stocks = len(df)

    # 计算上涨和下跌股票的数量
    up_stocks = df[df["涨跌幅"] >= 0].shape[0]
    down_stocks = df[df["涨跌幅"] < 0].shape[0]
    logger.info(f"上涨股票数量: {up_stocks}, 下跌股票数量: {down_stocks}")
    if down_stocks == 0:
        logger.info("没有下跌的股票，涨跌比为无穷大")
        return float("inf")

    up_down_ratio = (up_stocks / num_stocks) * 100
    logger.info(f"股票上涨百分比为: {up_down_ratio}")

    return up_down_ratio


@st.cache_data(ttl=180)
def top_n_stock_avg_price_change(n):
    """
    计算前 n% 成交金额的股票的平均涨幅。

    该函数获取A股的实时交易数据，将股票按成交金额降序排序，并计算总成交金额。
    然后确定构成前 n% 成交金额的股票数量，并计算这些股票的平均涨幅。

    参数:
        n (float): 要计算的股票百分比。

    返回:
        float: 前 n% 成交金额的股票的平均涨幅。如果数据为空，则返回0。
    """
    # 获取 A 股实时行情数据
    # 序号	代码	名称	最新价	涨跌幅	涨跌额	成交量	成交额	振幅	最高	...	量比	换手率	市盈率-动态	市净率	总市值	流通市值	涨速	5分钟涨跌	60日涨跌幅	年初至今涨跌幅
    df = ak.stock_zh_a_spot_em()
    if df.empty:
        logger.info("实时行情数据为空，无法计算平均涨幅")
        return 0

    # 按成交金额降序排序
    df_sorted = df.sort_values("成交额", ascending=False)

    # 计算前 n% 的股票数量
    num_stocks = len(df)
    top_n_percent = int(num_stocks * (n / 100))

    # 计算前 n% 股票的加权平均涨幅
    top_n_weighted_avg_price_change = (
        df_sorted["涨跌幅"].head(top_n_percent)
        * df_sorted["总市值"].head(top_n_percent)
    ).sum() / df_sorted["总市值"].head(top_n_percent).sum()

    # 计算前 n% 股票的算数平均涨幅，去除涨幅超过31%的股票
    top_n_avg_price_change = (
        df_sorted[df_sorted["涨跌幅"] < 31]["涨跌幅"].head(top_n_percent).mean()
    )

    logger.info(f"前 {n}% 成交金额的股票的平均涨幅为: {top_n_avg_price_change:.2f}%")

    return top_n_weighted_avg_price_change, top_n_avg_price_change


@st.cache_data(ttl=180)
def top_n_stock_amount_percent(n):
    """
    计算前 n% 的股票对总成交量的贡献百分比。

    该函数获取A股的实时交易数据，将股票按成交量降序排序，并计算总成交量。
    然后确定构成前 n% 成交量的股票数量，并计算这些股票的总成交量。
    最后，计算前 n% 的股票对总成交量的贡献百分比。

    参数:
        n (float): 要计算的股票百分比。

    返回:
        float: 前 n% 的股票对总成交量的贡献百分比。如果总成交量为零，则返回0。
    """

    # 获取 A 股实时行情数据
    df = ak.stock_zh_a_spot_em()
    # 按成交量降序排序
    df_sorted = df.sort_values("成交量", ascending=False)

    # 计算前 n% 的股票数量
    num_stocks = len(df)
    top_n_percent = int(num_stocks * (n / 100))

    # 计算总成交量
    total_amount = df["成交量"].sum()

    if total_amount == 0:
        logger.info("总成交量为0, 可能是盘前，无法计算拥挤度")
        return 0

    # 计算前 n% 股票的成交量总和
    top_n_percent_amount = df_sorted["成交量"].head(top_n_percent).sum()

    # 计算拥挤度
    crowdedness = top_n_percent_amount / total_amount
    logger.info(f"前 {n}% 成交量的股票占总成交量的 {crowdedness*100:.2f}%")

    return crowdedness


# 简单的预测模型
def predict_amount(current_amount, current_time):
    if not during_market_time(current_time):
        return current_amount  # 如果当前时间超过15:00，返回当前成交额作为预测值

    elapsed_minutes = minutes_since_market_open(current_time)

    # 使用 get_estimate_amount 获取预测的成交额
    predicted_amount = get_estimate_amount(elapsed_minutes, current_amount)

    return predicted_amount


def streamlit_options(etf):
    st.title(f"{etf}期权分析")

    # 300ETF期权分析
    filtered_df = find_primary_options(etf)
    atm_options, avg_amountatility, underlying_price = analyze_atm_options(filtered_df)

    st.write(f"标的ETF价格: {underlying_price:.3f}")
    st.write(f"平均ATM隐含波动率: :red[{avg_amountatility:.2f}%]")
    st.write("\nATM期权:")
    # 按照期权类型（购和沽）和行权价排序
    atm_options_sorted = atm_options.sort_values(by=["期权名称", "行权价"])

    st.dataframe(
        atm_options_sorted[["期权名称", "最新价", "隐含波动率", "行权价"]].reset_index(
            drop=True
        ),
        use_container_width=True,
    )

    # 额外分析
    call_options = atm_options[atm_options["期权名称"].str.contains("购")]
    put_options = atm_options[atm_options["期权名称"].str.contains("沽")]

    st.write(f"\nATM购期权平均隐含波动率: {call_options['隐含波动率'].mean():.2f}%")
    st.write(f"ATM沽期权平均隐含波动率: {put_options['隐含波动率'].mean():.2f}%")

    # 找到最接近ATM的期权
    closest_option = atm_options.loc[
        (atm_options["行权价"] - underlying_price).abs().idxmin()
    ]
    st.write(f"\n最接近ATM的期权: {closest_option['期权名称']}")
    st.write(f"行权价: {closest_option['行权价']:.3f}")
    st.write(f"隐含波动率: {closest_option['隐含波动率']:.2f}%")


@st.cache_data(ttl=180)
def get_top_n_popular_stocks(n):
    """
    获取成交额前 N 的股票详细信息和统计数据。

    参数:
        n (int): 获取前 N 只股票的信息

    返回:
        df: 包含前 N 只股票的详细信息和统计数据的 DataFrame。
    """
    try:
        # 获取 A 股实时行情数据
        df = ak.stock_zh_a_spot_em()
        if df.empty:
            logger.info("实时行情数据为空")
            return None

        # 按成交额降序排序
        df_sorted = df.sort_values("成交额", ascending=False)

        # 获取前 N 只股票
        top_n_stocks = df_sorted.head(n).copy()

        # 选择需要的列并重命名
        result_df = top_n_stocks[
            ["代码", "名称", "最新价", "涨跌幅", "成交额", "总市值", "换手率"]
        ].copy()

        # 格式化数值
        result_df["涨跌幅"] = result_df["涨跌幅"].apply(lambda x: f"{x:.2f}%")
        result_df["换手率"] = result_df["换手率"].apply(lambda x: f"{x:.2f}%")
        result_df["成交额"] = (result_df["成交额"] / 1e8).apply(lambda x: f"{int(x)}亿")
        result_df["总市值"] = (result_df["总市值"] / 1e8).apply(lambda x: f"{int(x)}亿")
        result_df["最新价"] = result_df["最新价"].apply(lambda x: f"{x:.2f}")

        # 设置索引为名称，但不显示索引名
        result_df.set_index("名称", inplace=True)

        return result_df

    except Exception as e:
        logger.error(f"获取股票信息时发生错误：{str(e)}")
        return None


@st.cache_data(ttl=180)
def calculate_top_n_stocks_avg_market_value(n):
    """
    计算成交额前N的股票的平均市值。

    参数:
        n (int): 要计算的股票数量

    返回:
        tuple: (avg_market_value, total_market_value, stocks_count)
        - avg_market_value: 平均市值（亿元）
        - total_market_value: 总市值（亿元）
        - stocks_count: 实际统计的股票数量
    """
    try:
        logger.info(f"开始计算前{n}只股票的平均市值")
        df = ak.stock_zh_a_spot_em()

        if df.empty:
            logger.warning("获取到的股票数据为空")
            return 0, 0, 0

        # 按成交额降序排序并获取前N只
        df_sorted = df.sort_values("成交额", ascending=False).head(n)

        # 计算市值（转换为亿元）
        total_market_value = df_sorted["总市值"].sum() / 1e8
        avg_market_value = df_sorted["总市值"].mean() / 1e8
        stocks_count = len(df_sorted)

        logger.info(f"前{stocks_count}只股票平均市值: {avg_market_value:.2f}亿")
        logger.info(f"前{stocks_count}只股票总市值: {total_market_value:.2f}亿")

        return avg_market_value, total_market_value, stocks_count
    except Exception as e:
        logger.error(f"计算平均市值时发生错误: {str(e)}")
        return 0, 0, 0


def get_market_heat():
    logger.info("程序启动")
    # Streamlit 页面设置

    # 获取当前成交额
    logger.info("开始获取当前成交额")
    sh_amount, sz_amount = get_a_amount()

    # 当前时间
    current_time = datetime.now()

    # 预测成交额
    sh_pred = predict_amount(sh_amount, current_time)
    sz_pred = predict_amount(sz_amount, current_time)

    # 计算总成交额和预测总成交额
    total_amount = sh_amount + sz_amount or 1  # Use 1 if total_amount is 0
    total_pred = sh_pred + sz_pred

    # 创业板成交占比（散户跟风指标）
    cyb_amount = get_index_amount("399006")

    # 计算创业板成交占总成交比例
    cyb_ratio = cyb_amount / total_amount * 100

    # 沪深 300 成交占比
    hs300_amount = get_index_amount("000300")
    hs300_ratio = hs300_amount / total_amount * 100

    # 中证 1000 成交占比
    zz1000_amount = get_index_amount("000852")
    zz1000_ratio = zz1000_amount / total_amount * 100

    # 中证 2000 成交占比
    zz2000_amount = get_index_amount("932000")
    zz2000_ratio = zz2000_amount / total_amount * 100

    # 获取5日均值
    avg_5_day = get_n_day_avg_amount(5)

    # 拥挤度，算法参见https://legulegu.com/stockdata/ashares-congestion
    crowdedness = top_n_stock_amount_percent(5) * 100

    # 中间股票涨幅
    middle_price_change_value = middle_price_change()

    # top5 成交额股票平均涨幅和加权平均涨幅
    top5_weighted_avg_price_change, top5_avg_price_change = (
        top_n_stock_avg_price_change(5)
    )
    # 股票涨跌比
    up_down_ratio = stock_up_down_ratio()

    # 涨停数量
    limit_up_count = count_limit_up_stocks()
    # 跌停数量
    limit_down_count = count_limit_down_stocks()

    # 计算前10只股票的平均市值
    avg_market_value, total_market_value, stocks_count = (
        calculate_top_n_stocks_avg_market_value(10)
    )
    # 显示前10只活跃股票的详细信息
    top_stocks = get_top_n_popular_stocks(10)

    # 创建数据字典
    data = {
        "指标": [
            "上证成交额",
            "深证成交额",
            "创业板成交额",
            "当前总成交额",
            "创业板成交占总成交比例",
            "中证 1000 成交占总成交比例",
            "中证 2000 成交占总成交比例",
            "沪深 300 成交占总成交比例",
            "预计今日总成交额",
            "5日均值",
            "交易拥挤度",
            "中位数股票涨幅",
            "前 5% 成交加权涨幅",
            "前 5% 成交算数涨幅",
            "股票上涨百分比",
            "涨停板股票数量",
            "跌停板股票数量",
            f"前{stocks_count}大成交额股票平均市值",
            f"前{stocks_count}大成交额股票活跃度",
        ],
        "数值": [
            int(sh_amount / 1e8),  # 上证成交额（亿）
            int(sz_amount / 1e8),  # 深证成交额（亿）
            int(cyb_amount / 1e8),  # 创业板成交额（亿）
            int(total_amount / 1e8),  # 当前总成交额（亿）
            round(cyb_ratio, 2),  # 创业板成交占比（%）
            round(zz1000_ratio, 2),  # 中证1000成交占比（%）
            round(zz2000_ratio, 2),  # 中证2000成交占比（%）
            round(hs300_ratio, 2),  # 沪深300成交占比（%）
            # 预计今日总成交额（亿）
            (
                int(total_pred / 1e8)
                if is_trade_date(datetime.now(pytz.timezone("Asia/Shanghai")).date())
                else None
            ),
            int(avg_5_day / 1e8),  # 5日均值（亿）
            round(crowdedness, 2),  # 交易拥挤度
            round(middle_price_change_value, 2),  # 中位数股票涨幅（%）
            round(top5_weighted_avg_price_change, 2),  # 前5%成交加权涨幅（%）
            round(top5_avg_price_change, 2),  # 前5%成交算数涨幅（%）
            round(up_down_ratio, 2),  # 股票上涨百分比（%）
            limit_up_count,  # 涨停板股票数量
            limit_down_count,  # 跌停板股票数量
            int(avg_market_value),  # 前N大成交额股票平均市值（亿）
            top_stocks,  # 前N大成交额股票活跃度
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


def streamlit_market_heat():
    data = get_market_heat()

    # 成交额指标
    st.header("成交额")
    for item, value in zip(data["指标"][0:10], data["数值"][0:10]):
        st.write(f"{item}: {value}")

    # 情绪指标
    st.header("情绪指标")
    for item, value in zip(data["指标"][10:17], data["数值"][10:17]):
        st.write(f"{item}: {value}")

    # 清除缓存按钮
    if st.button("清除缓存"):
        st.cache_data.clear()
        ak.clear_cache()
        st.success("缓存已清除")


def streamlit_spread_chart():
    st.markdown("### 📈 指数40日收益差分析")

    # 创建图表并获取当前收益差
    fig, hs300_zz1000_spread, zz1000_dividend_spread = create_spread_chart()

    # 显示图表
    st.plotly_chart(fig, use_container_width=True)

    # 显示当前收益差
    col1, col2 = st.columns(2)
    with col1:
        st.write(f"沪深300-中证1000收益差: {hs300_zz1000_spread:.2f}%")
    with col2:
        st.write(f"中证1000-红利指数收益差: {zz1000_dividend_spread:.2f}%")


def streamlit_app():
    current_time = datetime.now()
    is_trading = during_market_time(current_time)

    # 只在交易时间启用自动刷新
    if is_trading:
        st_autorefresh(interval=60000, key="data_refresh")

    # 创建主容器
    with st.empty():
        # 创建三个标签页
        tab1, tab2, tab3 = st.tabs(["💹 成交量与情绪", "🏢 龙头股分析", "📊 指数对比"])

    with tab1:
        # 第一个tab显示成交额和情绪指标
        col1, col2 = st.columns([3, 1])
        with col1:
            st.markdown("### 🎯 市场成交与情绪分析")

        try:
            data = get_market_heat()
        except Exception:
            st.error("开盘期间，无法获取数据，请稍后刷新。")
            return

        # 使用多列布局显示主要指标
        metrics_col1, metrics_col2, metrics_col3, metrics_col4 = st.columns(4)

        with metrics_col1:
            avg_amount = data["数值"][9]  # 5日均值（亿）
            pred_amount = data["数值"][8]  # 预估成交额（亿）

            # 预估成交额指标
            if pred_amount is not None:
                delta_vs_avg = pred_amount - avg_amount
                delta_color = "normal" if delta_vs_avg > 0 else "inverse"
                st.metric(
                    "预估成交额",
                    f"{pred_amount:,}亿",
                    delta=f"{delta_vs_avg:+,}亿 vs 5日均值",
                    delta_color=delta_color,
                )

        with metrics_col2:
            up_ratio = data["数值"][14]  # 上涨占比（%）
            st.metric("上涨占比", f"{up_ratio:.1f}%")

        with metrics_col3:
            limit_up = data["数值"][15]  # 涨停数量
            limit_down = data["数值"][16]  # 跌停数量
            st.metric(
                "涨停数量",
                str(limit_up),
                delta=f"-跌停 {limit_down}",
                delta_color="inverse",
            )

        with metrics_col4:
            middle_change = data["数值"][11]  # 中位数涨幅（%）
            st.metric(
                "中位数涨幅",
                f"{middle_change:.2f}%",
                delta=None,
                delta_color="inverse" if middle_change > 0 else "normal",
            )

        # 分两列显示详细数据
        col1, col2 = st.columns(2)

        with col1:

            def get_progress_html(value_pct):
                """根据百分比返回进度条HTML"""

                def get_color(pct):
                    if pct <= 20:
                        return "#90d4a2"  # 绿色
                    elif pct <= 40:
                        return "#27ae60"  # 深绿色
                    elif pct <= 60:
                        return "#f1c40f"  # 黄色
                    elif pct <= 80:
                        return "#e67e22"  # 橙色
                    else:
                        return "#e74c3c"  # 红色

                color = get_color(value_pct)
                width = value_pct  # percentage已经是百分比值，不需要再乘100
                return f"""
                    <div style="
                        width: 100%;
                        background-color: #eee;
                        border-radius: 3px;
                        padding: 3px;
                        box-sizing: border-box;
                    ">
                        <div style="
                            width: {width}%;
                            height: 20px;
                            background-color: {color};
                            border-radius: 2px;
                            transition: width 0.3s ease;
                        "></div>
                    </div>
                """

            st.markdown(
                """
            <style>
            .index-progress {
                margin-bottom: 1rem;
            }
            .index-progress .label {
                margin-bottom: 0.5rem;
                font-weight: 500;
                color: #333;
            }
            .index-progress .value {
                font-size: 0.9rem;
                color: #666;
                margin-top: 0.3rem;
                text-align: right;
            }
            </style>
            """,
                unsafe_allow_html=True,
            )

            st.markdown("#### 💰 指数成交占比")

            # 总成交额（亿）
            total = data["数值"][3]

            # 定义指数数据
            indices = [
                ("上证指数", data["数值"][0]),
                ("深证指数", data["数值"][1]),
                ("创业板", data["数值"][2]),
                ("中证1000", data["数值"][5] * total / 100),  # 转换百分比为实际值
                ("中证2000", data["数值"][6] * total / 100),  # 转换百分比为实际值
                ("沪深300", data["数值"][7] * total / 100),  # 转换百分比为实际值
            ]

            # 显示各指数进度条
            for name, amount in indices:
                st.markdown('<div class="index-progress">', unsafe_allow_html=True)
                cols = st.columns([2, 8])
                with cols[0]:
                    st.markdown(
                        f'<div class="label">{name}</div>', unsafe_allow_html=True
                    )
                with cols[1]:
                    percentage = (amount / total) * 100  # 转换为百分比
                    progress_html = get_progress_html(percentage)
                    st.markdown(progress_html, unsafe_allow_html=True)
                    st.markdown(
                        f'<div class="value">{percentage:.1f}%</div>',
                        unsafe_allow_html=True,
                    )
                st.markdown("</div>", unsafe_allow_html=True)

            # 显示总成交额和5日均值
            cols = st.columns(2)
            with cols[0]:
                st.info(f"**总成交额**: {data['数值'][3]} 亿")
            with cols[1]:
                st.info(f"**5日均值**: {data['数值'][9]} 亿")

        with col2:
            st.markdown("#### 💡 情绪指标")
            for item, value in zip(data["指标"][10:17], data["数值"][10:17]):
                # 处理带颜色标记的值
                output = f"**{item}**: {value}"
                # More Pythonic way to check for specific substrings
                if any(keyword in item for keyword in ["百分比", "涨幅"]):
                    output = output + "%"
                if value > 0:
                    st.success(output)
                else:
                    st.error(output)

    with tab2:
        # 第二个tab显示龙头股分析
        st.markdown("### 🔥 龙头股活跃度分析")
        data = get_market_heat()

        # 显示平均市值
        st.info(f"#### 📊 {data['指标'][17]}\n{data['数值'][17]}")

        if data["数值"][18] is not None:
            # 增加过滤和排序选项
            col1, col2 = st.columns([2, 2])
            with col1:
                sort_by = st.selectbox(
                    "排序依据", ["成交额", "涨跌幅", "换手率", "总市值"], index=0
                )

            # 获取原始DataFrame
            df = data["数值"][18]

            # 根据选择的列进行排序
            if sort_by == "涨跌幅":
                df = df.sort_values(
                    by="涨跌幅",
                    ascending=False,
                    key=lambda x: x.str.rstrip("%").astype(float),
                )
            elif sort_by in ["成交额", "总市值"]:
                df = df.sort_values(
                    by=sort_by,
                    ascending=False,
                    key=lambda x: x.str.rstrip("亿").astype(float),
                )
            elif sort_by == "换手率":
                df = df.sort_values(
                    by="换手率",
                    ascending=False,
                    key=lambda x: x.str.rstrip("%").astype(float),
                )

            # 美化数据表格显示
            styled_df = (
                df.style.map(color_negative_red, subset=["涨跌幅"])
                .set_properties(
                    **{
                        "background-color": "#f0f2f6",
                        "color": "#1f2937",
                        "font-size": "14px",
                    }
                )
                .set_table_styles(
                    [
                        {
                            "selector": "th",
                            "props": [
                                ("background-color", "#dfe3e8"),
                                ("color", "#374151"),
                            ],
                        }
                    ]
                )
            )

            st.dataframe(
                styled_df, use_container_width=True, height=400, hide_index=False
            )

    with tab3:
        # 第三个tab显示指数收益差分析
        streamlit_spread_chart()

    # 数据更新时间和状态显示
    current_time = datetime.now()
    updated_at = current_time.astimezone(pytz.timezone("Asia/Shanghai")).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    is_trading = during_market_time(current_time)
    status = (
        "（非交易时间 - 已暂停刷新）"
        if not is_trading
        else f"（交易中 - {60}秒自动刷新）"
    )

    # 使用 st.markdown 添加带样式的更新时间信息和刷新状态
    status_color = "#1f77b4" if is_trading else "#666"
    st.markdown(
        f"""
        <div style='
            padding: 10px;
            background-color: #f0f2f6;
            border-radius: 5px;
            font-size: 14px;
            color: {status_color};
            text-align: center;
            display: flex;
            justify-content: space-between;
            align-items: center;
        '>
            <span>⏰ 数据更新时间: {updated_at}</span>
            <span style='
                color: {status_color};
                font-weight: 500;
            '>{status}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    st.set_page_config(
        "成交量预测", "📈", layout="wide", initial_sidebar_state="expanded"
    )
    streamlit_app()

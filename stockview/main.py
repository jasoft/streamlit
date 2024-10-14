import streamlit as st
from datetime import date, datetime
import pandas as pd
from log import logger

import pytz
from streamlit_autorefresh import st_autorefresh

from akcache import akshare as ak
from stockview.options import analyze_atm_options
from stockview.helpers import during_market_time, minutes_since_market_open


@st.cache_data(ttl=60)
def is_trade_date(date):
    """
    判断是否是交易日。

    参数:
    date (datetime.date): 要检查的日期。

    返回:
    bool: 如果是交易日，则返回 True；否则返回 False。
    """
    logger.info(f"开始检查日期：{date}")
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
def get_vol_curve(ndays):
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
        df2 = df.iloc[:, [0, 5, 11]]
        df2.columns = ["day", "volume_sh", "volume_sz"]
        df2["volume_sh"] = pd.to_numeric(df2["volume_sh"])
        df2["volume_sz"] = pd.to_numeric(df2["volume_sz"])
        df2["date"] = pd.to_datetime(
            df2["day"], infer_datetime_format=True
        )  # format='%Y-%m-%d %H:%M:%S'
        df2["totalvolume"] = df2.apply(
            lambda x: (x["volume_sh"] + x["volume_sz"]), axis=1
        )
        df2.drop(["day"], axis=1, inplace=True)
        df2 = df2[
            df2["date"] < datetime.combine(date.today(), datetime.min.time())
        ]  # 获取本日之前15分钟数据，当日不要。
        df2 = df2.tail(ndays * 16)  # 取n天的数据，一天16个数据。
        logger.info(f"数据处理完成，共{len(df2)}条记录")

        df_all = pd.DataFrame()
        for i in range(ndays):
            df_range = df2.iloc[i * 16 : (i + 1) * 16]
            day_amount = df_range.totalvolume.sum()
            df_range["pct"] = df_range.apply(
                lambda x: (x["totalvolume"] / day_amount), axis=1
            )
            df_range.loc[:, "pct"] = df_range.apply(
                lambda x: (x["totalvolume"] / day_amount), axis=1
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
def get_estimate_vol(minutes, vol=None):
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
    curve = get_vol_curve(3)
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
        total_vol = get_stock_volume()
        vol = total_vol[0] + total_vol[1]
        logger.info(f"自动获取成交量：{vol}")
    try:
        estimated_vol = int(vol / a.iloc[0]) if vol > 0 else 0
        logger.info(f"估算的成交量：{estimated_vol}")
        return estimated_vol
    except ZeroDivisionError:
        logger.error("估算成交量时发生除零错误")
        return 0


@st.cache_data(ttl=180)
def get_index_value(symbol):
    try:
        index_data = ak.stock_zh_index_spot_em()
        index_value = index_data[index_data["代码"] == symbol]["最新价"].values[0]
        return index_value
    except Exception as e:
        logger.error(f"获取指数 {symbol} 当前价格时发生错误：{str(e)}")
        return None


# 获取当前成交额
@st.cache_data(ttl=180)
def get_stock_volume() -> tuple[float, float]:
    """
    获取上证和深证指数的成交量。

    该函数使用 akshare 库获取当前上证指数和深证指数的成交量。
    如果当前时间不在交易时间内，则将全局的 TTL 变量设置为 3600 秒。

    返回:
        tuple: 包含上证和深证指数成交量的元组，格式为 (sh_vol, sz_vol)。

    异常:
        KeyError: 如果在获取的数据中未找到预期的股票代码（上证为 "000001"，深证为 "399001"）。
    """

    try:
        logger.info("开始获取指数数据")
        spot_df_sh = ak.stock_zh_index_spot_em(symbol="上证系列指数")
        spot_df_sz = ak.stock_zh_index_spot_em(symbol="深证系列指数")
    except Exception as e:
        logger.error(f"获取指数数据时发生错误：{str(e)}")
        return 0, 0

    sh_vol = spot_df_sh[spot_df_sh["代码"] == "000001"]["成交额"].values[
        0
    ]  # 上证成交额
    sz_vol = spot_df_sz[spot_df_sz["代码"] == "399001"]["成交额"].values[
        0
    ]  # 深证成交额
    logger.info(f"获取上证和深证指数的成交量: 上证 {sh_vol}, 深证 {sz_vol}")
    if pd.isna(sh_vol) or pd.isna(sz_vol):
        logger.error("获取的成交量数据包含 NaN 值")
        return 0, 0

    return sh_vol, sz_vol


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
def top5_stock_vol_percent():
    """
    计算前5%的股票对总成交量的贡献百分比。

    该函数获取A股的实时交易数据，将股票按成交量降序排序，并计算总成交量。
    然后确定构成前5%成交量的股票数量，并计算这些股票的总成交量。
    最后，计算前5%的股票对总成交量的贡献百分比。

    返回:
        float: 前5%的股票对总成交量的贡献百分比。如果总成交量为零，则返回0。
    """

    # 获取 A 股实时行情数据
    df = ak.stock_zh_a_spot_em()
    # 按成交量降序排序
    df_sorted = df.sort_values("成交量", ascending=False)

    # 计算前5%的股票数量
    num_stocks = len(df)
    top_5_percent = int(num_stocks * 0.05)

    # 计算总成交量
    total_volume = df["成交量"].sum()

    if total_volume == 0:
        logger.info("总成交量为0, 可能是盘前，无法计算拥挤度")
        return 0

    # 计算前5%股票的成交量总和
    top_5_percent_volume = df_sorted["成交量"].head(top_5_percent).sum()

    # 计算拥挤度
    crowdedness = top_5_percent_volume / total_volume
    logger.info(f"前5%成交量的股票占总成交量的 {crowdedness*100:.2f}%")

    return crowdedness


# 简单的预测模型
def predict_volume(current_volume, current_time):
    if not during_market_time(current_time):
        return current_volume  # 如果当前时间超过15:00，返回当前成交额作为预测值

    elapsed_minutes = minutes_since_market_open(current_time)

    # 使用 get_estimate_vol 获取预测的成交额
    predicted_volume = get_estimate_vol(elapsed_minutes, current_volume)

    return predicted_volume


def steamlit_options():
    st.title("300ETF期权分析")

    # 300ETF期权分析
    option_value_analysis_em_df = ak.option_value_analysis_em()
    filtered_df = option_value_analysis_em_df[
        option_value_analysis_em_df["期权名称"].str.contains("300ETF.*10月", regex=True)
    ]
    atm_options, avg_volatility, underlying_price = analyze_atm_options(filtered_df)

    st.write(f"标的ETF价格: {underlying_price:.3f}")
    st.write(f"平均ATM隐含波动率: :red[{avg_volatility:.2f}%]")
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


def streamlit_market_heat():
    logger.info("程序启动")
    # Streamlit 页面设置
    st.title("A股观测")

    # 获取当前成交额
    logger.info("开始获取当前成交额")
    sh_vol, sz_vol = get_stock_volume()

    # 当前时间
    current_time = datetime.now()

    # 显示当前的成交额
    st.write(
        f"**上证成交额:** :red[{sh_vol/1e8:.0f}] 亿  **深证成交额:** :red[{sz_vol/1e8:.0f}] 亿"
    )

    # 预测成交额
    sh_pred = predict_volume(sh_vol, current_time)
    sz_pred = predict_volume(sz_vol, current_time)

    # 显示预测的成交额
    # if is_trade_date(date.today()):
    #     st.markdown(
    #         f"**预计上证总成交额:** :red[{sh_pred/1e8:.0f}] 亿 **预计深证总成交额:** :red[{sz_pred/1e8:.0f}] 亿"
    #     )
    # 插入分割线
    st.markdown("---")
    # 计算总成交额和预测总成交额
    total_amount = sh_vol + sz_vol
    total_pred = sh_pred + sz_pred

    # 显示总成交额和预测总成交额
    st.write(f"### 当前总成交额: :red[{total_amount/1e8:.0f}] 亿 ###")
    if is_trade_date(datetime.now(pytz.timezone("Asia/Shanghai")).date()):
        st.write(f"### 预计今日总成交额: :red[{total_pred/1e8:.0f}] 亿 ###")

    # 数据更新时间
    # 数据更新时间
    updated_at = current_time.astimezone(pytz.timezone("Asia/Shanghai")).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    status = "（非交易时间）" if not during_market_time(current_time) else ""
    crowdedness = top5_stock_vol_percent() * 100

    # 拥挤度，算法参见https://legulegu.com/stockdata/ashares-congestion
    if crowdedness > 50:
        st.write(f"### 交易拥挤度：:red[{crowdedness:.2f}] ###")
    else:
        st.write(f"### 交易拥挤度：:green[{crowdedness:.2f}] ###")

    # 中间股票涨幅
    middle_price_change_value = middle_price_change()
    st.write(f"### 中位数股票涨幅：:red[{middle_price_change_value:.2f}%] ###")

    st.caption(f"数据更新于: {updated_at} {status}")

    if st.button("清除缓存"):
        st.cache_data.clear()
        st.success("缓存已清除")


def streamlit():
    st.set_page_config("成交量预测", layout="wide")
    # Run the autorefresh about every 2000 milliseconds (2 seconds) and stop
    # after it's been refreshed 100 times.
    st_autorefresh(interval=60000, key="fizzbuzzcounter")
    col1, col2 = st.columns([5, 5])
    with col1:

        streamlit_market_heat()

    with col2:

        steamlit_options()


if __name__ == "__main__":
    streamlit()

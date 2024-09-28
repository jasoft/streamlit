import streamlit as st
import akshare as ak
from datetime import date, datetime
import pandas as pd
import coloredlogs
import logging

from streamlit_autorefresh import st_autorefresh

st.set_page_config("成交量预测")
# Run the autorefresh about every 2000 milliseconds (2 seconds) and stop
# after it's been refreshed 100 times.
st_autorefresh(interval=60000, key="fizzbuzzcounter")
dynamic_ttl = 180

coloredlogs.install(level="INFO")
logger = logging.getLogger("streamlit.stockview")

# 配置日志记录
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# 添加一个文件处理器，将日志写入文件
file_handler = logging.FileHandler("stockview.log")
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
)
logger.addHandler(file_handler)

logger.info("程序启动")


# 只需要每天执行一次，获取成交量分时比例
@st.cache_data(ttl=42000)
def get_vol_curve(ndays):
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
        )  # format='%Y-%m-%d %H:%M:%S')
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


def get_estimate_vol(minutes, vol=None):
    """
    获取估算的成交量
    <b>minutes</b>当日已经交易时间
    <b>vol</b>None是自动获取，否则指定
    """
    logger.info(f"开始估算成交量，已交易分钟数：{minutes}，指定成交量：{vol}")
    curve = get_vol_curve(3)
    df = pd.DataFrame(curve, columns=["amount"])
    t = minutes // 15
    remaining_minutes = minutes % 15
    try:
        a = df[0:t]["amount"].sum() + df.loc[t] * remaining_minutes / 15
        logger.debug(f"计算得到的累计比例：{a}")
    except KeyError:
        a = df[0:t]["amount"].sum()
        logger.warning(f"计算累计比例时发生KeyError，使用备选方案：{a}")

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


def during_market_time(current_time):
    market_close_time = datetime.combine(
        current_time.date(), datetime.strptime("15:00", "%H:%M").time()
    )
    market_open_time = datetime.combine(
        current_time.date(), datetime.strptime("09:30", "%H:%M").time()
    )
    return market_open_time <= current_time < market_close_time


# 获取当前成交额
@st.cache_data(ttl=dynamic_ttl)
def get_stock_volume():
    global dynamic_ttl
    if not during_market_time(datetime.now()):
        dynamic_ttl = 3600

    spot_df_sh = ak.stock_zh_index_spot_em(symbol="上证系列指数")
    spot_df_sz = ak.stock_zh_index_spot_em(symbol="深证系列指数")

    sh_vol = spot_df_sh[spot_df_sh["代码"] == "000001"]["成交额"].values[
        0
    ]  # 上证成交额
    sz_vol = spot_df_sz[spot_df_sz["代码"] == "399001"]["成交额"].values[
        0
    ]  # 深证成交额

    return sh_vol, sz_vol


# 简单的预测模型
def predict_volume(current_volume, current_time):
    if not during_market_time(current_time):
        return current_volume  # 如果当前时间超过15:00，返回当前成交额作为预测值
    market_open_time = datetime.combine(
        current_time.date(), datetime.strptime("09:30", "%H:%M").time()
    )
    elapsed_minutes = (current_time - market_open_time).seconds // 60

    # 使用 get_estimate_vol 获取预测的成交额
    predicted_volume = get_estimate_vol(elapsed_minutes, current_volume)

    return predicted_volume


# Streamlit 页面设置
st.title("A股实时成交额监控及预测")

# 获取当前成交额
sh_vol, sz_vol = get_stock_volume()

# 当前时间
current_time = datetime.now()

# 显示当前的成交额
st.write(
    f"**上证成交额:** :red[{sh_vol/1e8:.2f}] 亿  **深证成交额:** :red[{sz_vol/1e8:.2f}] 亿",
    unsafe_allow_html=True,
)

# 预测成交额
sh_pred = predict_volume(sh_vol, current_time)
sz_pred = predict_volume(sz_vol, current_time)

# 显示预测的成交额
st.markdown(
    f"**预计上证总成交额:** :red[{sh_pred/1e8:.2f}] 亿 **预计深证总成交额:** :red[{sz_pred/1e8:.2f}] 亿",
    unsafe_allow_html=True,
)

# 计算总成交额和预测总成交额
total_amount = sh_vol + sz_vol
total_pred = sh_pred + sz_pred

# 显示总成交额和预测总成交额
st.write(f"### 当前总成交额: :red[{total_amount/1e8:.2f}] 亿 ###")
st.write(f"### 预计今日总成交额: :red[{total_pred/1e8:.2f}] 亿 ###")

# 数据更新时间
st.write(f"数据更新于: {current_time.strftime('%H:%M:%S')}")

import re
import pandas as pd
import akshare
from akcache import CacheWrapper
from datetime import datetime

ak = CacheWrapper(akshare, cache_time=180)


def find_primary_options(etf):
    # Sample DataFrame
    df = ak.option_value_analysis_em()
    # Convert '到期日' to datetime
    df["到期日"] = pd.to_datetime(df["到期日"])

    # Get today's date
    today = pd.to_datetime(datetime.today().strftime("%Y-%m-%d"))

    # Calculate the difference between '到期日' and today
    df["diff"] = (df["到期日"] - today).abs()

    # Filter the row with the minimum difference
    # Sort the DataFrame by the 'diff' column
    df_sorted = df.sort_values(by="diff")

    # Filter out all rows which are the closest
    primary_contract = df_sorted[
        (df_sorted["diff"] == df_sorted["diff"].min())
        & (df_sorted["期权名称"].str.contains(etf))
    ]

    return primary_contract


def analyze_atm_options(data, price_range_percent=0.05):
    # Convert the data string to a DataFrame
    df = data.copy()

    # Extract the underlying price from the data
    underlying_price = df["标的最新价"].iloc[0]

    # Extract strike price from option name using regex
    df["行权价"] = df["期权名称"].apply(
        lambda x: float(re.search(r"(\d+)$", x).group(1)) / 1000
    )

    # Calculate price range
    lower_bound = underlying_price * (1 - price_range_percent)
    upper_bound = underlying_price * (1 + price_range_percent)

    # Filter options within the ATM range
    atm_options = df[
        (df["行权价"] >= lower_bound) & (df["行权价"] <= upper_bound)
    ].copy()

    # Calculate average implied volatility, excluding invalid values
    valid_volatility = atm_options["隐含波动率"][
        (atm_options["隐含波动率"] > 0) & (atm_options["隐含波动率"] < 200)
    ]
    avg_volatility = valid_volatility.mean()

    return atm_options, avg_volatility, underlying_price


# Use the function

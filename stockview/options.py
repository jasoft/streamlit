import re


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

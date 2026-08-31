"""共用回测引擎: 输入任意 Strategy 的 target_position 序列, 输出绩效与画图数据.

口径: 收盘出信号 -> 次日开盘成交 (无未来函数); 佣金万1; ETF 整手 100 份.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

COMMISSION = 0.0001  # ETF 佣金万1, 无印花税
LOT = 100


def backtest(df: pd.DataFrame, target: pd.Series, cash: float = 100_000.0) -> dict:
    """df: 单标的日K; target: 0/1 目标仓位序列 (按收盘决策).

    返回 stats / 逐年收益% / trades / equity / df / markers.
    """
    df = df.sort_values("date").reset_index(drop=True).copy()
    target = pd.Series(target).fillna(0).astype(int).reset_index(drop=True)

    cash_left, shares = cash, 0
    pending = None  # 次日开盘执行的指令 ("buy"/"sell")
    equity_curve, trades, markers = [], [], []
    entry_price = 0.0
    entry_date = ""

    for i, row in df.iterrows():
        # 1) 先执行昨日收盘产生的信号 (次日开盘成交, 无未来函数)
        if pending and i > 0:
            px = row["open"]
            if pending == "buy" and shares == 0:
                n = int(cash_left * (1 - COMMISSION) // px // LOT * LOT)
                if n > 0:
                    shares = n
                    cash_left -= n * px * (1 + COMMISSION)
                    entry_price = px
                    entry_date = str(row["date"])
                    markers.append({"date": pd.to_datetime(row["date"]),
                                    "price": px, "action": "买入"})
            elif pending == "sell" and shares > 0:
                cash_left += shares * px * (1 - COMMISSION)
                markers.append({"date": pd.to_datetime(row["date"]),
                                "price": px, "action": "卖出"})
                trades.append({
                    "entry_date": entry_date, "exit_date": str(row["date"]),
                    "entry": round(entry_price, 3), "exit": round(px, 3),
                    "ret_pct": round((px / entry_price - 1) * 100, 2),
                })
                shares = 0
            pending = None

        # 2) 收盘后更新次日指令
        if target.iloc[i] == 1 and shares == 0:
            pending = "buy"
        elif target.iloc[i] == 0 and shares > 0:
            pending = "sell"

        equity_curve.append(cash_left + shares * row["close"])

    equity = pd.Series(equity_curve, index=pd.to_datetime(df["date"]))
    total_ret = equity.iloc[-1] / cash - 1
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    ann_ret = (1 + total_ret) ** (1 / years) - 1 if years > 0 else 0.0
    roll_max = equity.cummax()
    max_dd = (equity / roll_max - 1).min()
    rets = equity.pct_change().dropna()
    # 日内数据 (5m等) 的年化因子按每日bar数折算
    if (pd.to_datetime(df["date"]).dt.time != dt.time(0)).any():
        bars_per_day = max(len(df) / max(pd.to_datetime(df["date"]).dt.date.nunique(), 1), 1)
        ann_factor = 252 * bars_per_day
    else:
        ann_factor = 252
    sharpe = rets.mean() / rets.std() * (ann_factor ** 0.5) if rets.std() > 0 else 0.0

    rets_pct = [t["ret_pct"] for t in trades]
    wins = [r for r in rets_pct if r > 0]
    bh = df["close"].iloc[-1] / df["close"].iloc[0] - 1

    yearly = equity.groupby(equity.index.year).last().pct_change()
    yearly.iloc[0] = equity.groupby(equity.index.year).last().iloc[0] / cash - 1

    return {
        "stats": {
            "数据区间": f"{equity.index[0].date()} ~ {equity.index[-1].date()}",
            "总收益率%": round(total_ret * 100, 2),
            "年化收益率%": round(ann_ret * 100, 2),
            "最大回撤%": round(max_dd * 100, 2),
            "Sharpe": round(sharpe, 2),
            "交易次数": len(trades),
            "胜率%": round(len(wins) / len(trades) * 100, 1) if trades else None,
            "买入持有总收益%": round(bh * 100, 2),
        },
        "逐年收益%": {str(y): round(v * 100, 2) for y, v in yearly.items()},
        "trades": trades,
        "equity": equity,
        "df": df.assign(date=pd.to_datetime(df["date"])),
        "markers": markers,
    }


def today_target(df: pd.DataFrame, target: pd.Series) -> dict:
    """最新一日的目标仓位与价格, 实盘层据此下单."""
    df = df.reset_index(drop=True)
    i = len(df) - 1
    return {
        "date": str(df.loc[i, "date"]),
        "close": float(df.loc[i, "close"]),
        "target": int(pd.Series(target).fillna(0).astype(int).iloc[i]),
    }

"""strategy/ 系统测试: 回测引擎精确值 / 策略发现 / 实盘计划与份数计算."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from strategy import config as config_mod
from strategy import registry, trader
from strategy.engine import backtest
from strategy.runner import in_session, next_session_start
from strategy.strategies.ma20_trend import Ma20Trend
from strategy.strategies.sma_cross import SmaCross


def make_df(closes, opens=None):
    n = len(closes)
    opens = opens or closes
    return pd.DataFrame({
        "date": pd.date_range("2025-01-01", periods=n, freq="B"),
        "open": [float(o) for o in opens],
        "high": [max(o, c) for o, c in zip(opens, closes)],
        "low": [min(o, c) for o, c in zip(opens, closes)],
        "close": [float(c) for c in closes],
        "volume": [1000.0] * n,
    })


class TestEngine(unittest.TestCase):
    def test_exact_trade_and_cash(self):
        # 5 天: 收盘出信号, 次日开盘成交
        df = make_df(closes=[9, 9, 11, 11, 11], opens=[10, 10, 10, 11, 11])
        target = pd.Series([0, 1, 0, 0, 0])  # day1 收盘想买 -> day2 开盘买; day2 收盘想卖 -> day3 开盘卖

        # 手工计算: day2 开盘 10 元买入, 现金 10w
        #   可买 = int(100000*0.9999 // 10 // 100 * 100) = 9900 股
        #   现金 = 100000 - 9900*10*1.0001 = 10000.1...? -> 100000 - 99009.9 = 990.1
        # day3 开盘 10 元卖出 -> 现金 += 9900*10*0.9999 = 98990.1 -> 99980.2
        r = backtest(df, target, cash=100_000.0)
        self.assertEqual(len(r["trades"]), 1)
        self.assertEqual(len(r["markers"]), 2)
        t = r["trades"][0]
        self.assertEqual(t["entry"], 10.0)
        self.assertEqual(t["exit"], 11.0)   # day4 开盘 11 元卖出
        self.assertAlmostEqual(t["ret_pct"], 10.0, places=6)
        # 期末权益 = 现金
        self.assertAlmostEqual(r["equity"].iloc[-1],
                               (100_000 - 9900 * 10 * 1.0001) + 9900 * 11 * 0.9999,
                               places=2)

    def test_buy_hold_scenario(self):
        df = make_df(closes=[10, 10, 10, 12], opens=[10, 10, 10, 12])
        target = pd.Series([1, 1, 1, 1])  # 一直持有
        r = backtest(df, target, cash=100_000.0)
        # day1 开盘 10 买入 9900 股, 期末价 12
        self.assertAlmostEqual(r["equity"].iloc[-1],
                               (100_000 - 9900 * 10 * 1.0001) + 9900 * 12, places=2)
        self.assertEqual(len(r["trades"]), 0)  # 尚未平仓
        self.assertEqual(r["markers"][0]["action"], "买入")

    def test_sell_only_no_crash(self):
        df = make_df(closes=[10] * 5, opens=[10] * 5)
        r = backtest(df, pd.Series([0] * 5), cash=100_000.0)
        self.assertEqual(r["stats"]["交易次数"], 0)
        self.assertAlmostEqual(r["equity"].iloc[-1], 100_000.0, places=6)


class TestRegistry(unittest.TestCase):
    def test_discover_finds_both(self):
        found = registry.discover()
        self.assertIn("ma20_trend", found)
        self.assertIn("sma_cross", found)

    def test_target_position_shape_and_values(self):
        df = make_df(closes=range(1, 41))
        for strat in (Ma20Trend(), SmaCross()):
            t = strat.target_position(df, strat.default_params())
            self.assertEqual(len(t), len(df))
            self.assertTrue(set(t.dropna().unique()).issubset({0, 1}))
            self.assertEqual(t.iloc[0], 0)  # 均线未形成前空仓

    def test_sma_cross_param_order_guard(self):
        df = make_df(closes=range(1, 41))
        strat = SmaCross()
        a = strat.target_position(df, {"fast": 10, "slow": 30})
        b = strat.target_position(df, {"fast": 30, "slow": 10})
        pd.testing.assert_series_equal(a, b)


class TestTrader(unittest.TestCase):
    def test_qty_for(self):
        self.assertEqual(trader.qty_for(10_000, 1.5), 6600)
        self.assertEqual(trader.qty_for(10_000, 15.0), 600)
        self.assertEqual(trader.qty_for(500, 15.0), 0)  # 不足一手

    def test_six_digit(self):
        self.assertEqual(trader.six_digit("sz159915"), "159915")
        self.assertEqual(trader.six_digit("sh510300"), "510300")

    def test_plan_orders(self):
        signals = [
            {"symbol": "sz159915", "date": "d", "close": 2.0, "target": 1},
            {"symbol": "sh510300", "date": "d", "close": 4.0, "target": 0},
            {"symbol": "sh588000", "date": "d", "close": 1.0, "target": 1},
        ]
        current = {"sh510300": {"target": 1, "qty": 2400}}
        orders = trader.plan_orders(signals, cash_per_symbol=10_000, current=current)
        actions = {(o["symbol"], o["action"]) for o in orders}
        self.assertEqual(actions, {("sz159915", "buy"), ("sh510300", "sell"),
                                   ("sh588000", "buy")})
        buy = next(o for o in orders if o["action"] == "buy" and o["symbol"] == "sz159915")
        self.assertEqual(buy["qty"], 5000)  # 10000 // 2.0 // 100 * 100
        sell = next(o for o in orders if o["action"] == "sell")
        self.assertEqual(sell["qty"], 2400)

    def test_plan_orders_no_change_no_order(self):
        signals = [{"symbol": "sz159915", "date": "d", "close": 2.0, "target": 1}]
        current = {"sz159915": {"target": 1, "qty": 5000}}
        self.assertEqual(trader.plan_orders(signals, 10_000, current), [])

    def test_config_defaults_fill(self):
        cfg = config_mod.load(registry.discover())
        for name in ("ma20_trend", "sma_cross"):
            scfg = cfg["strategies"][name]
            self.assertIn("enabled", scfg)
            self.assertIn("cash_per_symbol", scfg)
            self.assertIn("dry_run", scfg["live"])
            self.assertIn("execute_time", scfg["live"])


class TestLiveEval(unittest.TestCase):
    """runner 交易时段 + trader.evaluate / evals 日志."""

    def test_in_session(self):
        import datetime as dt
        mk = lambda h, m, wd=0: dt.datetime(2026, 8, 31, h, m)  # 周一
        self.assertTrue(in_session(mk(9, 30)))
        self.assertTrue(in_session(mk(10, 0)))
        self.assertTrue(in_session(mk(14, 55)))
        self.assertFalse(in_session(mk(9, 0)))    # 开盘前
        self.assertFalse(in_session(mk(12, 0)))   # 午休
        self.assertFalse(in_session(mk(15, 30)))  # 收盘后

    def test_next_session_start(self):
        import datetime as dt
        fri_night = dt.datetime(2026, 8, 28, 20, 0)   # 周五晚
        nxt = next_session_start(fri_night)
        self.assertEqual(nxt.weekday(), 0)            # 跳到周一
        self.assertEqual((nxt.hour, nxt.minute), (9, 25))
        early = dt.datetime(2026, 8, 31, 8, 0)        # 周一开盘前
        nxt2 = next_session_start(early)
        self.assertEqual(nxt2.date(), early.date())   # 当天 9:25
        self.assertEqual((nxt2.hour, nxt2.minute), (9, 25))

    def _cfg(self, tmp_state_target=0):
        return {
            "symbols": ["sz159915"], "params": {"window": 20},
            "cash_per_symbol": 10000,
            "live": {"dry_run": True, "execute_time": "14:55",
                     "qfq": False, "poll_seconds": 60},
        }

    def test_evaluate_alert(self):
        import datetime as dt
        from unittest.mock import patch
        closes = [10.0] * 18 + [11.0, 11.5]  # 20 日窗口, 最新价在 MA 上方
        df = make_df(closes)
        with patch.object(trader, "_fetch", return_value=df), \
             patch.object(trader, "load_state", return_value={}):
            out = trader.evaluate("ma20_trend", self._cfg())
        self.assertEqual(out[0]["symbol"], "sz159915")
        self.assertEqual(out[0]["target"], 1)
        self.assertTrue(out[0]["alert"])           # 应有仓位 0 -> 目标 1, 触发
        self.assertIn("买入", out[0]["msg"])

    def test_evaluate_no_alert_when_state_matches(self):
        from unittest.mock import patch
        closes = [10.0] * 18 + [9.0, 9.5]  # 最新价在 MA 下方 -> 目标 0
        df = make_df(closes)
        with patch.object(trader, "_fetch", return_value=df), \
             patch.object(trader, "load_state",
                          return_value={"sz159915": {"target": 0, "qty": 0}}):
            out = trader.evaluate("ma20_trend", self._cfg())
        self.assertEqual(out[0]["target"], 0)
        self.assertFalse(out[0]["alert"])
        self.assertEqual(out[0]["msg"], "未触发")

    def test_evals_roundtrip(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            with patch.object(trader, "STATE_DIR", Path(td)):
                trader.append_evals("t", [{"ts": "1", "symbol": "s", "alert": False}])
                trader.append_evals("t", [{"ts": "2", "symbol": "s", "alert": True}])
                rows = trader.read_evals("t", tail=1)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["alert"])


class TestIntradayT(unittest.TestCase):
    """日内做T: 量能方向判定 + 门控 + 强制了结 + 恐慌买入."""

    def _bars(self, closes, volumes=None, start="2026-08-21 09:35"):
        n = len(closes)
        vols = volumes or [100.0] * n
        return pd.DataFrame({
            "date": pd.date_range(start, periods=n, freq="5min"),
            "open": closes, "high": [c * 1.001 for c in closes],
            "low": [c * 0.999 for c in closes], "close": closes,
            "volume": vols,
        })

    def _strat(self):
        from strategy.strategies.intraday_t import IntradayT
        return IntradayT()

    def _amounts(self, day, val):
        import datetime as dt
        idx = pd.to_datetime([dt.date(2026, 8, 10) + dt.timedelta(days=i)
                              for i in range(12)]).date
        s = pd.Series(2e12, index=idx)
        s[pd.Timestamp(day).date()] = val
        return s

    def test_regime_mapping(self):
        import datetime as dt
        strat = self._strat()
        day = dt.date(2026, 8, 21)
        p = strat.default_params()
        amounts = self._amounts(day, 2.3e12)   # > 2万亿 且 > 1.05x 均值
        self.assertEqual(strat._regime(day, amounts, p), "buy_first")
        amounts2 = self._amounts(day, 1.5e12)  # < 2万亿
        self.assertEqual(strat._regime(day, amounts2, p), "sell_first")
        amounts3 = self._amounts(day, 1.99e12)  # 量能变化不大
        self.assertEqual(strat._regime(day, amounts3, p), "sell_first")

    def test_gate_before_10am(self):
        # 只有 9:35-9:55 的 bars: 全部处于门控前, 目标仓位保持 0
        df = self._bars([10.0] * 6)
        with patch.object(trader, "__name__", trader.__name__):
            pass
        strat = self._strat()
        import datetime as dt
        from strategy.strategies.intraday_t import _market_day_amounts
        with patch.object(strat, "_regime", return_value="buy_first"):
            t = strat.target_position(df, strat.default_params())
        self.assertTrue((t == 0).all())

    def test_panic_buy_then_vwap_sell(self):
        # day1 历史铺垫 (48根平盘) + day2: 阴跌放量 -> 买入, 回升 -> 卖出
        hist = self._bars([10.0] * 48, start="2026-08-20 09:35")
        closes = [10.0] * 12 + [9.6, 9.3, 9.1, 9.0, 8.95] + [9.4, 9.7, 9.9, 9.95, 9.98]
        vols = [100.0] * 12 + [150, 250, 500, 900, 1500] + [400, 350, 300, 280, 260]
        day2 = self._bars(closes, vols, start="2026-08-21 09:35")
        df = pd.concat([hist, day2]).reset_index(drop=True)

        strat = self._strat()
        import datetime as dt
        with patch("strategy.strategies.intraday_t._market_day_amounts") as ma:
            ma.return_value = self._amounts(dt.date(2026, 8, 21), 2.3e12)
            t = strat.target_position(df, strat.default_params()).iloc[48:].reset_index(drop=True)
        # 恐慌底部附近出现持仓 (>=1), 回升后归 0
        self.assertEqual(t.iloc[-1], 0)
        self.assertGreaterEqual(int((t == 1).sum()), 1)
        # 第一次买入应出现在下跌段 (idx < 25)
        first_buy = next(i for i, v in enumerate(t) if v == 1)
        self.assertLess(first_buy, 25)

    def test_force_cover_at_exit(self):
        # sell_first (缩量): 14:50 后强制回补为 1
        df = self._bars([10.0] * 48, start="2026-08-21 09:35")
        strat = self._strat()
        with patch("strategy.strategies.intraday_t._market_day_amounts") as ma:
            ma.return_value = self._amounts(pd.Timestamp("2026-08-21").date(), 1.5e12)
            t = strat.target_position(df, strat.default_params())
        # 14:50 之后的 bar (48 根连续 5m 从 9:35 起, 14:50 是第 63 根 — 用时间过滤)
        late = df["date"].dt.time >= dt_time(14, 50)
        self.assertTrue((t[late.values] == 1).all())


from datetime import time as dt_time

if __name__ == "__main__":
    unittest.main()

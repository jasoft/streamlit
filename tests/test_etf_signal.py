from __future__ import annotations

import datetime as dt
import unittest
from typing import List

import pandas as pd

from stockview.etf_signal import (
    FACTOR_WEIGHTS,
    RealtimeQuote,
    _parse_tencent_payload,
    composite_signal,
    compute_breadth_factor,
    compute_ma_alignment_score,
    compute_macd_score,
    compute_momentum_factor,
    compute_volume_factor,
    is_market_open,
    map_rating,
    merge_realtime_bar,
    news_sentiment_score,
    rsi_wilder,
)


def make_daily(closes: List[float], volumes: List[float] | None = None) -> pd.DataFrame:
    n = len(closes)
    if volumes is None:
        volumes = [1_000_000.0] * n
    base = dt.date(2026, 1, 1)
    dates = [base + dt.timedelta(days=i) for i in range(n)]
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": volumes,
            "amount": [v * c for v, c in zip(volumes, closes)],
        }
    )


def uptrend(n: int = 60) -> pd.DataFrame:
    return make_daily([10 + i * 0.1 for i in range(n)])


def downtrend(n: int = 60) -> pd.DataFrame:
    return make_daily([16 - i * 0.1 for i in range(n)])


class TestTencentParser(unittest.TestCase):
    def test_prefixed_symbol_is_used_as_key(self) -> None:
        payload = (
            'v_sz159915="51~创业板ETF易方达~159915~3.411~3.457~3.404~6514150~'
            '3008375~3477131~3.411~2367~3.410~13289~3.409~13468~3.408~5580~'
            '3.407~12277~3.413~32118~3.414~13783~3.415~3060~3.416~3932~3.417~'
            '4528~~20260825100033~-0.046~-1.33~3.425~3.376~'
            '3.411/6514150/2214471302~6514150~221447~3.56";'
        )
        quotes = _parse_tencent_payload(payload)
        self.assertIn("sz159915", quotes)
        q = quotes["sz159915"]
        self.assertEqual(q.name, "创业板ETF易方达")
        self.assertAlmostEqual(q.price, 3.411)
        self.assertAlmostEqual(q.change_pct, -1.33)
        self.assertAlmostEqual(q.high, 3.425)
        self.assertAlmostEqual(q.low, 3.376)
        self.assertEqual(q.time_text, "20260825100033")

    def test_invalid_lines_are_skipped(self) -> None:
        self.assertEqual(_parse_tencent_payload("bad line;"), {})


class TestMergeRealtimeBar(unittest.TestCase):
    def test_appends_new_row_for_new_day(self) -> None:
        hist = make_daily([10.0, 11.0])
        today = pd.Timestamp.now().floor("D")
        assert today > hist["date"].iloc[-1]
        q = RealtimeQuote("sz159915", "ETF", 12.0, 11.0, 11.5, 12.2, 11.4, 12345, 6789, 9.09)
        out, merged = merge_realtime_bar(hist, q)
        self.assertTrue(merged)
        self.assertEqual(len(out), len(hist) + 1)
        self.assertEqual(out["close"].iloc[-1], 12.0)

    def test_replaces_same_day_row(self) -> None:
        hist = make_daily([10.0, 11.0])
        hist.loc[hist.index[-1], "date"] = pd.Timestamp(pd.Timestamp.now().date())
        q = RealtimeQuote("sz159915", "ETF", 12.0, 11.0, 11.5, 12.2, 11.4, 12345, 6789, 9.09)
        out, merged = merge_realtime_bar(hist, q)
        self.assertTrue(merged)
        self.assertEqual(len(out), len(hist))
        self.assertEqual(out["close"].iloc[-1], 12.0)

    def test_no_quote_returns_copy(self) -> None:
        hist = make_daily([10.0, 11.0])
        out, merged = merge_realtime_bar(hist, None)
        self.assertFalse(merged)
        pd.testing.assert_frame_equal(out, hist)


class TestTrendFactors(unittest.TestCase):
    def test_uptrend_scores_positive(self) -> None:
        score, _desc = compute_ma_alignment_score(uptrend()["close"])
        self.assertGreater(score, 20)

    def test_downtrend_scores_negative(self) -> None:
        score, _desc = compute_ma_alignment_score(downtrend()["close"])
        self.assertLess(score, -20)

    def test_macd_expanding_red_column_positive(self) -> None:
        # 涨幅递增 -> 红柱放大
        close = pd.Series([10 + (i ** 2) * 0.01 for i in range(40)])
        score, desc = compute_macd_score(close)
        self.assertEqual(score, 30)
        self.assertIn("红", desc)

    def test_macd_expanding_green_column_negative(self) -> None:
        # 跌幅递增 -> 绿柱放大
        close = pd.Series([20 - (i ** 2) * 0.01 for i in range(40)])
        score, _ = compute_macd_score(close)
        self.assertEqual(score, -30)

    def test_rsi_all_up_near_100(self) -> None:
        val = rsi_wilder(uptrend()["close"]).iloc[-1]
        self.assertGreater(val, 90)


class TestMomentumFactor(unittest.TestCase):
    def test_strong_rally_positive(self) -> None:
        # 近5日大涨但 RSI 未到超买阈值前段 -> 动能分主导为正
        df = make_daily([10] * 40 + [10, 10.2, 10.4, 10.6, 10.8, 11.0])
        score, details = compute_momentum_factor(df)
        self.assertGreater(score, 10)
        self.assertTrue(any("近5日收益" in d for d in details))

    def test_sharp_drop_negative_with_oversold_bonus(self) -> None:
        df = make_daily([16] * 40 + [16, 15.6, 15.2, 14.8, 14.4, 14.0])
        score, _ = compute_momentum_factor(df)
        self.assertLess(score, -10)


class TestVolumeFactor(unittest.TestCase):
    def test_up_on_heavy_volume_positive(self) -> None:
        vols = [1_000_000] * 60 + [3_000_000]
        closes = [10] * 55 + [10.05, 10.1, 10.15, 10.2, 10.25, 10.5]
        score, _ = compute_volume_factor(make_daily(closes, vols))
        self.assertEqual(score, 40)

    def test_down_on_heavy_volume_negative(self) -> None:
        vols = [1_000_000] * 60 + [3_000_000]
        closes = [10] * 55 + [9.95, 9.9, 9.85, 9.8, 9.75, 9.5]
        score, _ = compute_volume_factor(make_daily(closes, vols))
        self.assertEqual(score, -40)

    def test_top_divergence_detected(self) -> None:
        # 价创新高但近5日均量 < 近20日均量*0.8 -> 基础25 - 背离20 = 5
        closes = [50 + c for c in range(1, 51)]
        vols = [2_000_000] * 35 + [500_000] * 15
        score, details = compute_volume_factor(make_daily(closes, vols))
        self.assertEqual(score, 5)
        self.assertTrue(any("背离" in d for d in details))


class TestBreadthFactor(unittest.TestCase):
    def _quotes(self, chgs: dict[str, float]) -> dict[str, RealtimeQuote]:
        result = {}
        for code, chg in chgs.items():
            result[code] = RealtimeQuote(code, code, 10, 10, 10, 10, 10, 100, 100, chg)
        return result

    def test_weighted_average_and_counts(self) -> None:
        cons = pd.DataFrame([
            {"code": "sz300001", "name": "A", "weight": 50},
            {"code": "sz300002", "name": "B", "weight": 50},
        ])
        quotes = self._quotes({"sz300001": 2.0, "sz300002": -2.0})
        score, details = compute_breadth_factor(cons, quotes)
        self.assertAlmostEqual(score, 0.0, delta=1e-6)
        self.assertTrue(any("上涨家数 1/2" in d for d in details))

    def test_catl_leader_bonus_applies_once(self) -> None:
        cons = pd.DataFrame([{"code": "sz300750", "name": "宁德", "weight": 100}])
        quotes = self._quotes({"sz300750": 4.0})
        score, details = compute_breadth_factor(cons, quotes)
        # wavg 4%->40, 家数比1/1->30, 宁王4*4=16 clip 10 => 80
        self.assertAlmostEqual(score, 80.0, delta=1e-6)
        self.assertTrue(any("宁德时代" in d for d in details))

    def test_all_up_positive(self) -> None:
        cons = pd.DataFrame([
            {"code": f"sz3007{i:02d}", "name": f"N{i}", "weight": 10} for i in range(10)
        ])
        quotes = self._quotes({f"sz3007{i:02d}": 3.0 for i in range(10)})
        score, _ = compute_breadth_factor(cons, quotes)
        self.assertGreaterEqual(score, 55)


class TestNewsSentiment(unittest.TestCase):
    def test_positive_news(self) -> None:
        score, tagged = news_sentiment_score(["公司盈利超预期，获政策支持"])
        self.assertGreater(score, 0)
        self.assertEqual(tagged[0][1], "✅利好")

    def test_negative_news(self) -> None:
        score, tagged = news_sentiment_score(["公司亏损下滑遭调查处罚"])
        self.assertLess(score, 0)
        self.assertEqual(tagged[0][1], "❌利空")

    def test_neutral_when_empty(self) -> None:
        score, _ = news_sentiment_score([])
        self.assertEqual(score, 0)


class TestCompositeAndRating(unittest.TestCase):
    def test_weights_sum_to_one(self) -> None:
        self.assertAlmostEqual(sum(FACTOR_WEIGHTS.values()), 1.0)

    def test_composite_total_in_range(self) -> None:
        cons = pd.DataFrame([{"code": "sz300750", "name": "宁德", "weight": 100}])
        quotes = {"sz300750": RealtimeQuote("sz300750", "宁德", 10, 10, 10, 10, 10, 100, 100, 5)}
        sig = composite_signal(uptrend(), cons, quotes, ["利好 增长 突破"])
        self.assertGreater(sig.total, 0)
        self.assertLessEqual(abs(sig.total), 100)

    def test_map_rating_boundaries(self) -> None:
        cases = {
            60: "强烈买入",
            55: "强烈买入",
            54.9: "买入",
            25: "买入",
            24.9: "偏多",
            10: "偏多",
            9.9: "中性观望",
            0: "中性观望",
            -10: "中性观望",
            -10.1: "偏空",
            -25: "偏空",
            -25.1: "卖出",
            -55: "卖出",
            -55.1: "强烈卖出",
            -100: "强烈卖出",
        }
        for total, expected in cases.items():
            label, _e, _c = map_rating(total)
            self.assertEqual(label, expected, msg=f"total={total}")


class TestMarketOpen(unittest.TestCase):
    def test_weekday_sessions(self) -> None:
        tue_1030 = dt.datetime(2026, 8, 25, 10, 30)
        tue_1400 = dt.datetime(2026, 8, 25, 14, 0)
        tue_lunch = dt.datetime(2026, 8, 25, 12, 0)
        tue_night = dt.datetime(2026, 8, 25, 20, 0)
        self.assertTrue(is_market_open(tue_1030))
        self.assertTrue(is_market_open(tue_1400))
        self.assertFalse(is_market_open(tue_lunch))
        self.assertFalse(is_market_open(tue_night))

    def test_weekend_closed(self) -> None:
        sat = dt.datetime(2026, 8, 22, 10, 30)
        sun = dt.datetime(2026, 8, 23, 14, 0)
        self.assertFalse(is_market_open(sat))
        self.assertFalse(is_market_open(sun))


if __name__ == "__main__":
    unittest.main()

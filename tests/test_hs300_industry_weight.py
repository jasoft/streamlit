import unittest

import pandas as pd

from data_analysis.hs300_industry_weight import (
    calculate_industry_weights,
    normalize_industry_dataframe,
    normalize_stock_code,
)


class Hs300IndustryWeightTest(unittest.TestCase):
    def test_normalize_stock_code(self):
        self.assertEqual(normalize_stock_code("600519.SH"), "600519")
        self.assertEqual(normalize_stock_code("sz000001"), "000001")
        self.assertIsNone(normalize_stock_code("not-a-code"))

    def test_calculate_industry_weights(self):
        weights = pd.DataFrame(
            [
                {"日期": "2026-04-30", "指数代码": "000300", "指数名称": "沪深300", "成分券代码": "600519", "成分券名称": "贵州茅台", "权重": 3.7},
                {"日期": "2026-04-30", "指数代码": "000300", "指数名称": "沪深300", "成分券代码": "000001", "成分券名称": "平安银行", "权重": 0.4},
                {"日期": "2026-04-30", "指数代码": "000300", "指数名称": "沪深300", "成分券代码": "600036", "成分券名称": "招商银行", "权重": 2.0},
            ]
        )
        payload = {
            "datas": [
                {"股票代码": "600519.SH", "股票简称": "贵州茅台", "所属同花顺一级行业": "食品饮料", "总市值[20260520]": 2_000_000_000_000},
                {"股票代码": "000001.SZ", "股票简称": "平安银行", "所属同花顺一级行业": "银行", "总市值[20260520]": 200_000_000_000},
                {"股票代码": "600036.SH", "股票简称": "招商银行", "所属同花顺一级行业": "银行", "总市值[20260520]": 1_000_000_000_000},
            ]
        }

        industry, industry_date = normalize_industry_dataframe(payload)
        result = calculate_industry_weights(weights, industry, industry_date=industry_date)

        self.assertEqual(result.metadata["weight_date"], "2026-04-30")
        self.assertEqual(result.metadata["industry_date"], "20260520")
        self.assertEqual(result.metadata["matched_count"], 3)
        by_name = result.summary.set_index("所属同花顺一级行业")
        self.assertAlmostEqual(by_name.loc["银行", "指数权重占比"], 2.4)
        self.assertAlmostEqual(by_name.loc["食品饮料", "指数权重占比"], 3.7)


if __name__ == "__main__":
    unittest.main()

"""自选股模块单元测试: symbol 归一化 / 增删 + tombstone / 持仓同步解析."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import watchlist  # noqa: E402


class TestNormalizeSymbol(unittest.TestCase):
    def test_bare_codes_get_prefix(self):
        cases = {
            "601899": "sh601899",      # 沪主板
            "688981": "sh688981",      # 科创板
            "000001": "sz000001",      # 深主板 (约定: 裸 000001 按股票处理)
            "300750": "sz300750",      # 创业板
            "510300": "sh510300",      # 沪 ETF
            "159915": "sz159915",      # 深 ETF
            "113050": "sh113050",      # 沪转债
            "123456": "sz123456",      # 深转债
            "830799": "bj830799",      # 北交所
        }
        for raw, want in cases.items():
            sym, code, market = watchlist.normalize_symbol(raw)
            self.assertEqual(sym, want, raw)
            self.assertEqual(code, raw, raw)
            self.assertEqual(market, want[:2], raw)

    def test_hk_codes(self):
        # 港股通: 5 位代码 (同花顺口径), 短代码补零, 支持 hk 前缀 / .HK 后缀
        cases = {
            "00700": ("hk00700", "00700"),
            "09992": ("hk09992", "09992"),
            "700": ("hk00700", "00700"),
            "700.hk": ("hk00700", "00700"),
            "hk700": ("hk00700", "00700"),
            "HK09992": ("hk09992", "09992"),
        }
        for raw, (want_sym, want_code) in cases.items():
            sym, code, market = watchlist.normalize_symbol(raw)
            self.assertEqual((sym, code), (want_sym, want_code), raw)
            self.assertEqual(market, "hk", raw)

    def test_prefixed_and_case(self):
        self.assertEqual(watchlist.normalize_symbol("SH601899"),
                         ("sh601899", "601899", "sh"))
        self.assertEqual(watchlist.normalize_symbol(" sz159915 "),
                         ("sz159915", "159915", "sz"))
        self.assertEqual(watchlist.normalize_symbol("bj830799"),
                         ("bj830799", "830799", "bj"))
        # 指数必须显式带前缀, 原样保留
        self.assertEqual(watchlist.normalize_symbol("sh000001"),
                         ("sh000001", "000001", "sh"))

    def test_invalid_and_non_a_share(self):
        with self.assertRaises(ValueError):
            watchlist.normalize_symbol("   ")
        # 期货等非 6 位代码原样接受, code/market 为空
        self.assertEqual(watchlist.normalize_symbol("rb2510"),
                         ("rb2510", "", ""))


class TestCrudAndTombstone(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        Path(self.tmp.name).unlink()  # 留路径不留文件
        self._patcher = patch.object(watchlist, "WATCHLIST_FILE",
                                     Path(self.tmp.name))
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_add_dedupe_and_name_fill(self):
        r1 = watchlist.add_stock("601899", name="紫金矿业")
        self.assertTrue(r1["added"])
        r2 = watchlist.add_stock("sh601899")  # 同股不同写法 -> 去重
        self.assertFalse(r2["added"])
        self.assertEqual(r2["name"], "紫金矿业")
        r3 = watchlist.add_stock("601899", name="新名字")  # 已有名称不覆盖
        self.assertEqual(r3["name"], "紫金矿业")
        self.assertEqual(len(watchlist.get_status()["stocks"]), 1)

    def test_remove_then_sync_no_readd(self):
        watchlist.add_stock("601899", name="紫金矿业")
        self.assertTrue(watchlist.remove_stock("601899"))
        self.assertFalse(watchlist.remove_stock("601899"))  # 二次删 -> 404 语义
        with patch.object(watchlist, "_run_ths_positions",
                          return_value=[{"code": "601899", "name": "紫金矿业"}]):
            r = watchlist.sync_positions()
        self.assertTrue(r["ok"])
        self.assertEqual(r["added"], [])       # tombstone 挡住, 不回加
        self.assertEqual(watchlist.get_status()["stocks"], [])

    def test_manual_readd_clears_tombstone(self):
        watchlist.add_stock("601899")
        watchlist.remove_stock("601899")
        watchlist.add_stock("601899", name="紫金矿业")
        with patch.object(watchlist, "_run_ths_positions",
                          return_value=[{"code": "601899", "name": "紫金矿业"}]):
            watchlist.sync_positions()
        stocks = watchlist.get_status()["stocks"]
        self.assertEqual(len(stocks), 1)
        self.assertEqual(stocks[0]["source"], "manual")  # 手动添加优先, 不改来源
        self.assertTrue(stocks[0]["last_seen_in_positions"])

    def test_sync_adds_new_positions(self):
        with patch.object(watchlist, "_run_ths_positions",
                          return_value=[{"code": "601899", "name": "紫金矿业"},
                                        {"code": "159915", "name": "创业板ETF"}]):
            r = watchlist.sync_positions()
        self.assertTrue(r["ok"])
        self.assertEqual(sorted(r["added"]), ["sh601899", "sz159915"])
        stocks = {s["code"]: s for s in watchlist.get_status()["stocks"]}
        self.assertEqual(stocks["601899"]["source"], "ths")
        self.assertEqual(stocks["159915"]["name"], "创业板ETF")

    def test_sync_failure_recorded(self):
        with patch.object(watchlist, "_run_ths_positions",
                          side_effect=RuntimeError("同花顺持仓查询失败: 找不到 持仓 tab")):
            r = watchlist.sync_positions()
        self.assertFalse(r["ok"])
        st = watchlist.get_status()
        self.assertIn("持仓 tab", st["last_sync_error"])
        self.assertEqual(st["stocks"], [])

    def test_auto_sync_toggle(self):
        watchlist.set_auto_sync(False)
        self.assertFalse(watchlist.get_status()["auto_sync"])
        watchlist.set_auto_sync(True)
        self.assertTrue(watchlist.get_status()["auto_sync"])


class TestPositionsParsing(unittest.TestCase):
    def setUp(self):
        # sync 用例会写状态文件, 必须隔离到 tmp, 避免与运行中的后端互相污染
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        Path(self.tmp.name).unlink()
        self._patcher = patch.object(watchlist, "WATCHLIST_FILE",
                                     Path(self.tmp.name))
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_rows_filtered_new_ths_columns(self):
        # 2026-09 实测同花顺列: 实际数量/股票余额 (无"持仓数量"), 交易市场含沪HK
        rows = [
            {"证券代码": "513120", "证券名称": "HK创新药", "实际数量": "149,400",
             "交易市场": "上海A股"},
            {"证券代码": "600988", "证券名称": "赤峰黄金", "实际数量": 21000,
             "交易市场": "上海A股"},
            {"证券代码": "00700", "证券名称": "腾讯控股", "实际数量": 400,
             "交易市场": "沪HK"},
            {"证券代码": "03441", "证券名称": "南方东西精选", "股票余额": 122000,
             "交易市场": "沪HK"},
            {"证券代码": "09992", "证券名称": "泡泡玛特", "实际数量": 0,
             "交易市场": "沪HK"},      # 0 持仓跳过
            {"证券代码": "0700.HK", "证券名称": "带后缀", "实际数量": 100},
            {"证券代码": "", "证券名称": "人民币"},                # 资金行跳过
            {"证券名称": "无代码行"},
            "not-a-dict",
        ]
        out = watchlist._positions_from_rows(rows)
        self.assertEqual(out, [
            {"code": "513120", "name": "HK创新药"},
            {"code": "600988", "name": "赤峰黄金"},
            {"code": "00700", "name": "腾讯控股"},
            {"code": "03441", "name": "南方东西精选"},
            {"code": "00700", "name": "带后缀"},   # 0700.HK -> 补零
        ])

    def test_rows_filtered_old_columns(self):
        rows = [{"证券代码": "601899", "证券名称": "紫金矿业", "持仓数量": "1,200"}]
        out = watchlist._positions_from_rows(rows)
        self.assertEqual(out, [{"code": "601899", "name": "紫金矿业"}])

    def test_qty_missing_accepted(self):
        out = watchlist._positions_from_rows(
            [{"证券代码": "510300", "证券名称": "沪深300ETF"}])
        self.assertEqual(len(out), 1)

    def test_sync_adds_hk_stocks(self):
        with patch.object(watchlist, "_run_ths_positions",
                          return_value=[{"code": "00700", "name": "腾讯控股"}]):
            r = watchlist.sync_positions()
        self.assertTrue(r["ok"])
        self.assertEqual(r["added"], ["hk00700"])
        stocks = watchlist.get_status()["stocks"]
        self.assertEqual(stocks[0]["symbol"], "hk00700")
        self.assertEqual(stocks[0]["code"], "00700")
        self.assertEqual(stocks[0]["name"], "腾讯控股")


if __name__ == "__main__":
    unittest.main()

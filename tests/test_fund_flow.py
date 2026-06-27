from __future__ import annotations

import importlib
import sys
import types
import unittest
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pandas as pd

from stockview.fund_flow import (
    FundFlowSnapshot,
    FundFlowTrendPoint,
    _build_trend_frame,
    _filter_rows,
    _is_effective_trading_time,
    _now_shanghai,
    _pick_default_top_names,
    _refresh_interval_seconds,
)


def _make_snapshot(names: List[str]) -> FundFlowSnapshot:
    rows: List[Dict[str, Any]] = [{"rank": idx + 1, "name": name, "change_pct": 0.0, "main_net_inflow": float(-idx)} for idx, name in enumerate(names)]
    return FundFlowSnapshot(
        trade_date=pd.Timestamp("2026-06-27 11:30:00", tz="Asia/Shanghai"),
        rows=rows,
        code_name_map={name: f"CODE{idx}" for idx, name in enumerate(names)},
        name_code_map={f"CODE{idx}": name for idx, name in enumerate(names)},
        source_type="sector",
        indicator="today",
    )


class TestHelperFunctions(unittest.TestCase):
    def test_trading_time_detection(self) -> None:
        self.assertTrue(_is_effective_trading_time(pd.Timestamp("2026-06-27 10:15:00", tz="Asia/Shanghai")))
        self.assertFalse(_is_effective_trading_time(pd.Timestamp("2026-06-27 12:30:00", tz="Asia/Shanghai")))
        self.assertTrue(_is_effective_trading_time(pd.Timestamp("2026-06-27 14:59:59", tz="Asia/Shanghai")))
        self.assertFalse(_is_effective_trading_time(pd.Timestamp("2026-06-27 15:00:01", tz="Asia/Shanghai")))

    def test_refresh_interval_seconds(self) -> None:
        self.assertEqual(_refresh_interval_seconds(pd.Timestamp("2026-06-27 10:00:00", tz="Asia/Shanghai")), 8)
        self.assertEqual(_refresh_interval_seconds(pd.Timestamp("2026-06-27 18:00:00", tz="Asia/Shanghai")), 45)

    def test_pick_default_top_names(self) -> None:
        self.assertEqual(_pick_default_top_names([{"name": "A"}, {"name": "B"}, {"name": "C"}], top_n=2), ["A", "B"])
        self.assertEqual(_pick_default_top_names([], top_n=5), [])

    def test_filter_rows(self) -> None:
        rows = [{"name": "A"}, {"name": "B"}, {"name": "C"}]
        self.assertEqual(_filter_rows(rows, ["B"]), [{"name": "B"}])
        self.assertEqual(_filter_rows(rows, []), rows)

    def test_build_trend_frame(self) -> None:
        klines = {
            "A": pd.DataFrame(
                {
                    "timestamp": [pd.Timestamp("2026-06-27 11:30:00")],
                    "main_net_inflow": [100.0],
                    "small_net_inflow": [0.0],
                    "medium_net_inflow": [0.0],
                    "large_net_inflow": [60.0],
                    "super_large_net_inflow": [40.0],
                }
            ),
            "B": pd.DataFrame(columns=["timestamp", "main_net_inflow", "small_net_inflow", "medium_net_inflow", "large_net_inflow", "super_large_net_inflow"]),
        }
        df = _build_trend_frame(klines)
        self.assertCountEqual(df.columns.tolist(), ["板块", "时间", "主力净流入"])
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["板块"], "A")


class TestSnapshot(unittest.TestCase):
    def test_snapshot_defaults(self) -> None:
        snapshot = _make_snapshot(["Alpha"])
        self.assertEqual(snapshot.indicator, "today")
        self.assertEqual(snapshot.name_code_map, {"CODE0": "Alpha"})


if __name__ == "__main__":
    unittest.main()

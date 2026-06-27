from __future__ import annotations

import importlib
import sys
import types
import unittest
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pandas as pd
import requests as _requests

from stockview.fund_flow import (
    FundFlowSnapshot,
    _build_trend_frame,
    _filter_rows,
    _pick_default_top_names,
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


class TestFundFlowSmoke(unittest.TestCase):
    def test_filter_rows_and_defaults(self) -> None:
        snapshot = _make_snapshot(["Alpha", "Beta", "Gamma"])
        self.assertEqual([row["name"] for row in _filter_rows(snapshot.rows, ["Beta"])], ["Beta"])
        self.assertEqual(_pick_default_top_names([], top_n=5), [])
        self.assertTrue(_build_trend_frame({}).empty)

    def test_render_fund_flow_page_runs(self) -> None:
        from stockview.fund_flow import render_fund_flow_page

        snapshot = _make_snapshot(["Alpha", "Beta", "Gamma"])
        streamlit = types.ModuleType("streamlit")
        streamlit.title = MagicMock()
        streamlit.caption = MagicMock()
        streamlit.subheader = MagicMock()
        streamlit.selectbox = MagicMock(side_effect=["industry", "today"])
        streamlit.slider = MagicMock(return_value=10)
        streamlit.number_input = MagicMock(return_value=15)
        streamlit.metric = MagicMock()
        streamlit.multiselect = MagicMock(return_value=["Alpha", "Beta"])
        streamlit.tabs = MagicMock(return_value=(MagicMock(), MagicMock()))
        streamlit.info = MagicMock()
        streamlit.plotly_chart = MagicMock()
        streamlit.dataframe = MagicMock()
        streamlit.markdown = MagicMock()
        streamlit.session_state = {}
        streamlit.cache_resource = lambda **kw: (lambda fn: fn)
        streamlit.cache_data = lambda **kw: (lambda fn: fn)
        streamlit.sidebar = MagicMock()
        streamlit.sidebar.subheader = MagicMock()
        streamlit.sidebar.selectbox = MagicMock(side_effect=["industry", "today"])
        streamlit.sidebar.slider = MagicMock(return_value=10)
        streamlit.sidebar.number_input = MagicMock(return_value=15)
        streamlit_autorefresh = types.ModuleType("streamlit_autorefresh")
        streamlit_autorefresh.st_autorefresh = MagicMock()
        import stockview.fund_flow as fund_flow

        original_load_rank = fund_flow._load_rank_snapshot
        original_minute = fund_flow._load_top_sector_minute_klines
        original_get_session = fund_flow._get_session
        fund_flow._load_rank_snapshot = MagicMock(return_value=snapshot)
        fund_flow._load_top_sector_minute_klines = MagicMock(
            return_value={
                "Alpha": pd.DataFrame(
                    {
                        "timestamp": [pd.Timestamp("2026-06-27 11:30:00")],
                        "main_net_inflow": [100.0],
                        "small_net_inflow": [0.0],
                        "medium_net_inflow": [0.0],
                        "large_net_inflow": [60.0],
                        "super_large_net_inflow": [40.0],
                    }
                ),
                "Beta": pd.DataFrame(
                    {
                        "timestamp": [pd.Timestamp("2026-06-27 11:30:00")],
                        "main_net_inflow": [-50.0],
                        "small_net_inflow": [0.0],
                        "medium_net_inflow": [0.0],
                        "large_net_inflow": [-30.0],
                        "super_large_net_inflow": [-20.0],
                    }
                ),
            }
        )
        try:
            fake_session = MagicMock()
            fake_session.get.return_value = MagicMock(status_code=200, raise_for_status=MagicMock(), json=MagicMock(return_value={"rc": 0, "data": {"total": 0, "diff": []}}))
            original_session_cls = _requests.Session
            _requests.Session = lambda: fake_session

            import streamlit as _st

            _real_modules = {
                "streamlit": sys.modules.get("streamlit"),
                "streamlit_autorefresh": sys.modules.get("streamlit_autorefresh"),
            }
            sys.modules["streamlit"] = streamlit
            sys.modules["streamlit_autorefresh"] = streamlit_autorefresh
            for key in list(sys.modules):
                if key.startswith("stockview.fund_flow"):
                    del sys.modules[key]
            from stockview.fund_flow import render_fund_flow_page as _render
            _render()
            streamlit.title.assert_called_once()
            streamlit.plotly_chart.assert_called_once()
        finally:
            fund_flow._load_rank_snapshot = original_load_rank
            fund_flow._load_top_sector_minute_klines = original_minute
            _requests.Session = original_session_cls
            for key, value in _real_modules.items():
                if value is None:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = value


if __name__ == "__main__":
    unittest.main()

"""端到端冒烟: 真实拉取数据渲染 ETF 信号页 (AppTest.from_string)."""

from __future__ import annotations

import unittest

from streamlit.testing.v1 import AppTest

BOOTSTRAP = """
import sys
sys.path.insert(0, "/Users/weiwang/Projects/streamlit")
from stockview.etf_signal import render_etf_signal_page
render_etf_signal_page()
"""


class TestEtfSignalPageSmoke(unittest.TestCase):
    def test_page_renders_without_exception(self) -> None:
        at = AppTest.from_string(BOOTSTRAP)
        at.run(timeout=120)
        self.assertFalse(at.exception, msg=f"页面渲染异常: {at.exception}")
        titles = [t.value for t in at.title]
        self.assertTrue(any("创业板ETF" in t for t in titles))


if __name__ == "__main__":
    unittest.main()

"""测试 fdata_client 保护机制: 探活、CLI 回退限流与防雪崩."""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from strategy import fdata_client


class TestFdataProtection(unittest.TestCase):
    def test_is_server_available(self):
        # 默认 9701 端口未启动长连接时返回 False
        res = fdata_client.is_server_available()
        self.assertIsInstance(res, bool)

    def test_throttle_cli_fallback_spacing(self):
        # 测试在密集调用时，节流器能够平滑缓冲
        t0 = time.time()
        for i in range(3):
            fdata_client._throttle_cli_fallback(f"test_spacing_{i}")
        elapsed = time.time() - t0
        # 3次调用之间有最小 0.1s 间隔，总耗时应 >= 0.15s
        self.assertGreaterEqual(elapsed, 0.15)

    def test_throttle_burst_backpressure(self):
        # 模拟瞬时突发高频调用（超过 6 次），应触发额外的 backpressure 睡眠
        t0 = time.time()
        for i in range(8):
            fdata_client._throttle_cli_fallback(f"test_burst_{i}")
        elapsed = time.time() - t0
        # 超过 6 次后会触发 0.3s 的背压睡眠
        self.assertGreaterEqual(elapsed, 0.5)

    @patch.object(fdata_client._client, "request")
    @patch("subprocess.run")
    def test_fallback_called_on_server_unavailable(self, mock_subproc, mock_req):
        mock_req.side_effect = fdata_client.ServerUnavailable("mocked down")
        mock_subproc.return_value = MagicMock(
            returncode=0,
            stdout='{"ok": true, "data": [{"code": "sh510300", "quote": {"last": 4.5, "pre_close": 4.4}}]}'
        )
        res = fdata_client.quote("sh510300")
        self.assertIsNotNone(res)
        self.assertEqual(res["code"], "sh510300")
        self.assertEqual(res["last"], 4.5)
        self.assertEqual(fdata_client._client.source, "eltdx(cli)")


if __name__ == "__main__":
    unittest.main()

"""Tests for fallback method selection.

Run with::

    python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tgwsproxy.fallback import FallbackConfig, attempt_fallback
from tgwsproxy.stats import Stats


class H2PreferenceTest(unittest.IsolatedAsyncioTestCase):
    def _args(self):
        reader = asyncio.StreamReader()
        writer = Mock()
        writer.is_closing.return_value = False
        return reader, writer, Mock(), Stats()

    async def test_media_skips_h2_when_plaintext_wins(self):
        reader, writer, ctx, stats = self._args()
        pool = SimpleNamespace(open=AsyncMock(return_value=object()))
        cfg = FallbackConfig(cfproxy_enabled=True)

        with patch(
            "tgwsproxy.fallback.cf_prefers_tls", return_value=False
        ), patch(
            "tgwsproxy.fallback._cfproxy",
            new=AsyncMock(return_value=True),
        ) as cfproxy:
            result = await attempt_fallback(
                reader, writer, b"init", "test", 2, True, ctx, stats, cfg,
                h2_pool=pool, proto_tag=b"\x00\x00\x00\x00",
            )

        self.assertTrue(result)
        pool.open.assert_not_awaited()
        cfproxy.assert_awaited_once()

    async def test_media_uses_h2_when_tls_is_preferred(self):
        reader, writer, ctx, stats = self._args()
        channel = object()
        pool = SimpleNamespace(open=AsyncMock(return_value=channel))
        cfg = FallbackConfig(cfproxy_enabled=True)

        with patch(
            "tgwsproxy.fallback.cf_prefers_tls", return_value=True
        ), patch(
            "tgwsproxy.cf_h2.bridge_h2", new=AsyncMock()
        ) as bridge_h2, patch(
            "tgwsproxy.fallback._cfproxy",
            new=AsyncMock(return_value=False),
        ):
            result = await attempt_fallback(
                reader, writer, b"init", "test", 2, True, ctx, stats, cfg,
                h2_pool=pool, proto_tag=b"\x00\x00\x00\x00",
            )

        self.assertTrue(result)
        pool.open.assert_awaited_once_with(2, "test")
        bridge_h2.assert_awaited_once_with(
            reader, writer, channel, ctx, b"\x00\x00\x00\x00"
        )
        self.assertEqual(stats.connections_h2, 1)
        self.assertEqual(stats.connections_cfproxy, 1)


if __name__ == "__main__":
    unittest.main()

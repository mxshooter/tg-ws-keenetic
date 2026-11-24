"""Tests for the bounded bad-handshake drain and the CF worker pool.

Run with::

    python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import asyncio
import time
import unittest
from collections import deque
from unittest import mock
from unittest.mock import AsyncMock, Mock

from tgwsproxy import handler as handler_module
from tgwsproxy.stats import Stats
from tgwsproxy.websocket import WsHandshakeError
from tgwsproxy.ws_pool import CfWorkerPool


class _FakeWS:
    def __init__(self, stale: bool = False):
        self.stale = stale
        self.writer = Mock()

    async def close(self) -> None:
        pass


class DrainTest(unittest.IsolatedAsyncioTestCase):
    async def test_drain_stops_at_the_size_limit(self):
        reader = asyncio.StreamReader()
        reader.feed_data(
            b"x" * (handler_module.BAD_HANDSHAKE_DRAIN_LIMIT + 4096)
        )
        reader.feed_eof()

        await handler_module._drain(reader)

        self.assertEqual(len(reader._buffer), 4096)

    async def test_drain_returns_when_the_peer_closes(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b"abc")
        reader.feed_eof()

        await handler_module._drain(reader)

        self.assertEqual(reader._buffer, b"")


class CfWorkerPoolTest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_pool_never_pools(self):
        pool = CfWorkerPool(stats=Stats())
        pool.set_target(0)
        pool._connect_one = AsyncMock(return_value=(_FakeWS(), "w.example"))

        self.assertIsNone(await pool.acquire(2, "1.2.3.4", ["w.example"]))
        pool._schedule_refill(2, "1.2.3.4", ["w.example"])

        self.assertNotIn(2, pool._refilling)
        self.assertNotIn(2, pool._rotating)

    async def test_stale_socket_is_dropped_on_acquire(self):
        pool = CfWorkerPool(stats=Stats())
        pool.set_target(1)
        pool._connect_one = AsyncMock(return_value=None)
        pool._idle.setdefault(2, deque()).append(
            (_FakeWS(stale=True), time.monotonic(), "w.example")
        )

        self.assertIsNone(await pool.acquire(2, "1.2.3.4", ["w.example"]))
        self.assertEqual(len(pool._idle[2]), 0)

        pool.reset()
        await asyncio.sleep(0)

    async def test_failed_refill_backs_off(self):
        pool = CfWorkerPool(stats=Stats())
        pool.set_target(1)
        pool._connect_one = AsyncMock(return_value=None)

        pool._schedule_refill(2, "1.2.3.4", ["w.example"])
        await pool._refilling[2]

        self.assertEqual(pool._refill_failures[2], 1)
        self.assertGreater(pool._refill_after[2], time.monotonic())

        # While the backoff is active a new refill is not scheduled.
        pool._schedule_refill(2, "1.2.3.4", ["w.example"])
        self.assertNotIn(2, pool._refilling)

        pool.reset()
        await asyncio.sleep(0)

    async def test_successful_refill_keeps_a_socket_warm(self):
        pool = CfWorkerPool(stats=Stats())
        pool.set_target(1)
        ws = _FakeWS()
        pool._connect_one = AsyncMock(return_value=(ws, "w.example"))

        pool._schedule_refill(2, "1.2.3.4", ["w.example"])
        await pool._refilling[2]

        self.assertEqual(len(pool._idle[2]), 1)
        self.assertNotIn(2, pool._refill_failures)

        got = await pool.acquire(2, "1.2.3.4", ["w.example"])
        self.assertIsNotNone(got)
        self.assertIs(got[0], ws)
        self.assertEqual(got[1], "w.example")

        pool.reset()
        await asyncio.sleep(0)


class CfWorkerSecureFlagTest(unittest.IsolatedAsyncioTestCase):
    async def test_pool_uses_secure_flag_from_config(self):
        pool = CfWorkerPool(stats=Stats())
        pool.set_target(1, secure=False)
        connect = AsyncMock(return_value=_FakeWS())
        with mock.patch("tgwsproxy.ws_pool.RawWebSocket.connect", connect):
            await pool._connect_one(["w.example"], "1.2.3.4", 2)

        self.assertFalse(connect.await_args.kwargs["secure"])


class CfWorkerFailureReportTest(unittest.TestCase):
    NOW = 1_700_000_000.0

    def _report(self, exc, now=None):
        pool = CfWorkerPool(stats=Stats())
        now = self.NOW if now is None else now
        with mock.patch(
            "tgwsproxy.ws_pool.time.monotonic", return_value=now
        ), mock.patch("tgwsproxy.ws_pool.time.time", return_value=now):
            pool.report_failure("w.example", exc)
        return pool

    def _available(self, pool, now):
        with mock.patch("tgwsproxy.ws_pool.time.monotonic", return_value=now):
            return pool.available_domains(["w.example"])

    def test_429_disables_domain_until_the_next_day(self):
        pool = self._report(WsHandshakeError(429, "429 Too Many Requests"))
        until = pool._exhausted_until["w.example"]
        self.assertGreater(until, self.NOW)
        self.assertLessEqual(until, self.NOW + 86400)
        self.assertEqual(self._available(pool, self.NOW), [])
        self.assertEqual(self._available(pool, until + 1), ["w.example"])

    def test_5xx_pauses_domain_for_a_minute(self):
        pool = self._report(WsHandshakeError(503, "503 Service Unavailable"))
        self.assertEqual(
            pool._exhausted_until["w.example"],
            self.NOW + CfWorkerPool.FAILURE_COOLDOWN,
        )
        self.assertEqual(self._available(pool, self.NOW), [])
        self.assertEqual(
            self._available(
                pool, self.NOW + CfWorkerPool.FAILURE_COOLDOWN + 1
            ),
            ["w.example"],
        )

    def test_network_errors_pause_domain(self):
        for exc in (OSError("connection reset"), asyncio.TimeoutError()):
            with self.subTest(exc=exc):
                pool = self._report(exc)
                self.assertEqual(self._available(pool, self.NOW), [])
                self.assertEqual(
                    self._available(
                        pool, self.NOW + CfWorkerPool.FAILURE_COOLDOWN + 1
                    ),
                    ["w.example"],
                )

    def test_client_4xx_keeps_domain_available(self):
        for status in (400, 403, 404):
            with self.subTest(status=status):
                pool = self._report(
                    WsHandshakeError(status, f"{status} error")
                )
                self.assertNotIn("w.example", pool._exhausted_until)
                self.assertEqual(
                    self._available(pool, self.NOW), ["w.example"]
                )

    def test_unknown_errors_are_ignored(self):
        pool = self._report(ValueError("unexpected"))
        self.assertNotIn("w.example", pool._exhausted_until)

    def test_longer_quota_pause_is_not_shortened(self):
        pool = self._report(WsHandshakeError(429, "429 Too Many Requests"))
        until = pool._exhausted_until["w.example"]
        with mock.patch(
            "tgwsproxy.ws_pool.time.monotonic", return_value=self.NOW + 1
        ), mock.patch(
            "tgwsproxy.ws_pool.time.time", return_value=self.NOW + 1
        ):
            pool.report_failure(
                "w.example", WsHandshakeError(503, "503 error")
            )
        self.assertEqual(pool._exhausted_until["w.example"], until)


if __name__ == "__main__":
    unittest.main()

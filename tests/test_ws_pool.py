"""Tests for the bounded bad-handshake drain and the CF worker pool.

Run with::

    python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import asyncio
import time
import unittest
from collections import deque
from unittest.mock import AsyncMock, Mock

from tgwsproxy import handler as handler_module
from tgwsproxy.stats import Stats
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


if __name__ == "__main__":
    unittest.main()

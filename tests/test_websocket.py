"""Tests for WebSocket socket lifecycle and CF front timeouts.

Run with::

    python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tgwsproxy import websocket as ws_module
from tgwsproxy.websocket import RawWebSocket, WsHandshakeError


class HandshakeCleanupTest(unittest.IsolatedAsyncioTestCase):
    async def test_init_send_failure_or_cancellation_aborts_transport(self):
        for error in (ConnectionResetError(), asyncio.CancelledError()):
            with self.subTest(error=type(error)):
                writer = Mock(drain=AsyncMock(side_effect=error))
                ws = RawWebSocket(asyncio.StreamReader(), writer)
                with self.assertRaises(type(error)):
                    await ws.send_init(b"init")
                writer.transport.abort.assert_called_once()
                self.assertTrue(ws._closed)

    async def test_cancelling_pending_upgrade_aborts_socket(self):
        reader = asyncio.StreamReader()
        writer = Mock()
        started = asyncio.Event()

        async def drain():
            started.set()
            await asyncio.Future()

        writer.drain = drain
        with patch(
            "tgwsproxy.websocket.asyncio.open_connection",
            AsyncMock(return_value=(reader, writer)),
        ), patch.object(ws_module, "apply_socket_options"):
            task = asyncio.create_task(
                RawWebSocket.connect("192.0.2.1", "example.org")
            )
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        writer.transport.abort.assert_called_once()

    async def test_failed_upgrade_aborts_socket(self):
        for error in (ConnectionResetError(), asyncio.TimeoutError()):
            with self.subTest(error=type(error)):
                reader = Mock(readline=AsyncMock(side_effect=error))
                writer = Mock(drain=AsyncMock())
                with patch(
                    "tgwsproxy.websocket.asyncio.open_connection",
                    AsyncMock(return_value=(reader, writer)),
                ), patch.object(ws_module, "apply_socket_options"):
                    with self.assertRaises(type(error)):
                        await RawWebSocket.connect(
                            "192.0.2.1", "example.org"
                        )
                writer.transport.abort.assert_called_once()

    async def test_non_101_response_aborts_socket(self):
        reader = Mock(
            readline=AsyncMock(
                side_effect=[
                    b"HTTP/1.1 404 Not Found\r\n",
                    b"\r\n",
                ]
            )
        )
        writer = Mock(drain=AsyncMock())
        with patch(
            "tgwsproxy.websocket.asyncio.open_connection",
            AsyncMock(return_value=(reader, writer)),
        ), patch.object(ws_module, "apply_socket_options"):
            with self.assertRaises(WsHandshakeError):
                await RawWebSocket.connect("192.0.2.1", "example.org")
        writer.transport.abort.assert_called_once()

    async def test_close_is_bounded_and_aborts(self):
        async def hang():
            await asyncio.Event().wait()

        writer = Mock(drain=AsyncMock())
        writer.wait_closed = hang
        ws = RawWebSocket(asyncio.StreamReader(), writer)
        with patch.object(RawWebSocket, "CLOSE_TIMEOUT", 0.05):
            started = time.monotonic()
            await ws.close()
            self.assertLess(time.monotonic() - started, 1.0)
        writer.transport.abort.assert_called_once()


class CfDomainTimeoutTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ws_module.reset_cf_timeouts()
        self.addCleanup(ws_module.reset_cf_timeouts)

    async def connect(self, *hosts):
        return await ws_module.connect_cf_domains(
            ((host, host) for host in hosts), secure=True, label="test"
        )

    async def test_five_timeouts_end_search_and_later_clients_skip(self):
        hosts = ("a", "http-error", "b", "c", "d", "e", "f")
        responses = [
            asyncio.TimeoutError(),
            WsHandshakeError(503, "unavailable"),
            TimeoutError(),
            TimeoutError(),
            TimeoutError(),
            TimeoutError(),
            WsHandshakeError(429, "busy"),
            object(),
        ]
        with patch.object(
            RawWebSocket, "connect", side_effect=responses
        ) as dial:
            self.assertIsNone(await self.connect(*hosts))
            self.assertEqual((await self.connect(*hosts))[1], "f")
            self.assertEqual(
                [
                    (call.args[0], call.kwargs["timeout"])
                    for call in dial.await_args_list
                ],
                [
                    ("a", 10),
                    ("http-error", 6),
                    ("b", 6),
                    ("c", 4),
                    ("d", 3),
                    ("e", 3),
                    ("http-error", 10),
                    ("f", 10),
                ],
            )

    async def test_timeout_retry_skips_to_other_front_without_waiting(self):
        with patch.object(ws_module, "CF_TIMEOUT_COOLDOWN", 0), patch.object(
            RawWebSocket, "connect", side_effect=asyncio.TimeoutError()
        ):
            self.assertIsNone(await self.connect("a"))

        started, release = asyncio.Event(), asyncio.Event()

        async def dial(host, *args, **kwargs):
            if host == "a":
                started.set()
                await release.wait()
                raise WsHandshakeError(429, "busy")
            return object()

        with patch.object(RawWebSocket, "connect", side_effect=dial) as dial_mock:
            retry = asyncio.create_task(self.connect("a"))
            try:
                await asyncio.wait_for(started.wait(), 1)
                result = await asyncio.wait_for(self.connect("a", "b"), 1)
                self.assertEqual(result[1], "b")
                self.assertEqual(dial_mock.await_count, 2)
            finally:
                release.set()
                await retry

        with patch.object(RawWebSocket, "connect", return_value=object()):
            self.assertEqual((await self.connect("a"))[1], "a")

    async def test_cancelling_timeout_retry_releases_it_for_next_client(self):
        with patch.object(ws_module, "CF_TIMEOUT_COOLDOWN", 0), patch.object(
            RawWebSocket, "connect", side_effect=asyncio.TimeoutError()
        ):
            await self.connect("a")
        started = asyncio.Event()

        async def dial(*args, **kwargs):
            started.set()
            await asyncio.Future()

        with patch.object(RawWebSocket, "connect", side_effect=dial):
            retry = asyncio.create_task(self.connect("a"))
            await asyncio.wait_for(started.wait(), 1)
            retry.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await retry

        with patch.object(RawWebSocket, "connect", return_value=object()):
            self.assertEqual((await self.connect("a"))[1], "a")


if __name__ == "__main__":
    unittest.main()

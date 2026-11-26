"""Tests for the WebSocket socket lifecycle.

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



if __name__ == "__main__":
    unittest.main()

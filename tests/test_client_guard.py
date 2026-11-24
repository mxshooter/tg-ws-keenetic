"""Tests for cancelling an upstream dial when the client leaves.

Run with::

    python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import asyncio
import unittest

from tgwsproxy.client_guard import ClientStreamProtocol, client_setup


class ClientSetupTest(unittest.IsolatedAsyncioTestCase):
    async def test_fin_cancels_pending_setup_and_closes_socket(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def handle(reader, writer):
            try:
                with client_setup(reader, writer):
                    started.set()
                    await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
            finally:
                writer.close()
                await writer.wait_closed()

        loop = asyncio.get_running_loop()
        server = await loop.create_server(
            lambda: ClientStreamProtocol(handle), "127.0.0.1", 0
        )
        async with server:
            port = server.sockets[0].getsockname()[1]
            peer, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                await asyncio.wait_for(started.wait(), 1)
                writer.write_eof()
                await asyncio.wait_for(cancelled.wait(), 1)
                self.assertEqual(
                    await asyncio.wait_for(peer.read(1), 1), b""
                )
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_plain_streams_keep_working(self):
        class _Plain:
            def at_eof(self):
                return False

        class _Transport:
            def get_protocol(self):
                return object()

        class _Writer:
            transport = _Transport()

            def is_closing(self):
                return False

        with client_setup(_Plain(), _Writer()):
            pass


if __name__ == "__main__":
    unittest.main()

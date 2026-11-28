"""Tests for bounded client shutdown in the top-level server.

Run with::

    python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tgwsproxy import server as server_module
from tgwsproxy.server import ProxyServer


class _StubServer:
    """Minimal stand-in for the asyncio listening server."""

    def __init__(self):
        self.closed = False

    def is_serving(self) -> bool:
        return not self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _make_server() -> ProxyServer:
    # Bypass Config parsing: stop() only touches the attributes below.
    srv = object.__new__(ProxyServer)
    srv._config = Mock()
    srv._server = _StubServer()
    srv._stop_event = asyncio.Event()
    srv._serve_task = None
    srv._stats_task = None
    srv._tasks = {}
    srv._handler = None
    srv._pool = None
    srv._h2_pool = None
    srv._stopping = False
    return srv


def _writer(write_buffer: int) -> Mock:
    writer = Mock()
    writer.transport.get_write_buffer_size.return_value = write_buffer
    return writer


class StopShutdownTest(unittest.IsolatedAsyncioTestCase):
    async def _run_stop(self, srv: ProxyServer):
        with patch.object(server_module, "CLIENT_CLOSE_TIMEOUT", 0.01), \
                patch.object(
                    server_module.cf_worker_pool, "close", AsyncMock()
                ), \
                patch.object(server_module.tcp_backoff, "reset"), \
                patch.object(server_module, "stop_cfproxy_domain_refresh"):
            await srv.stop()

    async def test_stop_aborts_transport_with_unsent_data_after_timeout(self):
        srv = _make_server()
        writer = _writer(write_buffer=64)
        release = asyncio.Event()
        started = asyncio.Event()

        async def stubborn() -> None:
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                # Ignore the first cancellation to model a silent TLS peer.
                await release.wait()

        task = asyncio.create_task(stubborn())
        srv._tasks[task] = writer
        await started.wait()

        # Release the stubborn handler shortly after stop() gives up waiting,
        # otherwise the final gather would block forever.
        asyncio.get_running_loop().call_later(0.05, release.set)
        try:
            await self._run_stop(srv)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

        writer.close.assert_called_once_with()
        writer.transport.abort.assert_called_once_with()
        self.assertEqual(srv._tasks, {})

    async def test_stop_keeps_transport_when_nothing_is_queued(self):
        srv = _make_server()
        writer = _writer(write_buffer=0)

        async def cooperative() -> None:
            await asyncio.Future()

        task = asyncio.create_task(cooperative())
        srv._tasks[task] = writer
        await asyncio.sleep(0)

        await self._run_stop(srv)

        writer.close.assert_called_once_with()
        writer.transport.abort.assert_not_called()
        self.assertEqual(srv._tasks, {})


class ClientTrackingTest(unittest.IsolatedAsyncioTestCase):
    async def test_on_client_tracks_writer_and_forgets_finished_task(self):
        srv = _make_server()
        done = asyncio.Event()

        async def handle(reader, writer) -> None:
            await done.wait()

        srv._handler = Mock()
        srv._handler.handle = handle
        writer = Mock()
        reader = Mock()

        srv._on_client(reader, writer)
        self.assertEqual(len(srv._tasks), 1)
        task, tracked = next(iter(srv._tasks.items()))
        self.assertIs(tracked, writer)

        done.set()
        await asyncio.gather(task)
        self.assertEqual(srv._tasks, {})

    async def test_on_client_rejects_new_clients_while_stopping(self):
        srv = _make_server()
        srv._stopping = True
        writer = Mock()

        srv._on_client(Mock(), writer)

        writer.close.assert_called_once_with()
        self.assertEqual(srv._tasks, {})


if __name__ == "__main__":
    unittest.main()

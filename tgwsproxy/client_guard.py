"""Cancel an in-flight upstream dial when its client goes away.

A dial to a dead Cloudflare front takes seconds while the client has often
already closed its socket. Without this, dials pile up behind the accept
queue and exhaust the router's NAT table.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager

log = logging.getLogger("tgwsproxy.client_guard")


class ClientStreamProtocol(asyncio.StreamReaderProtocol):
    """StreamReaderProtocol that reports when the accepted client is gone."""

    def __init__(self, client_cb):
        super().__init__(asyncio.StreamReader(), client_cb)
        self.cancel_setup = None

    def eof_received(self):
        keep_open = super().eof_received()
        if self.cancel_setup is not None:
            self.cancel_setup(False)
        return keep_open

    def connection_lost(self, exc):
        super().connection_lost(exc)
        if self.cancel_setup is not None:
            self.cancel_setup(True)


@contextmanager
def client_setup(reader, writer):
    """Cancel the current task if the client disconnects during setup.

    Used around every upstream dial (WS, CF proxy, TCP, HTTP/2). With a
    plain StreamReaderProtocol (tests, embedded use) it is a no-op.
    """
    if reader.at_eof() or writer.is_closing():
        raise asyncio.CancelledError()
    protocol = writer.transport.get_protocol()
    if not isinstance(protocol, ClientStreamProtocol):
        yield
        return

    task = asyncio.current_task()

    def cancel(lost):
        if lost or reader.at_eof():
            protocol.cancel_setup = None
            task.cancel()

    protocol.cancel_setup = cancel
    try:
        yield
    finally:
        protocol.cancel_setup = None

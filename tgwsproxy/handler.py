"""Per-connection MTProto handler.

Each accepted TCP connection goes through `handle`:
  1. Read PROXY-protocol line if enabled.
  2. Read the first byte to decide between Fake TLS and raw obfuscated MTP.
  3. Run Fake TLS handshake (if applicable) and unwrap the inner stream.
  4. Authenticate the MTProto obfuscation handshake using the secret.
  5. Take a WS connection from the pool, or fall back to CF/TCP.
  6. Bridge the rest of the connection.
"""
from __future__ import annotations

import asyncio
import logging
import random
import struct
import time
from dataclasses import dataclass
from typing import Dict, Optional

from .bridge import MessageSplitter, bridge_ws
from .crypto import (
    ClientHandshake,
    build_context,
    generate_relay_handshake,
    parse_client_handshake,
)
from .fake_tls import (
    SERVER_HELLO_DELAY,
    FakeTlsStream,
    build_server_hello,
    parse_client_hello,
    relay_to_masking_domain,
    verify_client_hello,
)
from .fallback import FallbackConfig, attempt_fallback
from .stats import Stats
from .websocket import apply_socket_options
from .ws_pool import WebSocketPool, ws_domains_for
from .constants import HANDSHAKE_LEN, TLS_RECORD_HANDSHAKE

log = logging.getLogger("tgwsproxy.handler")


@dataclass
class HandlerSettings:
    """Read-only-ish snapshot of config values the handler needs."""

    secret: bytes
    dc_redirects: Dict[int, str]
    buffer_size: int
    fake_tls_domain: str
    proxy_protocol: bool
    force_test_dc: bool
    fallback: FallbackConfig


class ClientHandler:
    """Holds the per-server state (pool) and serves clients."""

    def __init__(
        self,
        settings: HandlerSettings,
        pool: WebSocketPool,
        stats: Stats,
        h2_pool=None,
    ):
        self._settings = settings
        self._pool = pool
        self._stats = stats
        self._h2_pool = h2_pool

    def set_h2_pool(self, h2_pool) -> None:
        self._h2_pool = h2_pool

    async def handle(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        self._stats.connections_total += 1
        self._stats.connections_active += 1
        peer = client_writer.get_extra_info("peername")
        label = f"{peer[0]}:{peer[1]}" if peer else "?"
        apply_socket_options(client_writer.transport, self._settings.buffer_size)

        try:
            init = await self._read_init(client_reader, client_writer, label)
            if init is None:
                return
            handshake, reader, writer, label = init

            parsed = parse_client_handshake(handshake, self._settings.secret)
            if parsed is None:
                self._stats.connections_bad += 1
                log.warning("[%s] bad handshake (wrong secret or proto)", label)
                await _drain(reader)
                return

            await self._serve_authenticated(
                parsed, handshake, reader, writer, label
            )

        except asyncio.TimeoutError:
            log.warning("[%s] timeout during handshake", label)
        except asyncio.IncompleteReadError:
            log.debug("[%s] client disconnected", label)
        except asyncio.CancelledError:
            log.debug("[%s] cancelled", label)
        except ConnectionResetError:
            log.debug("[%s] connection reset", label)
        except OSError as exc:
            log.debug("[%s] OS error: %r", label, exc)
        except Exception as exc:
            log.error("[%s] unexpected: %s", label, exc, exc_info=True)
        finally:
            self._stats.connections_active -= 1
            try:
                client_writer.close()
                await client_writer.wait_closed()
            except BaseException:
                pass

    # --- init read -------------------------------------------------------

    async def _read_init(self, reader, writer, label):
        if self._settings.proxy_protocol:
            label = await self._consume_proxy_protocol(reader, label)

        try:
            first = await asyncio.wait_for(
                reader.readexactly(1), timeout=10
            )
        except asyncio.IncompleteReadError:
            log.debug("[%s] client disconnected before handshake", label)
            return None

        masking = self._settings.fake_tls_domain
        if first[0] == TLS_RECORD_HANDSHAKE and masking:
            return await self._read_init_via_fake_tls(
                reader, writer, first, masking, label
            )

        if masking:
            log.debug(
                "[%s] non-TLS first byte 0x%02X under fake-TLS -> redirect",
                label, first[0],
            )
            redirect = (
                f"HTTP/1.1 301 Moved Permanently\r\n"
                f"Location: https://{masking}/\r\n"
                f"Content-Length: 0\r\n"
                f"Connection: close\r\n\r\n"
            ).encode()
            writer.write(redirect)
            await writer.drain()
            return None

        try:
            rest = await asyncio.wait_for(
                reader.readexactly(HANDSHAKE_LEN - 1), timeout=10
            )
        except asyncio.IncompleteReadError:
            log.debug("[%s] disconnected mid-handshake", label)
            return None
        return first + rest, reader, writer, label

    async def _consume_proxy_protocol(self, reader, label):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
        except asyncio.IncompleteReadError:
            return label
        text = line.decode("ascii", errors="replace").strip()
        if not text.startswith("PROXY "):
            return label
        parts = text.split()
        if len(parts) >= 6:
            return f"{parts[2]}:{parts[4]}"
        return label

    async def _read_init_via_fake_tls(
        self, reader, writer, first_byte, masking, label
    ):
        try:
            hdr_rest = await asyncio.wait_for(
                reader.readexactly(4), timeout=10
            )
        except asyncio.IncompleteReadError:
            log.debug("[%s] incomplete TLS record header", label)
            return None

        tls_header = first_byte + hdr_rest
        record_len = struct.unpack(">H", tls_header[3:5])[0]
        try:
            record_body = await asyncio.wait_for(
                reader.readexactly(record_len), timeout=10
            )
        except asyncio.IncompleteReadError:
            log.debug("[%s] incomplete TLS record body", label)
            return None

        client_hello = tls_header + record_body
        verified = verify_client_hello(client_hello, self._settings.secret)
        if verified is None:
            log.debug(
                "[%s] Fake TLS verify failed -> masking via %s", label, masking
            )
            await relay_to_masking_domain(
                reader, writer, client_hello, masking, label
            )
            return None

        client_random, session_id, _ = verified
        started = time.monotonic()
        server_hello = build_server_hello(
            self._settings.secret,
            client_random,
            session_id,
            parse_client_hello(client_hello),
        )
        # A real TLS server never answers instantly. The jitter also hides the
        # cost of generating a pure-Python X25519 key on slow routers.
        remaining = random.uniform(*SERVER_HELLO_DELAY) - (
            time.monotonic() - started
        )
        if remaining > 0:
            await asyncio.sleep(remaining)
        writer.write(server_hello)
        await writer.drain()

        stream = FakeTlsStream(reader, writer)
        try:
            handshake = await asyncio.wait_for(
                stream.readexactly(HANDSHAKE_LEN), timeout=10
            )
        except asyncio.IncompleteReadError:
            log.debug("[%s] incomplete obfs2 init inside fake-TLS", label)
            return None
        return handshake, stream, stream, label

    # --- post-handshake -------------------------------------------------

    async def _serve_authenticated(
        self,
        parsed: ClientHandshake,
        handshake_bytes: bytes,
        reader,
        writer,
        label: str,
    ) -> None:
        dc, is_media = parsed.dc_id, parsed.is_media
        proto_int = parsed.proto_int

        is_test_dc = self._settings.force_test_dc or dc >= 10000
        if dc >= 10000:
            log.info("[%s] test DC%d -> DC%d", label, dc, dc - 10000)
            dc -= 10000

        media_tag = " media" if is_media else ""

        log.debug(
            "[%s] handshake ok DC%d%s proto=0x%08X",
            label, dc, media_tag, proto_int,
        )

        relay_init = generate_relay_handshake(parsed.proto_tag, parsed.dc_index)
        ctx = build_context(parsed.prekey_iv, self._settings.secret, relay_init)

        # Everything goes through the pool now (direct connections were
        # removed upstream). If the pool has nothing, fall back.
        ws = await self._pool.acquire(
            dc, is_media, self._settings.dc_redirects.get(dc, ""),
            ws_domains_for(dc, is_media), is_test_dc=is_test_dc,
        )
        if ws is None:
            log.info("[%s] DC%d%s WS pool unavailable -> fallback",
                     label, dc, media_tag)
            splitter = _safe_splitter(proto_int)
            if not await attempt_fallback(
                reader, writer, relay_init, label, dc, is_media, ctx,
                self._stats, self._settings.fallback, splitter,
                is_test_dc=is_test_dc, h2_pool=self._h2_pool,
                proto_tag=parsed.proto_tag,
            ):
                log.warning(
                    "[%s] DC%d%s no fallback available",
                    label, dc, media_tag,
                )
            return

        log.info("[%s] DC%d%s -> WS pool hit", label, dc, media_tag)
        self._stats.connections_ws += 1

        splitter = _safe_splitter(proto_int)
        await ws.send(relay_init)
        await bridge_ws(
            reader, writer, ws, label, ctx, self._stats,
            dc=dc, is_media=is_media, splitter=splitter,
        )


def _safe_splitter(proto_int: int) -> Optional[MessageSplitter]:
    try:
        return MessageSplitter(proto_int)
    except Exception:
        return None


async def _drain(reader) -> None:
    try:
        while await reader.read(4096):
            pass
    except Exception:
        pass

"""Minimal RFC 6455 WebSocket client over TLS.

We only implement what the bridge needs: binary frames, masked client→server,
ping/pong handling, fragmented message reassembly, clean close. The upstream
Telegram WS endpoint never fragments, but some fallback paths do, so inbound
messages are reassembled across continuation frames.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import socket as _socket
import ssl
import struct
from typing import List, Optional, Tuple

from .constants import (
    WS_FIN_BIT,
    WS_MASK_BIT,
    WS_OP_BINARY,
    WS_OP_CLOSE,
    WS_OP_CONT,
    WS_OP_PING,
    WS_OP_PONG,
    WS_OP_TEXT,
)

log = logging.getLogger("tgwsproxy.websocket")

_HEAD_BB = struct.Struct(">BB")
_HEAD_BBH = struct.Struct(">BBH")
_HEAD_BBQ = struct.Struct(">BBQ")
_HEAD_BB4 = struct.Struct(">BB4s")
_HEAD_BBH4 = struct.Struct(">BBH4s")
_HEAD_BBQ4 = struct.Struct(">BBQ4s")
_UNPACK_H = struct.Struct(">H")
_UNPACK_Q = struct.Struct(">Q")

# Precomputed XOR translation tables: _XOR_TABLES[key][value] == value ^ key.
# Lets large payloads mask 4 byte-lanes at a time via bytes.translate instead
# of building big integers, which is markedly faster on slow router CPUs.
_XOR_TABLES = tuple(bytes(value ^ key for value in range(256)) for key in range(256))


def set_x25519(context: ssl.SSLContext) -> None:
    """Pin the ECDH group to X25519 when the local OpenSSL allows it.

    Older Entware OpenSSL builds (and Windows 7 era ones) reject the curve;
    the failure is harmless, so it is swallowed.
    """
    try:
        context.set_ecdh_curve("X25519")
    except Exception:
        pass


def create_ssl_context(
    *, check_hostname: bool = False, verify: bool = False
) -> ssl.SSLContext:
    """Build an SSL context.

    Keenetic/Entware boxes rarely ship an up-to-date CA bundle, so certificate
    verification is disabled by default (matching the fork's historic
    behaviour). Callers that need a verified context (e.g. HTTP/2 to
    Cloudflare) can pass ``verify=True``.
    """
    context = ssl.create_default_context()
    set_x25519(context)
    if verify:
        context.check_hostname = check_hostname
    else:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


_SSL_CTX = create_ssl_context()
_SSL_CTX_FRONTING = create_ssl_context()


class WsHandshakeError(Exception):
    def __init__(
        self,
        status_code: int,
        status_line: str,
        headers: Optional[dict] = None,
        location: Optional[str] = None,
    ):
        self.status_code = status_code
        self.status_line = status_line
        self.headers = headers or {}
        self.location = location
        super().__init__(f"HTTP {status_code}: {status_line}")

    @property
    def is_redirect(self) -> bool:
        return self.status_code in (301, 302, 303, 307, 308)


def apply_socket_options(transport, buffer_size: int) -> None:
    """TCP_NODELAY + bigger socket buffers. Best-effort, ignores failures."""
    sock = transport.get_extra_info("socket") if transport else None
    if sock is None:
        return
    try:
        sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
    except (OSError, AttributeError):
        pass
    try:
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_RCVBUF, buffer_size)
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_SNDBUF, buffer_size)
    except OSError:
        pass


def _xor_mask(data: bytes, mask: bytes) -> bytes:
    if not data:
        return data
    n = len(data)
    if n >= 512:
        masked = bytearray(data)
        for offset in range(4):
            masked[offset::4] = masked[offset::4].translate(
                _XOR_TABLES[mask[offset]]
            )
        return bytes(masked)
    repeated = (mask * ((n // 4) + 1))[:n]
    return (
        int.from_bytes(data, "big") ^ int.from_bytes(repeated, "big")
    ).to_bytes(n, "big")


MAX_MESSAGE_LEN = 16 * 1024 * 1024


class RawWebSocket:
    """Bare-bones client. One stream in, one stream out, no threads."""

    __slots__ = ("reader", "writer", "_closed", "_frag")

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        self.reader = reader
        self.writer = writer
        self._closed = False
        self._frag = bytearray()

    @property
    def closed(self) -> bool:
        return self._closed or self.writer.transport.is_closing()

    @property
    def stale(self) -> bool:
        """True if the socket is closed, EOF'd, or errored — i.e. unusable."""
        try:
            if self._closed or self.writer.transport.is_closing():
                return True
            if self.reader.at_eof() or self.reader.exception() is not None:
                return True
        except Exception:
            return True
        return False

    @classmethod
    async def connect(
        cls,
        host: str,
        sni_domain: str,
        timeout: float = 10.0,
        path: str = "/apiws",
        buffer_size: int = 256 * 1024,
        *,
        sni: Optional[str] = None,
        secure: bool = True,
    ) -> "RawWebSocket":
        ssl_context = _SSL_CTX_FRONTING if sni else _SSL_CTX
        if sni is None:
            sni = sni_domain
        reader, writer = await asyncio.wait_for(
            (
                asyncio.open_connection(
                    host, 443, ssl=ssl_context, server_hostname=sni
                )
                if secure
                else asyncio.open_connection(host, 80)
            ),
            timeout=min(timeout, 10),
        )
        apply_socket_options(writer.transport, buffer_size)

        ws_key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {sni_domain}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Protocol: binary\r\n"
            f"\r\n"
        )
        try:
            writer.write(request.encode())
            await writer.drain()

            status_code, status_line, headers = await cls._read_response(
                reader, timeout
            )
            if status_code == 101:
                return cls(reader, writer)
        except BaseException:
            try:
                writer.close()
            except Exception:
                pass
            raise

        try:
            writer.close()
        except Exception:
            pass
        raise WsHandshakeError(
            status_code,
            status_line,
            headers,
            location=headers.get("location"),
        )

    @staticmethod
    async def _read_response(
        reader: asyncio.StreamReader, timeout: float
    ) -> Tuple[int, str, dict]:
        lines: List[str] = []
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if line in (b"\r\n", b"\n", b""):
                break
            lines.append(line.decode("utf-8", errors="replace").strip())

        if not lines:
            return 0, "empty response", {}

        first = lines[0]
        try:
            status_code = int(first.split(" ", 2)[1])
        except (ValueError, IndexError):
            status_code = 0

        headers: dict = {}
        for hl in lines[1:]:
            if ":" in hl:
                k, v = hl.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        return status_code, first, headers

    async def send(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("WebSocket closed")
        self.writer.write(self._frame(WS_OP_BINARY, data))
        await self.writer.drain()

    async def send_batch(self, parts: List[bytes]) -> None:
        if self._closed:
            raise ConnectionError("WebSocket closed")
        for part in parts:
            self.writer.write(self._frame(WS_OP_BINARY, part))
        await self.writer.drain()

    async def recv(self) -> Optional[bytes]:
        """Block until a binary/text frame, return its payload. None on close."""
        while not self._closed:
            opcode, payload, fin = await self._read_frame()

            if opcode == WS_OP_CLOSE:
                self._closed = True
                code, reason = self._parse_close(payload)
                log.debug(
                    "WS OP_CLOSE from upstream: code=%s reason=%r", code, reason
                )
                try:
                    self.writer.write(
                        self._frame(
                            WS_OP_CLOSE, payload[:2] if payload else b""
                        )
                    )
                    await self.writer.drain()
                except Exception:
                    pass
                return None

            if opcode == WS_OP_PING:
                try:
                    self.writer.write(self._frame(WS_OP_PONG, payload))
                    await self.writer.drain()
                except Exception:
                    pass
                continue

            if opcode == WS_OP_PONG:
                continue

            # Data frames (text/binary) plus continuation frames. Reassemble
            # fragmented messages so the caller always sees a whole payload.
            if opcode in (WS_OP_CONT, WS_OP_TEXT, WS_OP_BINARY):
                if fin and not self._frag:
                    return payload
                self._frag.extend(payload)
                if len(self._frag) > MAX_MESSAGE_LEN:
                    raise ConnectionError(
                        f"WS message too large: {len(self._frag)} bytes"
                    )
                if not fin:
                    continue
                message = bytes(self._frag)
                self._frag.clear()
                return message
        return None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.writer.write(self._frame(WS_OP_CLOSE, b""))
            await self.writer.drain()
        except Exception:
            pass
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass

    @staticmethod
    def _frame(opcode: int, data: bytes) -> bytes:
        """Build a single masked client→server frame."""
        length = len(data)
        flags = WS_FIN_BIT | opcode
        mask = os.urandom(4)
        masked = _xor_mask(data, mask)
        if length < 126:
            return _HEAD_BB4.pack(flags, WS_MASK_BIT | length, mask) + masked
        if length < 65536:
            return (
                _HEAD_BBH4.pack(flags, WS_MASK_BIT | 126, length, mask) + masked
            )
        return _HEAD_BBQ4.pack(flags, WS_MASK_BIT | 127, length, mask) + masked

    _WS_CLOSE_REASONS = {
        1000: "normal", 1001: "going_away", 1002: "protocol_error",
        1003: "unsupported_data", 1006: "abnormal", 1007: "bad_data",
        1008: "policy_violation", 1009: "too_big", 1010: "missing_extension",
        1011: "internal_error",
    }

    @classmethod
    def _parse_close(
        cls, payload: Optional[bytes]
    ) -> Tuple[Optional[int], str]:
        if not payload or len(payload) < 2:
            return None, ""
        try:
            code = int.from_bytes(payload[:2], "big")
            text = payload[2:].decode("utf-8", errors="replace")
            name = cls._WS_CLOSE_REASONS.get(code)
            return code, f"{text} ({name})" if name else text
        except Exception:
            return None, ""

    async def _read_frame(self) -> Tuple[int, bytes, bool]:
        header = await self.reader.readexactly(2)
        fin = bool(header[0] & WS_FIN_BIT)
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        if length == 126:
            length = _UNPACK_H.unpack(await self.reader.readexactly(2))[0]
        elif length == 127:
            length = _UNPACK_Q.unpack(await self.reader.readexactly(8))[0]
        if length > MAX_MESSAGE_LEN:
            raise ConnectionError(f"WS frame too large: {length} bytes")

        if header[1] & WS_MASK_BIT:
            mask = await self.reader.readexactly(4)
            payload = await self.reader.readexactly(length)
            return opcode, _xor_mask(payload, mask), fin

        payload = await self.reader.readexactly(length)
        return opcode, payload, fin

"""Fallback strategies when the primary WSS path to Telegram is down.

Order of attempts is decided here based on what is enabled in config:
  1. Cloudflare Worker (if `cfproxy_worker_domain` set)
  2. Cloudflare proxied domain pool (if `cfproxy` enabled)
  3. Direct TCP to the DC default IP on :443

The first method that returns True wins. If everything fails, the client
is dropped.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Optional, Set, Tuple
from urllib.parse import urlencode

from .balancer import balancer
from .bridge import MessageSplitter, bridge_tcp, bridge_ws
from .constants import DC_DEFAULT_IPS, DC_TEST_IPS
from .crypto import ReencryptionContext
from .stats import Stats
from .websocket import RawWebSocket
from .ws_pool import cf_worker_pool

log = logging.getLogger("tgwsproxy.fallback")

# Exponential backoff for direct TCP fallback — a downed fallback IP is
# expensive to retry on every connection, so we suppress attempts for a while.
TCP_BACKOFF_INITIAL = 30.0
TCP_BACKOFF_MAX = 3600.0


class _TcpBackoff:
    """Tracks TCP-fallback failures so we don't hammer a dead endpoint."""

    def __init__(self) -> None:
        self._failures: Dict[Tuple[str, int], int] = {}
        self._retry_after: Dict[Tuple[str, int], float] = {}
        self._connecting: Set[Tuple[str, int]] = set()

    def reset(self) -> None:
        self._failures.clear()
        self._retry_after.clear()
        self._connecting.clear()

    def is_blocked(self, key: Tuple[str, int]) -> bool:
        return key in self._connecting or time.monotonic() < self._retry_after.get(
            key, 0
        )

    def begin(self, key: Tuple[str, int]) -> None:
        self._connecting.add(key)

    def end(self, key: Tuple[str, int]) -> None:
        self._connecting.discard(key)

    def report_success(self, key: Tuple[str, int]) -> None:
        self._failures.pop(key, None)
        self._retry_after.pop(key, None)

    def report_failure(self, key: Tuple[str, int]) -> float:
        failures = self._failures.get(key, 0) + 1
        self._failures[key] = failures
        delay = min(
            TCP_BACKOFF_INITIAL * 2 ** min(failures - 1, 7), TCP_BACKOFF_MAX
        )
        self._retry_after[key] = time.monotonic() + delay
        return delay


tcp_backoff = _TcpBackoff()


class FallbackConfig:
    """Bundles the few flags that influence fallback selection."""

    __slots__ = ("cfproxy_enabled", "cfproxy_worker_domain")

    def __init__(self, cfproxy_enabled: bool, cfproxy_worker_domain: str):
        self.cfproxy_enabled = cfproxy_enabled
        self.cfproxy_worker_domain = cfproxy_worker_domain


async def attempt_fallback(
    client_reader,
    client_writer,
    relay_init: bytes,
    label: str,
    dc: int,
    is_media: bool,
    ctx: ReencryptionContext,
    stats: Stats,
    cfg: FallbackConfig,
    splitter: Optional[MessageSplitter] = None,
    is_test_dc: bool = False,
) -> bool:
    """Try each enabled fallback in order. Returns True if one took over."""
    ip_table = DC_TEST_IPS if is_test_dc else DC_DEFAULT_IPS
    target_ip = ip_table.get(dc)
    media_tag = " media" if is_media else ""

    if cfg.cfproxy_worker_domain and target_ip and not is_test_dc:
        if await _cfworker(
            client_reader,
            client_writer,
            relay_init,
            label,
            dc,
            is_media,
            target_ip,
            ctx,
            stats,
            cfg,
            splitter,
        ):
            return True

    if cfg.cfproxy_enabled and not is_test_dc:
        if await _cfproxy(
            client_reader,
            client_writer,
            relay_init,
            label,
            dc,
            is_media,
            ctx,
            stats,
            splitter,
        ):
            return True

    if target_ip:
        if await _tcp(
            client_reader,
            client_writer,
            target_ip,
            relay_init,
            label,
            dc,
            media_tag,
            ctx,
            stats,
        ):
            return True

    return False


async def _cfworker(
    client_reader,
    client_writer,
    relay_init,
    label,
    dc,
    is_media,
    target_ip,
    ctx,
    stats,
    cfg,
    splitter,
) -> bool:
    media_tag = " media" if is_media else ""
    worker_domains = [cfg.cfproxy_worker_domain]

    pooled = await cf_worker_pool.acquire(dc, target_ip, worker_domains)
    if pooled is not None:
        ws, worker_domain = pooled
        log.info("[%s] DC%d%s -> CF worker pool hit via %s for %s",
                 label, dc, media_tag, worker_domain, target_ip)
    else:
        query = urlencode({"dst": target_ip, "dc": str(dc)})
        path = f"/apiws?{query}"
        ws = None
        for worker_domain in cf_worker_pool.available_domains(worker_domains):
            log.info("[%s] DC%d%s -> CF worker %s for %s",
                     label, dc, media_tag, worker_domain, target_ip)
            try:
                ws = await RawWebSocket.connect(
                    worker_domain, worker_domain, timeout=10.0, path=path
                )
                break
            except Exception as exc:
                cf_worker_pool.report_failure(worker_domain, exc)
                log.warning("[%s] DC%d%s CF worker %s failed: %r",
                            label, dc, media_tag, worker_domain, exc)
                continue
        if ws is None:
            return False

    stats.connections_cfproxy += 1
    await ws.send(relay_init)
    await bridge_ws(
        client_reader, client_writer, ws, label, ctx, stats, dc, is_media,
        splitter=splitter,
    )
    return True


async def _cfproxy(
    client_reader,
    client_writer,
    relay_init,
    label,
    dc,
    is_media,
    ctx,
    stats,
    splitter,
) -> bool:
    media_tag = " media" if is_media else ""
    log.info("[%s] DC%d%s -> CF proxy pool", label, dc, media_tag)

    ws = None
    chosen = None
    for base in balancer.candidates_for(dc):
        domain = f"kws{dc}.{base}"
        try:
            ws = await RawWebSocket.connect(domain, domain, timeout=10.0)
            chosen = base
            break
        except Exception as exc:
            log.warning("[%s] DC%d%s CF %s failed: %r",
                        label, dc, media_tag, base, exc)

    if ws is None:
        return False

    if chosen and balancer.promote(dc, chosen):
        log.info("[%s] active CF domain for DC%d -> %s", label, dc, chosen)

    stats.connections_cfproxy += 1
    await ws.send(relay_init)
    await bridge_ws(
        client_reader, client_writer, ws, label, ctx, stats, dc, is_media,
        splitter=splitter,
    )
    return True


async def _tcp(
    client_reader,
    client_writer,
    dst,
    relay_init,
    label,
    dc,
    media_tag,
    ctx,
    stats,
) -> bool:
    key = (dst, 443)
    if tcp_backoff.is_blocked(key):
        log.debug("[%s] DC%d%s TCP fallback to %s:443 skipped (backoff)",
                  label, dc, media_tag, dst)
        return False

    tcp_backoff.begin(key)
    remote_writer = None
    try:
        try:
            log.info("[%s] DC%d%s -> TCP fallback %s:443",
                     label, dc, media_tag, dst)
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(dst, 443), timeout=10
            )
            remote_writer.write(relay_init)
            await remote_writer.drain()
        except Exception as exc:
            delay = tcp_backoff.report_failure(key)
            log.warning(
                "[%s] TCP fallback %s:443 failed: %r; retry in %.0fs",
                label, dst, exc, delay,
            )
            return False
        finally:
            tcp_backoff.end(key)

        tcp_backoff.report_success(key)
        stats.connections_tcp_fallback += 1
        await bridge_tcp(
            client_reader, client_writer, remote_reader, remote_writer,
            label, ctx, stats,
        )
        return True
    finally:
        if remote_writer is not None:
            try:
                remote_writer.close()
                await remote_writer.wait_closed()
            except (OSError, ConnectionError):
                pass

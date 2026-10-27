"""Top-level TCP server for the MTProto proxy.

Composes the handler, pool and stats objects, exposes a clean `start`/
`stop` interface that the entrypoint and the web UI both use.
"""
from __future__ import annotations

import asyncio
import logging
import socket as _socket
import time
from typing import Optional, Set

from .balancer import balancer
from .config import (
    Config,
    start_cfproxy_domain_refresh,
    stop_cfproxy_domain_refresh,
)
from .constants import DC_DEFAULT_IPS
from .fallback import FallbackConfig, tcp_backoff
from .handler import ClientHandler, HandlerSettings
from .stats import stats
from .ws_pool import WebSocketPool, cf_worker_pool

log = logging.getLogger("tgwsproxy.server")


def _make_h2_pool(config: Config):
    """Create the CF HTTP/2 media pool if enabled and httpx/h2 are present."""
    if not config.h2_enabled:
        return None
    try:
        from .cf_h2 import CfH2Pool
    except ImportError as exc:
        log.warning(
            "HTTP/2 media multiplexing is enabled but httpx/h2 are not "
            "installed (%s); falling back to WS for media", exc,
        )
        return None
    return CfH2Pool()


class ProxyServer:
    def __init__(self, config: Config):
        self._config = config
        self._server: Optional[asyncio.AbstractServer] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._serve_task: Optional[asyncio.Task] = None
        self._stats_task: Optional[asyncio.Task] = None
        self._tasks: Set[asyncio.Task] = set()
        self._handler: Optional[ClientHandler] = None
        self._pool: Optional[WebSocketPool] = None
        self._h2_pool = None
        self._stopping = False

    @property
    def listening(self) -> bool:
        return self._server is not None and self._server.is_serving()

    @property
    def stats(self):
        return stats

    @property
    def pool(self) -> Optional[WebSocketPool]:
        return self._pool

    @property
    def config(self) -> Config:
        return self._config

    async def start(self) -> None:
        if self.listening:
            return
        self._stop_event = asyncio.Event()

        if self._config.cfproxy and not self._config.cfproxy_user_domain:
            balancer.update_domains_list(self._config.cfproxy_default_pool())
            start_cfproxy_domain_refresh(user_overridden=False)
        elif self._config.cfproxy_user_domain:
            balancer.update_domains_list([self._config.cfproxy_user_domain])

        tcp_backoff.reset()
        self._stopping = False

        self._pool = WebSocketPool(
            target_size=self._config.pool_size,
            buffer_size=self._config.buffer_size,
            stats=stats,
        )
        self._h2_pool = _make_h2_pool(self._config)
        self._handler = ClientHandler(
            settings=self._build_handler_settings(),
            pool=self._pool,
            stats=stats,
            h2_pool=self._h2_pool,
        )
        stats.started_at = time.time()

        self._server = await asyncio.start_server(
            self._on_client, self._config.host, self._config.port
        )
        self._set_nodelay()

        log.info("=" * 60)
        log.info("  TG WS Proxy listening on %s:%d",
                 self._config.host, self._config.port)
        log.info("  Secret: %s", self._config.secret)
        log.info("  DC routes:")
        for dc, ip in sorted(self._config.dc_redirects.items()):
            log.info("    DC%d -> %s", dc, ip)
        if self._config.fake_tls_domain:
            log.info("  Fake TLS: %s", self._config.fake_tls_domain)
        if self._config.cfproxy_worker_domain:
            log.info("  CF worker: %s", self._config.cfproxy_worker_domain)
        if self._h2_pool is not None:
            log.info("  CF H2 media: enabled")
        if self._config.disable_secure:
            log.info("  No secure: enabled (port 80 for CF proxy/worker)")
        log.info("=" * 60)

        self._stats_task = asyncio.create_task(self._log_stats_loop())
        await self._pool.warmup(self._config.dc_redirects)

        if self._config.cfproxy_worker_domain and not self._config.force_test_dc:
            cf_fallbacks = {
                dc: ip for dc, ip in DC_DEFAULT_IPS.items()
                if dc not in self._config.dc_redirects
            }
            await cf_worker_pool.warmup(
                cf_fallbacks, [self._config.cfproxy_worker_domain]
            )

        self._serve_task = asyncio.create_task(self._serve_forever())

    async def stop(self) -> None:
        if not self.listening:
            return
        log.info("Stopping proxy server")
        self._stopping = True
        if self._stop_event is not None:
            self._stop_event.set()

        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass

        if self._stats_task is not None:
            self._stats_task.cancel()
            try:
                await self._stats_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stats_task = None

        if self._serve_task is not None:
            self._serve_task.cancel()
            try:
                await self._serve_task
            except (asyncio.CancelledError, Exception):
                pass
            self._serve_task = None

        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except BaseException:
                pass
        self._tasks.clear()

        if self._pool is not None:
            await self._pool.close()
        await cf_worker_pool.close()
        if self._h2_pool is not None:
            await self._h2_pool.close()
            self._h2_pool = None
        tcp_backoff.reset()
        stop_cfproxy_domain_refresh()

        self._server = None
        self._handler = None
        log.info("Proxy server stopped. Stats: %s", stats.summary())

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    def apply_config(self, new_config: Config) -> None:
        """Replace the config snapshot. Caller must `restart()` to apply."""
        self._config = new_config

    async def serve_until_stop(self) -> None:
        """Block until stop() is called or the loop is cancelled."""
        if self._stop_event is None:
            return
        await self._stop_event.wait()

    # --- internals -------------------------------------------------------

    def _build_handler_settings(self) -> HandlerSettings:
        return HandlerSettings(
            secret=bytes.fromhex(self._config.secret),
            dc_redirects=dict(self._config.dc_redirects),
            buffer_size=self._config.buffer_size,
            fake_tls_domain=self._config.fake_tls_domain,
            proxy_protocol=self._config.proxy_protocol,
            force_test_dc=self._config.force_test_dc,
            fallback=FallbackConfig(
                cfproxy_enabled=self._config.cfproxy,
                cfproxy_worker_domain=self._config.cfproxy_worker_domain,
                disable_secure=self._config.disable_secure,
            ),
        )

    def _on_client(self, reader, writer) -> None:
        if self._handler is None or self._stopping:
            try:
                writer.close()
            except Exception:
                pass
            return
        task = asyncio.create_task(self._handler.handle(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _set_nodelay(self) -> None:
        if self._server is None:
            return
        for sock in self._server.sockets or ():
            try:
                sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
            except (OSError, AttributeError):
                pass

    async def _serve_forever(self) -> None:
        try:
            assert self._server is not None
            async with self._server:
                await self._server.serve_forever()
        except asyncio.CancelledError:
            raise
        except RuntimeError as exc:
            # serve_forever() raises on a closed server during shutdown; that
            # is expected, only surface it if we are not stopping.
            if not self._stopping:
                log.error("Server crashed: %s", exc, exc_info=True)
        except Exception as exc:
            log.error("Server crashed: %s", exc, exc_info=True)

    async def _log_stats_loop(self) -> None:
        from .network_debug import log_ws_flow

        next_stats = time.monotonic() + 60
        try:
            while True:
                await asyncio.sleep(5)
                now = time.monotonic()
                if logging.getLogger("tgwsproxy").isEnabledFor(logging.DEBUG):
                    log_ws_flow(now)
                    if self._h2_pool is not None:
                        self._h2_pool.log_flow(now)
                if now < next_stats:
                    continue
                next_stats = now + 60
                log.info("stats: %s", stats.summary())
                if self._h2_pool is not None:
                    self._h2_pool.log_stats()
        except asyncio.CancelledError:
            raise

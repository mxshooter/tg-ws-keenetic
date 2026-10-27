"""Hot WebSocket connection pool, keyed by (DC, is_media).

Telegram clients open lots of short-lived MTProto sessions; opening a WSS
connection for each one is the slowest part of the path. We pre-open a
small pool per (DC, is_media) bucket and lend connections out as needed.

Two pools live here:
  * `WebSocketPool` — native kws*.web.telegram.org endpoints, with optional
    SNI fronting as a fallback when the direct handshake is blocked.
  * `CfWorkerPool` — Cloudflare Worker endpoints, one hot connection per DC.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Set, Tuple

from .stats import Stats, stats
from .websocket import RawWebSocket, WsHandshakeError

log = logging.getLogger("tgwsproxy.ws_pool")

PoolKey = Tuple[int, bool]


def ws_domains_for(dc: int, is_media: Optional[bool]) -> List[str]:
    """Native Telegram WS endpoints. Media variant is preferred for media DCs."""
    if dc == 203:
        dc = 2
    if not is_media:
        return [f"kws{dc}.web.telegram.org"]
    return [
        f"kws{dc}-1.web.telegram.org",
        f"kws{dc}.web.telegram.org",
    ]


class WebSocketPool:
    """Idle-keep-alive pool with background refill and rotation."""

    MAX_AGE_SECONDS = 120.0
    CHECK_INTERVAL = 5.0
    REFILL_BACKOFF_INITIAL = 1.0
    REFILL_BACKOFF_MAX = 3600.0
    FRONTING_SNI = "sprinthost.ru"

    def __init__(self, target_size: int, buffer_size: int, stats: Stats):
        self._target = max(0, target_size)
        self._buffer = buffer_size
        self._stats = stats
        self._idle: Dict[PoolKey, Deque[Tuple[RawWebSocket, float]]] = {}
        self._refilling: Dict[PoolKey, asyncio.Task] = {}
        self._rotating: Dict[PoolKey, asyncio.Task] = {}
        self._refill_failures: Dict[PoolKey, int] = {}
        self._refill_after: Dict[PoolKey, float] = {}
        self._try_fronting_first = False

    @property
    def target_size(self) -> int:
        return self._target

    def set_target(self, size: int) -> None:
        self._target = max(0, size)

    async def acquire(
        self, dc: int, is_media: bool, target_ip: str, domains: List[str]
    ) -> Optional[RawWebSocket]:
        key = (dc, is_media)
        bucket = self._idle.setdefault(key, deque())
        now = time.monotonic()

        while bucket:
            ws, created = bucket.popleft()
            if self._is_stale(ws, created, now):
                asyncio.create_task(self._quiet_close(ws))
                continue
            self._stats.pool_hits += 1
            self._schedule_refill(key, target_ip, domains)
            return ws

        self._stats.pool_misses += 1
        self._schedule_refill(key, target_ip, domains)
        return None

    def _is_stale(self, ws: RawWebSocket, created: float, now: float) -> bool:
        return now - created >= self.MAX_AGE_SECONDS or ws.stale

    async def warmup(self, dc_to_ip: Dict[int, str]) -> None:
        for dc, target_ip in dc_to_ip.items():
            if target_ip is None:
                continue
            for is_media in (False, True):
                self._schedule_refill(
                    (dc, is_media), target_ip, ws_domains_for(dc, is_media)
                )
        log.info("WS pool warmup started for %d DC(s)", len(dc_to_ip))

    def reset(self) -> None:
        loop = asyncio.get_running_loop()
        for task in list(self._rotating.values()) + list(self._refilling.values()):
            if not task.done() and task.get_loop() is loop:
                task.cancel()
        for bucket in self._idle.values():
            for ws, _ in bucket:
                try:
                    ws.writer.close()
                except Exception:
                    pass
        self._idle.clear()
        self._refilling.clear()
        self._rotating.clear()
        self._refill_failures.clear()
        self._refill_after.clear()
        self._try_fronting_first = False

    def _schedule_refill(
        self, key: PoolKey, target_ip: str, domains: List[str]
    ) -> None:
        if self._target <= 0:
            return
        self._schedule_rotation(key, target_ip, domains)
        if (
            key in self._refilling
            or time.monotonic() < self._refill_after.get(key, 0)
        ):
            return
        self._refilling[key] = asyncio.create_task(
            self._refill(key, target_ip, domains)
        )

    async def _refill(
        self, key: PoolKey, target_ip: str, domains: List[str]
    ) -> None:
        dc, is_media = key
        tasks: List[asyncio.Task] = []
        adopted: Set[RawWebSocket] = set()
        try:
            bucket = self._idle.setdefault(key, deque())
            needed = self._target - len(bucket)
            if needed <= 0:
                return
            connected = 0
            tasks = [
                asyncio.create_task(self._connect_one(target_ip, domains))
                for _ in range(needed)
            ]
            for task in asyncio.as_completed(tasks):
                try:
                    ws = await task
                except Exception:
                    continue
                if ws is None:
                    continue
                if self._refilling.get(key) is not asyncio.current_task():
                    return
                bucket.append((ws, time.monotonic()))
                adopted.add(ws)
                connected += 1
            if connected:
                self._refill_failures.pop(key, None)
                self._refill_after.pop(key, None)
            else:
                failures = self._refill_failures.get(key, 0) + 1
                self._refill_failures[key] = failures
                delay = min(
                    self.REFILL_BACKOFF_INITIAL
                    * (2 ** min(failures - 1, 12)),
                    self.REFILL_BACKOFF_MAX,
                )
                self._refill_after[key] = time.monotonic() + delay
                log.info(
                    "WS pool refill failed for DC%d%s, retry in %.0fs",
                    dc, "m" if is_media else "", delay,
                )
            log.debug(
                "WS pool refilled DC%d%s -> %d ready",
                dc, "m" if is_media else "", len(bucket),
            )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for ws in results:
                if (
                    ws is not None
                    and not isinstance(ws, BaseException)
                    and ws not in adopted
                ):
                    await self._quiet_close(ws)
            if self._refilling.get(key) is asyncio.current_task():
                self._refilling.pop(key, None)

    def _schedule_rotation(
        self, key: PoolKey, target_ip: str, domains: List[str]
    ) -> None:
        if key in self._rotating or self._target <= 0:
            return
        self._rotating[key] = asyncio.create_task(
            self._rotate(key, target_ip, domains)
        )

    async def _rotate(
        self, key: PoolKey, target_ip: str, domains: List[str]
    ) -> None:
        dc, is_media = key
        try:
            while self._target > 0:
                bucket = self._idle.setdefault(key, deque())
                now = time.monotonic()
                expired: List[RawWebSocket] = []
                ready: Deque[Tuple[RawWebSocket, float]] = deque()
                while bucket:
                    ws, created = bucket.popleft()
                    if self._is_stale(ws, created, now):
                        expired.append(ws)
                    else:
                        ready.append((ws, created))
                bucket.extend(ready)

                if expired:
                    for ws in expired:
                        asyncio.create_task(self._quiet_close(ws))
                    log.debug(
                        "WS pool rotated DC%d%s: %d stale, %d ready",
                        dc, "m" if is_media else "", len(expired), len(bucket),
                    )

                if len(bucket) < self._target:
                    self._schedule_refill(key, target_ip, domains)

                wake_at = now + self.CHECK_INTERVAL
                if bucket:
                    wake_at = min(
                        wake_at,
                        min(created + self.MAX_AGE_SECONDS for _, created in bucket),
                    )
                if (
                    key not in self._refilling
                    and self._refill_after.get(key, 0) > now
                ):
                    wake_at = min(wake_at, self._refill_after[key])
                await asyncio.sleep(max(0, wake_at - time.monotonic()))
        finally:
            if self._rotating.get(key) is asyncio.current_task():
                self._rotating.pop(key, None)

    async def _connect_one(
        self, target_ip: str, domains: List[str]
    ) -> Optional[RawWebSocket]:
        for domain in domains:
            modes = (True, False) if self._try_fronting_first else (False, True)
            for fronted in modes:
                try:
                    ws = await RawWebSocket.connect(
                        target_ip,
                        domain,
                        timeout=7 if fronted else 8,
                        buffer_size=self._buffer,
                        sni=self.FRONTING_SNI if fronted else None,
                    )
                except Exception as exc:
                    self._stats.ws_errors += 1
                    log.debug(
                        "WS pool connect %s via %s (fronting=%s): %r",
                        domain, target_ip, fronted, exc,
                    )
                    continue
                self._try_fronting_first = fronted
                if fronted:
                    self._stats.connections_fronting += 1
                log.debug(
                    "WS pool connected %s via %s (fronting=%s)",
                    domain, target_ip, fronted,
                )
                return ws
        return None

    @staticmethod
    async def _quiet_close(ws: RawWebSocket) -> None:
        try:
            await ws.close()
        except Exception:
            pass


class CfWorkerPool:
    """Idle-keep-alive pool for Cloudflare Worker endpoints (one per DC)."""

    MAX_AGE_SECONDS = 100.0
    PER_DC_LIMIT = 1

    def __init__(self, stats: Stats = stats):
        self._stats = stats
        self._idle: Dict[int, Deque[Tuple[RawWebSocket, float, str]]] = {}
        self._refilling: Dict[int, asyncio.Task] = {}
        self._exhausted_until: Dict[str, float] = {}

    async def acquire(
        self, dc: int, fallback_dst: str, worker_domains: List[str]
    ) -> Optional[Tuple[RawWebSocket, str]]:
        now = time.monotonic()
        bucket = self._idle.setdefault(dc, deque())
        while bucket:
            ws, created, worker_domain = bucket.popleft()
            if now - created > self.MAX_AGE_SECONDS or ws.stale:
                asyncio.create_task(self._quiet_close(ws))
                continue
            self._stats.cf_pool_hits += 1
            self._schedule_refill(dc, fallback_dst, worker_domains)
            return ws, worker_domain

        self._stats.cf_pool_misses += 1
        return None

    async def warmup(
        self, dc_to_fallback: Dict[int, str], worker_domains: List[str]
    ) -> None:
        if not worker_domains:
            return
        domains = list(worker_domains)
        for dc, fallback_dst in dc_to_fallback.items():
            self._schedule_refill(dc, fallback_dst, domains)
        log.info("CF worker pool warmup started for %d DC(s)", len(dc_to_fallback))

    def reset(self) -> None:
        loop = asyncio.get_running_loop()
        for task in list(self._refilling.values()):
            if not task.done() and task.get_loop() is loop:
                task.cancel()
        for bucket in self._idle.values():
            for ws, _, _ in bucket:
                try:
                    ws.writer.close()
                except Exception:
                    pass
        self._idle.clear()
        self._refilling.clear()
        self._exhausted_until.clear()

    def _schedule_refill(
        self, dc: int, fallback_dst: str, worker_domains: List[str]
    ) -> None:
        if dc in self._refilling:
            return
        self._refilling[dc] = asyncio.create_task(
            self._refill(dc, fallback_dst, list(worker_domains))
        )

    async def _refill(
        self, dc: int, fallback_dst: str, worker_domains: List[str]
    ) -> None:
        try:
            bucket = self._idle.setdefault(dc, deque())
            needed = self.PER_DC_LIMIT - len(bucket)
            if needed <= 0:
                return
            for _ in range(needed):
                connected = await self._connect_one(
                    worker_domains, fallback_dst, dc
                )
                if connected is None:
                    break
                ws, worker_domain = connected
                if self._refilling.get(dc) is not asyncio.current_task():
                    await self._quiet_close(ws)
                    return
                bucket.append((ws, time.monotonic(), worker_domain))
            log.debug("CF worker pool refilled DC%d: %d ready", dc, len(bucket))
        finally:
            if self._refilling.get(dc) is asyncio.current_task():
                self._refilling.pop(dc, None)

    async def _connect_one(
        self, worker_domains: List[str], fallback_dst: str, dc: int
    ) -> Optional[Tuple[RawWebSocket, str]]:
        from urllib.parse import urlencode

        path = f"/apiws?{urlencode({'dst': fallback_dst, 'dc': str(dc)})}"
        for worker_domain in self.available_domains(worker_domains):
            try:
                ws = await RawWebSocket.connect(
                    worker_domain, worker_domain, timeout=8, path=path
                )
                return ws, worker_domain
            except Exception as exc:
                self.report_failure(worker_domain, exc)
        return None

    def available_domains(self, worker_domains: List[str]) -> List[str]:
        now = time.time()
        domains: List[str] = []
        for domain in worker_domains:
            if domain in domains:
                continue
            exhausted_until = self._exhausted_until.get(domain, 0)
            if exhausted_until > now:
                continue
            if exhausted_until:
                self._exhausted_until.pop(domain, None)
            domains.append(domain)
        random.shuffle(domains)
        return domains

    def report_failure(self, worker_domain: str, exc: Exception) -> None:
        if not isinstance(exc, WsHandshakeError) or exc.status_code != 429:
            return
        now = time.time()
        if self._exhausted_until.get(worker_domain, 0) > now:
            return
        exhausted_until = now + (86400 - (now % 86400))
        self._exhausted_until[worker_domain] = exhausted_until
        log.warning(
            "CF worker %s reached its request limit, disabled for %d seconds",
            worker_domain, int(exhausted_until - now),
        )

    @staticmethod
    async def _quiet_close(ws: RawWebSocket) -> None:
        try:
            await ws.close()
        except Exception:
            pass


cf_worker_pool = CfWorkerPool()

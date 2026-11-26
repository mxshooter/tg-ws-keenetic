"""CF-proxied front domains: connect over TLS or plain HTTP.

Cloudflare proxied domains (`kws{dc}.<domain>`) accept WebSocket on both
443 and 80. When 443 is throttled or blocked, a TLS attempt times out, so
the next attempt for the same domain uses plain HTTP. The mode that worked
last is tried first for the following clients, and a domain that timed out
is skipped for a while in that mode only.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

from .websocket import RawWebSocket

log = logging.getLogger("tgwsproxy.cf_websocket")

# Hosts that timed out are skipped for a while: when the whole pool is
# blocked, every client would otherwise walk the same dead hosts.
CF_TIMEOUT_COOLDOWN = 30.0
CF_CONNECT_TIMEOUTS = (3.0, 3.0, 5.0, 5.0, 10.0)
_cf_timeout_until: Dict[Tuple[str, bool], float] = {}


@dataclass
class CfConStats:
    """Successful CF connections per mode, used to pick the next one."""

    tls: int = 0
    no_tls: int = 0


_cf_connection_stats = CfConStats()


def reset_cf_timeouts() -> None:
    """Forget CF host cooldowns and mode statistics (proxy restart)."""
    _cf_timeout_until.clear()
    _cf_connection_stats.no_tls = 0
    _cf_connection_stats.tls = 0


def cf_prefers_tls() -> bool:
    """True while TLS works at least as often as plain HTTP."""
    return _cf_connection_stats.tls >= _cf_connection_stats.no_tls


def cf_tls_in_cooldown(name: str) -> bool:
    """True if `name` timed out over TLS recently (HTTP may still work)."""
    retry_at = _cf_timeout_until.get((name, True))
    return retry_at is not None and time.monotonic() < retry_at


async def connect_cf_domains(
    candidates: Iterable[Tuple[str, str]],
    *,
    label: str,
    secure: bool,
    path: str = "/apiws",
    timeout: float = 10.0,
) -> Optional[Tuple[RawWebSocket, str]]:
    """Dial CF front domains, alternating TLS and HTTP attempts.

    A timeout switches the next attempt to the other mode; five timeouts end
    the search, so one client cannot spend minutes walking a dead pool. The
    domain and the mode that timed out are skipped for
    ``CF_TIMEOUT_COOLDOWN`` by later clients.
    """
    domains = list(candidates)
    retry_single_domain = secure and len(domains) == 1
    if retry_single_domain:
        domains.append(domains[0])

    timeouts = 0
    tls_first = cf_prefers_tls()

    for index, (name, host) in enumerate(domains):
        use_tls = secure and (tls_first != bool(timeouts % 2))
        if retry_single_domain and index == 1 and timeouts == 0:
            # The first try was skipped because of _cf_timeout_until.
            use_tls = not tls_first

        key = (name, use_tls)
        retry_at = _cf_timeout_until.get(key)
        if retry_at is not None:
            if time.monotonic() < retry_at:
                continue
            # Mark the retry as in progress; finally restores it if needed.
            _cf_timeout_until[key] = math.inf

        ws: Optional[RawWebSocket] = None
        try:
            attempt_timeout = min(timeout, CF_CONNECT_TIMEOUTS[timeouts])
            ws = await RawWebSocket.connect(
                host, host, path=path, secure=use_tls, timeout=attempt_timeout
            )
        except (asyncio.TimeoutError, TimeoutError):
            now = time.monotonic()
            if retry_at is not None or key not in _cf_timeout_until:
                for old_key, until in list(_cf_timeout_until.items()):
                    if until + CF_TIMEOUT_COOLDOWN <= now:
                        del _cf_timeout_until[old_key]
                _cf_timeout_until[key] = now + CF_TIMEOUT_COOLDOWN
                log.warning(
                    "[%s] CF WebSocket timed out (host=%s tls=%s); "
                    "retry in %.0fs",
                    label, host, use_tls, CF_TIMEOUT_COOLDOWN,
                )
            timeouts += 1
            if timeouts >= len(CF_CONNECT_TIMEOUTS):
                break
            continue
        except Exception as exc:
            log.warning(
                "[%s] CF WebSocket failed (host=%s tls=%s): %r",
                label, host, use_tls, exc,
            )
        finally:
            if retry_at is not None and _cf_timeout_until.get(key) == math.inf:
                _cf_timeout_until[key] = retry_at

        _cf_timeout_until.pop(key, None)

        if ws is not None:
            if use_tls:
                _cf_connection_stats.tls += 1
            else:
                _cf_connection_stats.no_tls += 1
            return ws, name

        if retry_single_domain:
            break

    return None

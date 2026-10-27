"""Lightweight in-process counters for the proxy."""
from __future__ import annotations

from dataclasses import dataclass, field


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


@dataclass
class Stats:
    connections_total: int = 0
    connections_active: int = 0
    connections_ws: int = 0
    connections_h2: int = 0
    h2_tcp_connections: int = 0
    h2_requests: int = 0
    h2_errors: int = 0
    connections_tcp_fallback: int = 0
    connections_cfproxy: int = 0
    connections_fronting: int = 0
    connections_bad: int = 0
    connections_masked: int = 0
    ws_errors: int = 0
    bytes_up: int = 0
    bytes_down: int = 0
    pool_hits: int = 0
    pool_misses: int = 0
    cf_pool_hits: int = 0
    cf_pool_misses: int = 0
    started_at: float = field(default=0.0)

    def summary(self) -> str:
        pool = self.pool_hits + self.pool_misses
        pool_s = f"{self.pool_hits}/{pool}" if pool else "n/a"
        cf_pool = self.cf_pool_hits + self.cf_pool_misses
        cf_pool_s = f"{self.cf_pool_hits}/{cf_pool}" if cf_pool else "n/a"
        return (
            f"total={self.connections_total} "
            f"active={self.connections_active} "
            f"ws={self.connections_ws} "
            f"h2={self.connections_h2} "
            f"tcp_fb={self.connections_tcp_fallback} "
            f"cf={self.connections_cfproxy} "
            f"front={self.connections_fronting} "
            f"bad={self.connections_bad} "
            f"masked={self.connections_masked} "
            f"err={self.ws_errors} "
            f"pool={pool_s} "
            f"cf_pool={cf_pool_s} "
            f"up={human_bytes(self.bytes_up)} "
            f"down={human_bytes(self.bytes_down)}"
        )

    def as_dict(self) -> dict:
        return {
            "connections": {
                "total": self.connections_total,
                "active": self.connections_active,
                "ws": self.connections_ws,
                "h2": self.connections_h2,
                "tcp_fallback": self.connections_tcp_fallback,
                "cfproxy": self.connections_cfproxy,
                "fronting": self.connections_fronting,
                "bad": self.connections_bad,
                "masked": self.connections_masked,
            },
            "traffic": {
                "bytes_up": self.bytes_up,
                "bytes_down": self.bytes_down,
                "human_up": human_bytes(self.bytes_up),
                "human_down": human_bytes(self.bytes_down),
            },
            "ws": {
                "errors": self.ws_errors,
                "pool_hits": self.pool_hits,
                "pool_misses": self.pool_misses,
                "cf_pool_hits": self.cf_pool_hits,
                "cf_pool_misses": self.cf_pool_misses,
            },
            "h2": {
                "tcp_connections": self.h2_tcp_connections,
                "requests": self.h2_requests,
                "errors": self.h2_errors,
            },
            "started_at": self.started_at,
        }


stats = Stats()

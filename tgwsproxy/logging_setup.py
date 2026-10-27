"""Logging configuration for both daemon and CLI invocations."""
from __future__ import annotations

import collections
import logging
import logging.handlers
import os
import re
import threading
import uuid
from typing import List, Optional, Tuple


# How many formatted log lines to keep in memory for the web UI. Bounded so a
# long-running service can't grow without limit on a memory-constrained router.
LOG_BUFFER_LINES = 2000


class DomainCensorFilter(logging.Filter):
    """Mask CF fallback domains in logs while leaving telegram.org intact."""

    _domain_pattern = re.compile(
        r"(?<![\w-])(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
        r"[a-zA-Z]{2,}(?![\w-])"
    )

    def _censor_match(self, match: "re.Match") -> str:
        domain = match.group()
        normalized = domain.casefold().rstrip(".")
        if (
            normalized == "telegram.org"
            or normalized.endswith(".telegram.org")
            or normalized.endswith(".log")
        ):
            return domain
        parts = domain.split(".")
        if len(parts) < 2:
            return domain
        return ".".join(
            part if i == len(parts) - 1 else
            part[: len(part) // 2] + "*" * (len(part) - len(part) // 2)
            for i, part in enumerate(parts)
        )

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._domain_pattern.sub(
            self._censor_match, record.getMessage()
        )
        record.args = ()
        return True


class RingLogHandler(logging.Handler):
    """Keeps recent formatted log lines in memory for the web UI.

    Records are numbered with a monotonically increasing sequence so the UI can
    fetch only what it has not seen yet (`snapshot(since=...)`). A fresh
    `session` id is generated per process, letting the UI detect a restart and
    reset its view. Thread-safe: the CF-domain refresh runs on its own thread.
    """

    def __init__(self, capacity: int = LOG_BUFFER_LINES) -> None:
        super().__init__()
        self._buf: "collections.deque[Tuple[int, str]]" = collections.deque(
            maxlen=max(1, capacity)
        )
        self._seq = 0
        self._lock = threading.Lock()
        self.session = uuid.uuid4().hex

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            self.handleError(record)
            return
        with self._lock:
            self._seq += 1
            self._buf.append((self._seq, line))

    def snapshot(self, since: int = 0) -> Tuple[int, List[str]]:
        """Return (current_seq, lines) with sequence numbers strictly > since."""
        with self._lock:
            return self._seq, [line for seq, line in self._buf if seq > since]


# Set by `configure_logging`; read by the web UI. None until then.
_log_buffer: Optional[RingLogHandler] = None


def get_log_buffer() -> Optional[RingLogHandler]:
    return _log_buffer


def configure_logging(
    log_file: Optional[str],
    max_mb: float,
    backups: int,
    verbose: bool,
) -> None:
    """Reset root logging to console + optional file + in-memory ring."""
    global _log_buffer
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level)

    censor = DomainCensorFilter()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(censor)
    root.addHandler(console)

    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=max(32 * 1024, int(max_mb * 1024 * 1024)),
                backupCount=max(0, backups),
                encoding="utf-8",
            )
            file_handler.setFormatter(fmt)
            file_handler.addFilter(censor)
            root.addHandler(file_handler)
        except OSError as exc:
            root.warning("Cannot open log file %s: %s", log_file, exc)

    # In-memory buffer powering the web UI's log viewer.
    _log_buffer = RingLogHandler()
    _log_buffer.setFormatter(fmt)
    _log_buffer.addFilter(censor)
    root.addHandler(_log_buffer)

    # Quiet down chatty asyncio.
    logging.getLogger("asyncio").setLevel(logging.WARNING)


"""A per-key lock registry: concurrent callers holding the same key
serialize; callers on different keys never block each other.

Used to serialize a provider token refresh per site — without it, two
concurrent refreshes for the same connection both read the same
single-use refresh token, and only one of them actually works (the
other gets a confusing "reconnect" error even though the account is
fine, see ventoryn/saas.py's fresh_x_access_token and
ventoryn/tiktok.py's fresh_access_token).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager


class KeyedLock:
    """One threading.Lock per key, created on first use. The registry
    never shrinks — fine for a small, bounded key space like site ids,
    not for unbounded or attacker-controlled keys."""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @contextmanager
    def __call__(self, key: str):
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            yield

"""Process-wide usage counter (calls / tokens / usd per service) written into metrics.json (NFR-E4)."""
from __future__ import annotations

import threading
from collections import defaultdict

_lock = threading.Lock()
_usage: dict[str, dict[str, float]] = defaultdict(lambda: {"calls": 0, "tokens": 0, "usd": 0.0})


def add(service: str, calls: int = 1, tokens: int = 0, usd: float = 0.0) -> None:
    with _lock:
        u = _usage[service]
        u["calls"] += calls
        u["tokens"] += tokens
        u["usd"] += usd


def snapshot() -> dict[str, dict[str, float]]:
    with _lock:
        return {k: dict(v) for k, v in _usage.items()}

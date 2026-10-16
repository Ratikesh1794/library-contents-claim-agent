"""On-disk response cache keyed by request hash, so a sweep can be re-run offline and for free.
Failures are never cached. `fetched_at` is returned so prices can record a true retrieval time."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

from backend.config import CFG


def cached(namespace: str, key_parts: list, fn: Callable[[], Any], cache_dir: Path | None = None):
    """Return (value, fetched_at_iso). `fn` must return something JSON-serialisable."""
    key = hashlib.sha256(json.dumps(key_parts, sort_keys=True, default=str).encode()).hexdigest()
    p = Path(cache_dir or CFG.cache_dir) / namespace / f"{key}.json"
    if p.exists():
        d = json.loads(p.read_text())
        return d["value"], d["at"]
    value = fn()
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if value is not None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"value": value, "at": at}))
    return value, at

"""Cached JSON GET/POST for adapters. Errors raise; callers decide how to degrade."""
from __future__ import annotations

import requests

from backend.core.cache import cached
from backend.config import CFG


def get_json(ns: str, url: str, params: dict | None = None, headers: dict | None = None,
             key_extra: list | None = None):
    """-> (json, fetched_at). Cache key ignores headers (they hold secrets)."""
    def call():
        r = requests.get(url, params=params, headers=headers, timeout=CFG.http_timeout_s)
        r.raise_for_status()
        return r.json()
    return cached(ns, [url, params, key_extra], call)

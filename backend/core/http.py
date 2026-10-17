"""Cached JSON GET/POST for adapters. Errors raise; callers decide how to degrade."""
from __future__ import annotations

import re

import requests

from backend.core.cache import cached
from backend.config import CFG


_SECRET = re.compile(r"([?&](?:key|api_key|apikey|token|access_token|client_secret)=)[^&\s'\")]+", re.I)


def scrub(text: str) -> str:
    """Hide secrets in a message: requests puts the full URL, query string included, in its error text, and error text
    ends up in logs and on screen (an API key was printed that way)."""
    return _SECRET.sub(r"\1***", text)


def get_json(ns: str, url: str, params: dict | None = None, headers: dict | None = None,
             key_extra: list | None = None, timeout: float | None = None):
    """-> (json, fetched_at). Cache key ignores headers (they hold secrets)."""
    def call():
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout or CFG.http_timeout_s)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            raise RuntimeError(scrub(str(e))) from None           # `from None`: the chained original carries the URL too
    return cached(ns, [url, params, key_extra], call)


def post_json(ns: str, url: str, body: dict, headers: dict | None = None, key_extra: list | None = None):
    """-> (json, fetched_at). Like get_json: cached (the key ignores headers, which hold secrets), errors scrubbed."""
    def call():
        try:
            r = requests.post(url, json=body, headers=headers, timeout=CFG.http_timeout_s)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            raise RuntimeError(scrub(str(e))) from None
    return cached(ns, [url, body, key_extra], call)

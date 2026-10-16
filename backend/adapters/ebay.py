"""eBay Browse API as the price source for BOTH new (replacement) and used listings.
Needs EBAY_CLIENT_ID / EBAY_CLIENT_SECRET; without them every search returns [] and prices stay
blank (honest) instead of crashing.

TO UPGRADE: add a shopping-search or retailer-API class with the same three methods (ports.PriceSource)
and combine sources in deps.price_source(). Condition "new" listings on a marketplace are not
a retailer allow-list; a retailer API is the better replacement-cost source.
"""
from __future__ import annotations

import base64
import os
import time

import requests

from backend.core import meter
from backend.core.cache import cached
from backend.config import CFG
from backend.ports import Listing

# country -> eBay marketplace id. Countries without an eBay marketplace fall back (and get FX-converted).
MARKETPLACES = {"US": "EBAY_US", "GB": "EBAY_GB", "UK": "EBAY_GB", "DE": "EBAY_DE", "FR": "EBAY_FR",
                "IT": "EBAY_IT", "ES": "EBAY_ES", "CA": "EBAY_CA", "AU": "EBAY_AU"}
BOOKS_CATEGORY = "267"
API = "https://api.ebay.com"


class Ebay:
    def __init__(self):
        self._token: tuple[str, float] | None = None

    def local_market(self, country: str | None) -> str | None:
        return MARKETPLACES.get((country or "").upper())

    def fallback_markets(self) -> list[str]:
        return ["EBAY_US"]

    def _auth(self) -> str | None:
        cid, sec = os.environ.get("EBAY_CLIENT_ID"), os.environ.get("EBAY_CLIENT_SECRET")
        if not (cid and sec):
            return None
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        r = requests.post(f"{API}/identity/v1/oauth2/token", timeout=CFG.http_timeout_s,
                          headers={"Authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode()},
                          data={"grant_type": "client_credentials", "scope": f"{API}/oauth/api_scope"})
        r.raise_for_status()
        j = r.json()
        self._token = (j["access_token"], time.time() + j["expires_in"])
        return self._token[0]

    def search(self, query: str, market: str, condition: str, books: bool = True) -> list[Listing]:
        tok = self._auth()
        if not tok:
            return []
        params = {"q": query, "limit": 30, "filter": f"conditions:{{{condition.upper()}}},buyingOptions:{{FIXED_PRICE}}"}
        if books:
            params["category_ids"] = BOOKS_CATEGORY

        def call():
            meter.add("price")
            r = requests.get(f"{API}/buy/browse/v1/item_summary/search", params=params, timeout=CFG.http_timeout_s,
                             headers={"Authorization": f"Bearer {tok}", "X-EBAY-C-MARKETPLACE-ID": market})
            r.raise_for_status()
            return r.json()

        data, at = cached("ebay", [query, market, condition, books], call)
        out = []
        for it in data.get("itemSummaries", []):
            p = it.get("price") or {}
            if not p.get("value"):
                continue
            out.append(Listing(title=it.get("title", ""), price=float(p["value"]), currency=p.get("currency", ""),
                               url=it.get("itemWebUrl", ""), retrieved_at=at, source=f"ebay:{market}",
                               condition=condition))
        return out

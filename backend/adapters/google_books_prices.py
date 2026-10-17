"""Google Books store prices as a PriceSource: the only real, retrievable price source that needs no eBay account.

A Google Books volume that is FOR_SALE in a country carries `saleInfo.retailPrice` (amount + currency) and a `buyLink`.
That is a real price with a URL and a retrieval date, so it satisfies "every price has a source". Its limits are stated
rather than hidden:
  * it is an EBOOK price (source says `google_books_ebook`): an equivalent of the book, not the print edition
  * new only: there is no used price here, so used value stays blank (never invented)
  * only books Google sells: others (e.g. most textbooks) get no price and go to the review queue
  * books only: items (chairs, lamps...) are not sold here
Matching is strict: English volumes only (a Kannada edition is not the English book), and the volume's title and FIRST
author are put in the listing title so the price stage's own title check (price._accept_book) rejects summaries,
other books by the same author and other titles.
"""
from __future__ import annotations

import logging
import os

from backend.config import CFG
from backend.core.http import get_json
from backend.ports import Listing

log = logging.getLogger(__name__)
URL = "https://www.googleapis.com/books/v1/volumes"
SOLD = ("FOR_SALE", "FOR_SALE_AND_RENTAL")


class GoogleBooksPrices:
    """Google Books store prices (saleInfo.retailPrice): new ebook prices only, per country, books only."""
    wants_hints = True                                    # the price stage passes {title, author}

    def local_market(self, country: str | None) -> str | None:
        return (country or "").upper() or None            # Google Play Books sells per country: the code is the market

    def fallback_markets(self) -> list[str]:
        return list(CFG.price_fallback_markets)

    def search(self, query: str, market: str, condition: str, books: bool = True, hint: dict | None = None) -> list[Listing]:
        """Union of three query styles (measured: each finds books the others miss, e.g. "The Power of Now" only with the
        paid-ebooks filter and "Shoe Dog" only in plain text). Safe to widen: every listing is checked strictly afterwards
        by the price stage. A failed query (timeout, quota) loses only itself."""
        if condition != "new" or not books:               # no used prices here; items are not sold here
            return []
        tries = [(query, {}), (query, {"filter": "paid-ebooks"})]
        if hint and hint.get("title"):
            tries.append((hint["title"], {"filter": "paid-ebooks"}))
        seen, out = set(), []
        for q, extra in tries:
            try:
                got = self._fetch(q, market, extra)
            except Exception as e:
                log.warning("google books price query failed (%s): %s", q, e)
                continue
            for l in got:
                if (l.url, l.price) not in seen:
                    seen.add((l.url, l.price))
                    out.append(l)
        return out

    def _fetch(self, q: str, market: str, extra: dict) -> list[Listing]:
        params = {"q": q, "country": market, "maxResults": 40, **extra}
        if os.environ.get("GOOGLE_BOOKS_API_KEY"):
            params["key"] = os.environ["GOOGLE_BOOKS_API_KEY"]
        data, at = get_json("googlebooks_price", URL, params, key_extra=["v1", market])
        out = []
        for it in data.get("items", []):
            v, s = it.get("volumeInfo", {}), it.get("saleInfo", {})
            price = s.get("retailPrice") or {}
            if s.get("saleability") not in SOLD or not price.get("amount") or not str(v.get("language", "")).startswith("en"):
                continue
            first_author = (v.get("authors") or [""])[0].strip()
            out.append(Listing(title=f"{v.get('title', '')} {first_author}".strip(), price=float(price["amount"]),
                               currency=price.get("currencyCode", ""), url=s.get("buyLink") or v.get("infoLink", ""),
                               retrieved_at=at, source=f"google_books_ebook:{market}", condition="new"))
        return out

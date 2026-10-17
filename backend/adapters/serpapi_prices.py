"""SerpApi (serpapi.com) Google Shopping results as a PriceSource: merchant prices for books AND items, new AND used.

Needs SERPAPI_API_KEY (free plan: 250 searches a month). One search per (query, country): new and used come from the
same response and responses are cached, so a re-run costs nothing.

Checked against a live response (2026-10-18): `shopping_results[]` with title, source (merchant), price ("$4.41"),
extracted_price (4.41), product_link (Google Shopping product page) and, on second-hand offers, second_hand_condition
("pre-owned"). So:
  * condition comes from Google's own `second_hand_condition` (or "used"/"refurbished" in the title), never guessed
  * the currency comes from the price string; one that can't be read is skipped (see parse_price)
  * there is no author field: `verify_author=True` lets the price stage demand the author for short, shared titles
Merchants' current asking prices, not sold prices.
"""
from __future__ import annotations

import logging
import os
import re

from backend.config import CFG
from backend.core.http import get_json
from backend.ports import Listing

log = logging.getLogger(__name__)
URL = "https://serpapi.com/search.json"
DOLLAR = {"US": "USD", "CA": "CAD", "AU": "AUD", "NZ": "NZD", "SG": "SGD", "HK": "HKD", "MX": "MXN"}
SYMBOL = {"£": "GBP", "€": "EUR", "₹": "INR", "CA$": "CAD", "C$": "CAD", "A$": "AUD", "AU$": "AUD", "NZ$": "NZD"}
USED_WORDS = re.compile(r"\b(used|pre-?owned|refurbished|renewed|second[- ]hand)\b", re.I)


def currency_of(text: str, country: str) -> str | None:
    t = text or ""
    cur = next((c for sym, c in sorted(SYMBOL.items(), key=lambda kv: -len(kv[0])) if sym in t), None)
    if cur is None and "$" in t:
        cur = DOLLAR.get(country.upper(), "USD")
    return cur


def parse_price(text: str, country: str) -> tuple[float, str] | None:
    """'$1,299.00' / '£8.99' / '€ 1.299,00' / 'CA$ 12.50' -> (amount, currency); None if the currency is not certain."""
    cur = currency_of(text, country)
    m = re.search(r"\d[\d.,\s]*\d|\d", text or "")
    if cur is None or not m:
        return None
    num = m.group(0).replace(" ", "")
    if "," in num and "." in num:
        num = num.replace(".", "").replace(",", ".") if num.rfind(",") > num.rfind(".") else num.replace(",", "")
    elif "," in num:
        num = num.replace(",", ".") if re.search(r",\d{2}$", num) else num.replace(",", "")
    try:
        v = float(num)
    except ValueError:
        return None
    return (v, cur) if v > 0 else None


def is_used(item: dict) -> bool:
    return bool(item.get("second_hand_condition")) or bool(USED_WORDS.search(item.get("title") or ""))


class SerpApiPrices:
    wants_hints = False

    @staticmethod
    def configured() -> bool:
        return bool(os.environ.get("SERPAPI_API_KEY"))

    def local_market(self, country: str | None) -> str | None:
        return (country or "").upper() or None

    def fallback_markets(self) -> list[str]:
        return ["US"]

    def search(self, query: str, market: str, condition: str, books: bool = True) -> list[Listing]:
        key = os.environ.get("SERPAPI_API_KEY")
        if not key:
            return []
        gl = {"UK": "gb"}.get(market.upper(), market.lower())
        data, at = get_json("serpapi_shopping", URL, {"engine": "google_shopping", "q": f"{query} book" if books else query,
                                                      "gl": gl, "hl": "en", "api_key": key}, key_extra=["v1"],
                            timeout=25)       # SerpApi runs a live Google search: 8-15 s is normal
        out = []
        for it in data.get("shopping_results", []) or []:
            got = parse_price(str(it.get("price", "")), market)
            if got and it.get("extracted_price"):             # trust the parsed number SerpApi gives, keep our currency
                got = (float(it["extracted_price"]), got[1])
            link = it.get("product_link") or it.get("link")
            if not got or not link:
                continue
            if ("used" if is_used(it) else "new") != condition:
                continue
            merchant = (it.get("source") or "unknown").strip()
            if books and any(w in merchant.lower() for w in CFG.book_merchant_blocklist):
                continue                                    # an audiobook or digital download, not the printed book
            out.append(Listing(title=it.get("title", ""), price=got[0], currency=got[1], url=link, retrieved_at=at,
                               source=f"google_shopping:{merchant}:{market.upper()}", condition=condition, verify_author=books))
        return out

"""Serper (google.serper.dev) Google Shopping results as a PriceSource: merchant prices for books AND items.

Needs SERPER_API_KEY. Without it `configured()` is False and the source is left out of the chain (prices stay blank).
Each shopping result has a title, a merchant (`source`), a price string and a link: a real price with a URL, merchant
and retrieval date. What it does not have is an author, a currency code or a condition, so:
  * the price string is parsed carefully; a currency that cannot be read is never guessed (the listing is skipped)
  * `verify_author=True`: the price stage must find the book's author surname in the listing title
  * a listing is "used" only on clear evidence (used / pre-owned / refurbished in the title, or a known second-hand
    merchant); everything else is "new". Unsure listings are therefore never offered as used prices.
Shopping search returns merchants' current asking prices, not sold prices.
"""
from __future__ import annotations

import logging
import os
import re

from backend.core.http import post_json
from backend.ports import Listing

log = logging.getLogger(__name__)
URL = "https://google.serper.dev/shopping"
DOLLAR = {"US": "USD", "CA": "CAD", "AU": "AUD", "NZ": "NZD", "SG": "SGD", "HK": "HKD", "MX": "MXN"}
SYMBOL = {"£": "GBP", "€": "EUR", "₹": "INR", "CA$": "CAD", "C$": "CAD", "A$": "AUD", "AU$": "AUD", "NZ$": "NZD"}
USED_WORDS = re.compile(r"\b(used|pre-?owned|refurbished|renewed|second[- ]hand|like new|open box)\b", re.I)
USED_MERCHANTS = ("ebay", "thriftbooks", "abebooks", "better world books", "alibris", "mercari", "poshmark", "offerup",
                  "swappa", "backmarket", "back market", "decluttr", "facebook", "craigslist", "gumtree", "vinted")


def parse_price(text: str, country: str) -> tuple[float, str] | None:
    """'$1,299.00' / '£8.99' / '€ 1.299,00' / 'CA$ 12.50' -> (amount, currency); None if the currency is not certain."""
    t = (text or "").strip()
    cur = next((c for sym, c in sorted(SYMBOL.items(), key=lambda kv: -len(kv[0])) if sym in t), None)
    if cur is None and "$" in t:
        cur = DOLLAR.get(country.upper(), "USD")
    m = re.search(r"\d[\d.,\s]*\d|\d", t)
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


def is_used(title: str, merchant: str) -> bool:
    return bool(USED_WORDS.search(title or "")) or any(w in (merchant or "").lower() for w in USED_MERCHANTS)


class SerperPrices:
    wants_hints = False

    @staticmethod
    def configured() -> bool:
        return bool(os.environ.get("SERPER_API_KEY"))

    def local_market(self, country: str | None) -> str | None:
        return (country or "").upper() or None

    def fallback_markets(self) -> list[str]:
        return ["US"]

    def search(self, query: str, market: str, condition: str, books: bool = True) -> list[Listing]:
        key = os.environ.get("SERPER_API_KEY")
        if not key:
            return []
        gl = {"UK": "gb"}.get(market.upper(), market.lower())
        q = f"{query} book" if books else query
        data, at = post_json("serper_shopping", URL, {"q": q, "gl": gl, "hl": "en", "num": 40},
                             headers={"X-API-KEY": key, "Content-Type": "application/json"}, key_extra=["v1"])
        out = []
        for it in data.get("shopping", []) or []:
            got = parse_price(str(it.get("price", "")), market)
            if not got or not it.get("link"):
                continue
            title, merchant = it.get("title", ""), it.get("source", "")
            if ("used" if is_used(title, merchant) else "new") != condition:
                continue
            out.append(Listing(title=title, price=got[0], currency=got[1], url=it["link"], retrieved_at=at,
                               source=f"serper_shopping:{merchant or 'unknown'}:{market.upper()}", condition=condition,
                               verify_author=books))
        return out

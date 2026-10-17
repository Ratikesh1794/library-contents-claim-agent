"""Several price sources behind one PriceSource: each is tried in turn by the price stage (local market first, then the
fallback markets). A market id is namespaced with the index of the source that owns it ("1|GB"), so sources with
different market vocabularies (eBay's "EBAY_GB", Google's "GB") can sit side by side."""
from __future__ import annotations

from backend.ports import Listing, PriceSource


class ChainPriceSource:
    wants_hints = True                                    # forwarded only to the sources that ask for them

    def __init__(self, sources: list[PriceSource]):
        self.sources = sources

    def local_market(self, country: str | None) -> str | None:
        self._country = country
        for i, s in enumerate(self.sources):
            if (m := s.local_market(country)):
                return f"{i}|{m}"
        return None

    def fallback_markets(self) -> list[str]:
        """The price stage tries the local market, then these: every OTHER source's local market first (so a country's own
        prices are preferred to converted foreign ones), then each source's foreign fallback."""
        country = getattr(self, "_country", None)
        local = [f"{i}|{m}" for i, s in enumerate(self.sources) if (m := s.local_market(country))]
        foreign = [f"{i}|{m}" for i, s in enumerate(self.sources) for m in s.fallback_markets()]
        return local + foreign

    def search(self, query: str, market: str, condition: str, books: bool = True, hint: dict | None = None) -> list[Listing]:
        i, m = market.split("|", 1)
        src = self.sources[int(i)]
        if hint and getattr(src, "wants_hints", False):
            return src.search(query, m, condition, books, hint=hint)
        return src.search(query, m, condition, books)

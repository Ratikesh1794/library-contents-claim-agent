"""THE place where concrete implementations are chosen. Swap a service by editing one line.
Stages call these only when no dependency was injected (tests inject fakes)."""
from __future__ import annotations

from backend.ports import FX, Catalogue, PriceSource, Reconstructor, Vision


_local_detector = None      # models load once per process


def vision() -> Vision:
    global _local_detector
    from backend.adapters.openai_vision import OpenAIVision
    from backend.config import CFG
    if CFG.detector != "local":
        return OpenAIVision()                     # detection by the DETECT prompt
    from backend.adapters.local_detect import LocalDetector, LocalDetectVision
    _local_detector = _local_detector or LocalDetector()
    return LocalDetectVision(_local_detector, OpenAIVision())


def catalogue() -> Catalogue:
    from backend.adapters.google_books import GoogleBooks
    from backend.adapters.open_library import OpenLibrary
    from backend.stages.catalogue import ChainCatalogue
    return ChainCatalogue([OpenLibrary(), GoogleBooks()])   # first source with hits wins


def price_source() -> PriceSource:
    """In order: eBay (new + used + items) when its keys are set; Google Shopping via SerpApi (merchant prices for books
    and items, new + used) when SERPAPI_API_KEY is set; Google Books ebook prices (new, books only)."""
    from backend.adapters.ebay import Ebay
    from backend.adapters.google_books_prices import GoogleBooksPrices
    from backend.adapters.price_chain import ChainPriceSource
    from backend.adapters.serpapi_prices import SerpApiPrices
    return ChainPriceSource(([Ebay()] if Ebay.configured() else []) + ([SerpApiPrices()] if SerpApiPrices.configured() else [])
                            + [GoogleBooksPrices()])


def fx() -> FX:
    from backend.adapters.frankfurter import Frankfurter
    return Frankfurter()


def reconstructor() -> Reconstructor:
    # Swap for a VGGT/COLMAP adapter (GPU worker) when you have one. See README "Room".
    from backend.adapters.null_reconstructor import NullReconstructor
    return NullReconstructor()

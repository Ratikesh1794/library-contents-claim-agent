"""THE place where concrete implementations are chosen. Swap a service by editing one line.
Stages call these only when no dependency was injected (tests inject fakes)."""
from __future__ import annotations

from backend.ports import FX, Catalogue, PriceSource, Reconstructor, Vision


def vision() -> Vision:
    from backend.adapters.openai_vision import OpenAIVision
    return OpenAIVision()


def catalogue() -> Catalogue:
    from backend.adapters.google_books import GoogleBooks
    from backend.adapters.open_library import OpenLibrary
    from backend.stages.catalogue import ChainCatalogue
    return ChainCatalogue([OpenLibrary(), GoogleBooks()])   # first source with hits wins


def price_source() -> PriceSource:
    from backend.adapters.ebay import Ebay
    return Ebay()


def fx() -> FX:
    from backend.adapters.frankfurter import Frankfurter
    return Frankfurter()


def reconstructor() -> Reconstructor:
    # Swap for a VGGT/COLMAP adapter (GPU worker) when you have one. See README "Room".
    from backend.adapters.null_reconstructor import NullReconstructor
    return NullReconstructor()

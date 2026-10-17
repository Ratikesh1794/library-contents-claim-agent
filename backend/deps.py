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
    from backend.adapters.ebay import Ebay
    return Ebay()


def fx() -> FX:
    from backend.adapters.frankfurter import Frankfurter
    return Frankfurter()


def reconstructor() -> Reconstructor:
    # Swap for a VGGT/COLMAP adapter (GPU worker) when you have one. See README "Room".
    from backend.adapters.null_reconstructor import NullReconstructor
    return NullReconstructor()

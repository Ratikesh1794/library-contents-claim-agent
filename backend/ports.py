"""The seams. Every external dependency is a Protocol here; stages only know these.

To upgrade a part (new vision model, new price API):
write a class with the same method(s) in backend/adapters/ and point backend/deps.py at it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from backend.schemas import BBox, Candidate, ItemGuess


@dataclass
class Detection:
    bbox: BBox
    kind: str                      # "book" | "item"
    layout: str | None = None      # upright | flat | stacked | cover_only | unknown
    quad: list[tuple[float, float]] | None = None   # rotated rectangle around the object's outline (4 corners, px)
    label: str | None = None       # detector's class name, e.g. "chair" (a hint; the item category comes from describe)
    conf: float = 1.0
    poly: object | None = None     # outline polygon (N x 2 array), used to mask people out; not stored anywhere


@dataclass
class SpineRead:
    title: str | None
    author: str | None
    publisher: str | None
    legible: bool
    is_book: bool = True       # the model's check that the crop really is a book spine (not a bottle, box, DVD...)


class Vision(Protocol):
    def detect(self, img: np.ndarray) -> list[Detection]: ...
    def read_spine(self, crop: np.ndarray) -> SpineRead: ...
    def describe_item(self, crop: np.ndarray) -> ItemGuess: ...


class Catalogue(Protocol):
    def search(self, title: str, author: str | None) -> list[Candidate]: ...


@dataclass
class Listing:
    title: str
    price: float
    currency: str
    url: str
    retrieved_at: str
    source: str
    condition: str                 # "new" | "used"
    isbn: str | None = None
    verify_author: bool = False    # the source has no author field: the price stage must find the author in the title


class PriceSource(Protocol):
    def search(self, query: str, market: str, condition: str) -> list[Listing]: ...
    def local_market(self, country: str | None) -> str | None: ...        # None => no local market
    def fallback_markets(self) -> list[str]: ...


class FX(Protocol):
    def rate(self, base: str, quote: str) -> tuple[float, str]: ...       # (rate, date)


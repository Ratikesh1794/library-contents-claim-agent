"""The seams. Every external dependency is a Protocol here; stages only know these.

To upgrade a part (new vision model, new price API, real room reconstruction):
write a class with the same method(s) in backend/adapters/ and point backend/deps.py at it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

from backend.schemas import BBox, Candidate, Frame, ItemGuess


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


class PriceSource(Protocol):
    def search(self, query: str, market: str, condition: str) -> list[Listing]: ...
    def local_market(self, country: str | None) -> str | None: ...        # None => no local market
    def fallback_markets(self) -> list[str]: ...


class FX(Protocol):
    def rate(self, base: str, quote: str) -> tuple[float, str]: ...       # (rate, date)


@dataclass
class Reconstruction:
    """Unscaled 3D reconstruction. `up` is the gravity-up direction in model space."""
    points: np.ndarray                                  # (N, 3)
    up: np.ndarray                                      # (3,)
    marker_sides_model: list[float] = field(default_factory=list)  # marker side in model units


class Reconstructor(Protocol):
    def reconstruct(self, sweep_dir: Path, frames: list[Frame]) -> Reconstruction | None: ...

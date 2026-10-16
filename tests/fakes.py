"""Fakes for the ports, so stage tests run offline and deterministically."""
from __future__ import annotations

import cv2
import numpy as np

from backend.ports import Detection, Listing, SpineRead
from backend.schemas import Candidate, Frame, ItemGuess


class FakeVision:
    def __init__(self, dets=None, reads=None, item=None):
        self.dets, self.reads, self.item, self.read_calls = dets or [], list(reads or []), item, 0

    def detect(self, img): return self.dets
    def describe_item(self, crop): return self.item or ItemGuess(category="lamp", material="metal")

    def read_spine(self, crop):
        self.read_calls += 1
        return self.reads.pop(0) if self.reads else SpineRead(None, None, None, False)


class FakeCatalogue:
    def __init__(self, table): self.table, self.calls = table, 0   # {title_lower: [Candidate]}

    def search(self, title, author):
        self.calls += 1
        return self.table.get((title or "").lower(), [])


class FakeSource:
    def __init__(self, new=None, used=None, local="EBAY_US"):
        self.new, self.used, self._local = new or {}, used or {}, local

    def local_market(self, country): return self._local
    def fallback_markets(self): return ["EBAY_US"]

    def search(self, query, market, condition, books=True):
        return (self.new if condition == "new" else self.used).get(market, [])


class FakeFX:
    def __init__(self, rates=None): self.rates = rates or {}
    def rate(self, base, quote): return (1.0, "2026-01-01") if base == quote else (self.rates[(base, quote)], "2026-01-01")


def cand(title, author="A. Writer", **kw):
    return Candidate(title=title, author=author, source_url="http://cat/" + title.replace(" ", "_"), **kw)


def listing(title, price, cond="new", cur="USD", market="ebay:EBAY_US"):
    return Listing(title=title, price=price, currency=cur, url="http://l/" + title.replace(" ", "_") + str(price),
                   retrieved_at="2026-01-01T00:00:00Z", source=market, condition=cond)


def make_sweep(tmp_path, n_frames=1, size=(700, 600), country="US", currency="USD", texture=True):
    """A sweep dir with n noisy-textured frames (sharp enough to pass the blur gate)."""
    from backend.core import store
    sd = store.new_sweep(country, currency, tmp_path)
    rng = np.random.default_rng(0)
    frames = []
    for i in range(n_frames):
        img = (rng.integers(80, 180, (*size, 3))).astype(np.uint8) if texture else np.full((*size, 3), 120, np.uint8)
        fid = f"f_{i:04d}"
        cv2.imwrite(str(sd / "frames" / f"{fid}.jpg"), img)
        frames.append(Frame(id=fid, path=f"frames/{fid}.jpg", t=float(i), w=size[1], h=size[0], blur=100, glare=0, dark=120, seq=i))
    store.write(sd, "frames.json", frames)
    return sd, frames

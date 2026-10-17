"""BookTracker: one ShelfMap per sweep plus the bookkeeping the pipeline needs.

  * which sighting belongs to which physical book  -> `groups()`, used by dedupe so each book is counted once,
    whichever way the camera moved, and whether or not its title could be read;
  * what was already read from each book            -> `plan()` / `note_*()`, used by identify so each book is read
    once: a book already identified is never read again, a book the reader called "not a book" is dropped in every
    later frame, and a book that failed to read is retried only from a clearly sharper crop (at most `read_tries`).

It is the only place where the two live paths (frame by frame while the user walks, and the batch pass at the end)
share state. Thread-safe: reads for one frame run in parallel.
"""
from __future__ import annotations

import logging
import threading

import numpy as np

from backend.config import CFG, Config
from backend.schemas import Sighting
from backend.stages.shelf_map import Book, ShelfMap, canonical

log = logging.getLogger(__name__)

READ_FIELDS = ("accepted", "candidates", "score", "raw_text", "legible", "rotation_deg")


class BookTracker:
    def __init__(self, cfg: Config = CFG):
        self.cfg = cfg
        self.map = ShelfMap(cfg)
        self._lock = threading.RLock()
        self._records: list[tuple[Sighting, Book]] = []
        self._view_frame: str | None = None                      # the latest frame and the books in it (for guidance)
        self._view: list[tuple[Sighting, Book]] = []

    # ---------------------------------------------------------------- map
    def register(self, frame_id: str, img: np.ndarray, boxes: list) -> list[Book | None]:
        """Place one frame's book boxes on the map -> the Book each box belongs to (None for all if the map failed:
        callers then simply read everything, as they did before the map existed)."""
        with self._lock:
            self._view_frame, self._view = frame_id, []
        if not boxes:
            return []
        with self._lock:
            try:
                return list(self.map.add_frame(img, [tuple(b) for b in boxes], frame_id)["owners"])
            except Exception as e:                       # never let the map take the live path down
                log.warning("shelf map failed on %s: %s", frame_id, e)
                return [None] * len(boxes)

    def attach(self, s: Sighting, book: Book | None) -> None:
        """Record that `s` is a view of `book` (and keep the identity read from it, if any)."""
        if book is None:
            return
        with self._lock:
            self._records.append((s, book))
            if s.frame_id == self._view_frame:
                self._view.append((s, book))
            m = canonical(book).meta
            m["reason"] = None if s.accepted else s.reason          # latest word on why it is unresolved
            if s.accepted and "read" not in m:
                m["read"] = s

    def add_sightings(self, frame_id: str, img: np.ndarray, sightings: list[Sighting]) -> None:
        """Register the book sightings of a frame that was NOT detected here (copied from an earlier frame, or loaded
        from disk) and attach them."""
        books = [s for s in sightings if s.kind == "book"]
        for s, b in zip(books, self.register(frame_id, img, [s.bbox for s in books])):
            self.attach(s, b)

    def view(self, frame_id: str) -> list[tuple[Sighting, Book]]:
        """(sighting, book) for the books in `frame_id`, if it is the latest frame placed on the map (else empty)."""
        with self._lock:
            return [(s, canonical(b)) for s, b in self._view] if frame_id == self._view_frame else []

    def offscreen(self, in_view_ids: set[int]) -> list[tuple[Book, float, float, int, int]]:
        """Books on the current map that are not in the latest frame: (book, x, y, w, h) with x/y in the current view's
        pixels (see ShelfMap.locate). Books on other maps (frames that never lined up) are left out."""
        with self._lock:
            out = []
            for b in self.map.books:
                if id(b) in in_view_ids:
                    continue
                loc = self.map.locate(b)
                if loc is not None:
                    out.append((b, *loc))
            return out

    def groups(self) -> list[list[str]]:
        """Sighting ids per physical book (books with at least one sighting)."""
        with self._lock:
            out: dict[int, list[str]] = {}
            for s, b in self._records:
                out.setdefault(canonical(b).id, []).append(s.id)
            return list(out.values())

    @property
    def count(self) -> int:
        return len(self.groups())

    # ---------------------------------------------------------------- read once
    def _same_look(self, m: dict, sig) -> bool:
        """Does a crop with colour signature `sig` look like the crop that was read? The map can drift, and then a box
        of a DIFFERENT book lands on a registered one: copying the read would give it the wrong identity without ever
        looking at it. No signature on either side = no objection."""
        known = m.get("sig")
        if known is None or sig is None:
            return True
        return float(np.linalg.norm(np.subtract(known, sig))) <= self.cfg.copy_color_max

    def plan(self, book: Book | None, sharp: float, sig=None) -> tuple[str, Sighting | None]:
        """What to do with a crop of `book` whose sharpness is `sharp` and colour signature `sig`:
        ("drop", None)   the reader already said this is not a book
        ("copy", read)   already identified: reuse that read
        ("skip", None)   tried before and this crop isn't clearly better: don't pay for another read
        ("read", None)   read it (also when the crop does not look like the book that was read: see _same_look)"""
        if book is None:
            return "read", None
        with self._lock:
            m = canonical(book).meta
            if not self._same_look(m, sig):
                return "read", None
            if m.get("not_book"):
                return "drop", None
            if m.get("read") is not None:
                return "copy", m["read"]
            tries = m.get("tries", 0)
            if tries and (tries >= self.cfg.read_tries or sharp < self.cfg.reread_gain * m.get("sharp", 0.0)):
                return "skip", None
            return "read", None

    def note_read(self, book: Book | None, sharp: float, is_book: bool = True, sig=None) -> None:
        if book is None:
            return
        with self._lock:
            m = canonical(book).meta
            if sig is not None:
                m.setdefault("sig", tuple(float(v) for v in sig))     # what the book that was read looks like
            m["tries"] = m.get("tries", 0) + 1
            m["sharp"] = max(m.get("sharp", 0.0), sharp)
            if not is_book:
                m["not_book"] = True

    @staticmethod
    def copy_read(src: Sighting, dst: Sighting) -> None:
        for f in READ_FIELDS:
            setattr(dst, f, getattr(src, f))

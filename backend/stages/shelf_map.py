"""Count each book once while the camera moves forward AND backward along a shelf: register every frame on a shared
map of the shelf, place each detected book on that map, and count by map position.

Why position: a book is the same book if it sits in the same spot on the shelf. That holds whichever way the camera
moves, when the title can't be read, and for two identical copies side by side (two spots = two books).

How:
  1. Each frame is lined up (ORB features + RANSAC homography, as in track.py) against earlier frames whose view
     overlaps it, not just the previous one, so coming back to a shelf later still lands on the same map spot.
     The map is the first frame's plane; every frame gets a homography `H` (frame pixels -> map).
  2. A detected book box is projected onto the map. If it covers an already-registered book it is a repeat; if it lands
     on empty map space it is a new book. Leftover boxes that overlap a registered book are pieces of it (the detector
     often cuts one book in two), and a whole view of a book swallows the unfinished pieces registered earlier.
  3. A frame that can't be lined up with anything (blur, too fast, a different bookcase) starts a new component with its
     own map. A later frame that lines up with two components joins them and drops the books seen in both.
  4. Count = books registered across all components.

Every Book has an `id`; a book swallowed by another keeps pointing at it (`into`), so `canonical(book)` is always the
book to count. `meta` is free space for the caller (e.g. what was read from the spine) and travels with merges.

Flat-shelf assumption: a homography is exact for the plane of a shelf front and close for books at similar depth.
Not thread-safe: callers serialise access (see book_tracker.BookTracker).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from backend.config import CFG, Config


@dataclass(eq=False)
class _Key:
    pts: np.ndarray                 # (N, 2) keypoints, full-resolution frame pixels
    desc: np.ndarray
    H: np.ndarray                   # frame pixels -> map
    foot: np.ndarray                # (4, 2) frame outline on the map
    comp: int


@dataclass(eq=False)
class Book:
    id: int
    box: np.ndarray                 # [x0, y0, x1, y1] on the map (the largest view of the book so far)
    comp: int
    n: int = 1                      # sightings
    partial: bool = True            # never seen whole yet: every sighting was cut off by the frame edge
    best: dict = field(default_factory=dict)   # sharpest sighting: {frame, box (frame px), sharp}
    meta: dict = field(default_factory=dict)   # caller's notes about this book
    into: "Book | None" = None      # set when this book was swallowed by another


def canonical(b: Book) -> Book:
    while b.into is not None:
        b = b.into
    return b


def _foot(H: np.ndarray, w: int, h: int) -> np.ndarray:
    c = np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(c, H).reshape(-1, 2)


def _poly_iou(a: np.ndarray, b: np.ndarray) -> float:
    inter, _ = cv2.intersectConvexConvex(a.astype(np.float32), b.astype(np.float32))
    ua = cv2.contourArea(a.astype(np.float32)) + cv2.contourArea(b.astype(np.float32)) - inter
    return float(inter) / ua if ua > 0 else 0.0


def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _inter(a, b) -> float:
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def _to_map(H: np.ndarray, box) -> np.ndarray:
    x0, y0, x1, y1 = box
    p = cv2.perspectiveTransform(np.float32([[x0, y0], [x1, y0], [x1, y1], [x0, y1]]).reshape(-1, 1, 2), H).reshape(-1, 2)
    return np.array([p[:, 0].min(), p[:, 1].min(), p[:, 0].max(), p[:, 1].max()])


def _overlap(a, b) -> float:
    inter = _inter(a, b)
    if inter <= 0:
        return 0.0
    return max(inter / (_area(a) + _area(b) - inter), 0.7 * inter / (min(_area(a), _area(b)) or 1))


def _cut(box, w: int, h: int, m: int = 3) -> bool:
    """Does the box touch the frame border (the book is probably cut off)?"""
    return box[0] <= m or box[1] <= m or box[2] >= w - m or box[3] >= h - m


class ShelfMap:
    def __init__(self, cfg: Config = CFG):
        self.cfg = cfg
        self.keys: list[_Key] = []
        self.books: list[Book] = []
        self.n_comp = 0
        self._next_id = 0
        self._last: tuple[int, np.ndarray] | None = None        # (component, outline on the map) of the last frame
        self._view: tuple[np.ndarray, int, int] | None = None   # (H, w, h) of the last frame: map -> what the camera sees
        self.stats = {"frames": 0, "registered": 0, "new_components": 0, "repeats": 0, "pieces": 0, "new_books": 0,
                      "merged_components": 0, "merged_books": 0}

    @property
    def count(self) -> int:
        return len(self.books)

    # ---------------------------------------------------------------- registration
    def _features(self, img: np.ndarray):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        s = min(1.0, self.cfg.track_max_side / max(gray.shape[:2]))
        small = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else gray
        kp, desc = cv2.ORB_create(self.cfg.track_features).detectAndCompute(small, None)
        pts = np.float32([k.pt for k in kp]) / s if kp else np.zeros((0, 2), np.float32)
        return pts, desc

    def _homography(self, pts, desc, key: _Key):
        """new frame -> key's frame, with the inlier count."""
        if desc is None or key.desc is None or len(pts) < 8 or len(key.pts) < 8:
            return None, 0
        pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(desc, key.desc, k=2)
        good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
        if len(good) < self.cfg.track_min_inliers:
            return None, 0
        src = pts[[m.queryIdx for m in good]]
        dst = key.pts[[m.trainIdx for m in good]]
        H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 6.0)
        if H is None:
            return None, 0
        n = int(mask.sum())
        if n < self.cfg.track_min_inliers or not 0.25 < abs(np.linalg.det(H[:2, :2])) < 4.0:
            return None, 0
        return H, n

    def _register(self, pts, desc):
        """-> (main component, {component: (H frame->that map, inliers)}) or None. Looks first around the previous frame,
        then (lost, or several maps exist) at keyframes of the other components. A frame that lines up with two
        components is the bridge between them (they get merged in add_frame). Bounded by `map_search_budget_s`."""
        found: dict[int, tuple[np.ndarray, int]] = {}
        strong = 4 * self.cfg.track_min_inliers
        deadline = time.monotonic() + self.cfg.map_search_budget_s

        def try_keys(keys):
            for k in keys:
                if time.monotonic() > deadline:
                    return
                H, n = self._homography(pts, desc, k)
                if H is not None and (k.comp not in found or n > found[k.comp][1]):
                    found[k.comp] = (k.H @ H, n)
                    if n >= strong:                                 # unmistakable: no need to look further
                        return
        if self._last is not None:
            comp, foot = self._last
            near = sorted((k for k in self.keys if k.comp == comp), key=lambda k: -_poly_iou(foot, k.foot))
            try_keys(near[:self.cfg.map_candidates])
        if not found:                                                # lost: look at earlier keyframes, newest first
            try_keys(self.keys[::-1][:self.cfg.map_max_search])
        elif self.n_comp > 1:                                        # found: still check the other components
            per_comp: dict[int, int] = {}
            others = []
            for k in reversed(self.keys):
                if k.comp not in found and per_comp.get(k.comp, 0) < self.cfg.map_cross:
                    per_comp[k.comp] = per_comp.get(k.comp, 0) + 1
                    others.append(k)
            try_keys(others)
        if not found:
            return None
        last = self._last[0] if self._last else None
        main = last if last in found else max(found, key=lambda c: found[c][1])
        return main, found

    # ---------------------------------------------------------------- books
    def _new_book(self, box, comp: int, partial: bool) -> Book:
        b = Book(self._next_id, box, comp, partial=partial)
        self._next_id += 1
        self.books.append(b)
        return b

    def _swallow(self, keep: Book, gone: Book) -> None:
        """`gone` is the same book as `keep` (or a piece of it): fold it in and point it at `keep`."""
        keep.n += gone.n
        keep.partial = keep.partial and gone.partial
        if _area(gone.box) > _area(keep.box):
            keep.box = gone.box
        if gone.best.get("sharp", -1) > keep.best.get("sharp", -1):
            keep.best = gone.best
        for k, v in gone.meta.items():                              # what was learned about it travels along...
            if k != "not_book":                                     # ...except "not a book": a piece may be a non-book sliver
                keep.meta.setdefault(k, v)
        gone.into = keep
        self.books = [b for b in self.books if b is not gone]

    def _merge(self, main: int, other: int, T: np.ndarray) -> None:
        """Move component `other` into `main`'s map (T: other's map -> main's map); fold in books seen in both."""
        for k in self.keys:
            if k.comp == other:
                k.H, k.comp = T @ k.H, main
                k.foot = cv2.perspectiveTransform(k.foot.reshape(-1, 1, 2).astype(np.float32), T).reshape(-1, 2)
        moved = [b for b in self.books if b.comp == other]
        for b in moved:
            b.box, b.comp = _to_map(T, b.box), main
        keep = [b for b in self.books if b.comp == main and b not in moved]
        pairs = sorted(((_overlap(m.box, k.box), i, j) for i, m in enumerate(moved) for j, k in enumerate(keep)), reverse=True)
        gone: set[int] = set()
        for sc, i, j in pairs:
            if sc < self.cfg.map_absorb or i in gone:
                continue
            gone.add(i)
            self._swallow(keep[j], moved[i])
        self.stats["merged_components"] += 1
        self.stats["merged_books"] += len(gone)

    def locate(self, book: Book) -> tuple[float, float, int, int] | None:
        """Where `book` is in the CURRENT camera view's pixel coordinates -> (x, y, frame w, frame h), or None if it is on
        another map. x < 0 is off the left edge, x > w off the right, y < 0 above, y > h below: what the user sees."""
        if self._view is None or self._last is None or book.comp != self._last[0]:
            return None
        H, w, h = self._view
        try:
            c = np.float32([[[(book.box[0] + book.box[2]) / 2, (book.box[1] + book.box[3]) / 2]]])
            x, y = cv2.perspectiveTransform(c, np.linalg.inv(H)).reshape(2)
        except (np.linalg.LinAlgError, cv2.error):
            return None
        return float(x), float(y), w, h

    # ---------------------------------------------------------------- frames
    def add_frame(self, img: np.ndarray, boxes: list[tuple[int, int, int, int]], frame_id: str | int = "") -> dict:
        """Register one frame and its detected book boxes.
        -> {"registered", "component", "matched", "pieces", "new", "owners"}; owners[i] is the Book that boxes[i] belongs to."""
        cfg = self.cfg
        h, w = img.shape[:2]
        self.stats["frames"] += 1
        first = not self.keys
        pts, desc = self._features(img)
        reg = None if first else self._register(pts, desc)
        if reg is None:
            comp, H = self.n_comp, np.eye(3)
            self.n_comp += 1
            if self.keys:
                self.stats["new_components"] += 1
        else:
            comp, found = reg
            H = found[comp][0]
            for other, (Ho, _) in found.items():
                if other != comp:
                    try:
                        T = H @ np.linalg.inv(Ho)
                    except np.linalg.LinAlgError:
                        continue
                    if 0.25 < abs(np.linalg.det(T[:2, :2])) < 4.0:
                        self._merge(comp, other, T)
            self.stats["registered"] += 1
        foot = _foot(H, w, h)
        if not self.keys or all(not (k.comp == comp and _poly_iou(foot, k.foot) > cfg.map_same_view) for k in self.keys):
            self.keys.append(_Key(pts, desc, H, foot, comp))     # a view we don't have yet
        self._last = (comp, foot)
        self._view = (H, w, h)

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        owners: list[Book | None] = [None] * len(boxes)
        mbox = [_to_map(H, b) for b in boxes]
        mine = [b for b in self.books if b.comp == comp]
        cands = []
        for i, mb in enumerate(mbox):
            for j, b in enumerate(mine):
                s = _overlap(mb, b.box)
                if s >= cfg.map_match:
                    cands.append((s, i, j))
        used_j, matched = set(), 0
        for s, i, j in sorted(cands, reverse=True):
            if owners[i] is not None or j in used_j:
                continue
            used_j.add(j); matched += 1
            b = mine[j]
            owners[i] = b
            if _area(mbox[i]) > _area(b.box):                  # the biggest view of a book is its best box
                b.box = mbox[i]
            b.n += 1
            b.partial = b.partial and _cut(boxes[i], w, h)
            self._best(b, gray, boxes[i], frame_id)
        # Leftover boxes, biggest first (so the whole book is registered before its pieces). The detector often cuts one
        # book into overlapping pieces (more so at an angle): a leftover that overlaps a registered book, one created a
        # moment ago in this frame included, is a piece of it and not a new book.
        new = pieces = 0
        for i in sorted((i for i in range(len(boxes)) if owners[i] is None), key=lambda i: -_area(boxes[i])):
            near = max(((_overlap(mbox[i], b.box), b) for b in self.books if b.comp == comp), default=(0.0, None),
                       key=lambda t: t[0])
            if near[1] is not None and near[0] >= cfg.map_absorb:
                owners[i] = near[1]
                near[1].n += 1
                pieces += 1
                continue
            nb = self._new_book(mbox[i], comp, _cut(boxes[i], w, h))
            self._best(nb, gray, boxes[i], frame_id)
            owners[i] = nb
            new += 1
        # A whole view of a book swallows the unfinished pieces of it registered earlier (a sliver cut off on each side
        # by two different frames). Only pieces: a whole book inside a big box is a neighbour, never swallowed.
        for i, b in enumerate(owners):
            if _cut(boxes[i], w, h) or b.into is not None:
                continue
            for o in [o for o in self.books if o.comp == comp and o is not b and o.partial
                      and _inter(mbox[i], o.box) >= 0.7 * _area(o.box)]:
                self._swallow(b, o)
                pieces += 1
        owners = [canonical(b) for b in owners]
        self.stats["repeats"] += matched
        self.stats["pieces"] += pieces
        self.stats["new_books"] += new
        return {"registered": reg is not None or first, "component": comp, "matched": matched, "pieces": pieces,
                "new": new, "owners": owners}

    @staticmethod
    def _best(book: Book, gray: np.ndarray, box, frame_id) -> None:
        x0, y0, x1, y1 = (int(v) for v in box)
        crop = gray[max(0, y0):y1, max(0, x0):x1]
        if crop.size < 100:
            return
        sharp = float(cv2.Laplacian(cv2.resize(crop, (64, 128)), cv2.CV_32F).var())
        if sharp > book.best.get("sharp", -1):
            book.best = {"frame": frame_id, "box": (x0, y0, x1, y1), "sharp": sharp}

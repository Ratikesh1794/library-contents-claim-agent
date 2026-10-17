"""Count each book once while the camera sweeps along a shelf: stitch every frame into a running list per shelf row.

A shelf row is a fixed left-to-right sequence of books (a pile is a top-to-bottom one). Each frame shows a window onto
that sequence. We keep one master list (a "strip") per row and line every new frame up against it:

  1. a frame row is a short ordered list of Spines (title/author if read, plus relative thickness and colour);
  2. it is aligned to each master strip with a fuzzy local alignment (Smith-Waterman), so a misread or a missed book
     does not break it;
  3. the alignment is accepted only if >= `min_run` books in a row match (3 by default; a frame row shorter than that
     must match completely). Those books are the overlap, so they are NOT counted again;
  4. books of the frame outside the overlap are new: they extend the strip on the left or right (whichever way the
     user is moving), or fill a gap inside it; a row that matches no strip starts a new strip (another bookcase);
  5. the count is the number of Spines in all strips.

Matching: two readable books match on title (similar text); an unreadable book matches on position plus relative
thickness and colour. A book cut off by the frame edge matches on colour/text only (its width is not comparable) and is
completed when a later frame shows it whole.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import cv2
import numpy as np


@dataclass
class Spine:
    title: str | None
    author: str | None
    t: float                                 # thickness relative to the median of its row (1.0 = typical)
    color: tuple[float, float, float]        # mean Lab colour of the spine
    partial: bool = False                    # cut off by the frame edge along the row direction
    seen: int = 1


@dataclass
class Params:
    text_same: float = 0.85                  # title similarity that counts as the same book
    text_diff: float = 0.6                   # below this, two readable titles are different books
    color_max: float = 25.0                  # Lab distance for "looks like the same spine"
    thick_max: float = 0.3                   # |log(t_a / t_b)| allowed for unreadable books
    gap: float = -1.5                        # alignment: gap penalty
    min_run: int = 3                         # consecutive matching books that prove an overlap
    edge_margin: float = 0.02                # fraction of the frame treated as "touching the edge"


def norm(s: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split())


def _text_sim(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if min(len(a), len(b)) >= 4 and (a in b or b in a):      # "ROAD" inside "THE ROAD"
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


def score(a: Spine, b: Spine, p: Params) -> float:
    """Alignment score of two spines: +2 same text, +1 looks alike, +0.5 a cut-off sliver in the same slot, -2 different."""
    ts = _text_sim(a.title, b.title) if a.title and b.title else None
    if ts is not None:
        if ts >= p.text_same:
            return 2.0
        if ts < p.text_diff:
            return -2.0
    if a.partial or b.partial:             # a sliver at the frame edge: its colour and width can't be trusted, so it
        return 0.5                         # may stand in for the book in that slot, but it is never proof of overlap
    if float(np.linalg.norm(np.subtract(a.color, b.color))) > p.color_max:
        return -2.0
    if abs(math.log(max(a.t, 1e-3) / max(b.t, 1e-3))) > p.thick_max:
        return -2.0
    return 1.0


def align(frame: list[Spine], master: list[Spine], p: Params) -> list[tuple[int, int, bool]]:
    """Smith-Waterman local alignment -> [(frame index, master index, is_match)] in order. A diagonal step that does
    not match (a misread between two matched books) is kept with is_match=False: the same book, read badly or cut off."""
    m, n = len(frame), len(master)
    if not m or not n:
        return []
    S = np.array([[score(f, s, p) for s in master] for f in frame])
    H = np.zeros((m + 1, n + 1))
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            H[i, j] = max(0.0, H[i - 1, j - 1] + S[i - 1, j - 1], H[i - 1, j] + p.gap, H[i, j - 1] + p.gap)
    i, j = np.unravel_index(int(np.argmax(H)), H.shape)
    path = []
    while i > 0 and j > 0 and H[i, j] > 0:
        if H[i, j] == H[i - 1, j - 1] + S[i - 1, j - 1]:
            path.append((i - 1, j - 1, bool(S[i - 1, j - 1] >= 1.0)))
            i, j = i - 1, j - 1
        elif H[i, j] == H[i - 1, j] + p.gap:
            i -= 1
        else:
            j -= 1
    return path[::-1]


def longest_run(path: list[tuple[int, int, bool]]) -> int:
    """Longest run of consecutive books that really match (substitutions do not count as proof)."""
    best = run = 0
    for k, (i, j, ok) in enumerate(path):
        run = run + 1 if ok and k and path[k - 1][2] and path[k - 1][:2] == (i - 1, j - 1) else (1 if ok else 0)
        best = max(best, run)
    return best


def _merge(old: Spine, new: Spine) -> None:
    """The same book seen again: keep what we know, fill in what we didn't, replace a cut-off view by a whole one."""
    old.title, old.author = old.title or new.title, old.author or new.author
    if old.partial and not new.partial:
        old.t, old.color, old.partial = new.t, new.color, False
    old.seen += 1


class ShelfStitcher:
    def __init__(self, params: Params | None = None):
        self.p = params or Params()
        self.strips: list[list[Spine]] = []

    @property
    def count(self) -> int:
        return sum(len(s) for s in self.strips)

    def add_row(self, row: list[Spine]) -> dict:
        """Line one frame row up against the strips -> {"matched": n, "new": n, "strip": index}."""
        p = self.p
        best = None
        for k, strip in enumerate(self.strips):
            path = align(row, strip, p)
            need = min(p.min_run, len(row), len(strip))
            if path and longest_run(path) >= need and (best is None or sum(ok for *_, ok in path) > sum(ok for *_, ok in best[1])):
                best = (k, path)
        if best is None:                                     # nothing to line up with: a new bookcase / row
            self.strips.append([Spine(s.title, s.author, s.t, s.color, s.partial) for s in row])
            return {"matched": 0, "new": len(row), "strip": len(self.strips) - 1}
        k, path = best
        strip, at = self.strips[k], {i: j for i, j, _ in path}
        for i, j, _ in path:
            _merge(strip[j], row[i])
        first_i, last_i = min(at), max(at)
        n = len(strip)
        placed: list[tuple[float, Spine]] = [(float(j), s) for j, s in enumerate(strip)]
        for i, s in enumerate(row):
            if i in at:
                continue
            if i < first_i:                                  # before the overlap: beyond any master books the frame skipped
                pos = -0.5 - 0.001 * (first_i - i)
            elif i > last_i:                                 # after the overlap: likewise on the right
                pos = n - 0.5 + 0.001 * (i - last_i)
            else:                                            # a gap inside the overlap: right after its left neighbour
                left = max(x for x in at if x < i)
                pos = at[left] + 0.5 + 0.001 * (i - left)
            placed.append((pos, Spine(s.title, s.author, s.t, s.color, s.partial)))
        placed.sort(key=lambda x: x[0])
        self.strips[k] = [s for _, s in placed]
        return {"matched": len(path), "new": len(row) - len(path), "strip": k}

    def add_frame(self, rows: list[list[Spine]]) -> list[dict]:
        return [self.add_row(r) for r in rows if r]


# ---------------------------------------------------------------- detections -> rows of Spines
def rows_from_books(img: np.ndarray, books: list[dict], p: Params | None = None) -> list[list[Spine]]:
    """Group one frame's books ({box, layout, title, author}) into ordered rows.
    Vertical books: rows by vertical position, ordered left to right. Stacked books: piles by horizontal position,
    ordered top to bottom. A book touching the frame edge along the row direction is `partial`."""
    p = p or Params()
    H, W = img.shape[:2]
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    out: list[list[Spine]] = []
    for layout in ("vertical", "stacked"):
        bs = [b for b in books if b["layout"] == layout]
        if not bs:
            continue
        along, across = (0, 1) if layout == "vertical" else (1, 0)          # box index pairs: x or y
        size = [(b["box"][along + 2] - b["box"][along]) for b in bs]           # thickness along the row direction
        cross_c = [(b["box"][across] + b["box"][across + 2]) / 2 for b in bs]
        cross_len = [(b["box"][across + 2] - b["box"][across]) for b in bs]
        order = sorted(range(len(bs)), key=lambda i: cross_c[i])
        groups, cur = [], [order[0]]
        for a, b in zip(order, order[1:]):
            if cross_c[b] - cross_c[a] > 0.5 * float(np.median(cross_len)):
                groups.append(cur)
                cur = []
            cur.append(b)
        groups.append(cur)
        limit = W if layout == "vertical" else H
        for g in groups:
            g.sort(key=lambda i: bs[i]["box"][along])
            med = float(np.median([size[i] for i in g])) or 1.0
            row = []
            for i in g:
                x0, y0, x1, y1 = bs[i]["box"]
                cx0, cy0 = x0 + (x1 - x0) // 5, y0 + (y1 - y0) // 5                  # inner 60%: skip edges/background
                patch = lab[cy0:max(cy0 + 1, y1 - (y1 - y0) // 5), cx0:max(cx0 + 1, x1 - (x1 - x0) // 5)]
                color = tuple(float(v) for v in patch.reshape(-1, 3).mean(0)) if patch.size else (0.0, 0.0, 0.0)
                lo, hi = bs[i]["box"][along], bs[i]["box"][along + 2]
                edge = p.edge_margin * limit
                row.append(Spine(bs[i].get("title"), bs[i].get("author"), size[i] / med, color,
                                 partial=lo <= edge or hi >= limit - edge))
            out.append(row)
    return out

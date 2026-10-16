"""DEDUPE stage: sightings.json + frames  ->  objects.json   (also links.json, the cached frame alignments)

Goal: one Object per physical book/item, WITHOUT ever dropping an unidentified book (count stays honest).

  1. Tracking (backend/stages/track.py): consecutive overlapping frames are aligned; a box that projects onto a box of
     the same kind in the next frame(s) is the same object. This works for books that were never read, which
     is what made the count explode before (every unread sighting became its own "book").
  2. Never merge two different identified works (a tracking slip must not turn two books into one).
  3. Coming back to a shelf later (frames that don't overlap): identified groups with the same work are merged
     unless they were seen side by side in one frame (then they are two copies).
  4. A group gets its identity from its best accepted read, so one good read names the book in every frame.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Literal

import numpy as np

from backend.core import store
from backend.stages import track
from backend.stages.catalogue import work_key
from backend.config import CFG, Config
from backend.schemas import BBox, Frame, Object, Reason, Sighting


def assign_rows(boxes: list[tuple[int, int, int, int]]) -> list[int]:
    """Cluster boxes into shelf rows by vertical centre. Row 0 is the top. Returns a row per box."""
    if not boxes:
        return []
    cy = [(b[1] + b[3]) / 2 for b in boxes]
    gap = 0.5 * median(b[3] - b[1] for b in boxes)
    order = sorted(range(len(boxes)), key=lambda i: cy[i])
    rows, r = [0] * len(boxes), 0
    for a, b in zip(order, order[1:]):
        if cy[b] - cy[a] > gap:
            r += 1
        rows[b] = r
    return rows


class _Groups:
    """Union-find over sighting ids that refuses to merge groups holding different identified works."""

    def __init__(self, sightings: list[Sighting]):
        self.parent = {s.id: s.id for s in sightings}
        self.keys = {s.id: ({work_key(s.accepted)} if s.accepted else set()) for s in sightings}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def can_merge(self, a: str, b: str) -> bool:
        ka, kb = self.keys[self.find(a)], self.keys[self.find(b)]
        return not (ka and kb and ka != kb)

    def union(self, a: str, b: str) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        if not self.can_merge(ra, rb):
            return False
        self.parent[rb] = ra
        self.keys[ra] |= self.keys.pop(rb)
        return True

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for x in self.parent:
            out[self.find(x)].append(x)
        return out


def _overlap(a: BBox, b: BBox) -> float:
    """Intersection over the SMALLER box: 1.0 when one box sits inside the other."""
    small = min(track.area(a), track.area(b))
    return track.intersection(a, b) / small if small > 0 else 0.0


def _same_item_kind(a: Sighting, b: Sighting) -> bool:
    ca, cb = (a.item.category if a.item else None), (b.item.category if b.item else None)
    return ca is None or cb is None or ca == cb


def _link_pairs(frame_a: list[Sighting], frame_b: list[Sighting], h: np.ndarray, cfg: Config) -> list[tuple[Sighting, Sighting]]:
    """Greedy one-to-one matching of A boxes (projected into B) to B boxes of the same kind.
    Books: plain IoU (spines are thin and side by side, so containment would merge neighbours).
    Items: the vision model boxes them loosely and inconsistently (a lamp alone vs lamp + table), so a box
    mostly inside the other also counts, provided the categories agree."""
    cands = []
    for sa in frame_a:
        pa = track.project(h, sa.bbox)
        for sb in frame_b:
            if sb.kind != sa.kind:
                continue
            o = track.iou(pa, sb.bbox)
            if sa.kind == "item" and _same_item_kind(sa, sb):
                o = max(o, _overlap(pa, sb.bbox) * cfg.item_contain_weight)
            if o >= cfg.track_iou:
                cands.append((o, sa, sb))
    used_a, used_b, out = set(), set(), []
    for o, sa, sb in sorted(cands, key=lambda c: -c[0]):
        if sa.id not in used_a and sb.id not in used_b:
            used_a.add(sa.id); used_b.add(sb.id); out.append((sa, sb))
    return out


def _make(oid: str, kind: Literal["book", "item"], group: list[Sighting], frames: dict[str, Frame],
          row_pos: dict[str, tuple[str, int]]) -> Object:
    accepted = [s for s in group if s.accepted]
    b = max(accepted or group, key=lambda s: (s.carried_from is None, s.score or 0.0, s.crop_blur or 0.0))
    shelf, pos = row_pos.get(b.id, (None, None))
    reasons: list[Reason] = [s.reason for s in group if s.reason is not None]
    o = Object(id=oid, kind=kind, sighting_ids=[s.id for s in group],
               shelf=shelf, position=pos, status="identified", frame_ref=frames[b.frame_id].path,
               bbox=b.bbox, layout=b.layout, rotation_deg=b.rotation_deg)
    if kind == "book":
        if b.accepted:
            c = b.accepted
            o.title, o.author, o.edition, o.isbn, o.year = c.title, c.author, c.edition, c.isbn, c.year
            o.catalogue_match_score, o.source_url = c.score, c.source_url
            o.id_confidence = b.score
        else:
            o.status = "unidentified"
            o.reason = max(set(reasons), key=reasons.count) if reasons else "low_confidence"
    else:
        it = next((s.item for s in sorted(group, key=lambda s: -(s.crop_blur or 0)) if s.item and s.item.category), None)
        if it:
            o.category, o.material, o.brand_model, o.dims_hint = it.category, it.material, it.brand_model, it.dims_hint
        o.status = "identified" if o.category else "unidentified"
        o.reason = None if o.category else (reasons[0] if reasons else "low_confidence")
    return o


def build_objects(sightings: list[Sighting], frames: list[Frame],
                  links: dict[tuple[str, str], np.ndarray | None] | None = None, cfg: Config = CFG) -> list[Object]:
    fr = {f.id: f for f in frames}
    sightings = [s for s in sightings if s.frame_id in fr]
    by_id = {s.id: s for s in sightings}
    by_frame: dict[str, list[Sighting]] = defaultdict(list)
    for s in sightings:
        by_frame[s.frame_id].append(s)

    # shelf row + left-to-right position inside each frame (for the report and the Coach)
    row_pos: dict[str, tuple[str, int]] = {}
    for fid, ss in by_frame.items():
        rows: dict[str, list[Sighting]] = defaultdict(list)
        for s in ss:
            if s.kind == "book":
                rows[s.shelf_hint or "row?"].append(s)
        for rname, row in rows.items():
            for i, s in enumerate(sorted(row, key=lambda s: s.bbox[0])):
                row_pos[s.id] = (f"{fid}:{rname}", i)

    g = _Groups(sightings)
    # 1. tracking across overlapping frames
    for (a, b), H in (links or {}).items():
        if H is not None and a in by_frame and b in by_frame:
            for sa, sb in _link_pairs(by_frame[a], by_frame[b], H, cfg):
                g.union(sa.id, sb.id)

    # 3. same identified work in groups that were never in the same frame => the user came back to it
    groups = g.groups()
    frames_of = {root: {by_id[i].frame_id for i in ids} for root, ids in groups.items()}
    by_key: dict[str, list[str]] = defaultdict(list)
    for root, ids in groups.items():
        keys = g.keys.get(root) or set()
        if len(keys) == 1 and by_id[ids[0]].kind == "book":
            by_key[next(iter(keys))].append(root)
    for roots in by_key.values():
        copies: list[tuple[set[str], list[str]]] = []          # (frames seen in, groups) per physical copy
        for r in sorted(roots, key=lambda root: min(fr[f].t for f in frames_of[root])):
            c = next((c for c in copies if not (c[0] & frames_of[r])), None)
            if c:
                c[0].update(frames_of[r]); c[1].append(r)
            else:
                copies.append((set(frames_of[r]), [r]))
        for _, rs in copies:
            for r in rs[1:]:
                g.union(rs[0], r)

    # objects, ordered by first appearance (time, then left to right)
    out_groups = [[by_id[i] for i in ids] for ids in g.groups().values()]
    first = lambda grp: min((fr[s.frame_id].t, s.bbox[0]) for s in grp)
    books = sorted((x for x in out_groups if x[0].kind == "book"), key=first)
    items = sorted((x for x in out_groups if x[0].kind == "item"), key=first)
    return ([_make(f"b_{n:03d}", "book", grp, fr, row_pos) for n, grp in enumerate(books, 1)] +
            [_make(f"i_{n:03d}", "item", grp, fr, row_pos) for n, grp in enumerate(items, 1)])


def run(sd: Path, cfg: Config = CFG) -> None:
    sd = Path(sd)
    frames = store.read(sd, "frames.json", list[Frame], default=[])
    sightings = store.read(sd, "sightings.json", list[Sighting], default=[])
    store.write(sd, "objects.json", build_objects(sightings, frames, track.links(sd, frames, cfg), cfg))

"""MEASURE-BOOKS stage: objects.json + geometry.json(scale)  ->  geometry.json (+books, shelf_run_m)

Spine height / thickness from the best sighting that has a marker in its frame. Upright books:
height = box height, thickness = box width. Flat / stacked / cover_only: swapped. No marker => dims
None with reason "no_scale" (never a guess).

Uncertainty (`unc_cm`) = 2 px of box error + 5% of the value, converted with the local scale.
shelf_run_m = sum of per-book widths along the shelf (gaps ignored).
ponytail: rows are per-best-frame, so shelf_run/shelved area are approximations. Upgrade path =
shelf coordinates from marker homography or 3D.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from statistics import median

from backend.core import store
from backend.config import CFG, Config
from backend.stages.measure_scale import nearest_marker
from backend.schemas import BookDims, Frame, Geometry, Object, Sighting


def _dims(s: Sighting, ppc: float) -> tuple[float, float]:
    w, h = (s.bbox[2] - s.bbox[0]) / ppc, (s.bbox[3] - s.bbox[1]) / ppc
    swap = s.layout in ("flat", "stacked", "cover_only") or (s.layout in (None, "unknown") and w > h)
    return (w, h) if swap else (h, w)          # -> (spine height, thickness)


def run(sd: Path, cfg: Config = CFG) -> None:
    sd = Path(sd)
    objs = store.read(sd, "objects.json", list[Object], default=[])
    sightings = {s.id: s for s in store.read(sd, "sightings.json", list[Sighting], default=[])}
    geo = store.read(sd, "geometry.json", Geometry, default=Geometry())
    geo.books = {}
    for o in (o for o in objs if o.kind == "book"):
        cands = [sightings[i] for i in o.sighting_ids if i in sightings and sightings[i].frame_id in geo.scale]
        if not cands:
            geo.books[o.id] = BookDims(reason="no_scale")
            continue
        s = max(cands, key=lambda s: s.crop_blur or 0.0)
        m = nearest_marker(geo.scale[s.frame_id], s.bbox)
        height, thick = _dims(s, m.px_per_cm)
        unc = 2 / m.px_per_cm + 0.05 * height
        geo.books[o.id] = BookDims(height_cm=round(height, 1), thickness_cm=round(thick, 1),
                                   unc_cm=round(unc, 1), scale_frame_id=s.frame_id,
                                   confidence=round(max(0.0, 1 - (m.skew - 1)), 2))
    heights = [b.height_cm for b in geo.books.values() if b.height_cm]
    lo, hi = cfg.spine_height_sane_cm
    geo.scale_suspect = bool(heights) and not (lo <= median(heights) <= hi)
    rows: dict[str, list[Object]] = defaultdict(list)
    for o in objs:
        if o.kind == "book" and geo.books[o.id].thickness_cm and o.shelf:
            rows[o.shelf].append(o)
    if rows:
        run_cm = sum(geo.books[o.id].thickness_cm for r in rows.values() for o in r)
        geo.shelf_run_m = round(run_cm / 100, 2)
        geo.shelved_wall_area_m2 = round(sum(
            sum(geo.books[o.id].thickness_cm for o in r) * max(geo.books[o.id].height_cm for o in r)
            for r in rows.values()) / 1e4, 2)
    store.write(sd, "geometry.json", geo)

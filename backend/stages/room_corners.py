"""Room size from floor corners measured in the browser with WebXR (Chrome on ARCore-capable Android phones).

The user aims a crosshair at each floor corner in order around the room, then (optionally) at a point where a
wall meets the ceiling. WebXR's hit test returns each point in metres in a gravity-aligned space (y is up), so:
  floor area   shoelace area of the (x, z) polygon
  length/width the minimum-area rectangle around the corners
  height       ceiling point y - mean floor y   (ignored if < 1.5 m: probably a mis-tap)
  wall area    perimeter x height, gross (openings not subtracted)
Corners not tapped in order (the outline crosses itself) fall back to their convex hull, and the notes say so.
"""
from __future__ import annotations

import cv2
import numpy as np

from backend.schemas import Room

FT2_PER_M2 = 10.7639


def _self_intersecting(pts: np.ndarray) -> bool:
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue                                  # adjacent edges share a corner
            c, d = pts[j], pts[(j + 1) % n]
            if cross(a, b, c) * cross(a, b, d) < 0 and cross(c, d, a) * cross(c, d, b) < 0:
                return True
    return False


def room_from_corners(corners: list[list[float]], ceiling_y: float | None) -> Room:
    pts = np.array([[c[0], c[2]] for c in corners], dtype=np.float64)
    floor_y = float(np.mean([c[1] for c in corners]))
    notes = [f"{len(corners)} floor corners measured with WebXR (phone motion tracking, metres)"]
    if _self_intersecting(pts):
        pts = cv2.convexHull(pts.astype(np.float32)).reshape(-1, 2).astype(np.float64)
        notes.append("corners were not tapped in order around the room; used their convex hull (L-shapes over-estimated)")
    x, z = pts[:, 0], pts[:, 1]
    area = 0.5 * abs(float(np.dot(x, np.roll(z, -1)) - np.dot(z, np.roll(x, -1))))
    perimeter = float(np.sum(np.linalg.norm(pts - np.roll(pts, -1, axis=0), axis=1)))
    (_, _), (a, b), _ = cv2.minAreaRect(pts.astype(np.float32))
    height = round(ceiling_y - floor_y, 2) if ceiling_y is not None and ceiling_y - floor_y > 1.5 else None
    if ceiling_y is not None and height is None:
        notes.append("ceiling point was less than 1.5 m above the floor; height ignored")
    wall = perimeter * height if height else None
    notes.append("wall area is gross: openings not subtracted")
    return Room(length_m=round(max(a, b), 2), width_m=round(min(a, b), 2), height_m=height,
                floor_area_m2=round(area, 2), floor_area_ft2=round(area * FT2_PER_M2, 1),
                wall_area_m2=round(wall, 2) if wall else None, wall_area_ft2=round(wall * FT2_PER_M2, 1) if wall else None,
                scale_method="webxr_corners", notes=notes)

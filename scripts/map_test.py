"""Test position-based counting (backend/stages/shelf_map.py): detect books locally (free), register every frame on a
shared shelf map, and count. No Gemini calls.

    python scripts/map_test.py --sweep sweeps/<id> [--cache dets.json]       # frames of a real sweep, in time order
    python scripts/map_test.py a.jpg b.jpg ...                               # your own ordered frames

Scenarios: forward; forward then BACKWARD over the same frames (retracing must add no books); forward twice;
backward only. A good result: all counts about equal. Stats show how many frames could not be registered.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.stages.shelf_map import ShelfMap   # noqa: E402


def detect_all(paths: list[Path], cache: Path | None) -> dict[str, list]:
    store = json.loads(cache.read_text()) if cache and cache.exists() else {}
    todo = [p for p in paths if str(p) not in store]
    if todo:
        from backend.adapters.local_detect import LocalDetector
        det = LocalDetector()
        for p in todo:
            store[str(p)] = [list(d.bbox) for d in det.detect(cv2.imread(str(p))) if d.kind == "book"]
        if cache:
            cache.write_text(json.dumps(store))
    return store


def run(paths: list[Path], dets: dict, order: list[int]) -> ShelfMap:
    m = ShelfMap()
    for k in order:
        m.add_frame(cv2.imread(str(paths[k])), [tuple(b) for b in dets[str(paths[k])]], k)
    return m


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="*", type=Path)
    ap.add_argument("--sweep", type=Path)
    ap.add_argument("--cache", type=Path)
    ap.add_argument("--limit", type=int, default=0, help="use only the first N frames")
    a = ap.parse_args()
    paths = sorted((a.sweep / "frames").glob("*.jpg")) if a.sweep else a.images
    paths = paths[:a.limit] if a.limit else paths
    dets = detect_all(paths, a.cache)
    n = len(paths)
    fwd, back = list(range(n)), list(range(n))[::-1]
    print(f"{n} frames, books detected per frame: min {min(len(v) for v in dets.values())} "
          f"median {sorted(len(v) for v in dets.values())[n // 2]} max {max(len(v) for v in dets.values())}")
    print(f"{'scenario':<22}{'count':>6}{'components':>12}{'unregistered':>14}{'repeats':>9}{'pieces':>8}{'new':>6}{'merged':>8}")
    for name, order in (("forward", fwd), ("forward then back", fwd + back), ("forward twice", fwd + fwd), ("backward only", back)):
        m = run(paths, dets, order)
        print(f"{name:<22}{m.count:>6}{len({k.comp for k in m.keys}):>12}{m.stats['new_components']:>14}{m.stats['repeats']:>9}{m.stats['pieces']:>8}{m.stats['new_books']:>6}{m.stats['merged_components']:>8}")

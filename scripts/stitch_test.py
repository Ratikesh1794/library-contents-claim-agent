"""Live test of shelf stitching: cut photos into overlapping "frames" (like a sweep), read each with Gemini, stitch,
and compare the count with reading the whole photo at once.

    python scripts/stitch_test.py img2.jpg img5.jpg ... [--cache cache.json] [--windows 4] [--size 0.5]

For each image: whole = books Gemini finds in the full photo; naive = sum over frames (what you'd get with no
de-duplication); stitched = ShelfStitcher count after the frames come in 4 ways: forward, reverse, every frame sent
3 times, and back-and-forth. Gemini results are cached so re-runs cost nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
import gemini_books as gb                                   # noqa: E402
from backend.stages.shelf_strip import ShelfStitcher, rows_from_books   # noqa: E402


def read_cached(img: np.ndarray, cache: dict, path: Path | None) -> list[dict]:
    key = hashlib.sha1(img.tobytes()).hexdigest()
    if key not in cache:
        cache[key] = gb.read_image(img)[0]
        if path:
            path.write_text(json.dumps(cache))
    return cache[key]


def frames_of(img: np.ndarray, axis: int, n: int, size: float) -> list[np.ndarray]:
    L = img.shape[axis]
    w = int(L * size)
    starts = [int(k * (L - w) / (n - 1)) for k in range(n)]
    return [img[:, s:s + w] if axis == 1 else img[s:s + w] for s in starts]


def stitch(frames_books: list[tuple[np.ndarray, list[dict]]], order: list[int]) -> ShelfStitcher:
    st = ShelfStitcher()
    for k in order:
        img, books = frames_books[k]
        st.add_frame(rows_from_books(img, books))
    return st


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="+", type=Path)
    ap.add_argument("--cache", type=Path)
    ap.add_argument("--windows", type=int, default=4)
    ap.add_argument("--size", type=float, default=0.5, help="frame size as a fraction of the photo")
    a = ap.parse_args()
    cache = json.loads(a.cache.read_text()) if a.cache and a.cache.exists() else {}
    print(f"{'image':<10}{'whole':>6}{'naive':>7}{'forward':>9}{'reverse':>9}{'x3 each':>9}{'back&forth':>12}   titles(whole/stitched/both)")
    for path in a.images:
        img, _ = gb.load(path)
        whole = read_cached(img, cache, a.cache)
        stacked = sum(b["layout"] == "stacked" for b in whole) > len(whole) / 2
        frames = frames_of(img, 0 if stacked else 1, a.windows, a.size)
        fb = [(f, read_cached(f, cache, a.cache)) for f in frames]
        n = len(fb)
        res = {name: stitch(fb, order) for name, order in {
            "forward": list(range(n)), "reverse": list(range(n))[::-1],
            "x3": [k for k in range(n) for _ in range(3)], "bf": list(range(n)) + list(range(n))[::-1] + list(range(n))}.items()}
        titles_w = {gb_title for b in whole if (gb_title := (b["title"] or "").strip().lower())}
        titles_s = {s.title.strip().lower() for strip in res["forward"].strips for s in strip if s.title}
        print(f"{path.stem:<10}{len(whole):>6}{sum(len(b) for _, b in fb):>7}{res['forward'].count:>9}{res['reverse'].count:>9}"
              f"{res['x3'].count:>9}{res['bf'].count:>12}   {len(titles_w)}/{len(titles_s)}/{len(titles_w & titles_s)}"
              f"   strips={len(res['forward'].strips)}")

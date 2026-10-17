"""INGEST: turn saved footage (a video file or a folder of JPEGs) into a sweep dir, so the whole
pipeline can be run and re-run offline without the phone.   (NFR-E1)

    python -m backend.ingest clip.mp4 --country GB --currency GBP
    python -m backend.pipeline sweeps/<printed id>
Keeps ~2 frames/s, drops blurry ones and near-duplicates, same idea as the browser's keyframe filter.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

from backend.core import store
from backend.stages import quality
from backend.config import CFG
from backend.schemas import Frame


def ingest(src: Path, country: str, currency: str, fps_keep: float = 2.0, blur_floor: float = 30.0,
           root: Path | None = None) -> Path:
    sd = store.new_sweep(country, currency, root, source=str(src))
    frames: list[Frame] = []
    last_small = None

    def offer(img, t):
        nonlocal last_small
        s = quality.score(img)
        small = cv2.cvtColor(cv2.resize(img, (64, 36)), cv2.COLOR_BGR2GRAY).astype(np.float32)
        if s["blur"] < blur_floor or (last_small is not None and np.abs(small - last_small).mean() < 4):
            return
        last_small = small
        fid = f"f_{len(frames):04d}"
        cv2.imwrite(str(sd / "frames" / f"{fid}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        frames.append(Frame(id=fid, path=f"frames/{fid}.jpg", t=round(t, 2), w=img.shape[1], h=img.shape[0],
                            seq=len(frames), **s))

    if src.is_dir():
        for i, p in enumerate(sorted(src.glob("*.jpg")) + sorted(src.glob("*.png"))):
            offer(cv2.imread(str(p)), i / fps_keep)
    else:
        cap = cv2.VideoCapture(str(src))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        step, i = max(1, int(fps / fps_keep)), 0
        while True:
            ok, img = cap.read()
            if not ok:
                break
            if i % step == 0:
                offer(img, i / fps)
            i += 1
    store.write(sd, "frames.json", frames)
    return sd


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path)
    ap.add_argument("--country", required=True, help="two-letter code of the policyholder's country, e.g. GB")
    ap.add_argument("--currency", required=True, help="three-letter claim currency, e.g. GBP")
    a = ap.parse_args()
    d = ingest(a.src, a.country, a.currency)
    print(d)

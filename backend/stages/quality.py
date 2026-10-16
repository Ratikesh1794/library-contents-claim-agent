"""Blur / glare / darkness scores for a frame or a sighting crop.

Both measures are made resolution- and content-independent (learned from real sweeps):
  * blur  = Laplacian variance on a copy resized to `blur_norm_px` on its long side. On full-resolution phone
            photos the raw variance is tiny even for sharp text (it measures per-pixel change), so a fixed
            size is the only way one threshold works for every crop size.
  * dark  = 99th-percentile brightness, not the mean: a black spine with thin gold lettering has a low mean but
            is perfectly readable; a crop is too dark only when even its brightest parts are dark.
The browser computes its own quick scores on a 320 px copy (web/app.js) only to decide what to upload.
"""
from __future__ import annotations

import cv2
import numpy as np

from backend.config import CFG, Config


def score(img: np.ndarray, cfg: Config = CFG) -> dict:
    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    f = cfg.blur_norm_px / max(gray.shape[:2])
    small = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_AREA) if f < 1 else gray
    return {
        "blur": float(cv2.Laplacian(small, cv2.CV_64F).var()),
        "glare": float((gray > 250).mean()),
        "dark": float(np.percentile(gray, 99)),
    }


def failing_reason(s: dict, cfg: Config = CFG) -> str | None:
    """Per-crop gate: computed on the sighting crop, because one shelf can be blurry while the frame is fine."""
    if s["blur"] < cfg.blur_min:
        return "blur"
    if s["glare"] > cfg.glare_max:
        return "glare"
    if s["dark"] < cfg.dark_min:
        return "too_dark"
    return None

import cv2
import numpy as np

from backend.stages import quality
from backend.config import CFG


def test_blur_is_size_independent_and_dark_uses_highlights():
    rng = np.random.default_rng(1)
    sharp_small = rng.integers(0, 255, (100, 100), dtype=np.uint8)
    sharp_big = cv2.resize(sharp_small, (2000, 2000), interpolation=cv2.INTER_NEAREST)   # same content, 20x the pixels
    assert quality.failing_reason(quality.score(sharp_small), CFG) is None
    assert quality.failing_reason(quality.score(sharp_big), CFG) is None                  # was rejected as "blur" before
    assert quality.failing_reason(quality.score(np.full((100, 100), 120, np.uint8)), CFG) == "blur"


def test_dark_spine_with_bright_lettering_is_not_too_dark():
    spine = np.full((400, 60), 25, np.uint8)                            # near-black spine...
    cv2.putText(spine, "AB", (5, 200), cv2.FONT_HERSHEY_SIMPLEX, 1.5, 200, 3)   # ...with light lettering
    s = quality.score(spine)
    assert s["dark"] >= CFG.dark_min and quality.failing_reason(s, CFG) != "too_dark"
    really_dark = (np.random.default_rng(2).integers(0, 40, (400, 60))).astype(np.uint8)
    assert quality.failing_reason(quality.score(really_dark), CFG) == "too_dark"


def test_glare():
    s = quality.score(np.full((100, 100), 255, np.uint8)); s["blur"] = 999
    assert quality.failing_reason(s, CFG) == "glare"

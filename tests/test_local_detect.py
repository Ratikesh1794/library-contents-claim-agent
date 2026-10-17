"""Code-level parts of local detection (no model weights needed): in-frame dedupe, outline -> quad, straightened crop."""
import cv2
import numpy as np

from backend.adapters.local_detect import dedupe, quad_of
from backend.ports import Detection
from backend.stages.identify import _crop_quad, _thickness


def test_dedupe_keeps_one_per_object():
    book = Detection((100, 100, 140, 400), "book", conf=0.9)
    again = Detection((102, 98, 141, 402), "book", conf=0.5)          # same book found twice (IoU)
    fragment = Detection((105, 300, 138, 390), "book", conf=0.8)      # piece of the same book (inside it)
    neighbour = Detection((141, 100, 180, 400), "book", conf=0.9)     # the next book: kept
    chair = Detection((90, 90, 200, 420), "item", conf=0.6)           # other kind overlapping: kept
    group = Detection((98, 95, 182, 405), "book", conf=0.7)           # both books as one box: dropped
    kept = dedupe([again, fragment, book, neighbour, chair, group], 0.5, 0.8)
    assert sorted((d.kind, d.bbox) for d in kept) == sorted([("book", book.bbox), ("book", neighbour.bbox), ("item", chair.bbox)])


def test_quad_layout_and_straight_crop():
    img = np.zeros((600, 600, 3), np.uint8)
    rect = ((300, 300), (60, 400), 10)                              # a spine 60 x 400 px, tilted 10 degrees
    cv2.fillPoly(img, [cv2.boxPoints(rect).astype(np.int32)], (255, 255, 255))
    quad, layout = quad_of(cv2.boxPoints(rect))
    assert layout == "upright"
    crop = _crop_quad(img, quad, 0.0)
    h, w = crop.shape[:2]
    assert abs(h - 400) <= 3 and abs(w - 60) <= 3                   # upright again, at its true size
    assert crop[5:-5, 5:-5].mean() > 240                            # all spine, no background corners
    assert abs(_thickness(Detection((0, 0, 1, 1), "book", quad=quad)) - 60) <= 1
    _, flat = quad_of(cv2.boxPoints(((300, 300), (400, 60), 5)))
    assert flat == "flat"

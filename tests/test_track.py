import cv2
import numpy as np

from backend.core import store
from backend.stages import track
from backend.config import CFG
from backend.schemas import Frame


def scene(w=1600, h=1000, seed=0):
    """A textured 'bookshelf': random coloured spines with edges, so ORB finds features."""
    rng = np.random.default_rng(seed)
    img = np.full((h, w, 3), 230, np.uint8)
    x = 0
    while x < w:
        sw = int(rng.integers(20, 60))
        cv2.rectangle(img, (x, 100), (x + sw, 900), tuple(int(c) for c in rng.integers(0, 255, 3)), -1)
        cv2.putText(img, "AB", (x + 2, int(rng.integers(150, 850))), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        x += sw + 2
    return img


def test_homography_recovers_a_pan_and_projects_boxes():
    big = scene()
    a, b = big[:, :1200], big[:, 150:1350]          # camera panned 150 px right
    H = track.homography(a, b, CFG)
    assert H is not None
    x0, y0, x1, y1 = track.project(H, (400, 200, 440, 800))
    assert abs(x0 - 250) <= 4 and abs(y0 - 200) <= 4 and abs(x1 - 290) <= 4


def test_unrelated_frames_are_not_linked():
    assert track.homography(scene(seed=1)[:, :1200], scene(seed=2)[:, :1200], CFG) is None


def test_links_are_cached_per_sweep(tmp_path):
    sd = store.new_sweep("US", "USD", tmp_path)
    big = scene()
    frames = []
    for i, off in enumerate((0, 150, 300)):
        cv2.imwrite(str(sd / "frames" / f"f_{i:04d}.jpg"), big[:, off:off + 1200])
        frames.append(Frame(id=f"f_{i:04d}", path=f"frames/f_{i:04d}.jpg", t=i, w=1200, h=1000, blur=1, glare=0, dark=1))
    L = track.links(sd, frames, CFG)
    assert set(L) == {("f_0000", "f_0001"), ("f_0001", "f_0002"), ("f_0000", "f_0002")}   # span 2
    assert all(H is not None for H in L.values()) and (sd / "links.json").exists()
    (sd / "frames" / "f_0000.jpg").unlink()           # cached: no image needed the second time
    assert track.links(sd, frames, CFG).keys() == L.keys()

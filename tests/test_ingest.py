import cv2
import numpy as np

from backend import ingest
from backend.core import store
from backend.schemas import Frame


def test_ingest_folder_keeps_sharp_distinct_frames(tmp_path):
    src = tmp_path / "shots"; src.mkdir()
    rng = np.random.default_rng(0)
    for i in range(3):
        cv2.imwrite(str(src / f"{i}.jpg"), rng.integers(0, 255, (200, 300, 3), dtype=np.uint8))
    cv2.imwrite(str(src / "9.jpg"), np.full((200, 300, 3), 100, np.uint8))        # flat => blurry => dropped
    sd = ingest.ingest(src, "US", "USD", root=tmp_path / "out")
    frames = store.read(sd, "frames.json", list[Frame])
    assert len(frames) == 3 and all((sd / f.path).exists() for f in frames)

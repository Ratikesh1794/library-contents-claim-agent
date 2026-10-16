"""'Only analyse what is new': a frame that mostly shows the last analysed frame copies its results."""
import dataclasses

import cv2

from backend.config import CFG
from backend.core import store
from backend.ports import Detection, SpineRead
from backend.schemas import Frame, Sighting
from backend.stages import identify
from tests.fakes import FakeCatalogue, FakeVision, cand
from tests.test_track import scene

BIG = scene(w=2400, h=1000)


def sweep(tmp_path, offsets, zoom_last=False):
    """Frames cut from one wide shelf image; offset = camera pan in px."""
    sd = store.new_sweep("US", "USD", tmp_path)
    frames = []
    for i, off in enumerate(offsets):
        img = BIG[:, off:off + 1200].copy()
        if zoom_last and i == len(offsets) - 1:          # step closer: centre crop blown up 1.4x
            c = img[180:820, 200:1000]
            img = cv2.resize(c, (1200, 1000))
        cv2.imwrite(str(sd / "frames" / f"f_{i:04d}.jpg"), img)
        frames.append(Frame(id=f"f_{i:04d}", path=f"frames/f_{i:04d}.jpg", t=float(i), w=1200, h=1000, blur=100, glare=0, dark=120))
    store.write(sd, "frames.json", frames)
    return sd, frames


def identified(frame, x=400, reason=None):
    ok = reason is None
    return Sighting(id=f"s_{frame.id[2:]}_00", frame_id=frame.id, kind="book", bbox=(x, 200, x + 40, 800),
                    accepted=cand("Dune") if ok else None, score=0.95 if ok else None, reason=reason, shelf_hint="row1")


def test_mostly_same_view_is_carried_without_model_calls(tmp_path):
    sd, fr = sweep(tmp_path, [0, 60])                    # small pan: ~95% of frame 1 was in frame 0
    out = identify.carry_over(sd, fr[1], (fr[0], [identified(fr[0])]), CFG)
    assert out is not None and len(out) == 1
    s = out[0]
    assert s.carried_from == "s_0000_00" and s.frame_id == "f_0001" and s.accepted.title == "Dune"
    assert abs(s.bbox[0] - 340) <= 5                    # moved 60 px left with the pan
    assert identify.is_carried(out)


def test_new_content_or_fixable_failures_or_moving_closer_are_analysed(tmp_path):
    sd, fr = sweep(tmp_path, [0, 600])                   # big pan: half the view is new
    assert identify.carry_over(sd, fr[1], (fr[0], [identified(fr[0])]), CFG) is None
    sd, fr = sweep(tmp_path / "b", [0, 60])
    assert identify.carry_over(sd, fr[1], (fr[0], [identified(fr[0], reason="blur")]), CFG) is None   # user may be fixing it
    assert identify.carry_over(sd, fr[1], None, CFG) is None
    assert identify.carry_over(sd, fr[1], (fr[0], []), CFG) is None
    sd, fr = sweep(tmp_path / "c", [0, 0], zoom_last=True)
    assert identify.carry_over(sd, fr[1], (fr[0], [identified(fr[0])]), CFG) is None                  # stepped closer


def test_batch_run_only_calls_the_model_for_new_views(tmp_path):
    sd, fr = sweep(tmp_path, [0, 60, 120, 900])          # 1 and 2 re-show frame 0; 3 is a new part of the shelf
    v = FakeVision([Detection((400, 200, 440, 800), "book", "upright")], reads=[SpineRead("Dune", "Frank Herbert", None, True)] * 2)
    identify.run(sd, v, FakeCatalogue({"dune": [cand("Dune", "Frank Herbert")]}), dataclasses.replace(CFG, crop_workers=1))
    assert v.detect_calls == 2                           # frame 0 and frame 3 only
    per_frame = {f.id: store.read(sd, f"sightings/{f.id}.json", list[Sighting]) for f in fr}
    assert identify.is_carried(per_frame["f_0001"]) and identify.is_carried(per_frame["f_0002"])
    assert not identify.is_carried(per_frame["f_0003"])
    # frame 2 is compared with frame 0 (the last ANALYSED frame), not with the carried frame 1
    assert per_frame["f_0002"][0].carried_from == per_frame["f_0000"][0].id

import numpy as np
from backend.core import store
from backend.stages import identify
from backend.config import CFG
from backend.ports import Detection, SpineRead
from backend.schemas import Sighting
from tests.fakes import FakeCatalogue, FakeVision, cand, make_sweep

UPRIGHT = Detection((100, 50, 160, 450), "book", "upright")        # 60 x 400 px


def run_one(tmp_path, vision, catalogue, **kw):
    sd, frames = make_sweep(tmp_path)
    return identify.identify_frame(sd, frames[0], vision, catalogue, CFG)


def test_orient_picks_best(tmp_path):
    # first rotation tried returns garbage, the next one is readable
    v = FakeVision([UPRIGHT], reads=[SpineRead(None, None, None, False), SpineRead("Dune", "Frank Herbert", None, True)])
    s = run_one(tmp_path, v, FakeCatalogue({"dune": [cand("Dune", "Frank Herbert")]}))[0]
    assert s.accepted and s.accepted.title == "Dune" and s.rotation_deg == 270 and s.reason is None


def test_no_promote(tmp_path):
    v = FakeVision([UPRIGHT], reads=[SpineRead("Dnne", "Frank Herbert", None, True)] * 2)
    s = run_one(tmp_path, v, FakeCatalogue({"dnne": [cand("Quantum Gardening")]}))[0]
    assert s.raw_text == "Dnne | Frank Herbert" and s.accepted is None and s.reason == "no_catalogue_match"


def test_blur_skips_the_read(tmp_path):
    sd, frames = make_sweep(tmp_path, texture=False)       # flat grey => blur gate fails
    v = FakeVision([UPRIGHT], reads=[SpineRead("Dune", None, None, True)])
    s = identify.identify_frame(sd, frames[0], v, FakeCatalogue({}), CFG)[0]
    assert s.reason == "blur" and v.read_calls == 0


def test_rotations_that_disagree_are_unidentified(tmp_path):
    import dataclasses
    cfg = dataclasses.replace(CFG, orient_early_exit=False)
    v = FakeVision([UPRIGHT], reads=[SpineRead("Dune", "Frank Herbert", None, True), SpineRead("Emma", "Jane Austen", None, True)])
    cat = FakeCatalogue({"dune": [cand("Dune", "Frank Herbert")], "emma": [cand("Emma", "Jane Austen")]})
    sd, frames = make_sweep(tmp_path)
    s = identify.identify_frame(sd, frames[0], v, cat, cfg)[0]
    assert s.accepted is None and s.reason == "low_confidence"


def test_item_and_run_writes_sightings(tmp_path):
    sd, frames = make_sweep(tmp_path)
    v = FakeVision([Detection((10, 10, 200, 300), "item")])
    identify.run(sd, v, FakeCatalogue({}), CFG)
    out = store.read(sd, "sightings.json", list[Sighting])
    assert len(out) == 1 and out[0].item.category == "lamp"
    v2 = FakeVision([])                                  # re-run: frame already done, no detect needed
    identify.run(sd, v2, FakeCatalogue({}), CFG)
    assert len(store.read(sd, "sightings.json", list[Sighting])) == 1


def test_book_already_identified_in_previous_frame_is_not_read_again(tmp_path):
    from backend.schemas import Frame
    sd, frames = make_sweep(tmp_path, n_frames=2)
    import shutil; shutil.copy(sd / frames[0].path, sd / frames[1].path)          # identical view: identity homography
    v = FakeVision([UPRIGHT], reads=[SpineRead("Dune", "Frank Herbert", None, True)])
    cat = FakeCatalogue({"dune": [cand("Dune", "Frank Herbert")]})
    first = identify.identify_frame(sd, frames[0], v, cat, CFG)
    calls = v.read_calls
    second = identify.identify_frame(sd, frames[1], v, cat, CFG, prev=(frames[0], first))
    assert second[0].accepted.title == "Dune" and v.read_calls == calls         # reused, no new read


def test_crop_the_reader_says_is_not_a_book_is_dropped(tmp_path):
    v = FakeVision([UPRIGHT], reads=[SpineRead(None, None, None, False, is_book=False)])
    c = FakeCatalogue({})
    assert run_one(tmp_path, v, c) == [] and c.calls == 0      # e.g. a perfume box: never counted, never priced


# ---- wrong reads that used to be accepted
def test_a_garbled_orientation_cannot_override_a_different_legible_read(tmp_path):
    # orientation 1 reads a different title (legible, not in the catalogue); orientation 2 "reads" a famous one from garbage
    v = FakeVision([UPRIGHT], reads=[SpineRead("The Philosophy of Science", "Karl Popper", None, True),
                                     SpineRead("The Art of War", "Sun Tzu", None, True)])
    s = run_one(tmp_path, v, FakeCatalogue({"the art of war": [cand("The Art of War", "Sun Tzu")]}))[0]
    assert s.accepted is None and s.reason == "low_confidence"


def test_a_garbage_first_orientation_does_not_stop_the_right_read_being_accepted(tmp_path):
    v = FakeVision([UPRIGHT], reads=[SpineRead(None, None, None, False), SpineRead("Dune", "Frank Herbert", None, True)])
    s = run_one(tmp_path, v, FakeCatalogue({"dune": [cand("Dune", "Frank Herbert")]}))[0]
    assert s.accepted and s.accepted.title == "Dune"


def test_two_orientations_that_agree_are_fine(tmp_path):
    import dataclasses
    cfg = dataclasses.replace(CFG, orient_early_exit=False)
    v = FakeVision([UPRIGHT], reads=[SpineRead("The Art of War", "Sun Tzu", None, True)] * 2)
    sd, frames = make_sweep(tmp_path)
    s = identify.identify_frame(sd, frames[0], v, FakeCatalogue({"the art of war": [cand("The Art of War", "Sun Tzu")]}), cfg)[0]
    assert s.accepted and s.accepted.title == "The Art of War"


def test_one_box_around_several_books_is_dropped_but_a_lone_big_book_is_not(tmp_path):
    spines = [Detection((40 + 70 * i, 50, 100 + 70 * i, 450), "book", "upright") for i in range(5)]   # five 60 x 400 spines
    group = Detection((20, 40, 420, 460), "book", "upright")                                         # 400 x 420: 7x a spine, squarish
    v = FakeVision(spines + [group], reads=[SpineRead(None, None, None, False)] * 40)
    sd, frames = make_sweep(tmp_path)
    assert len(identify.identify_frame(sd, frames[0], v, FakeCatalogue({}), CFG)) == 5
    lone = FakeVision([Detection((20, 40, 420, 460), "book", "upright")], reads=[SpineRead(None, None, None, False)] * 4)
    assert len(identify.identify_frame(sd, frames[0], lone, FakeCatalogue({}), CFG)) == 1                 # nothing to compare with
    cover = Detection((20, 40, 300, 440), "book", "upright")                                              # a face-out cover: 280 x 400
    face = FakeVision(spines + [cover], reads=[SpineRead(None, None, None, False)] * 40)
    assert len(identify.identify_frame(sd, frames[0], face, FakeCatalogue({}), CFG)) == 6                 # ~4.7x a spine: kept

"""Books are read once and counted once (BookTracker + shelf map wired into identify and dedupe). No network."""
import shutil

import numpy as np

from backend.config import CFG
from backend.core import store
from backend.ports import Detection, SpineRead
from backend.schemas import Object, Sighting
from backend.stages import dedupe, identify
from backend.stages.book_tracker import BookTracker
from backend.stages.shelf_map import Book
from tests.fakes import FakeCatalogue, FakeVision, cand, make_sweep

UPRIGHT = Detection((100, 50, 160, 450), "book", "upright")        # 60 x 400 px


def same_view(tmp_path, n):
    """n identical textured frames: the camera did not move, so every book lands on the same map spot."""
    sd, frames = make_sweep(tmp_path, n_frames=n)
    for f in frames[1:]:
        shutil.copy(sd / frames[0].path, sd / f.path)
    return sd, frames


def identify_all(sd, frames, vision, catalogue, tracker):
    return [identify.identify_frame(sd, f, vision, catalogue, CFG, tracker=tracker) for f in frames]


def test_an_identified_book_is_read_once_however_many_frames_show_it(tmp_path):
    sd, frames = same_view(tmp_path, 3)
    v = FakeVision([UPRIGHT], reads=[SpineRead("Dune", "Frank Herbert", None, True)] * 9)
    t = BookTracker()
    out = identify_all(sd, frames, v, FakeCatalogue({"dune": [cand("Dune", "Frank Herbert")]}), t)
    assert v.read_calls == 1                                   # first frame only (no previous frame is passed in)
    assert [s[0].accepted.title for s in out] == ["Dune"] * 3 and t.count == 1


def test_a_book_that_could_not_be_read_is_not_paid_for_again_from_an_equal_crop(tmp_path):
    sd, frames = same_view(tmp_path, 3)
    v = FakeVision([UPRIGHT], reads=[SpineRead(None, None, None, False)] * 30)
    t = BookTracker()
    first = identify.identify_frame(sd, frames[0], v, FakeCatalogue({}), CFG, tracker=t)
    calls = v.read_calls
    assert calls > 0 and first[0].accepted is None
    rest = [identify.identify_frame(sd, f, v, FakeCatalogue({}), CFG, tracker=t) for f in frames[1:]]
    assert v.read_calls == calls                               # same sharpness: the retry rule says skip
    assert all(len(r) == 1 and r[0].accepted is None and r[0].reason for r in rest) and t.count == 1


def test_something_the_reader_called_not_a_book_is_dropped_in_every_later_frame(tmp_path):
    sd, frames = same_view(tmp_path, 3)
    v = FakeVision([UPRIGHT], reads=[SpineRead(None, None, None, False, is_book=False)] * 9)
    t = BookTracker()
    out = identify_all(sd, frames, v, FakeCatalogue({}), t)
    assert out == [[], [], []] and v.read_calls == 1 and t.count == 0          # e.g. a perfume box: one read, ever


def test_retry_rules():
    t = BookTracker()
    b = Book(0, np.zeros(4), 0)
    assert t.plan(b, 50)[0] == "read"                          # never tried
    t.note_read(b, 100)
    assert t.plan(b, 120)[0] == "skip"                         # not clearly sharper
    assert t.plan(b, 160)[0] == "read"                         # 1.6x sharper: worth another try
    t.note_read(b, 160); t.note_read(b, 400)
    assert t.plan(b, 10_000)[0] == "skip"                      # read_tries used up
    ok = Sighting(id="s", frame_id="f", kind="book", bbox=(0, 0, 1, 1), accepted=cand("Dune"))
    t.attach(ok, b)
    assert t.plan(b, 1)[0] == "copy"                           # identified once: always reuse it
    t.note_read(b, 1, is_book=False)
    assert t.plan(b, 1)[0] == "drop"


def test_dedupe_counts_a_never_read_book_once_and_an_item_once(tmp_path):
    sd, frames = same_view(tmp_path, 3)
    book = lambda i: Sighting(id=f"s_{i}_b", frame_id=f"f_{i:04d}", kind="book", bbox=(100, 50, 160, 450), reason="blur")
    item = lambda i: Sighting(id=f"s_{i}_i", frame_id=f"f_{i:04d}", kind="item", bbox=(300, 300, 500, 500))
    store.write(sd, "sightings.json", [x(i) for i in range(3) for x in (book, item)])
    dedupe.run(sd)
    objs = store.read(sd, "objects.json", list[Object])
    assert sum(o.kind == "book" for o in objs) == 1 and sum(o.kind == "item" for o in objs) == 1


def test_dedupe_falls_back_to_frame_tracking_when_an_image_is_missing(tmp_path):
    sd, frames = same_view(tmp_path, 2)
    s = [Sighting(id=f"s_{i}", frame_id=f"f_{i:04d}", kind="book", bbox=(100, 50, 160, 450), reason="blur") for i in range(2)]
    store.write(sd, "sightings.json", s)
    (sd / frames[1].path).unlink()
    dedupe.run(sd)                                             # must not raise
    assert len(store.read(sd, "objects.json", list[Object])) >= 1


def test_the_batch_pass_does_not_re_read_a_book_the_live_pass_already_read(tmp_path):
    sd, frames = same_view(tmp_path, 2)
    A, B = (100, 50, 160, 450), (300, 50, 360, 450)
    live = [Sighting(id="s_0000_00", frame_id="f_0000", kind="book", bbox=A, accepted=cand("Dune", "Frank Herbert"), score=1.0),
            Sighting(id="s_0000_01", frame_id="f_0000", kind="book", bbox=B, reason="blur")]    # B still needs a look
    store.write(sd, "sightings/f_0000.json", live)             # frame 1 is missing and, with B unread, must be analysed
    v = FakeVision([Detection(A, "book", "upright"), Detection(B, "book", "upright")],
                   reads=[SpineRead("Emma", "Jane Austen", None, True)] * 9)
    cat = FakeCatalogue({"dune": [cand("Dune", "Frank Herbert")], "emma": [cand("Emma", "Jane Austen")]})
    identify.run(sd, v, cat, CFG)
    assert v.read_calls == 1                                   # only B was read; A came from the rebuilt shelf map
    got = {s.bbox: s.accepted.title for s in store.read(sd, "sightings/f_0001.json", list[Sighting])}
    assert got == {A: "Dune", B: "Emma"}                       # had A been read again it would have become "Emma"


def test_a_read_is_not_copied_to_a_crop_that_looks_like_a_different_book():
    t = BookTracker()
    b = Book(0, np.zeros(4), 0)
    t.note_read(b, 100, sig=(60.0, 10.0, 10.0))                      # what the book that was read looks like
    ok = Sighting(id="s", frame_id="f", kind="book", bbox=(0, 0, 1, 1), accepted=cand("Dune"))
    t.attach(ok, b)
    assert t.plan(b, 100, (62.0, 12.0, 8.0))[0] == "copy"            # same colours: the same book
    assert t.plan(b, 100, (30.0, 60.0, -40.0))[0] == "read"          # clearly different colours: look at it, don't copy
    assert t.plan(b, 100, None)[0] == "copy"                         # no signature = no objection


def test_a_box_that_lands_on_a_read_book_but_shows_another_book_is_read_not_copied(tmp_path):
    import cv2
    sd, frames = same_view(tmp_path, 2)
    img = cv2.imread(str(sd / frames[1].path))                       # frame 1: a much darker book where the first one was
    x0, y0, x1, y1 = UPRIGHT.bbox
    img[y0:y1, x0:x1] = np.random.default_rng(5).integers(0, 40, (y1 - y0, x1 - x0, 3)).astype(np.uint8)
    cv2.imwrite(str(sd / frames[1].path), img)
    v = FakeVision([UPRIGHT], reads=[SpineRead("Dune", "Frank Herbert", None, True), SpineRead("Emma", "Jane Austen", None, True)])
    cat = FakeCatalogue({"dune": [cand("Dune", "Frank Herbert")], "emma": [cand("Emma", "Jane Austen")]})
    t = BookTracker()
    first = identify.identify_frame(sd, frames[0], v, cat, CFG, tracker=t)
    second = identify.identify_frame(sd, frames[1], v, cat, CFG, tracker=t)
    assert first[0].accepted.title == "Dune" and second[0].accepted.title == "Emma"      # a copy would have said "Dune"


# ---------------------------------------------------------------- items (chairs, cabinets...): described once
class CountingVision(FakeVision):
    def __init__(self, *a, **k): super().__init__(*a, **k); self.describe_calls = 0
    def describe_item(self, crop):
        self.describe_calls += 1
        return super().describe_item(crop)


LAMP = Detection((300, 300, 500, 500), "item")


def test_an_item_is_described_once_however_many_frames_show_it(tmp_path):
    sd, frames = same_view(tmp_path, 3)
    v = CountingVision([LAMP])
    t = BookTracker()
    out = identify_all(sd, frames, v, FakeCatalogue({}), t)
    assert v.describe_calls == 1
    assert [s[0].item.category for s in out] == ["lamp"] * 3


def test_an_item_box_that_shows_something_else_is_described_not_copied(tmp_path):
    import cv2
    sd, frames = same_view(tmp_path, 2)
    img = cv2.imread(str(sd / frames[1].path))
    x0, y0, x1, y1 = LAMP.bbox
    img[y0:y1, x0:x1] = np.random.default_rng(9).integers(0, 40, (y1 - y0, x1 - x0, 3)).astype(np.uint8)   # a darker object
    cv2.imwrite(str(sd / frames[1].path), img)
    v = CountingVision([LAMP])
    t = BookTracker()
    identify_all(sd, frames, v, FakeCatalogue({}), t)
    assert v.describe_calls == 2


def test_items_are_never_counted_as_books():
    import cv2
    t = BookTracker()
    img = np.random.default_rng(1).integers(0, 255, (700, 600, 3), dtype=np.uint8)
    book = Sighting(id="b", frame_id="f0", kind="book", bbox=(100, 50, 160, 450))
    lamp = Sighting(id="i", frame_id="f0", kind="item", bbox=(300, 300, 500, 500))
    t.add_sightings("f0", img, [book, lamp])
    assert t.count == 1                                                          # the book, not the lamp
    assert sorted(len(g) for g in t.groups()) == [1, 1]                          # but both are tracked


def test_the_item_registry_matches_by_iou_and_stays_apart_from_books():
    from backend.stages.shelf_map import ShelfMap
    img = np.random.default_rng(2).integers(0, 255, (700, 900, 3), dtype=np.uint8)
    m = ShelfMap()
    m.add_frame(img, [(100, 50, 160, 450)])
    a = m.place_items([(300, 300, 500, 500)])[0]
    b = m.place_items([(305, 302, 505, 498)])[0]                                 # the same object a moment later
    c = m.place_items([(600, 300, 800, 500)])[0]                                 # another object
    inside = m.place_items([(320, 320, 400, 400)])[0]                            # small box inside the first: NOT the same item
    assert a is b and a is not c and inside is not a and len(m.items) == 3 and m.count == 1

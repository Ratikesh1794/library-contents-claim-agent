import numpy as np

from backend.stages.dedupe import build_objects
from backend.schemas import Frame, ItemGuess, Sighting
from tests.fakes import cand


def fr(i, t=None):
    return Frame(id=f"f_{i:04d}", path=f"frames/f_{i:04d}.jpg", t=float(i if t is None else t), w=1000, h=1000, blur=100, glare=0, dark=100)


def shift(dx):           # homography of a camera pan: scene moves dx px left->right between frames
    return np.array([[1, 0, dx], [0, 1, 0], [0, 0, 1]], float)


def book(fid, x, title=None, row="row1", y=100, w=40):
    return Sighting(id=f"s_{fid}_{x}", frame_id=f"f_{fid:04d}", kind="book", bbox=(x, y, x + w, y + 300),
                    shelf_hint=row, accepted=cand(title) if title else None, score=0.95 if title else None,
                    crop_blur=50.0, reason=None if title else "blur")


def item(fid, x, cat="lamp"):
    return Sighting(id=f"i_{fid}_{x}", frame_id=f"f_{fid:04d}", kind="item", bbox=(x, 500, x + 200, 800),
                    item=ItemGuess(category=cat))


def test_unread_book_tracked_across_frames_counts_once():
    # the camera pans 30 px per frame; the same unreadable spine appears in 5 frames
    ss = [book(i, 100 - 30 * i) for i in range(5)]
    links = {(f"f_{i:04d}", f"f_{i + 1:04d}"): shift(-30) for i in range(4)}
    objs = build_objects(ss, [fr(i) for i in range(5)], links)
    assert len(objs) == 1 and len(objs[0].sighting_ids) == 5 and objs[0].status == "unidentified"


def test_one_good_read_names_the_tracked_book():
    ss = [book(0, 100), book(1, 70, "Dune"), book(2, 40)]
    links = {("f_0000", "f_0001"): shift(-30), ("f_0001", "f_0002"): shift(-30)}
    objs = build_objects(ss, [fr(i) for i in range(3)], links)
    assert len(objs) == 1 and objs[0].title == "Dune" and objs[0].status == "identified"


def test_without_overlap_unread_books_stay_separate():
    # no link between the frames (camera moved to another shelf): never merge unread books blindly
    objs = build_objects([book(0, 100), book(1, 100)], [fr(0), fr(1)], {("f_0000", "f_0001"): None})
    assert len(objs) == 2


def test_two_copies_side_by_side_count_two():
    ss = [book(0, 100, "Dune"), book(0, 150, "Dune"), book(1, 70, "Dune"), book(1, 120, "Dune")]
    objs = build_objects(ss, [fr(0), fr(1)], {("f_0000", "f_0001"): shift(-30)})
    assert len(objs) == 2


def test_returning_to_a_shelf_later_does_not_double_count():
    # frames 0 and 5 don't overlap (user walked away and came back) but read the same single copy
    objs = build_objects([book(0, 100, "Dune"), book(5, 400, "Dune")], [fr(0), fr(5)], {})
    assert len(objs) == 1 and len(objs[0].sighting_ids) == 2


def test_tracking_never_merges_two_different_books():
    # a bad link lands Dune's box on Emma's box: they must stay two books
    objs = build_objects([book(0, 100, "Dune"), book(1, 100, "Emma")], [fr(0), fr(1)], {("f_0000", "f_0001"): shift(0)})
    assert sorted(o.title for o in objs) == ["Dune", "Emma"]


def test_items_tracked_and_kinds_never_mixed():
    ss = [item(0, 300), item(1, 270), book(0, 300, w=200)]       # book box overlaps the item box in frame 0
    objs = build_objects(ss, [fr(0), fr(1)], {("f_0000", "f_0001"): shift(-30)})
    assert sum(o.kind == "item" for o in objs) == 1 and sum(o.kind == "book" for o in objs) == 1


def test_loosely_boxed_item_still_tracks_but_different_categories_do_not():
    tight = Sighting(id="a", frame_id="f_0000", kind="item", bbox=(300, 500, 400, 650), item=ItemGuess(category="lamp"))
    loose = Sighting(id="b", frame_id="f_0001", kind="item", bbox=(150, 400, 700, 900), item=ItemGuess(category="lamp"))
    other = Sighting(id="c", frame_id="f_0001", kind="item", bbox=(150, 400, 700, 900), item=ItemGuess(category="rug"))
    L = {("f_0000", "f_0001"): shift(0)}
    assert len(build_objects([tight, loose], [fr(0), fr(1)], L)) == 1          # lamp boxed tight, then with its table
    assert len(build_objects([tight, other], [fr(0), fr(1)], L)) == 2          # a rug is not the lamp


def test_evidence_prefers_a_detected_box_over_a_carried_copy():
    real = book(0, 100, "Dune")
    copy = book(1, 70, "Dune").model_copy(update={"carried_from": real.id, "crop_blur": 999.0})
    objs = build_objects([real, copy], [fr(0), fr(1)], {("f_0000", "f_0001"): shift(-30)})
    assert len(objs) == 1 and objs[0].frame_ref == "frames/f_0000.jpg"

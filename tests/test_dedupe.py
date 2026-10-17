import dataclasses

import numpy as np

from backend.config import CFG
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
    one = dataclasses.replace(CFG, item_min_sightings=1)
    assert len(build_objects([tight, other], [fr(0), fr(1)], L, one)) == 2     # a rug is not the lamp


def test_evidence_prefers_a_detected_box_over_a_carried_copy():
    real = book(0, 100, "Dune")
    copy = book(1, 70, "Dune").model_copy(update={"carried_from": real.id, "crop_blur": 999.0})
    objs = build_objects([real, copy], [fr(0), fr(1)], {("f_0000", "f_0001"): shift(-30)})
    assert len(objs) == 1 and objs[0].frame_ref == "frames/f_0000.jpg"


def test_a_title_seen_twice_apart_in_only_one_frame_is_a_ghost_not_a_second_copy():
    # one blurry frame shows the same spine twice, shifted by about its own width; the other frames show it once
    ss = [book(0, 100, "Dune"), book(1, 70, "Dune"), book(2, 40, "Dune"), book(1, 130, "Dune")]
    objs = build_objects(ss, [fr(0), fr(1), fr(2)], {("f_0000", "f_0001"): shift(-30), ("f_0001", "f_0002"): shift(-30)})
    assert len(objs) == 1


def test_the_same_pair_apart_in_two_frames_is_two_copies():
    ss = [book(0, 100, "Dune"), book(0, 150, "Dune"), book(1, 70, "Dune"), book(1, 120, "Dune")]
    assert len(build_objects(ss, [fr(0), fr(1)], {("f_0000", "f_0001"): shift(-30)})) == 2


def test_items_of_different_categories_are_never_merged_even_if_the_map_put_them_together():
    one = dataclasses.replace(CFG, item_min_sightings=1)
    ss = [item(0, 100, "lamp"), item(1, 100, "rug")]
    objs = build_objects(ss, [fr(0), fr(1)], None, one, book_groups=[[ss[0].id, ss[1].id]])
    assert len(objs) == 2
    same = build_objects([item(0, 100, "lamp"), item(1, 100, "lamp")], [fr(0), fr(1)], None,
                         book_groups=[["i_0_100", "i_1_100"]])
    assert len(same) == 1


def test_items_must_be_a_category_the_claim_asks_for_and_seen_in_two_frames():
    ok = [item(0, 100, "lamp"), item(1, 100, "lamp")]                                   # a lamp seen twice: kept
    once = [item(0, 600, "furniture")]                                                  # seen once: a fragment
    other = [item(0, 700, "other"), item(1, 700, "other")]                              # "other": not asked for
    undescribed = [Sighting(id="u0", frame_id="f_0000", kind="item", bbox=(10, 10, 60, 60)),
                   Sighting(id="u1", frame_id="f_0001", kind="item", bbox=(10, 10, 60, 60))]
    links = {("f_0000", "f_0001"): shift(0)}
    objs = [o for o in build_objects(ok + once + other + undescribed, [fr(0), fr(1)], links) if o.kind == "item"]
    assert [(o.id, o.category) for o in objs] == [("i_001", "lamp")]                    # and the id is renumbered, no gaps


def test_an_item_left_and_returned_to_counts_once_but_two_side_by_side_count_twice():
    one = dataclasses.replace(CFG, item_min_sightings=1)
    named = lambda s, n: s.model_copy(update={"item": s.item.model_copy(update={"name": n})})
    # the desk at frames 0 and 3, no tracking link between the visits (the camera swung away)
    desk = [named(item(0, 100, "furniture"), "wooden desk"), named(item(3, 400, "furniture"), "wooden desk")]
    assert len(build_objects(desk, [fr(0), fr(3)], None, one)) == 1
    # two chairs in one frame, clearly apart: two items
    chairs = [named(item(0, 100, "furniture"), "office chair"), named(item(0, 600, "furniture"), "office chair")]
    assert len(build_objects(chairs, [fr(0)], None, one)) == 2


def test_one_stray_label_does_not_merge_two_different_items():
    one = dataclasses.replace(CFG, item_min_sightings=1)
    named = lambda s, n: s.model_copy(update={"item": s.item.model_copy(update={"name": n})})
    big = [named(item(0, 100, "electronics"), "floor speaker"), named(item(1, 100, "electronics"), "floor speaker"),
           named(item(2, 100, "electronics"), "wireless speaker")]                  # one mislabelled sighting
    small = [named(item(5, 400, "electronics"), "wireless speaker")]
    objs = build_objects(big + small, [fr(i) for i in (0, 1, 2, 5)], None, one, book_groups=[[s.id for s in big]])
    assert len(objs) == 2

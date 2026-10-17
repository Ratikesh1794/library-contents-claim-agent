"""Proactive guidance: what is said, how often, and never over the user or over another message. No network."""
from backend.config import CFG
from backend.live.guide import Guide


def test_blurry_gets_a_slow_down_message():
    m = Guide(CFG).on_blurry(100.0)
    assert m["type"] == "GUIDE" and m["key"] == "slow" and "Slow down" in m["ask"]


def test_not_repeated_inside_the_cooldown_and_firmer_when_it_continues():
    g = Guide(CFG)
    first = g.on_blurry(100.0)["ask"]
    assert g.on_blurry(105.0) is None                              # same topic again too soon
    second = g.on_blurry(100.0 + CFG.guide_slow_gap_s + 1)["ask"]  # still blurry after the cooldown: firmer
    assert second != first and "hold the phone still" in second
    third = g.on_blurry(100.0 + 2 * CFG.guide_slow_gap_s + 2)["ask"]
    assert third == second                                         # stays at the firmest wording, no further escalation


def test_the_wording_resets_after_a_quiet_spell():
    g = Guide(CFG)
    first = g.on_blurry(100.0)["ask"]
    g.on_blurry(100.0 + CFG.guide_slow_gap_s + 1)
    later = g.on_blurry(100.0 + CFG.guide_repeat_window_s + 100)["ask"]
    assert later == first


def test_never_over_the_user_or_right_after_something_else_was_spoken():
    g = Guide(CFG)
    assert g.on_blurry(100.0, last_user_speech_t=99.0) is None     # the user spoke 1 s ago
    g.note_spoken(100.0)                                           # the coach just said something
    assert g.on_blurry(100.0 + CFG.guide_gap_s - 1) is None
    assert g.on_blurry(100.0 + CFG.guide_gap_s + 1) is not None


def test_screens_get_a_coach_event_whatever_the_message_type():
    from backend.live.guide import screen_event
    for kind in ("CAPTURE_ISSUE", "STATUS", "GUIDE"):
        e = screen_event({"type": kind, "ask": "Hold steady."})
        assert e["type"] == "coach" and e["kind"] == kind and e["ask"] == "Hold steady."


# ---------------------------------------------------------------- books in view that stay unreadable
import numpy as np

from backend.live.guide import where
from backend.schemas import Sighting
from backend.stages.shelf_map import Book


def book(i, reason="low_confidence", box=(50, 300, 150, 700), accepted=False, row="row1"):
    from tests.fakes import cand
    s = Sighting(id=f"s{i}", frame_id="f", kind="book", bbox=box, reason=reason, shelf_hint=row,
                 accepted=cand("Dune") if accepted else None)
    return s, Book(i, np.zeros(4), 0)


W, H = 900, 900                                              # box above: centre (100, 500) = left column, middle row


def test_nothing_is_said_until_a_book_stays_unreadable_for_a_moment():
    g, v = Guide(CFG), [book(1)]
    assert g.on_frame(100.0, W, H, v) is None                                   # just appeared: the next frame may be sharper
    m = g.on_frame(100.0 + CFG.guide_settle_s + 0.1, W, H, v)
    assert m["type"] == "GUIDE" and m["ask"] == "Hold steady on the book on the left for a second."


def test_the_ask_depends_on_why_it_failed():
    said = {}
    for reason in ("too_far", "glare", "too_dark", "blocked", "partial", "blur"):
        g, v = Guide(CFG), [book(1, reason)]
        g.on_frame(100.0, W, H, v)
        said[reason] = g.on_frame(103.0, W, H, v)["ask"]
    assert said["too_far"].startswith("Move a bit closer") and "glare" in said["glare"] and "too dark" in said["too_dark"]
    assert "in front" in said["blocked"] and "Pan a little further" in said["partial"] and said["blur"].startswith("Hold steady")


def test_failures_the_user_cannot_fix_are_never_mentioned():
    g = Guide(CFG)
    v = [book(1, "author_not_visible"), book(2, "no_catalogue_match"), book(3, "ambiguous_match"), book(4, accepted=True)]
    g.on_frame(100.0, W, H, v)
    assert g.on_frame(110.0, W, H, v) is None


def test_where_the_books_are_in_words():
    assert where([("left", "middle")]) == "the book on the left"
    assert where([("right", "middle"), ("right", "bottom")]) == "the books on the right"
    assert where([("left", "top")]) == "the book at the top left"
    assert where([("middle", "bottom")]) == "the book at the bottom"
    assert where([("middle", "middle")]) == "the book in the middle"
    assert where([("left", "top"), ("right", "bottom"), ("middle", "middle")]) == "the books in view"
    from backend.live.guide import cell
    assert cell((0, 0, 100, 100), 900, 900) == ("left", "top") and cell((800, 800, 900, 900), 900, 900) == ("right", "bottom")


def test_the_same_book_is_asked_twice_then_given_up_once_then_left_alone():
    g, v = Guide(CFG), [book(1)]
    t = 100.0
    g.on_frame(t, W, H, v)
    gap = CFG.guide_book_gap_s + 1
    t += 3; first = g.on_frame(t, W, H, v)
    assert g.on_frame(t + 1, W, H, v) is None                                   # not again straight away
    t += gap; second = g.on_frame(t, W, H, v)
    t += gap; third = g.on_frame(t, W, H, v)
    t += gap; after = g.on_frame(t, W, H, v)
    assert first["key"] == second["key"] == "book:hold"
    assert third["key"] == "giveup" and "I will note it and move on" in third["ask"]
    assert after is None                                                         # it is closed: no more about this book


def test_a_book_that_gets_read_stops_being_asked_about():
    g, (s, b) = Guide(CFG), book(1)
    g.on_frame(100.0, W, H, [(s, b)])
    b.meta["read"] = s                                                           # the tracker identified it meanwhile
    assert g.on_frame(110.0, W, H, [(s, b)]) is None


def test_skip_this_shelf_silences_the_books_in_that_row():
    class T:
        def __init__(self, v): self.v = v
        def view(self, frame_id): return self.v
    g, v = Guide(CFG), [book(1, row="row1"), book(2, row="row2", box=(500, 300, 600, 700))]
    g.on_frame(100.0, W, H, v)
    assert g.skip(T(v), "f", "row1") == 1
    m = g.on_frame(110.0, W, H, v)
    assert m and "on the left" not in m["ask"]                                   # row 1 is skipped, row 2 still asked about

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


# ---------------------------------------------------------------- go back to books that were passed unread
from backend.stages.book_tracker import BookTracker
from tests.fakes import cand
from tests.test_shelf_map import BOOKS, WIN, frame


def sweep_sightings(x, unreadable=(0,)):
    """The books in view at wall position x as sightings; the listed wall books could not be read."""
    img, boxes = frame(x)
    out = []
    for (bx0, _, bx1, _), box in zip([b for b in BOOKS if min(b[2], x + WIN) - max(b[0], x) >= 15], boxes):
        i = BOOKS.index(next(b for b in BOOKS if b[0] == bx0))
        bad = i in unreadable
        out.append(Sighting(id=f"s{x}_{i}", frame_id=f"f{x}", kind="book", bbox=box, shelf_hint="row1",
                            reason="low_confidence" if bad else None, accepted=None if bad else cand(f"Book {i}")))
    return img, out


def walk(guide, tracker, xs, t0=100.0, dt=2.0, unreadable=(0,)):
    said = []
    for k, x in enumerate(xs):
        img, ss = sweep_sightings(x, unreadable)
        tracker.add_sightings(f"f{x}", img, ss)
        m = guide.on_frame(t0 + k * dt, WIN, 700, tracker.view(f"f{x}"), tracker=tracker)
        if m:
            said.append((k, m))
    return said


def test_go_back_to_an_unreadable_book_you_passed_on_your_left():
    said = walk(Guide(CFG), BookTracker(), [0, 300, 600, 900, 1200])
    back = [(k, m) for k, m in said if m["key"] == "back"]
    assert back and back[0][1]["ask"] == "I could not read the book you passed on your left. Go back and hold steady on it."
    assert back[0][0] >= 2                                           # not the moment it left the view: it had to be out of view a while


def test_sweeping_leftwards_the_passed_book_is_on_your_right():
    said = walk(Guide(CFG), BookTracker(), [1500, 1200, 900, 600, 300], unreadable=(11,))
    back = [m for _, m in said if m["key"] == "back"]
    assert back and "on your right" in back[0]["ask"]


def test_go_back_is_not_repeated_straight_away_and_stops_after_two_asks():
    g = Guide(CFG)
    t = BookTracker()
    said = walk(g, t, [0, 300, 600, 900, 1200, 1500, 1200, 900, 600, 300, 0], dt=CFG.guide_back_gap_s + 1)
    assert [m["key"] for _, m in said].count("back") <= CFG.guide_back_asks


def test_a_book_read_in_the_meantime_is_never_asked_about():
    tracker = BookTracker()
    img, ss = sweep_sightings(0)
    tracker.add_sightings("f0", img, ss)
    for _, b in tracker.view("f0"):
        if b.meta.get("reason"):
            b.meta["read"] = ss[0]                                   # the tracker identified it from another frame
    assert [m for _, m in walk(Guide(CFG), tracker, [300, 600, 900, 1200]) if m["key"] == "back"] == []


def test_books_that_cannot_be_fixed_are_not_worth_going_back_for():
    g, t = Guide(CFG), BookTracker()
    img, ss = sweep_sightings(0)
    ss[0].reason = "author_not_visible"                              # the spine just has no author on it
    t.add_sightings("f0", img, ss)
    g.on_frame(100.0, WIN, 700, t.view("f0"), tracker=t)
    assert [m for _, m in walk(g, t, [300, 600, 900, 1200]) if m["key"] == "back"] == []


def test_in_view_problems_come_before_going_back():
    g, t = Guide(CFG), BookTracker()
    said = walk(g, t, [0, 300, 600], unreadable=(0, 4))              # book 4 (wall x 780..880) is in view at 600 and unreadable
    keys = [m["key"] for _, m in said]
    assert "back" not in keys                                        # nothing about the passed book while one is in view


def test_a_book_in_view_that_was_already_asked_about_does_not_block_going_back():
    class T:
        def __init__(self, items): self.items = items
        def offscreen(self, in_view): return self.items
    g = Guide(CFG)
    s1, b1 = book(1)                                                 # in view, unreadable
    b1.meta.update(seen_t=90.0, pending_since=90.0, asked=1, asked_t=95.0, reason="low_confidence")
    far = Book(2, np.zeros(4), 0)                                    # passed 20 s ago, now off the left edge
    far.meta.update(seen_t=80.0, reason="low_confidence")
    m = g.on_frame(100.0, W, H, [(s1, b1)], tracker=T([(far, -500.0, 300.0, W, H)]))
    assert m and m["key"] == "back" and "on your left" in m["ask"]


def test_go_back_waits_while_a_book_in_view_is_about_to_be_asked_about():
    class T:
        def offscreen(self, in_view): return [(far, -500.0, 300.0, W, H)]
    g = Guide(CFG)
    s1, b1 = book(1)
    far = Book(2, np.zeros(4), 0)
    far.meta.update(seen_t=80.0, reason="low_confidence")
    assert g.on_frame(100.0, W, H, [(s1, b1)], tracker=T()) is None          # b1 just appeared: its ask comes first


# ---------------------------------------------------------------- progress and "this area looks done"
def test_progress_is_announced_at_every_tenth_book_only():
    g, said = Guide(CFG), []
    for n in range(1, 26):
        m = g.on_tick(100.0 + n * CFG.guide_progress_gap_s, n)       # a new book each tick, spaced far enough apart
        if m:
            said.append((n, m["ask"]))
    assert said == [(10, "That is 10 books found so far. Keep going."), (20, "That is 20 books found so far. Keep going.")]


def test_a_jump_past_a_milestone_is_announced_once_with_the_real_number():
    g = Guide(CFG)
    assert g.on_tick(100.0, 3) is None
    assert g.on_tick(200.0, 27)["ask"] == "That is 27 books found so far. Keep going."
    assert g.on_tick(300.0, 28) is None


def test_the_area_looks_done_after_a_quiet_spell_and_never_before_any_book():
    g = Guide(CFG)
    assert g.on_tick(100.0, 0) is None and g.on_tick(500.0, 0) is None            # nothing found yet: nothing to be done with
    g.on_tick(1000.0, 4)                                                          # four books found
    assert g.on_tick(1000.0 + CFG.guide_idle_s - 1, 4) is None
    m = g.on_tick(1000.0 + CFG.guide_idle_s + 1, 4)
    assert m["key"] == "idle" and m["ask"].startswith("That area looks done") and "say finish" in m["ask"]


def test_a_new_book_restarts_the_quiet_timer():
    g = Guide(CFG)
    g.on_tick(1000.0, 4)
    assert g.on_tick(1000.0 + CFG.guide_idle_s - 1, 5) is None                    # a fifth book: still sweeping
    assert g.on_tick(1000.0 + 2 * CFG.guide_idle_s - 2, 5) is None                # the clock restarted at that fifth book
    assert g.on_tick(1000.0 + 2 * CFG.guide_idle_s, 5)["key"] == "idle"


def test_not_done_while_a_book_in_view_still_needs_attention():
    g = Guide(CFG)
    g.on_tick(1000.0, 4)
    s, b = book(1)
    g.on_frame(1000.0 + CFG.guide_idle_s - 1, W, H, [(s, b)])                     # an unreadable book is in view right now
    assert g.on_tick(1000.0 + CFG.guide_idle_s + 1, 4) is None                    # so the user is busy, not finished


def test_not_done_straight_after_something_else_was_said():
    g = Guide(CFG)
    g.on_tick(1000.0, 4)
    g.note_spoken(1000.0 + CFG.guide_idle_s - 1)                                  # e.g. "go back to the book on your left"
    assert g.on_tick(1000.0 + CFG.guide_idle_s + 1, 4) is None


def test_the_second_done_message_is_shorter_and_says_how_to_finish_and_they_are_spaced_out():
    g = Guide(CFG)
    g.on_tick(1000.0, 4)
    t = 1000.0 + CFG.guide_idle_s + 1
    first = g.on_tick(t, 4)["ask"]
    assert g.on_tick(t + 5, 4) is None
    second = g.on_tick(t + CFG.guide_idle_gap_s + 1, 4)["ask"]
    assert first != second and second.startswith("Nothing new for a while")


def test_setup_focus_then_room_then_the_sweep():
    g = Guide(CFG)
    assert g.on_tick(10, 0) is None and not g.in_setup                    # no hello (older phone): no setup at all
    g.setup_event(0, "hello", ar=True)
    assert g.setup_tick(1) is None                                        # let the greeting finish first
    assert g.setup_tick(4)["key"] == "setup:focus"
    assert g.on_blurry(12) is None and g.on_frame(12, 100, 100, []) is None   # book guidance waits for setup
    g.setup_event(13, "focused")
    assert g.setup_tick(17)["key"] == "setup:room"
    g.setup_event(30, "room", area=12.5)
    m = g.setup_tick(30)
    assert m["key"] == "setup:start" and "12.5 square metres" in m["ask"] and not g.in_setup


def test_setup_without_ar_and_unanswered_steps_move_on():
    g = Guide(CFG)
    g.setup_event(0, "hello", ar=False)
    asks = [m["key"] for t in range(0, 200, 2) if (m := g.on_tick(t, 0))]
    assert asks[:3] == ["setup:focus", "setup:focus", "setup:start"] and not g.in_setup
    g2 = Guide(CFG)
    g2.setup_event(0, "hello", ar=False)
    g2.setup_event(1, "focused")
    assert "cannot measure the room" in g2.setup_tick(1)["ask"]

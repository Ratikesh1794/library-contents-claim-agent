"""Stitching frames of a shelf: each book counted once however the camera moves (offline, no model calls)."""
import numpy as np

from backend.stages.shelf_strip import ShelfStitcher, Spine

TITLES = ["Dune", "Emma", "Ulysses", "Middlemarch", "Beloved", "Hamlet", "Candide", "Persuasion", "Dracula",
          "Walden", "Kidnapped", "Silas Marner", "Rebecca", "Lolita", "Siddhartha", "Atonement"]


def shelf(n, readable=True, start=0):
    """n distinct books; colour and thickness differ per book so unreadable ones can still be told apart."""
    rng = np.random.default_rng(7)
    cols = rng.uniform(0, 120, (60, 3))
    th = rng.uniform(0.6, 1.5, 60)
    return [Spine(TITLES[(start + i) % len(TITLES)] if readable and start + i < len(TITLES) else None, None,
                  float(th[start + i]), tuple(cols[start + i])) for i in range(n)]


def window(books, a, b, partial_edges=False):
    w = [Spine(s.title, s.author, s.t, s.color) for s in books[a:b]]
    if partial_edges and w:
        w[0].partial = w[-1].partial = True
    return w


def sweep(books, size, step, order=None):
    starts = list(range(0, len(books) - size + 1, step))
    if starts[-1] != len(books) - size:
        starts.append(len(books) - size)
    return [window(books, a, a + size) for a in (order(starts) if order else starts)]


def run(frames):
    s = ShelfStitcher()
    for f in frames:
        s.add_frame([f])
    return s


def test_sweep_right_counts_each_book_once():
    assert run(sweep(shelf(14), 6, 2)).count == 14           # frames overlap by 4 books


def test_sweep_left_gives_the_same_count():
    assert run(sweep(shelf(14), 6, 2, order=lambda s: s[::-1])).count == 14


def test_the_same_frame_sent_many_times_is_not_counted_again():
    frames = sweep(shelf(12), 6, 3)
    assert run([f for f in frames for _ in range(4)]).count == 12


def test_moving_back_and_forth_and_coming_back_later():
    frames = sweep(shelf(14), 6, 2)
    assert run(frames + frames[::-1] + frames).count == 14


def test_unreadable_books_are_aligned_by_thickness_and_colour():
    assert run(sweep(shelf(16, readable=False), 6, 2)).count == 16


def test_a_misread_title_does_not_create_a_duplicate():
    frames = sweep(shelf(12), 6, 2)
    frames[2][3].title = "Persuasin"                           # a vision slip on Persuasion in one frame
    frames[3][1].title = None                                 # the same book unreadable in another
    assert run(frames).count == 12


def test_a_book_missed_in_one_frame_is_tolerated_and_filled_in_later():
    frames = sweep(shelf(12), 6, 2)
    del frames[1][3]                                          # the model skipped one book in this frame
    assert run(frames).count == 12


def test_identical_neighbours_are_anchored_by_the_books_around_them():
    books = shelf(10)
    for k in (4, 5, 6):                                       # three copies of the same title in a row
        books[k] = Spine("Hamlet", None, 1.0, (50.0, 5.0, 5.0))
    assert run(sweep(books, 7, 2)).count == 10


def test_books_cut_by_the_frame_edge_are_completed_not_doubled():
    books = shelf(12)
    s = ShelfStitcher()
    for a in range(0, 7, 2):
        s.add_frame([window(books, a, a + 6, partial_edges=True)])
    assert s.count == 12


def test_a_different_bookcase_starts_a_new_strip():
    s = run(sweep(shelf(8), 6, 2))
    s.add_frame([window(shelf(8, start=8), 0, 6)])            # shares no book with the first shelf
    assert s.count == 14 and len(s.strips) == 2


def test_a_two_book_row_must_match_completely():
    s = run(sweep(shelf(8), 6, 2))
    s.add_frame([[Spine("Emma", None, 1.0, (0.0, 0.0, 0.0)), Spine("Nothing Here", None, 1.0, (90.0, 90.0, 90.0))]])
    assert len(s.strips) == 2                                 # one shared title is not proof of an overlap


def test_an_unreadable_sliver_at_the_frame_edge_is_the_book_already_in_that_slot():
    books = shelf(5)
    s = ShelfStitcher()
    s.add_frame([window(books, 0, 5)])                        # frame 1: five whole books
    cut = window(books, 0, 4) + [Spine(None, None, 0.3, (200.0, 10.0, 10.0), partial=True)]   # frame 2: 5th is a sliver
    for _ in range(3):                                        # sent again and again: still the same five
        s.add_frame([[Spine(b.title, b.author, b.t, b.color, b.partial) for b in cut]])
    assert s.count == 5

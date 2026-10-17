"""Position-based counting on a synthetic shelf: a textured wall with 12 books, a camera that slides along it.
No models, no network. Frames overlap; books at the frame edge are cut off, as with a real camera."""
import cv2
import numpy as np

from backend.stages.shelf_map import ShelfMap

W, H, BOOK_W, STEP, WIN = 2300, 700, 100, 170, 800        # wall size, book width, book pitch, camera window width


def wall():
    rng = np.random.default_rng(3)
    img = cv2.GaussianBlur(rng.integers(40, 200, (H, W, 3), dtype=np.uint8), (0, 0), 1.2)
    boxes = []
    for i in range(12):
        x0 = 100 + STEP * i
        col = rng.integers(30, 220, 3)
        img[100:600, x0:x0 + BOOK_W] = (0.6 * col + 0.4 * rng.integers(0, 255, (500, BOOK_W, 3))).astype(np.uint8)
        boxes.append((x0, 100, x0 + BOOK_W, 600))
    return img, boxes


IMG, BOOKS = wall()


def frame(x):
    """The camera window starting at wall x -> (image, boxes of the books in view, cut at the frame edge)."""
    out = []
    for x0, y0, x1, y1 in BOOKS:
        a, b = max(x0, x), min(x1, x + WIN)
        if b - a >= 15:                                     # a sliver of 15 px is still detected
            out.append((a - x, y0, b - x, y1))
    return IMG[:, x:x + WIN].copy(), out


def run(starts):
    m = ShelfMap()
    for s in starts:
        m.add_frame(*frame(s))
    return m


FWD = [0, 300, 600, 900, 1200, 1500]


def test_sweeping_right_counts_each_book_once():
    assert run(FWD).count == 12


def test_sweeping_left_gives_the_same_count():
    assert run(FWD[::-1]).count == 12


def test_going_forward_then_backward_adds_no_books():
    m = run(FWD + FWD[::-1])
    assert m.count == 12 and len({k.comp for k in m.keys}) == 1


def test_the_same_frame_sent_many_times_is_not_counted_again():
    assert run([s for s in FWD for _ in range(4)]).count == 12


def test_uneven_movement_and_returning_to_an_earlier_spot():
    assert run([0, 100, 700, 900, 1500, 1300, 200, 0, 1400, 600]).count == 12


def test_a_jump_to_a_distant_spot_is_bridged_when_the_views_connect_again():
    m = run([0, 1500, 700, 1000, 300])                      # 0 and 1500 share nothing; 700 -> 1000 links them up
    assert m.count == 12 and len({k.comp for k in m.keys}) == 1


def test_a_book_cut_in_two_by_the_detector_is_one_book():
    m = ShelfMap()
    img, boxes = frame(0)
    x0, y0, x1, y1 = boxes[1]
    boxes = boxes[:1] + [(x0, y0, x1, y0 + 300), (x0 + 10, y0 + 150, x1, y1)] + boxes[2:]   # two overlapping pieces
    m.add_frame(img, boxes)
    assert m.count == len(frame(0)[1])
    assert len({id(o) for o in m.add_frame(img, boxes)['owners']}) == m.count     # every box has an owner, pieces share one

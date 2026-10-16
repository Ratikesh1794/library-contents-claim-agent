from backend.live.coach import Coach
from backend.config import CFG
from backend.schemas import Sighting


def sg(i, reason=None, row="row1", ok=False):
    from tests.fakes import cand
    return Sighting(id=f"s{i}", frame_id="f", kind="book", bbox=(0, 0, 10, 100), shelf_hint=row,
                    reason=reason, accepted=cand("T") if ok else None)


def asks(msgs): return [m for m in msgs if m["type"] == "CAPTURE_ISSUE"]


def test_settle_then_ask_then_rate_limit():
    c = Coach(CFG)
    bad = [sg(1, "blur"), sg(2, "blur")]
    assert asks(c.update(bad, now=0)) == []                      # settle_s not elapsed
    first = asks(c.update(bad, now=3))
    assert len(first) == 1 and first[0]["reason"] == "blur"
    assert asks(c.update(bad, now=5)) == []                      # < ask_gap_s since last ask


def test_region_closed_after_two_asks():
    c = Coach(CFG)
    bad = [sg(1, "glare")]
    c.update(bad, 0)
    assert asks(c.update(bad, 3))                                # ask 1
    assert asks(c.update(bad, 12))                               # ask 2
    msgs = c.update(bad, 21)                                     # still failing -> give up, no 3rd ask
    assert not asks(msgs) and any(m["type"] == "STATUS" for m in msgs)
    assert not asks(c.update(bad, 40))


def test_resolved_region_stops_asking():
    c = Coach(CFG)
    c.update([sg(1, "blur")], 0)
    c.update([sg(2, ok=True), sg(3, ok=True), sg(4, ok=True), sg(5, ok=True)], 3)
    assert asks(c.update([sg(6, ok=True)], 20)) == []


def test_stack_hidden_never_asks_and_user_speech_silences():
    c = Coach(CFG)
    c.update([sg(1, "stack_hidden")], 0)
    assert asks(c.update([sg(1, "stack_hidden")], 10)) == []
    c2 = Coach(CFG)
    c2.update([sg(1, "blur")], 0)
    assert asks(c2.update([sg(1, "blur")], 3, last_user_speech_t=2.5)) == []

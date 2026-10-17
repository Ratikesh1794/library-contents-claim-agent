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

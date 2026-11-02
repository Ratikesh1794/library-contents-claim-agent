"""The voice agent's tools (backend/live/agent.py), as the Pipecat engine runs them."""
from backend.live import agent


class Hooks:
    def __init__(self):
        self.facts, self.skips, self.locale = [], [], None
    def on_fact(self, ref, text, turn_id): self.facts.append((ref, text))
    def on_skip(self, region, action): self.skips.append((region, action))
    def on_locale(self, country, currency): self.locale = (country, currency)


def test_unknown_tool_is_rejected_not_executed():
    assert agent.execute_tool(Hooks(), "delete_everything", {})["ok"] is False


def test_half_filled_tool_calls_are_rejected_not_recorded():
    h = Hooks()
    assert not agent.execute_tool(h, "mark_region", {"region": "", "action": "skip"})["ok"]
    assert not agent.execute_tool(h, "record_user_fact", {"target_hint": "b_001"})["ok"]
    assert h.skips == [] and h.facts == []
    assert agent.execute_tool(h, "mark_region", {"region": "row2", "action": "skip"})["ok"] and h.skips == [("row2", "skip")]


def test_spoken_shelf_names_map_to_coach_rows():
    for said in ("shelf2", "second shelf", "shelf row 2", "Row 2"):
        assert agent.normalize_region(said) == "row2"
    assert agent.normalize_region("top shelf") == "row1" and agent.normalize_region("by the window") == "by the window"
    h = Hooks()
    agent.execute_tool(h, "mark_region", {"region": "shelf2", "action": "Skip"})
    agent.execute_tool(h, "record_user_fact", {"target_hint": "second shelf", "fact": "all signed"})
    agent.execute_tool(h, "record_user_fact", {"target_hint": "Dune", "fact": "first edition"})
    assert h.skips == [("row2", "skip")] and h.facts == [("row2", "all signed"), ("Dune", "first edition")]


def test_set_locale_records_what_the_user_said_and_rejects_guesses():
    h = Hooks()
    assert agent.execute_tool(h, "set_locale", {"country": "uk", "currency": "gbp"})["ok"] and h.locale == ("GB", "GBP")
    assert not agent.execute_tool(h, "set_locale", {"country": "Britain", "currency": "pounds"})["ok"]
    assert not agent.execute_tool(h, "set_locale", {"country": "GB"})["ok"]                   # half-filled: ask
    assert "unknown" in agent.instructions({"country": None, "currency": None})

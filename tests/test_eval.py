"""Quick book-count check against a hand count (backend/eval.py)."""
from backend.core import store
from backend.eval import quick_count


def objs(tmp_path, books):
    sd = tmp_path / "s"
    sd.mkdir(exist_ok=True)
    store.write(sd, "objects.json", books + [{"kind": "item", "status": "identified"}])        # items are never counted
    return sd


def test_count_error_against_the_five_percent_bar(tmp_path):
    books = [{"kind": "book", "status": "identified", "title": f"T{i}"} for i in range(19)] + \
            [{"kind": "book", "status": "unidentified", "reason": "partial"}]
    r = quick_count(objs(tmp_path, books), 20)
    assert (r["books_found"], r["identified"], r["error"], r["within_5_percent"]) == (20, 19, 0.0, True)
    assert r["unidentified"] == {"partial": 1}
    r = quick_count(objs(tmp_path, books), 25)
    assert r["error"] == 0.2 and not r["within_5_percent"]
    assert quick_count(objs(tmp_path, books), 21)["within_5_percent"]                         # 1 off of 21 = 4.8%

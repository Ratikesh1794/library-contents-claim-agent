"""EVAL: claim_packet.json vs ground truth -> eval.json + printed pass-bar table.  (Not in the production path.)

ground_truth/ files (all optional; only the metrics you provide are scored):
  books.csv  columns: title, spine_height_cm, spine_thickness_cm, replacement_price   (blank = unknown)
             one row per book, `title` blank if you could not read it yourself
  items.csv  columns: category
  room.json  {"floor_area_m2": .., "wall_area_m2": .., "true_book_count": ..}
Books are matched to truth by fuzzy title (shelf/position ids are not stable across frames).

    python -m backend.eval sweeps/<id> ground_truth/
    python -m backend.eval sweeps/<id> --count 42      # quick check of just the book count, from objects.json (no packet needed)
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

from backend.core import store
from backend.stages.catalogue import token_set_ratio
from backend.config import CFG
from backend.schemas import Packet


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _rel(a, b):
    return abs(a - b) / b if a is not None and b else None


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def evaluate(sd: Path, gt: Path) -> list[dict]:
    p = Packet.model_validate_json((Path(sd) / "claim_packet.json").read_text())
    rows: list[dict] = []
    add = lambda m, v, bar, ok, n=None: rows.append({"metric": m, "value": None if v is None else round(v, 4), "bar": bar,
                                                       "pass": None if v is None else ok(v), "n": n})
    truth = []
    if (gt / "books.csv").exists():
        truth = list(csv.DictReader((gt / "books.csv").open()))
    room = json.loads((gt / "room.json").read_text()) if (gt / "room.json").exists() else {}
    true_n = room.get("true_book_count") or (len(truth) or None)
    if true_n:
        add("book count error %", _rel(p.totals.book_count, true_n), "<= 0.05", lambda v: v <= 0.05, true_n)
    legible = [t for t in truth if t.get("title")]
    accepted = [b for b in p.books if b.status == "identified"]

    def match(b):
        best = max(legible, key=lambda t: token_set_ratio(b.title, t["title"]), default=None)
        return best if best and token_set_ratio(b.title, best["title"]) >= 0.85 else None
    if legible:
        correct = {id(match(b)) for b in accepted if match(b)}
        add("ID correct % of legible", len(correct) / len(legible), ">= 0.70", lambda v: v >= 0.70, len(legible))
        wrong = [b for b in accepted if not match(b)]
        add("ID confidently-wrong % of accepted", len(wrong) / len(accepted) if accepted else None, "<= 0.03", lambda v: v <= 0.03, len(accepted))
        dim_err = [_mean([_rel(b.spine_height_cm, _f(t["spine_height_cm"])), _rel(b.spine_thickness_cm, _f(t["spine_thickness_cm"]))])
                   for b in accepted if (t := match(b)) and _f(t.get("spine_height_cm"))]
        add("spine dims error % (mean)", _mean(dim_err), "<= 0.15", lambda v: v <= 0.15, len(dim_err))
        pr_err = [_rel(b.replacement.amount, _f(t["replacement_price"])) for b in accepted
                  if (t := match(b)) and b.replacement and b.replacement.amount and _f(t.get("replacement_price"))]
        add("book price error % (mean)", _mean(pr_err), "<= 0.25", lambda v: v <= 0.25, len(pr_err))
    if (gt / "items.csv").exists():
        want = [r["category"] for r in csv.DictReader((gt / "items.csv").open())]
        have = [i.category for i in p.items]
        hit = 0
        for c in want:
            if c in have:
                have.remove(c)
                hit += 1
        add("item recall (category)", hit / len(want) if want else None, ">= 0.80", lambda v: v >= 0.80, len(want))
    if p.room:
        if room.get("floor_area_m2"):
            add("floor area error %", _rel(p.room.floor_area_m2, room["floor_area_m2"]), "<= 0.10", lambda v: v <= 0.10)
        if room.get("wall_area_m2"):
            add("wall area error %", _rel(p.room.wall_area_m2, room["wall_area_m2"]), "<= 0.15", lambda v: v <= 0.15)
    m = store.read(sd, "metrics.json", dict, default={})
    if m:
        add("time to packet (s)", m.get("total_seconds"), "<= 300", lambda v: v <= 300)
        add("cost per sweep (usd)", m.get("total_usd"), "<= 3", lambda v: v <= 3)
    store.write(sd, "eval.json", rows)
    return rows


def quick_count(sd: Path, true_n: int) -> dict:
    """Book count vs a hand count, straight from objects.json (so it works before pricing/the packet): found, identified,
    unidentified (with reasons), error against the 5% bar, and the identified titles to eyeball against the shelf."""
    objs = [o for o in store.read(Path(sd), "objects.json", list[dict], default=[]) if o.get("kind") == "book"]
    ident = [o for o in objs if o.get("status") == "identified"]
    reasons: dict[str, int] = {}
    for o in objs:
        if o.get("status") != "identified":
            reasons[o.get("reason") or "unknown"] = reasons.get(o.get("reason") or "unknown", 0) + 1
    err = _rel(len(objs), true_n)
    return {"books_found": len(objs), "true_count": true_n, "error": round(err, 4) if err is not None else None,
            "within_5_percent": err is not None and err <= 0.05, "identified": len(ident), "unidentified": reasons,
            "titles": sorted(o["title"] for o in ident)}


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[2] == "--count":
        r = quick_count(Path(sys.argv[1]), int(sys.argv[3]))
        print(f"books found {r['books_found']} vs hand count {r['true_count']}: error {r['error']:.1%} "
              f"({'within' if r['within_5_percent'] else 'OUTSIDE'} the 5% bar) | identified {r['identified']} | not identified {r['unidentified']}")
        print("identified titles:", "; ".join(r["titles"]))
        sys.exit(0)
    out = evaluate(Path(sys.argv[1]), Path(sys.argv[2] if len(sys.argv) > 2 else "ground_truth"))
    for r in out:
        print(f"{r['metric']:<38} {str(r['value']):>10}  bar {r['bar']:<8} n={r['n']}  {'PASS' if r['pass'] else 'FAIL' if r['pass'] is False else 'n/a'}")

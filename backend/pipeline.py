"""Post-sweep orchestration (plain Python, design D7):

  identify -> dedupe -> [ measure_scale -> measure_books ] ‖ [ measure_room ] ‖ [ price ] -> assemble(+report)

Every stage is file-in/file-out and idempotent, so you can re-run one alone:
    python -m backend.pipeline sweeps/<id>                 # everything
    python -m backend.pipeline sweeps/<id> --only price    # one stage
    python -m backend.pipeline sweeps/<id> --from dedupe   # this stage onward
Timings + per-service usage (calls/tokens/usd, per process) land in metrics.json.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend.core import meter, store
from backend.stages import assemble, dedupe, identify, measure_books, measure_room, measure_scale, price
from backend.config import CFG, Config

log = logging.getLogger(__name__)
ORDER = ["identify", "dedupe", "measure", "room", "price", "assemble"]


def run(sd: Path, only: str | None = None, start: str | None = None, cfg: Config = CFG, deps_: dict | None = None):
    """deps_ can inject vision/catalogue/source/fx/reconstructor (tests, alternative providers)."""
    sd, d = Path(sd), deps_ or {}
    t_all = time.time()
    metrics: dict = {"stages": {}}

    def timed(name, fn):
        t0 = time.time()
        try:
            fn()
        finally:
            metrics["stages"][name] = {"start": t0, "end": time.time(), "seconds": round(time.time() - t0, 2)}

    def want(n): return (only == n) if only else (ORDER.index(n) >= ORDER.index(start or "identify"))

    if want("identify"):
        timed("identify", lambda: identify.run(sd, d.get("vision"), d.get("catalogue"), cfg, time.time() + cfg.budget_identify_s))
    if want("dedupe"):
        timed("dedupe", lambda: dedupe.run(sd, cfg))
    parallel = []
    if want("measure"):
        parallel.append(("measure", lambda: (measure_scale.run(sd, cfg), measure_books.run(sd, cfg))))
    if want("room"):
        parallel.append(("room", lambda: measure_room.run(sd, d.get("reconstructor"), cfg)))
    if want("price"):
        parallel.append(("price", lambda: price.run(sd, d.get("source"), d.get("fx"), cfg, time.time() + cfg.budget_price_s)))
    with ThreadPoolExecutor(3) as ex:
        for f in [ex.submit(timed, n, fn) for n, fn in parallel]:
            f.result()
    packet = None
    if want("assemble"):
        timed("assemble", lambda: None)
        packet = assemble.run(sd, cfg)
    metrics["total_seconds"] = round(time.time() - t_all, 2)
    metrics["services"] = meter.snapshot()
    metrics["total_usd"] = round(sum(v["usd"] for v in metrics["services"].values()), 4)
    store.write(sd, "metrics.json", metrics)
    return packet


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("sweep_dir")
    ap.add_argument("--only", choices=ORDER)
    ap.add_argument("--from", dest="start", choices=ORDER)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    pk = run(Path(a.sweep_dir), a.only, a.start)
    if pk:
        print(json.dumps(pk.totals.model_dump(), indent=2))

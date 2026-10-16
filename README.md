# Library Contents Claim Agent

Walk a room once with a phone while talking to a voice agent. The backend returns a **claim packet**
(`claim_packet.json` + a self-contained `report.html`): every book (identified, measured, priced), every other
item, and room areas. Every number traces to a saved frame or a price URL.
**Unknown is a valid answer; invented is a defect.**

Design docs: [`docs/system-design.md`](docs/system-design.md) (what and why) and
[`docs/low-level-design.md`](docs/low-level-design.md) (how, plus implementation status in §16).

## Quick start

Needs Python 3.12 and an OpenAI API key.

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
source .venv/bin/activate
pytest                     # 71 offline tests; every external service is faked
cp .env.example .env       # then fill in OPENAI_API_KEY (see "Configuration" below)
```

### Run from a phone

Browsers only allow the camera and microphone over HTTPS, so the server is reached through a tunnel.

```bash
brew install ngrok         # once, then: ngrok config add-authtoken <your token>
scripts/run.sh             # starts the server + tunnel, prints the link and a QR code
```

Scan the QR code, allow camera and microphone, keep the screen on, tap **Start sweep**, and talk.
Set `NGROK_DOMAIN` in `.env` to your free static ngrok domain and the link and QR code stay the same on every run.
Same Wi-Fi and media must never leave your network? Use `scripts/mkcert_setup.sh` instead (local HTTPS).

Troubleshooting:
- *"This link has expired"*: the server restarted with a different token or domain. Scan the new QR code.
- *Camera denied*: the page must be opened over `https://`; allow camera and mic in the browser's site settings.
- *No voice*: tap anywhere on the page once; the Transcript tab shows "Voice error / reconnecting" lines if OpenAI fails.
- *Noisy room*: tap "Noisy room? Use hold-to-talk" under the mic button.

### Re-run on saved footage (no phone)

```bash
python -m backend.ingest clip.mp4 --country US --currency USD    # video or photo folder -> sweeps/<id>
python -m backend.pipeline sweeps/<id>                           # whole pipeline
python -m backend.pipeline sweeps/<id> --only price              # one stage (stages are file-in/file-out)
python -m backend.eval sweeps/<id> ground_truth/                 # pass-bar table (CSV format in backend/eval.py)
```

## Configuration (`.env`)

| Variable | Needed | What it does |
|---|---|---|
| `OPENAI_API_KEY` | yes | Vision (finding and reading books) and the voice agent |
| `VISION_MODEL`, `LIVE_MODEL`, `TRANSCRIBE_MODEL`, `VOICE` | no | Defaults are the mini models `gpt-4.1-mini`, `gpt-realtime-mini`, `gpt-4o-mini-transcribe`; set `gpt-4.1` / `gpt-realtime` for full size |
| `NOISE_REDUCTION`, `VAD_THRESHOLD` | no | Voice in noisy rooms: `far_field`/`near_field`/`off`, and 0..1 (higher ignores more noise) |
| `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` | for prices | Without them every price is blank (honest blank, listed in the review queue) |
| `GOOGLE_BOOKS_API_KEY` | no | Raises the catalogue quota |
| `MARKER_SIDE_CM` | for dimensions | Side of the printed ArUco marker, measured with a ruler. Unset means book and room dimensions stay blank |
| `SWEEP_TOKEN` | no | Fixed access token, so the phone link survives restarts |
| `NGROK_DOMAIN` | no | Your static ngrok domain, so the link and QR never change |

Every other threshold lives in `backend/config.py`.

## How it is organised

```
backend/
  config.py          every threshold / model / default (one dataclass). Start here to tune.
  prompts/           EVERY prompt sent to a model, as Python constants:
                       vision.py (DETECT, READ_SPINE, DESCRIBE_ITEM), agent.py (SYSTEM), coach.py (ASKS)
  schemas.py         Pydantic models = the JSON on disk and the packet contract
  ports.py           interfaces (Protocols) for every external service        <- the seams
  deps.py            the one place that picks the concrete implementation     <- swap here
  pipeline.py        runs the stages (CLI).  ingest.py  footage -> sweep (CLI).  eval.py  scores a packet (CLI)
  core/              plumbing: store (sweep files), cache, http, meter (cost), facts (voice corrections)
  stages/            the pipeline, one module per stage, file-in/file-out:
                       quality -> identify (+ catalogue) -> track + dedupe -> measure_scale -> measure_books
                       | measure_room | price -> assemble -> report
  live/              app.py   FastAPI: phone app, API, /ws/live, /voice signalling, token auth
                     agent.py voice agent (OpenAI Realtime over WebRTC, server attached sideband)
                     coach.py capture-issue state machine
  adapters/          one file per external service: openai_vision, open_library, google_books, ebay,
                     frankfurter, null_reconstructor
  templates/         report.html.j2
web/                 index.html, app.js, style.css (phone app; tunables in CONFIG at the top of app.js)
scripts/             run.sh (server + tunnel + QR), mkcert_setup.sh, print_qr.py
tests/               one small file per stage; fakes.py holds the fake services
docs/                system and low-level design
sweeps/<id>/         one folder per sweep (not committed): meta, frames/, sightings, links, objects,
                     geometry, room, prices, transcript, claim_packet, report, metrics
```

Stages never call each other. They read and write files in the sweep folder, so you can replace or re-run any one.

### How a sweep flows

1. The phone uploads sharp, changed **video frames** as keyframes. The server works on the newest one; if most of
   it was already analysed, it copies those results with no model calls. Otherwise it analyses it (find boxes →
   quality gate → read the spine and check it really is a book → match a real catalogue record), reusing reads for
   books already identified.
2. Overlapping frames are aligned (`stages/track.py`), so one physical book counts once whether or not it was read.
3. The **voice agent** runs over WebRTC directly between the phone and OpenAI. The server creates the call
   (the API key stays on the laptop), listens alongside it to run tools (skip a shelf, "that one is signed",
   finish), injects capture tips from the Coach, and streams live captions to the phone.
4. On finish: dedupe → measure ∥ price → assemble → report; the agent reads a templated summary.

## How to change a part

| I want to… | Touch |
|---|---|
| Change what any model is told | `backend/prompts/` (one constant per prompt) |
| Use a different vision model or provider | `VISION_MODEL`, or a new class with `detect` / `read_spine` / `describe_item` in `backend/adapters/`, wired in `deps.vision()` |
| Better spine boxes (local detector: Grounding DINO / OWLv2 / YOLO-World + SAM) | new adapter for `detect`; keep `OpenAIVision` for reading; compose both in `deps.vision()` |
| Add a catalogue (e.g. WorldCat) | class with `search(title, author)` in `backend/adapters/`, add it to `deps.catalogue()` |
| Add a price source (retailer or shopping API) | class implementing `PriceSource` in `backend/adapters/`, return it from `deps.price_source()` |
| Real room measurement | adapter returning `Reconstruction(points, up, marker_sides_model)`; plane fit, scale and areas already exist in `stages/measure_room.py` |
| Change the voice agent's tools | `backend/live/agent.py` (`TOOLS`) |
| Tune thresholds (accept, review, appraisal, quality, tracking) | `backend/config.py` |
| Change dedupe / tracking | `stages/dedupe.py` → `build_objects`; `stages/track.py`; `track_*` settings in `config.py` |
| Change the report look | `backend/templates/report.html.j2` |

`ponytail:` comments in the code mark each deliberate shortcut and what to upgrade it to.

## Honesty rules enforced in code

- **Strict identity:** a book is identified only when the title **and** the author read from its spine both match the same catalogue record (and no other matching record is a different book). Edition and year are recorded only when the spine's publisher matches too; the ISBN only when, in addition, the record is a single edition. Everything unconfirmed stays blank (`None`). The vision model's raw read is kept as `raw_text` for audit and never promoted to a field.
- A price exists only with source + URL + retrieval time (`schemas.Price` refuses otherwise). No listing → blank, excluded from totals, review queue.
- Signed, pre-1900, above-threshold, or art without "it's a print" → `needs_appraisal`, never priced.
- Totals are computed in code (`stages/assemble.py: compute_totals`) and re-checked after validation. No LLM does arithmetic.
- A failing external service degrades to blank + review queue; it does not crash the sweep.
- The voice agent has no tool that writes a title, price or measurement.

## Status

**Built and tested:** quality gating, identification (skip already-seen views, newest frame first, parallel reads,
read-once reuse, not-a-book check), per-sweep rate limits, catalogue matching,
frame tracking + dedupe, Coach, marker scale, book dimensions, room math (on synthetic points), pricing with FX and
appraisal rules, packet assembly and validation, self-contained report, pipeline, eval, ingest, API auth and limits,
WebRTC voice signalling and the sideband agent. The voice path was verified end to end with a scripted WebRTC client
against real OpenAI.

**Known issues**
- Non-books are rejected by a check in the spine-read call (verified on a small real sample); detector boxes can still be loose.
- Background voices (TV, other people) still trigger the agent in hands-free mode; use hold-to-talk there.
- Without eBay keys every price is blank.
- Item counts after tracking have not yet been checked on a sweep captured with the new camera path.

**Not built yet:** room reconstruction and markerless scale (room fields stay blank and go to review), room
fallbacks, marker-plane rectification and per-shelf scale, the Coach's "missed shelf" map, art "original or print?"
questions, linking spoken references ("the book on the left") to an object, a second price source, item dimensions.
Details in [`docs/low-level-design.md` §16](docs/low-level-design.md).

## Assumptions (state these when presenting results)

1. A printed ArUco marker (`DICT_4X4_50`, side measured with a ruler → `MARKER_SIDE_CM`) on the shelf counts as a reference object.
2. Replacement cost = lowest credible new listing; used value = median of ≥3 used listings, condition assumed "good".
3. Appraisal threshold is `appraisal_threshold_usd` in `config.py`, converted to the sweep currency with the day's ECB rate.
4. Wall area is gross (openings not subtracted); non-rectangular rooms are over-estimated by the convex hull and say so in `notes`.
5. With a tunnel, keyframes transit the tunnel provider; voice audio goes directly between the phone and OpenAI.

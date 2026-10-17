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
pytest                     # 84 offline tests; every external service is faked
cp .env.example .env       # then fill in OPENAI_API_KEY (see "Configuration" below)
```

### Run from a phone

Browsers only allow the camera and microphone over HTTPS, so the server is reached through a tunnel.

```bash
brew install ngrok         # once, then: ngrok config add-authtoken <your token>
scripts/run.sh             # starts the server + tunnel and opens the laptop dashboard (phone QR is there)
```

Scan the QR code, allow camera and microphone, keep the screen on, tap **Start sweep**, and talk.

**Two screens:**
- **Phone:** camera, the voice agent (state, mute / hold-to-talk, last thing each side said), book/item counts,
  *Measure the room*, *Finish*.
- **Laptop dashboard** (`run.sh` opens `http://localhost:8000/dashboard?t=<token>` automatically): headline numbers,
  every book / item / review line in tables with photo thumbnails, search and filters, a live activity feed (each
  frame analysed or skipped, coach tips, room measurements), the full conversation, and the packet totals + report
  link when the sweep finishes. It follows a newly started sweep automatically; older sweeps are in the picker.
Set `NGROK_DOMAIN` in `.env` to your free static ngrok domain and the link and QR code stay the same on every run.
Same Wi-Fi and media must never leave your network? Use `scripts/mkcert_setup.sh` instead (local HTTPS).

Troubleshooting:
- *"This link has expired"*: the server restarted with a different token or domain. Scan the new QR code.
- *Camera denied*: the page must be opened over `https://`; allow camera and mic in the browser's site settings.
- *No voice*: tap anywhere on the page once; the Transcript tab shows "Voice error / reconnecting" lines if OpenAI fails.
- *Noisy room*: tap "Noisy room? Use hold-to-talk" under the mic button.

### Measuring the room (browser, Android Chrome)

During a sweep, Chrome on an ARCore-capable Android phone shows **📐 Measure the room**. It opens an AR view in the
browser (WebXR; nothing to install): aim the circle at each floor corner **in order around the room** and tap
*Add point*, then *Next*, then aim at a point where a wall meets the ceiling (for the height) or *Skip*. The phone's
motion tracking gives the points in metres → floor area, length, width, height and wall area (m² and ft²).
While measuring, the page's camera pauses (AR needs it); the voice agent keeps running. iPhone Safari has no WebXR
AR, so the button doesn't appear there and room size stays blank (review queue) unless measured another way.

### Re-run on saved footage (no phone)

```bash
python -m backend.ingest clip.mp4 --country GB --currency GBP    # video or photo folder -> sweeps/<id>
python -m backend.pipeline sweeps/<id>                           # whole pipeline
python -m backend.pipeline sweeps/<id> --only price              # one stage (stages are file-in/file-out)
python -m backend.eval sweeps/<id> ground_truth/                 # pass-bar table (CSV format in backend/eval.py)
```

## Configuration (`.env`)

| Variable | Needed | What it does |
|---|---|---|
| `OPENAI_API_KEY` | yes | Vision (finding and reading books) and the voice agent |
| `VISION_MODEL`, `LIVE_MODEL`, `TRANSCRIBE_MODEL`, `VOICE` | no | Defaults are the mini models `gpt-4.1-mini`, `gpt-realtime-mini`, `gpt-4o-mini-transcribe`; set `gpt-4.1` / `gpt-realtime` for full size |
| `VOICE_ENGINE` | no | `pipecat` (default: the laptop filters noise and decides turns; phone on the **same Wi-Fi**) or `direct` (phone straight to OpenAI; works on any network) |
| `VAD_MIN_VOLUME`, `NOISE_FILTER` | no | Pipecat engine: loudness needed to count as speech (default 0.72; raise if a TV still triggers it) and `rnnoise`/`off` |
| `NOISE_REDUCTION`, `VAD_THRESHOLD` | no | OpenAI-side noise reduction `far_field`/`near_field`/`off`; `VAD_THRESHOLD` is for the direct engine |
| `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` | for used prices and item prices | Without them: used value and every item price stay blank (honest blank, listed in the review queue). New-book prices still come from Google Books store prices (ebook, labelled `google_books_ebook`) for the books Google sells; `GOOGLE_BOOKS_API_KEY` is used for that |
| `SERPAPI_API_KEY` | no (recommended) | Google Shopping prices through serpapi.com: new and used book prices and item prices from real merchants. Free plan: 250 searches/month; responses are cached so re-running a sweep costs nothing. Used when eBay keys are absent |
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
  live/              app.py   FastAPI: phone app, laptop dashboard, API, /ws/live (fan-out to every screen), /voice, auth
                     voice_pipecat.py  voice engine "pipecat": RNNoise + volume-gated Silero VAD + OpenAI Realtime
                     agent.py voice engine "direct" (phone -> OpenAI over WebRTC, server sideband) + shared tools
                     coach.py capture-issue state machine
  adapters/          one file per external service: openai_vision, open_library, google_books, ebay,
                     frankfurter, null_reconstructor
  templates/         report.html.j2
web/                 index.html, app.js, style.css (phone); dashboard.html, dashboard.js, dashboard.css (laptop)
scripts/             run.sh (server + tunnel; opens the dashboard), mkcert_setup.sh
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
3. The **voice agent** (default engine: Pipecat) runs over WebRTC between the phone and the laptop on the same Wi-Fi.
   The laptop removes steady noise (RNNoise), counts only speech loud enough to come from the person holding the
   phone (Silero VAD volume gate, so a TV across the room is ignored), and talks to OpenAI Realtime. Tools (skip a
   shelf, "that one is signed", finish), Coach tips and live captions work the same with either engine.
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
- Background voices: the Pipecat engine's volume gate ignored a TV ~20 dB quieter than the user in testing; a loud TV near the phone can still pass (raise `VAD_MIN_VOLUME`, or use hold-to-talk). Soft first words can be clipped by the gate.
- The Pipecat engine needs the phone on the same Wi-Fi as the laptop (or a TURN server in `ICE_SERVERS`); `VOICE_ENGINE=direct` works anywhere.
- Without eBay keys: book prices exist only for books Google Books sells as ebooks (new, labelled ebook); used values and item prices are blank. `python scripts/price_second_country.py sweeps/<id> GB` shows the same books priced for a second country.
- Item counts after tracking have not yet been checked on a sweep captured with the new camera path.

**Not built yet:** room size on iPhone (WebXR room measuring is Android Chrome only), markerless spine scale, room
fallbacks, marker-plane rectification and per-shelf scale, the Coach's "missed shelf" map, art "original or print?"
questions, linking spoken references ("the book on the left") to an object, a second price source, item dimensions.
Details in [`docs/low-level-design.md` §16](docs/low-level-design.md).

## Assumptions (state these when presenting results)

1. A printed ArUco marker (`DICT_4X4_50`, side measured with a ruler → `MARKER_SIDE_CM`) on the shelf counts as a reference object.
2. Replacement cost = lowest credible new listing; used value = median of ≥3 used listings, condition assumed "good".
3. Appraisal threshold is `appraisal_threshold_usd` in `config.py`, converted to the sweep currency with the day's ECB rate.
4. Wall area is gross (openings not subtracted); non-rectangular rooms are over-estimated by the convex hull and say so in `notes`.
5. With a tunnel, keyframes transit the tunnel provider; voice audio goes directly between the phone and OpenAI.

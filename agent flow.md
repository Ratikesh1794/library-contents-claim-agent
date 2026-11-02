# Agent Flow (interview cheat sheet)

## 0. One-line pitch
- Walk a room with a phone, **talk to a voice agent**. Backend finds, reads, measures and prices everything -> **claim packet**.
- **Unknown is a valid answer. Invented is a defect.**
- Agent never decides. **Code decides what/when to say. Agent only speaks it.**

## 1. Big picture (one line each)
`Start` -> `Capture frames` -> `Live analysis` -> `Voice + Guide` -> `Finish` -> `Post-processing` -> `Packet + report`
- Everything is files in one folder: `sweeps/sw_<time>_<id>/` (`backend/core/store.py`). Stages talk only via files, so any stage can re-run alone.

---

## 1a. Backend directories: what each one and each file does (`backend/`)
**Pattern:** `stages/` = logic, `adapters/` = outside services, `core/` = shared plumbing, `live/` = real-time session, `prompts/` = model text. `ports.py` defines the interfaces, `deps.py` picks the implementations.

### Top-level files
| File | What it does |
|---|---|
| `config.py` | Every tunable setting in one frozen dataclass (`CFG`): thresholds, model names, timings. **Start here to tune.** |
| `schemas.py` | Every data shape that crosses a stage (Pydantic, the JSON on disk). Unknown = `None`, never `""` or `0`. Includes `Price`, which refuses to exist without source + URL + time. |
| `ports.py` | Interfaces (Protocols) for outside services: `Vision`, `Catalogue`, `PriceSource`, `FX`. Stages only know these. |
| `deps.py` | **The one place that chooses the real implementation** (which price sources, which detector). Swap a service by editing one line. |
| `pipeline.py` | Runs post-sweep stages: identify -> dedupe -> (measure + room + price in parallel) -> assemble. Also a command line to re-run a saved sweep. |
| `templates/report.html.j2` | The report's HTML layout. |

### `adapters/` = the outside world (one file per service, behind a port)
| File | What it does |
|---|---|
| `local_detect.py` | **YOLO on the laptop**: finds books, items, people; turns each outline into a rotated rectangle; removes people + duplicates. Free, no API. |
| `openai_vision.py` | **OpenAI** reads one spine crop (`read_spine`) and describes one item (`describe_item`). Cached. |
| `open_library.py` | Book catalogue lookup (first choice). Verifies title + author. |
| `google_books.py` | Fallback catalogue, only if Open Library returns nothing. |
| `serpapi_prices.py` | **SerpAPI -> Google Shopping**: merchant prices, new + used, books + items. |
| `google_books_prices.py` | Google Books **new ebook** prices (fallback). |
| `price_chain.py` | Combines price sources into one; orders markets (user's country first). |
| `frankfurter.py` | **Frankfurter** exchange rates for converting foreign prices. |

### `core/` = shared plumbing
| File | What it does |
|---|---|
| `store.py` | Reads/writes the sweep folder (`sweeps/<id>/`). Atomic writes. Creates a new sweep. |
| `cache.py` | Saves every API response on disk keyed by request hash. Re-runs are free. Failures never cached. |
| `http.py` | Cached JSON requests for adapters. **Scrubs API keys out of error messages.** |
| `meter.py` | Counts calls, tokens, cost per service -> `metrics.json`. |
| `locale.py` | Cleans country + currency (`gb` -> `GB`); not ISO-shaped -> `None`. **No default country, ever.** |
| `facts.py` | Reads the user's voice fixes ("signed", "print", "skip") against a book/item. Used by price + assemble. |

### `live/` = the live session while the user walks
| File | What it does |
|---|---|
| `app.py` | FastAPI server: phone page + dashboard, API routes, `/ws/live` WebSocket, auth token, rate limits, frame worker, guide timer, finish step. |
| `voice_pipecat.py` | **Pipecat** pipeline (RNNoise, Silero VAD, OpenAI Realtime), the 4 tools, caption/state messages. |
| `agent.py` | What each tool does to the app (`execute_tool`) + the agent's instructions. Rejects half-filled tool calls. |
| `guide.py` | **The Guide:** decides what to say and when (setup, unreadable books, go back, progress, done). |

### `prompts/` = every model prompt, as Python text
| File | What it holds |
|---|---|
| `__init__.py` | The `fill()` helper. A missing placeholder raises an error, so a half-filled prompt is never sent. |
| `vision.py` | `READ_SPINE`, `DESCRIBE_ITEM` (sent to OpenAI vision). |
| `agent.py` | `SYSTEM`: the voice agent's 10 rules. |
| `coach.py` | Exact wording the Guide has the agent say. Old Coach name kept; it holds Guide wording now. |

### `stages/` = pipeline steps (each reads files, writes files)
| File | What it does |
|---|---|
| `identify.py` | Per frame: filter detections, crop + straighten spines, quality-check, rotate, read, match to catalogue. Skips already-seen views. |
| `quality.py` | Blur, glare, darkness scores for a frame or crop. |
| `catalogue.py` | **Strict identity:** title **and** author must both match one record. Also the catalogue chain. |
| `shelf_map.py` | Shared map of the shelf; every frame lined up on it, so **one spot = one book**. |
| `book_tracker.py` | Wraps the shelf map for the session; remembers what was read, so each book is **read once**. |
| `track.py` | Frame-to-frame alignment (ORB features); used mainly to match items. |
| `dedupe.py` | Merges sightings into one object per physical book/item. Never merges two different identified works. |
| `measure_scale.py` | Finds the printed ArUco marker -> pixels per cm. |
| `measure_books.py` | Spine height + thickness from that scale; no marker = blank. |
| `room_corners.py` | Room area, size, wall area from WebXR floor corners. |
| `measure_room.py` | Uses those corners if present, otherwise leaves the room blank. |
| `price.py` | Prices books + items from real listings, with appraisal rules. |
| `assemble.py` | Builds the claim packet, computes totals in code, builds the review queue, validates. |
| `report.py` | Renders the single-file `report.html`. |

### Outside `backend/`
- **`web/`**: phone page (`index.html`, `app.js`, `style.css`) + laptop dashboard (`dashboard.html/.js/.css`)
- **`scripts/run.sh`**: starts server + tunnel
- **`tests/`**: one small test file per stage + `fakes.py` (fake services, runs offline)
- **`docs/`**: `system-design.md`, `low-level-design.md`

### How to remember it
- **`stages/` = what happens** to the data
- **`adapters/` = who we call** (OpenAI, SerpAPI, Open Library...)
- **`core/` = tools everyone uses** (files, cache, HTTP)
- **`live/` = the real-time session** (server, voice, guide)
- **`prompts/` = what we tell the models**
- Honest note: `prompts/coach.py` is named after the old Coach but holds the Guide's wording.

---

## 2. How we START a sweep
1. Run `scripts/run.sh`: starts **uvicorn** (FastAPI, port 8000) + **ngrok** tunnel (HTTPS needed for camera/mic). Uses `NGROK_DOMAIN` for a fixed link.
2. Laptop opens **dashboard** `/dashboard?t=<TOKEN>`. Shows the **QR code** for the phone.
3. Phone opens `/?t=<TOKEN>`. Token checked in `check_token` (`backend/live/app.py`). Bad token -> "link expired" page.
4. Start screen (`web/index.html`): optional **country** + **currency** (blank = agent asks), **camera dropdown**.
5. Tap **Start sweep** -> `start()` in `web/app.js`:
   - `getUserMedia`: video (4K ideal) + audio (echo cancel, noise suppression, auto gain)
   - `POST /api/sweeps` -> `create_sweep` -> `store.new_sweep()` makes the folder + `meta.json`, `frames.json`, `user_facts.json`
   - country/currency cleaned by `core/locale.py` (invalid -> `null`, **never assumed**)
   - `session_for()` starts 3 background tasks: **`frame_worker`**, **`fanout`**, **`guide_loop`**
   - then: wake lock (screen stays on), WebSocket `/ws/live` (sends `hello` + `ar`), keyframe loop, voice (`startVoice()`)

## 3. Phone vs laptop camera + mic
- **Same web page** for both (`web/app.js`). Camera dropdown: default = rear camera (phone); pick a laptop/USB webcam to use the laptop.
- Mic: same `getUserMedia`. Mute button, or **hold-to-talk** for noisy rooms.
- **Laptop dashboard** (`web/dashboard.html/.js`) is a **view-only second screen**: tables, thumbnails, live activity, conversation. Joins WS as `role=dashboard`, replays history.
- **Room measuring (WebXR AR)** = Android Chrome only. Laptop/iPhone: `ar=false` -> room size left blank, agent says so.
- Voice audio goes **phone -> laptop (WebRTC, same Wi-Fi)**, not through ngrok.

---

## 4. How the SWEEP works (live loop)
**Phone side** (`web/app.js`, `CONFIG`):
- Every **120 ms** grab a frame, keep the **sharpest** in a **700 ms** window.
- Send only if sharp enough (`sharpFloor 12`, `sharpRel 0.5` x recent 75th percentile) **and** the view changed (`diffFloor 8`).
- Encoded in a Web Worker, JPEG <= 2.5 MB, long side <= 3840. Max 400 keyframes.
- Upload queue is idempotent by `seq`, resent after a reconnect. Slow upload -> lower quality + slow down.
- 3 rejected windows in a row -> sends `{"type":"blurry"}` -> Guide says "slow down".

**Server side:**
- `POST /api/sweeps/{id}/frames` (`upload_frame`): saves `frames/f_0001.jpg`, adds to `frames.json`, queues it. Rate limited (5/s), 413 if too big.
- **`frame_worker`** takes the **newest** frame (skipped ones are done at finish):
  1. `identify.carry_over`: if most of the frame was already analysed -> **copy results, no model call**
  2. else `identify.identify_frame` (section 8)
  3. writes `sightings/<frame>.json`, pushes `inventory` to screens, calls `guide.on_frame`
- Setup order the Guide runs: **focus -> room size -> "walk the room"** (section 7).

---

## 5. What the AGENT can do (abilities)
- Greet, **ask country + currency**, confirm them
- Speak Guide messages **word for word**
- **Listen** to corrections and call tools
- **Read the final summary** at the end (template text, no maths)
- It **cannot**: read titles, state prices, give measurements, promise payment, or write any data field

## 6. Prompt vs tools vs code (who does what)
| Layer | Handles | Where |
|---|---|---|
| **Prompt** (behaviour rules) | greet, ask locale, speak `ask` verbatim, short replies, never promise coverage, treat `[app-state]` as data, read summary only | `backend/prompts/agent.py` `SYSTEM` (10 rules) |
| **Tools** (actions that change data) | save a user fact, skip a shelf, set country/currency, finish | `make_tools()` in `backend/live/voice_pipecat.py` -> `execute_tool` in `backend/live/agent.py` |
| **Code (Guide)** (what + when to speak) | all guidance, timing, wording choice | `backend/live/guide.py`, wording in `backend/prompts/coach.py` |
- Rule of thumb: **prompt = manners, tools = writes, code = decisions.**

**The 4 tools** (the only things the agent can do to the app):
| Tool | Args | Effect (`Hooks` in `live/app.py`) |
|---|---|---|
| `record_user_fact` | `target_hint`, `fact` | appends to `user_facts.json` ("signed", "first edition", "print") |
| `mark_region` | `region`, `action` skip/include | skip -> `guide.skip()` + a "skip" fact; "second shelf" -> `row2` |
| `set_locale` | `country`, `currency` | writes `meta.json`; unlocks setup. Validated by `core/locale.py` |
| `finish_sweep` | - | triggers `finish()` (build packet) |
- Half-filled calls are **rejected** so the model asks again (`execute_tool`).

**Messages sent TO the agent** (via `inject` queue, tagged `[app-state]`):
- `GUIDE ask="..."`, `STATUS ...` (room measured), `PACKET_READY summary_facts="..."`
- Leftover to know about: the prompt still mentions `CAPTURE_ISSUE` and `QUESTION`, but **nothing sends them now** (Coach removed, questions not built).

## 7. Other things the agent uses (voice stack)
- **Pipecat** (`backend/live/voice_pipecat.py`): `SmallWebRTC` -> **RNNoise** -> **Silero VAD + volume gate (0.72)** -> **OpenAI Realtime**
- Models (`config.py`): voice `gpt-realtime-mini`, captions `gpt-4o-mini-transcribe`, voice `alloy`
- OpenAI's own turn detection **OFF** (a TV used to trigger it)
- `UIBridge`: turns Pipecat frames into captions + state (listening/hearing/thinking/speaking)
- `say()`: adds the message AND asks for a response (earlier it was stored but never spoken)
- `_pump_inject`: waits until nobody is talking, then speaks
- **Guide** (`guide.py`) topics: setup, unreadable book, give up, go back, progress, idle, "slow down"
- **Gate** (`_gate`): 6 s between messages, per-topic cooldown, silent 2 s after the user speaks, repeats get firmer
- `guide_loop`: timer every 2 s (a still user sends no frames)
- Delivery chain: `Guide` -> `guide_say` -> `inject` -> `_pump_inject` -> `say()` -> speaks (+ banner on screens)

---

## 8. How a book is found, CROPPED and read (`backend/stages/identify.py`)
1. **Detect** (`adapters/local_detect.py`, on the laptop, free):
   - **YOLO11m-seg** class "book" (finds books + people) and **YOLOE-seg** for items (vocab list in `config.item_vocab`)
   - Each outline (mask) -> a **rotated rectangle `quad`** (`quad_of`)
   - People removed (`drop_people`), duplicates removed (`dedupe`)
2. **Filter** (`_valid`, `_drop_group_boxes`, `_drop_tiny_items`): too small (<900 px), too long/thin (aspect >40), one box around several books.
3. **Shelf row** (`assign_rows`) -> `shelf_hint` "row2". **Shelf map** (`shelf_map.py`, `book_tracker.py`): one spot on the shelf = one book -> **read once per physical book**.
4. **CROP the spine:**
   - `_crop_quad`: **perspective-warp** the tilted rectangle into an upright crop (`cv2.getPerspectiveTransform` + `warpPerspective`), **8% margin** (`crop_margin`)
   - fallback `_crop`: plain box crop
   - `_upscale`: if text looks <32 px tall, enlarge up to 3x (`target_text_px`, `max_upscale`)
5. **Quality gate** (`quality.py`) on the crop, not the frame:
   - `too_far` (thickness too small), `blur` (<80), `glare` (>4% pixels), `too_dark` (<60). Failing crops skip the paid read and feed the Guide.
   - Soft reasons: `blocked` (item in front), `partial` (cut by frame edge), `stack_hidden`
6. **Orientation** (`_rotations`): tall upright -> try 90/270; flat -> 0/180; else all four. Stops at the first accepted match.
7. **Read** (`adapters/openai_vision.py`, `READ_SPINE` prompt, `gpt-4.1-mini`): returns `is_book, title, author, publisher, legible`. Not a book (perfume, DVD...) -> **dropped, never counted**.
8. **Rotations disagree** on the title -> `low_confidence` (never a guess).
9. **Catalogue match** (`catalogue.resolve`):
   - Search **Open Library**, then **Google Books** (`ChainCatalogue`, first with hits wins)
   - Accept only if **title >= 0.85 AND author >= 0.8** match the same record, and no other matching record is a different book (`ambiguous_match`)
   - **Edition/year/ISBN** only if the spine's publisher also matches (and the record is one edition)
   - Raw read kept as `raw_text`, **never promoted** to a field
- Failure reasons: `low_confidence`, `author_not_visible`, `no_catalogue_match`, `ambiguous_match`

## 9. How OTHER information is identified
- **Non-book items:** YOLOE finds them -> `describe_item` (`DESCRIBE_ITEM` prompt) gives `name, category, material, brand_model, dims_hint`
  - `brand_model` only if **literally visible** as text/logo
  - Described once per item (`plan_item`); kept only if category in `item_keep` (furniture, shelving, lamp, appliance, art, rug, electronics, decor)
  - Item seen in only 1 frame = dropped (`item_min_sightings 2`)
- **Spine size:** printed **ArUco marker** (`DICT_4X4_50`, side = `MARKER_SIDE_CM`, measure with a ruler) -> px per cm (`measure_scale.py`) -> height/thickness (`measure_books.py`). **No marker -> blank, reason `no_scale`.**
- **Room size:** WebXR floor corners -> shoelace area, min-area rectangle, wall area (`room_corners.py`, `measure_room.py`). Wall area is gross.
- **User facts:** keywords in what the user said (`core/facts.py` `flags`): "sign/inscri" = signed, "print", "first edition", "skip"
- **Country/currency:** only what the user says (`set_locale`)
- **Price:** see section 11

---

## 10. POST-PROCESSING (on Finish)
Phone: Finish (tap twice to confirm) -> flushes the upload queue -> `POST /finish` -> `finish()` in `live/app.py`.
1. **Locale check:** no country/currency -> agent asks, **nothing is built**.
2. `pipeline.run` (`backend/pipeline.py`), in a thread:
   - `identify` (frames the live worker skipped; books already read are **not read again**)
   - `dedupe` (`dedupe.py`): books grouped by **shelf map position**; items tracked frame to frame (`track.py`, ORB features). **Never merges two different identified works**; same work seen side by side = **two copies**.
   - then **in parallel:** `measure_scale` -> `measure_books`, `measure_room`, `price`
   - `assemble` (`assemble.py`)
3. **Assemble:** builds `claim_packet.json`; **totals computed in code** (`compute_totals`); validated (frame files exist, totals recompute identically).
4. **Review queue** (one reason per line, priority order): `needs_appraisal`, `unidentified`, `no_price`, `low_confidence`, `converted`, `wide_range`, `no_scale`, `scale_suspect` (+ `room_scale_missing`, `stage_incomplete`)
5. **Report** (`report.py`, `templates/report.html.j2`): one self-contained `report.html` with embedded thumbnails.
6. **Summary** (`voice_summary`): a **template**, not an LLM. Sent as `packet_ready` to screens + `PACKET_READY` to the agent, who reads it.
7. `transcript.json` saved (audit trail). Time budgets: identify 60 s, price 60 s; overrun = `incomplete`.
- Output files: `claim_packet.json`, `report.html`, `prices.json`, `objects.json`, `geometry.json`, `room.json`, `sightings/`, `transcript.json`

---

## 11. PRICES (`backend/stages/price.py`)
**Rule: model never invents a price.** Needs **source + URL + retrieval time** (`schemas.Price` refuses otherwise).

| Source | Gives | File |
|---|---|---|
| **SerpAPI (Google Shopping)** | **new AND used**, books + items | `adapters/serpapi_prices.py` |
| **Google Books** | **new ebook** only (fallback) | `adapters/google_books_prices.py` |
| **Frankfurter** | exchange rates (not a price source) | `adapters/frankfurter.py` |
- Chain order set in `backend/deps.py` (`price_chain.py`). **Used books = SerpAPI only.**

**Per book:**
1. Search **SerpAPI** in the user's own country first
2. Google marks second-hand itself (`second_hand_condition`)
3. Reject bundles, box sets, summaries, audiobooks, Etsy/Kobo/Audible, wrong titles (`_accept_book`, title match >= 0.8; short titles need author surname)
4. **Used** = **median** of 3+ used offers (fewer = low-high range, no single number)
5. **New** = **lowest** accepted new offer (ebooks: median)
6. Store source + URL + time + listing count

**Not in the user's country? Fallback US, then UK** (`price_fallback_markets`) -> converted with **Frankfurter**:
- Example: GBP 8.99 x 127.22 = INR 1,143.70
- Stored: `converted=true`, `fx_rate`, `fx_date`. Rate cached per day. Frankfurter down -> that book `unpriced` -> review.

**Appraisal, NO number, if:** signed, edition before 1900, any price above **$100** (converted), used prices differ >5x.
**Items:** known model -> one listing; no model -> 25th-75th percentile of 3+ listings (`wide_range` if >5x). Art needs appraisal unless "print".

---

## 12. Honesty rules (quote these)
- Identified only if title **and** author match one catalogue record
- Price only with source + URL + time
- Totals computed in code, no LLM maths
- Failing outside service = blank + review queue, never a crash
- Agent has no tool that writes data

## 13. Limits (say first)
- **Asking prices, not sold prices** (eBay account rejected -> removed)
- Used + item prices need **SerpAPI** (free: 250/month). No key = blank
- Google Books = **ebook** price, not print
- Voice needs **same Wi-Fi** (or TURN: `ICE_SERVERS`)
- Loud TV near the phone can pass the volume gate -> hold-to-talk
- Room size **Android Chrome only**
- Spine size needs the printed marker
- Frankfurter = daily reference rates, not bank rates
- Prompt mentions `CAPTURE_ISSUE`/`QUESTION` that nothing sends (leftover)

## 14. Cleanup I did (shows judgment)
Removed code that never ran: **eBay**, old "direct" voice engine, OpenAI box detection, 3D reconstruction stub, old Coach (replaced by **Guide**), offline ingest/eval tools.

## 15. Next steps
- "You missed a shelf" coverage map
- TURN relay for other networks
- Room-size fallback for iPhone
- Sold-price data for real appraisals

---

## 16. "Show me X" -> open this
| Interviewer asks | Open |
|---|---|
| How does the sweep start? | `web/app.js` `start()`, `live/app.py` `create_sweep`, `scripts/run.sh` |
| How are frames chosen/sent? | `web/app.js` `sampleTick`, `commitKeyframe`, `pump` |
| Live analysis loop | `live/app.py` `frame_worker` |
| How is a spine cropped? | `stages/identify.py` `_crop_quad`, `_upscale` |
| How is a book identified? | `stages/identify.py` `_read_book`, `stages/catalogue.py` `resolve` |
| Where is YOLO? | `adapters/local_detect.py` |
| How is a book counted once? | `stages/shelf_map.py`, `book_tracker.py`, `dedupe.py` |
| The agent's rules/prompt | `prompts/agent.py` |
| The agent's tools | `live/voice_pipecat.py` `make_tools`, `live/agent.py` `execute_tool` |
| Pipecat pipeline | `live/voice_pipecat.py` `run()` |
| How does the agent guide? | `live/guide.py`, `prompts/coach.py`, `live/app.py` `guide_say` |
| Prices | `stages/price.py`, `adapters/serpapi_prices.py`, `google_books_prices.py`, `frankfurter.py` |
| Totals + review queue | `stages/assemble.py` |
| Config / thresholds | `backend/config.py` |
| Swap a service | `backend/deps.py`, `backend/ports.py` |

# Library Contents Claim Agent: System Design

## 1. Problem in one paragraph

A policyholder walks a room once with a phone, talking to a voice agent. The system returns a claim packet: every book (identified, measured, priced), every non-book item (classified, priced), and room floor/wall area. Every number must trace to a saved frame or a price URL. Unknown is a valid answer; invented is a defect.

## 2. Scope

**In scope:** single-room sweep, live voice+camera, 6 required capabilities, packet JSON + HTML report, eval harness.
**Out of scope:** auth, deployment, policy database, visual polish, multi-room merge, condition grading, floor plan (stretch only).

## 3. Functional requirements

IDs are referenced by tests and the results sheet.

### Live sweep (FR-L)
| ID | Requirement |
|---|---|
| FR-L1 | Browser app streams mic audio and camera video to a live voice agent; agent replies by voice. |
| FR-L2 | Agent opens by confirming country and currency, and explains the sweep in 1-2 sentences. |
| FR-L3 | Agent detects blur, glare, occlusion, and missed shelves during the sweep and tells the user in the moment. |
| FR-L4 | Agent asks short questions video cannot answer (art: original or print? signed?). |
| FR-L5 | User can interrupt and correct by voice ("first edition", "skip that shelf"). Corrections update the inventory. |
| FR-L6 | Inventory panel updates live as items are recognised. |
| FR-L7 | Sweep is one continuous pass. System never asks for barcodes, ISBNs, pulling books out, or per-item photos. A targeted re-capture of one shelf/corner within the pass is allowed. |

### Books (FR-B)
| ID | Requirement |
|---|---|
| FR-B1 | Detect and count every book from the spine, including flat and stacked. |
| FR-B2 | Read title, author, publisher where visible; resolve to a specific work, and edition when evidence supports it. |
| FR-B3 | Measure spine height and thickness (cm); report total shelf run (m). |
| FR-B4 | Unreadable spine becomes `unidentified` with dimensions. Never guess a title. |
| FR-B5 | Same physical book seen in many frames is counted once. |

### Book valuation (FR-P)
| ID | Requirement |
|---|---|
| FR-P1 | Two figures per book: replacement cost and used value, in the policyholder's currency. |
| FR-P2 | Every price carries source, URL, retrieval date, and condition assumed. |
| FR-P3 | No local price: convert from another market and set `converted: true`. No price at all: leave blank and exclude from totals. |
| FR-P4 | Rare, signed, antiquarian, or above-threshold items become `needs_appraisal` and are never auto-priced. |
| FR-P5 | Locale (country, currency) is configuration. Same 10 books can be re-priced for a second country. |

### Non-book items (FR-N)
| ID | Requirement |
|---|---|
| FR-N1 | Detect and classify non-book contents (furniture, shelving, lamps, appliances, art, rugs, electronics, decor). |
| FR-N2 | Record category, material, approximate dimensions, brand/model if legible. |
| FR-N3 | Price at local replacement cost with source, or a sourced low/high range when model unknown. |
| FR-N4 | Art and portraits default to `needs_appraisal` unless user states it is a print. Agent asks. |

### Room (FR-R)
| ID | Requirement |
|---|---|
| FR-R1 | Floor area and wall area in m² and ft². |
| FR-R2 | Length, width, ceiling height; document how non-rectangular shape was handled. |
| FR-R3 | Shelved wall area as a separate figure. |
| FR-R4 | All from the same sweep. No separate measuring pass. |

### Packet (FR-K)
| ID | Requirement |
|---|---|
| FR-K1 | One `claim_packet.json` matching the output contract, plus one HTML report generated from it. |
| FR-K2 | Review queue lists every low-confidence, unidentified, or appraisal line with a reason. |
| FR-K3 | Totals computed in code from lines, never by an LLM. |
| FR-K4 | Every `frame_ref` resolves to a saved frame or timestamp. |
| FR-K5 | Agent reads a spoken summary after the packet is built. |

## 4. Non-functional requirements

### Accuracy (the pass bars become NFRs)
| ID | Target | Measured against |
|---|---|---|
| NFR-A1 | Book count within 5% | true count |
| NFR-A2 | Title ID: ≥70% of legible spines correct; ≤3% confidently wrong | hand-read titles |
| NFR-A3 | Spine dims within 15% | 20 hand-measured spines |
| NFR-A4 | Book price within 25% | 15 hand-checked prices, same source type |
| NFR-A5 | Non-book items ≥80% found with correct category | hand list |
| NFR-A6 | Floor area within 10%, wall area within 15% | tape measure |

### Performance
| ID | Target |
|---|---|
| NFR-P1 | Sweep ≤ 3 min (target). |
| NFR-P2 | Packet ready ≤ 5 min after sweep ends (hard bar). Internal budget: 3 min. |
| NFR-P3 | Live feedback (blur/glare warning) within ~2 s of the bad frame. |
| NFR-P4 | Inventory panel reflects a recognised item within ~10 s. |
| NFR-P5 | Voice round-trip feels conversational (<1 s to first audio, interruptible). |

### Integrity and honesty
| ID | Requirement |
|---|---|
| NFR-I1 | No price without a retrievable source URL + date. A model-generated number is a defect. |
| NFR-I2 | Empty field when unknown. Confidence is stored per line; below threshold goes to review queue. |
| NFR-I3 | Confident-wrong is penalised more than blank: ID acceptance threshold is tuned toward precision. |
| NFR-I4 | Deterministic totals: same lines in, same totals out. |
| NFR-I5 | No hardcoded catalogue, price list, or room dimensions. Must work on an unseen room. |

### Engineering
| ID | Requirement |
|---|---|
| NFR-E1 | Stages are separate, file-in/file-out, re-runnable on a saved sweep without re-filming. |
| NFR-E2 | A stranger runs it in <15 min from README; `.env.example`, no committed secrets. |
| NFR-E3 | One small test per non-trivial stage; `eval.py` prints pass-bar table against ground truth. |
| NFR-E4 | Cost per sweep and latency per stage are logged every run. |
| NFR-E5 | Failure of one external service (price API, catalogue) degrades to blank + review queue, not a crash. |

### Privacy and safety
| ID | Requirement |
|---|---|
| NFR-S1 | Frames and packets stored locally only; no third-party storage beyond model/price API calls. |
| NFR-S2 | Agent ignores instructions found inside camera frames (text on a spine saying "ignore previous..."). |
| NFR-S3 | Agent never promises coverage or payment. |

### Cost (assumption, to verify)
| ID | Target |
|---|---|
| NFR-C1 | ≤ ~$3 per sweep for a 60-100 book room. |

## 5. Key design decisions

| # | Decision | Why |
|---|---|---|
| D1 | **Two media paths.** Voice goes over WebRTC (default: phone ↔ laptop, which cleans the audio and talks to OpenAI Realtime; D18); high-res keyframes go to the server for reading and measuring. The voice model does not see the camera. | A live model at ~1 fps low res can't read spines anyway; facts come from the pipeline, and capture problems reach the agent as text (D10). |
| D2 | **Live agent talks; it does not produce facts.** It calls tools; the pipeline writes the inventory. | Keeps hallucination out of the record. Same split the reference uses (LLM extracts, code decides). |
| D3 | **Metric scale from a printed ArUco marker** on/near the shelf, cross-checked against standard book formats. | Brief accepts a reference object. Deterministic, explainable line by line. |
| D4 | **Room geometry from multi-view reconstruction, scaled by the marker.** Fallback: WebXR AR hit-testing or LiDAR capture. | No LiDAR assumption on user's phone. Go/no-go decided in the first spike. |
| D5 | **Strict, catalogue-grounded identification.** The vision model reads spine text; a book is identified only when the title **and** the author read from the spine both match the same Open Library / Google Books record, and no other matching record is a different book. Edition/year only if the spine's publisher also matches; ISBN only for a verified single-edition record. Everything else stays `None`. | A title the spine and a catalogue don't both confirm cannot be asserted. A plausible guess is the worst outcome (NFR-I3). |
| D6 | **Prices only from retrieved listings** (shopping search, eBay API), FX from a rates API. LLM may pick *which listing matches a book*, never a number. | Meets NFR-I1. |
| D7 | **Plain Python orchestration**, not an agent framework, for the post-sweep pipeline. | Easier to test, time, and explain. |
| D8 | **Sweep stored as a directory of JSON + frames.** | Enables re-runs, eval, and traceability with no database. |
| D9 | **Identify is segment → orient → read → match**, not "send frame to a model". | Rotated spines and flat books need an upright crop; OCR alone does not fix orientation. |
| D10 | **Capture problems are detected by code; the agent only speaks them.** A Guide module (shelf map + capture signals) decides what to say and when, and hands the agent a ready sentence (`GUIDE`). | Agent cannot skip or invent a reading. Behaviour is testable. |
| D11 | **Give-up rule:** max 2 asks per book, then one "I will note it and move on" and the book stays `unidentified` with its reason. | No-guess guarantee; keeps the sweep moving. |
| D12 | **No AR/LiDAR dependency.** Any phone browser camera; marker for scale; reconstruction from RGB. LiDAR/WebXR only as optional upgrade. | Works on cheap phones; laptop webcam is a last resort. |
| D13 | **Take perception and lookup from open/hosted components; build only glue and honesty rules.** | See section 5.1. |
| D15 | **Voice over WebRTC, never raw audio over our WebSocket.** The phone always sends a WebRTC offer to our server (`/voice`); the API key stays on the laptop. | Raw audio over our WebSocket bypassed the browser's echo cancellation and noise suppression: the agent heard itself and the room. |
| D16 | **Dedupe by frame-to-frame tracking** (homography between overlapping keyframes). | Linking by identified neighbours fails when few books are identified (a real sweep turned 97 sightings into 97 books). |
| D17 | **Prompts are code constants in one package** (`backend/prompts/`); **mini models by default.** | One place to change what any model is told; lower cost per sweep. |
| D18 | **Two voice engines behind one interface** (`VOICE_ENGINE`). Default `pipecat`: phone → laptop over WebRTC (same Wi-Fi), RNNoise, then a Silero VAD with a **volume gate** decides when the user speaks; OpenAI's own turn detection is off. Fallback `direct`: phone ↔ OpenAI Realtime, server attached sideband. | Background voices (a TV) made OpenAI's turn detection start turns and cut the agent off. The person holding the phone is much louder than a TV across the room: measured, a gate at 0.72 ignored a TV ~20 dB quieter while still hearing the user. `direct` stays for networks where the phone can't reach the laptop. |
| D20 | **Room size from WebXR in the browser** (Chrome on ARCore-capable Android): the user aims at the floor corners in order, then a wall/ceiling point; hit tests give metres. No native app. | Users can't be asked to print a marker or install an app; the phone's own AR tracking is metric. Trade-offs: a short measuring step within the sweep (camera feed pauses), and iPhone Safari has no WebXR AR. |
| D21 | **Two screens.** The phone shows only what the person sweeping needs (camera, voice, counts); a laptop dashboard shows every record, the live activity and the conversation. Live messages fan out to every connected screen. | A phone is too small to review records while walking a room; the person at the laptop can watch quality and coverage as it happens. The phone's QR code lives on the dashboard, not in the terminal. |
| D19 | **Analyse only what is new.** A keyframe that mostly shows the last analysed frame copies its results; the live worker always works on the newest frame; the spine-read call also says whether a crop is a book at all. | Cost and latency scale with model calls, not frames; a stale queue made coach tips lag; non-books (perfume boxes) were being counted as books. |
| D22 | **Count books by position, read each once.** Every frame is registered on a shared map of the shelf; a book that lands on an already-registered spot is the same book (forward, backward, return visits, unreadable titles, identical copies side by side all work). Each book is read once and a non-book is remembered. Items still use frame-to-frame tracking. | Title matching fails when titles are unreadable or repeated; a model remembering recent frames forgets books and double-counts when the user goes back. Position is independent of both. On two blurred real sweeps: 198 -> 15 books (11 identified) and 0 -> 25 (15 identified). |
| D23 | **Guidance is decided by code and must be answered by the model.** The Guide covers slowing down (the phone says it cannot get sharp frames), holding steady on a named book ("the book on the left"), going back to a book that was passed unread (direction from the shelf map, in what the user sees), progress every 10 books, and "this area looks done, move on or say finish". App messages are sent to the Realtime model as a user message followed by a response request. | In Pipecat's Realtime service a context update is only stored and its messages-append handler is a stub, so every coach message was accepted and silently dropped: the agent greeted and then never guided. Position words and directions come from the map, not from the model. |
| D14 | **Phone is the capture device; the laptop runs the server.** The phone opens the web app in its browser over HTTPS (tunnel or local HTTPS). Laptop webcam is not the primary camera. | Walking a room needs a handheld rear camera; browsers allow camera/mic only on HTTPS, so access topology is part of the design. |

### 5.1 Build vs take

| Problem | Take (free / open / hosted) | We build |
|---|---|---|
| Live voice | OpenAI Realtime (`gpt-realtime-mini`); Pipecat (SmallWebRTC transport, RNNoise, Silero VAD); browser WebRTC echo cancellation | Engine switch, volume-gate tuning, UI bridge (captions/state), tools, coaching messages |
| Blur / glare / brightness | OpenCV | Thresholds, per-region aggregation |
| Find and read spines | Local detection: COCO YOLO11-seg (books) + YOLOE-seg open vocabulary (items), straightened crops in code; vision LLM only reads spine text / describes items | Detect → dedupe → crop → orient → read flow, structured output, reason codes |
| Title to real book | Open Library, Google Books APIs | Strict title + author verification, ambiguity check, field-level edition/ISBN rules |
| Metric scale | OpenCV ArUco | px to cm per shelf plane, cross-checks |
| Room dimensions | RGB reconstruction model (e.g. VGGT, COLMAP); optional monocular metric depth as a cross-check | Plane fitting, scaling with marker, area math, fallbacks |
| Prices | eBay Browse API, shopping search API, Frankfurter FX | Listing-to-book matching, source/URL/date logging, `converted` flag, appraisal rules |
| Same object across frames | OpenCV ORB + RANSAC homography | Tracking, carry-over of already-seen views, dedupe rules |
| Dedupe, totals, review queue, report, eval | Nothing | All ours; this is the graded core |

We do not train models, write OCR, or write 3D reconstruction. Model licences (some reconstruction weights are non-commercial) are noted in the README.

### 5.2 Capture quality tiers

| Tier | Camera | What changes |
|---|---|---|
| A | Phone, rear camera, ≥1080p | Normal path. |
| B | Low-end phone / laptop webcam (≤720p, wide-angle) | Max video resolution requested (up to 4K); lens undistortion; closer shooting distance; more `unidentified` expected and reported honestly. |
| C | No marker visible | Fallback scale (known object / standard book format / user-stated door height), confidence lowered, line goes to review queue. |

Rule: below the readable-pixel threshold the system does not infer. It marks `unidentified` with dimensions and a reason.

## 6. High-level design

### 6.1 Component view

```
┌──────────────────────── PHONE BROWSER ──────────────────────────┐
│  Mic / speaker ◄──── WebRTC voice (echo cancellation) ───────────┼──┐  default engine: to the laptop (same Wi-Fi)
│  Camera ─┬─► quality check (320 px) ─► keyframes (video frames) ──┼──┼─► POST /api/sweeps/<id>/frames
│          └─► preview                                             │  │
│  Agent captions + counts ◄──── /ws/live?role=phone ◄──────────────┼──┼──┐
└──────────────────────────────────────────────────────────────────┘  │  │
   LAPTOP BROWSER: dashboard (records, live activity, conversation,    │  │
   phone QR) ◄── /ws/live?role=dashboard (fan-out + history replay) ◄──┼──┤
                         │ HTTPS/WSS (tunnel: page, API, signalling)    │  │
┌────────────────────────▼──────────────────────── BACKEND (FastAPI) ───▼──┴─────────────────────────┐
│  /voice: WebRTC signalling (API key stays here)                                                    │
│  Voice engine "pipecat":  RNNoise ─► Silero VAD + volume gate ─► OpenAI Realtime (turns decided here)│
│     (engine "direct": phone ↔ OpenAI Realtime, server attached sideband)                           │
│  Tools record_user_fact, mark_region, finish_sweep; captions + agent state ─► /ws/live            │
│  Guide: shelf map + capture signals -> GUIDE sentences for the agent                               │
│  Sweep Store (disk):  sweeps/<id>/ frames/ frames.json sightings/ links.json user_facts.json …     │
│                                                                                                    │
│  Live worker: newest frame ─► already seen? carry results over : IDENTIFY                          │
│  Pipeline (each stage: JSON in → JSON out)                                                         │
│   ┌──────────┐  ┌─────────────┐  ┌─────────┐  ┌────────┐  ┌──────────┐                             │
│   │ IDENTIFY │─►│ TRACK +     │─►│ MEASURE │─►│ PRICE  │─►│ ASSEMBLE │─► packet + report           │
│   │ spines + │  │ DEDUPE      │  │ scale + │  │ sourced│  │ totals + │                             │
│   │ items    │  │             │  │ room 3D │  │ only   │  │ review q │                             │
│   └────┬─────┘  └─────────────┘  └────┬────┘  └───┬────┘  └──────────┘                             │
└────────┼──────────────────────────────┼───────────┼────────────────────────────────────────────────┘
         ▼                              ▼           ▼
   OpenAI vision (gpt-4.1-mini)   ArUco (OpenCV)   eBay Browse, FX rates
   Open Library / Google Books    3D reconstruction (not built yet)
```

### 6.2 Responsibilities

| Component | Responsibility | Never does |
|---|---|---|
| Browser quality checker | Blur, glare, brightness scores per frame; "which shelf" hint from motion | Identify content |
| Live server + voice engine | WebRTC signalling; filter and gate the user's audio (pipecat) or attach sideband (direct); run tools; push captions / agent state / inventory to the UI | Write inventory facts itself; send the API key to the phone |
| Live voice agent | Converse, coach, ask questions, relay user corrections via tools | State prices, titles, or dimensions as fact |
| Identify | Per keyframe: segment each book, classify orientation, rotate/upscale crop, read text, catalogue match + score; non-book classification; emit reason code on failure | Guess below threshold |
| Guide | Per-book state (asked, when, how often) kept on the shelf map; turns capture signals, unreadable books and the map into one rate-limited `GUIDE` sentence; gives up on a book after 2 asks | Read, name, or judge content |
| Dedupe | Merge sightings of the same object (shelf + position + frame overlap, 3D location when available) | Drop unidentified books |
| Measure | Marker → cm/px per shelf plane; spine dims; shelf run; room dims and areas | Estimate scale by model "feel" |
| Price | Query sources, match listing to item, record URL/date/condition, FX with `converted` flag | Output a number without a source |
| Assemble | Sum totals, build review queue, validate schema, render report | Call an LLM for numbers |
| Eval | Compare packet to ground truth, print pass-bar table, timings, cost | Run in production path |

### 6.3 Sweep lifecycle

```
IDLE → GREETING (country/currency) → CAPTURING ⇄ RECAPTURE → FINALIZING → READY
                                         │
                      live loop: frame → quality check → warn? → agent speaks
                      background: keyframe → identify → panel update
FINALIZING (≤ 3 min budget): dedupe → measure ∥ price → assemble → voice summary
```

Measure and Price run in parallel after Dedupe, since price needs only identity and measure needs only geometry.

### 6.4 Interfaces

**WebSocket `/ws/live`** (backend → browser only)
- `inventory` (live lines), `coach` (capture tip), `transcript` (role, item id, text, append|set), `agent_state`, `status`, `packet_ready`

**HTTP**
- `POST /api/sweeps` → create, returns `sweep_id`
- `POST /api/sweeps/{id}/frames` → hi-res keyframe (JPEG body) + `seq`, timestamp, quality scores
- `POST /api/sweeps/{id}/voice` → WebRTC SDP offer in, SDP answer out (starts the voice call)
- `POST /api/sweeps/{id}/finish` → run pipeline
- `GET /api/sweeps/{id}/packet` → JSON; `/report` → HTML (self-contained, thumbnails embedded)
- `GET /api/sweeps/{id}/frames/{ref}` → saved frame

**Live agent tools**
| Tool | Purpose |
|---|---|
| (inbound) `GUIDE{ask}` | Sent by the Guide, not called by the agent. Agent speaks the ask word for word, one sentence. (`CAPTURE_ISSUE` is the older per-region form.) |
| `record_user_fact(target_hint, fact)` | e.g. "first edition", "print", "signed"; stored with the transcript |
| `mark_region(region, action)` | skip/include a shelf on user instruction |
| `finish_sweep()` | user says done; starts finalize |

### 6.5 Stage contracts (JSON on disk)

```
sweeps/<id>/
  meta.json            sweep, country, currency, device, timings
  frames/<id>.jpg      keyframes (every frame_ref points here)
  frames.json          keyframe list (time, size, quality scores)
  links.json           frame-to-frame homographies (tracking cache)
  transcript.json      what was said during the sweep
  user_facts.json      corrections with turn ids
  sightings.json       Identify output (per frame: boxes, text, candidates, score)
  objects.json         Dedupe output (unique books/items, sighting ids)
  geometry.json        Measure output (scale method, per-object dims, room)
  prices.json          Price output (per object: amounts + source/url/retrieved_at)
  claim_packet.json    Assemble output (the contract)
  report.html
  eval.json            vs ground truth
  metrics.json         per-stage latency + cost
```

### 6.6 Data model (core)

```
Sighting  { id, frame_ref, bbox, raw_text, kind: book|item }
Object    { id, kind, sighting_ids[], shelf, position, status, id_confidence }
Book      Object + { title, author, edition, isbn, catalogue_match_score,
                     spine_height_cm, spine_thickness_cm }
Item      Object + { category, material, brand_model, dims_cm{w,h,d} }
Price     { object_id, kind: replacement|used, amount|low/high, currency,
            source, url, retrieved_at, condition?, converted, fx_rate? }
ReviewRef { ref_id, reason }
```

Status rules (code, not model):
- `identified` only if the title **and** author read from the spine both match one catalogue record and no other matching record is a different book; edition/year/ISBN only when verified (D5)
- `unidentified` otherwise (dims still recorded)
- `needs_appraisal` if signed/rare flag, art without print confirmation, or estimated value > threshold

### 6.7 Identify detail

```
keyframe (hi-res)
 → already seen? (≥85% of it was in the last analysed frame, not closer, no fixable failures) => copy results
 → segment: one region per book (box/mask); non-book objects separate
 → classify layout: upright | rotated | flat | stacked | cover_only
 → crop + rectify (marker homography when available) + upscale
 → orient: try 2 rotations for tall boxes (4 for unknown); keep best catalogue score
 → read: vision model returns {is_book, title, author, publisher} JSON from the crop only; is_book=false => dropped
 → match: Open Library / Google Books; identified only if title AND author match one record, else unidentified
 → output: Sighting{status, reason?, candidates[], score}
```

Layout rules:

| Layout | Handling |
|---|---|
| Upright spine | Text vertical; try both 90° rotations. Thickness = horizontal extent. |
| Flat, spine to camera | Text horizontal. Thickness = vertical extent. |
| Stacked | One band per book; count bands; thickness per band; read if spine faces camera. |
| Spine hidden / cover or page edge only | Count, record dims, `unidentified` (`stack_hidden`). |
| Partly occluded | Count only if clearly a separate book; else review queue. |

### 6.8 Guide

Code decides what to say and when; the agent speaks the sentence word for word and has no tool that writes a title, price or measurement.

| Topic | Trigger (computed in code) | Says (examples) |
|---|---|---|
| `slow` | The phone rejected several frames in a row as too blurry and told the server | "Slow down a little. Pause on each shelf for a second." Then, if it continues: "Still too blurry. Stop for a moment and hold the phone still." |
| `book:*` | A book in view stays unreadable for 2 s. The cause comes from the failed crop (`too_far`, `blur`/`low_confidence`, `glare`, `too_dark`, `blocked`, `partial`); the position from where it sits in the frame | "Hold steady on the book on the left for a second." / "Move a bit closer to the books on the right." / "Tilt the phone a little to cut the glare on the book at the top." / "Pan a little further so the book on the left is fully in view." |
| `giveup` | A book was asked about twice | "I could not read the book on the left. I will note it and move on." |
| `back` | An unreadable book left the view 4 s ago or more. The direction is where the shelf map puts it in what the user sees | "I could not read the book you passed on your left. Go back and hold steady on it." (right, higher up, lower down; "two books", "a few books") |
| `progress` | Every 10 books found | "That is 20 books found so far. Keep going." |
| `idle` | 15 s with no new book and nothing in view needing attention (a timer, so it also works when the user holds still and no frames arrive) | "That area looks done. Move on to the next shelf, or say finish when you have covered everything." Then, after a minute: "Nothing new for a while. Say finish when you are done." |

Behaviour:
- One message at a time, at least 6 s between any two, a cooldown per topic, silence for 2 s after the user speaks. The same topic again gets firmer wording.
- Failures the user cannot fix (`author_not_visible`, `no_catalogue_match`, `ambiguous_match`, `stack_hidden`) are never mentioned.
- Max 2 asks per book, then one "I will note it and move on" and the book is left alone. "Skip this shelf" silences that row of the current view.
- "Go back" waits while a book in view is about to be asked about; in-view problems come first.
- Never ask the user to pull out, open, rearrange, or individually photograph books. Moving an obstruction or moving closer is allowed.

### 6.9 Room measurement without AR

```
frames → reconstruct (unscaled 3D) → fit planes (floor, walls, ceiling)
       → scale by marker (known side length vs reconstructed length)
       → L, W, H, floor area, wall area; shelved area from detected shelf rectangles
```

Fallbacks, in order: marker-scaled plane fit from single-view homography on floor; user-stated known dimension (e.g. door height) with lowered confidence; otherwise leave room fields empty and queue for review. Non-rectangular rooms: report as sum of rectangles or bounding box, stated in `scale_method` notes.

### 6.10 Deployment and access topology

```
 Phone (browser)                         Laptop
 ┌──────────────────┐   HTTPS / WSS    ┌──────────────────────────────┐
 │ rear camera, mic │ ◄──────────────► │ FastAPI: web app, /ws/live,  │
 │ web app + panel  │  via tunnel or   │ frames API, /voice, pipeline │
 └────────┬─────────┘  local HTTPS     │ API keys (never sent to phone)│
          │                            └──────────────┬───────────────┘
          │  WebRTC voice, same Wi-Fi (engine pipecat) │
          └──────────────────────────────────────────►┤
                                                      ▼  OpenAI (vision; Realtime voice), catalogue, price APIs
 (engine direct: phone ◄── WebRTC voice ──► OpenAI Realtime; the laptop only signals and attaches sideband)
```

| Aspect | Decision |
|---|---|
| Serving | Laptop serves the static web app and API on one origin, so no CORS and one WSS endpoint. |
| HTTPS | Required for camera/mic on the phone. Default: ngrok on a fixed domain (`NGROK_DOMAIN`, same link and QR every run), else a quick tunnel (cloudflared or ngrok). Alternative on same Wi-Fi: `mkcert` local HTTPS (lower latency, CA must be trusted on the phone). |
| Voice network | Engine `pipecat`: the phone's WebRTC audio must reach the laptop directly (same Wi-Fi; a tunnel carries only the page and signalling). Other networks need a TURN relay (`ICE_SERVERS`) or `VOICE_ENGINE=direct`. |
| Access control | Sweep token in the URL (fixed via `SWEEP_TOKEN`); server rejects WS/API calls without it. Per-sweep rate limits (HTTP 429): frames 5/s, voice offers 6/min, finish 3/min, new sweeps 10/min. No user accounts (out of scope). |
| Secrets | API keys stay in laptop `.env`; the phone only receives the page and media endpoints. |
| Bandwidth | ~1.4 keyframes/s at ≤ 2560 px, ~1 MB each. Browser lowers JPEG quality/rate if the upload queue grows. |
| Keep-alive | Screen Wake Lock on the phone; foreground tab required; voice reconnects up to 5 times; capture continues without voice. |
| Privacy | With a tunnel, keyframes transit the tunnel provider. Voice: engine `pipecat` keeps it on the local network until the laptop forwards it to OpenAI; engine `direct` sends it from the phone to OpenAI. |
| Reference change | Remove its localhost-only middleware and origin checks; replace with token + allowed-origin config. |

## 7. Failure and degradation matrix

| Failure | Behaviour |
|---|---|
| Marker not visible on a shelf | Fall back to standard-format heuristic; lower confidence; add to review queue |
| Room measuring not available (iPhone, non-AR phone) or skipped | Room fields blank, `room_scale_missing` in the review queue |
| Corners tapped out of order | Outline crosses itself → convex hull used, noted on the room line |
| 3D reconstruction fails or is implausible | Room dims from marker-scaled plane fits; flag low confidence; review queue |
| Catalogue API down / no match | `unidentified`, never a guessed title |
| Spine shows a title but no author | `unidentified` (`author_not_visible`), no catalogue call; counted and measured |
| Two catalogue records match but are different books | `unidentified` (`ambiguous_match`) |
| Non-book detected as a book (perfume box, DVD…) | Spine-read check says `is_book=false`; the box is dropped, never counted or priced |
| Price source down / no listing | blank price, counted in `excluded_from_totals` |
| Live model session drops | Phone re-establishes the WebRTC call; frames already saved so pipeline unaffected |
| Pipeline exceeds 3 min budget | Stages emit partial results; packet built with what exists; unfinished lines go to review queue |
| Low-res camera / spine too small | `unidentified` + `too_far`/`blur`; dims kept; honest count in results |
| Book still unreadable after 2 asks | One "I will note it and move on"; stays `unidentified`, reason in review queue |
| Spine not facing camera (stacked) | Counted, dims recorded, `stack_hidden`, no ask |
| Phone upload stalls (weak Wi-Fi / tunnel) | Browser lowers keyframe quality/fps; the Guide tells the user to slow down; keyframes queue and flush; sweep continues |
| Background voices (TV, other people) | Engine `pipecat`: RNNoise + volume gate ignore distant voices (a TV ~20 dB quieter than the user, tested); a loud voice near the phone can still pass: raise `VAD_MIN_VOLUME` or use hold-to-talk |
| Phone can't reach the laptop for voice (other network) | Voice reconnect attempts fail; capture continues; switch to `VOICE_ENGINE=direct` or add a TURN server |
| Voice call drops | Browser re-offers (up to 5 times with backoff); the server hangs up the old call and starts the new one; capture continues meanwhile |
| Phone sleeps or tab backgrounded | Wake Lock requested; on disconnect the app shows a resume prompt and reconnects; frames already saved are kept |
| Tunnel URL changes between runs | Page and API share one origin; the laptop dashboard shows the current phone link as a QR code (`/api/connect`), and `NGROK_DOMAIN` keeps it fixed |

## 8. Traceability model

Every number answers "where did this come from?":

- Count, title: `frame_ref` → saved JPEG + bbox
- Dimension: `frame_ref` + `scale_method` (marker id, px/cm)
- Room area: `geometry.json` with plane fits and scale method
- Price: `url` + `retrieved_at` + `condition_assumed`
- Total: recomputed by `assemble.py` from lines, covered by a test

## 9. Evaluation harness

`ground_truth.csv` (true count, titles, 20 measured spines, 15 priced, item list, room tape) vs `claim_packet.json` → `eval.json` with each pass bar, miss size, and per-shelf breakdown so easy shelves cannot hide hard ones.

## 10. Risks (ranked) and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Room area from reconstruction is off | Misses 10%/15% bars | De-risk in first spike; fallback capture mode decided early |
| Duplicate counting across frames | Blows 5% count bar | Frame-to-frame tracking + same-work merge + evaluation per shelf |
| Spine OCR wrong on small/rotated text | Confident-wrong titles | Strict title + author verification against one record, ambiguity check, high-res crops |
| Background voices trigger the agent | Agent interrupts itself, answers the TV | Laptop-side RNNoise + volume-gated VAD (D18); hold-to-talk fallback |
| Detector boxes non-books as books | Inflated count | `is_book` check in the read call (D19); local detector gives tight outlines (no invented boxes) |
| Used-price sources thin in some countries | Many blanks/converted | Accept and label honestly; second country chosen for good coverage |
| Guidance accepted by the model service but never spoken | The agent greets, then never guides | App messages are sent as a user message plus a response request (D23); verified against the real voice agent, with regression tests |
| Live agent drifts into stating facts | Fabrication in speech | Prompt + tool-only fact path; voice summary read from the packet |
| Overfitting to own room | Collapse at review | Test on a second, unseen shelf before submission |

## 11. Assumptions to state in README

1. A printed marker placed on the shelf counts as a reference object, not a separate measuring pass.
2. Replacement cost = lowest credible new price from a local retailer; used value = median of comparable used listings.
3. Appraisal threshold is set once in config and stated in the report.
4. "Local" means retailers/marketplaces serving the chosen country; otherwise converted and labelled.
5. Latency budget: sweep ≤3 min, finalize ≤3 min, leaving 2 min slack against the 5 min bar.

## 12. Open questions (answer before build)

1. Phone model and OS (LiDAR optional, not required). Tunnel or local HTTPS for the recorded demo, based on Wi-Fi quality in the room.
2. Primary and second country for pricing.
3. Which room/shelves for the recorded sweep, and a second unseen space for testing.
4. GPU access for reconstruction (rented worker or local).
5. Budget ceiling per sweep.

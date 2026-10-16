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
| D1 | **Two media paths.** Voice goes phone ↔ OpenAI Realtime over WebRTC (the server only signals and attaches sideband); high-res keyframes go to the server for reading and measuring. The voice model does not see the camera. | A live model at ~1 fps low res can't read spines anyway; facts come from the pipeline, and capture problems reach the agent as text (D10). |
| D2 | **Live agent talks; it does not produce facts.** It calls tools; the pipeline writes the inventory. | Keeps hallucination out of the record. Same split the reference uses (LLM extracts, code decides). |
| D3 | **Metric scale from a printed ArUco marker** on/near the shelf, cross-checked against standard book formats. | Brief accepts a reference object. Deterministic, explainable line by line. |
| D4 | **Room geometry from multi-view reconstruction, scaled by the marker.** Fallback: WebXR AR hit-testing or LiDAR capture. | No LiDAR assumption on user's phone. Go/no-go decided in the first spike. |
| D5 | **Catalogue-grounded identification.** Vision model reads spine text; Open Library / Google Books resolves it; fuzzy score below threshold means `unidentified`. | A title not found in a catalogue cannot be "confidently" asserted. |
| D6 | **Prices only from retrieved listings** (shopping search, eBay API), FX from a rates API. LLM may pick *which listing matches a book*, never a number. | Meets NFR-I1. |
| D7 | **Plain Python orchestration**, not an agent framework, for the post-sweep pipeline. | Easier to test, time, and explain. |
| D8 | **Sweep stored as a directory of JSON + frames.** | Enables re-runs, eval, and traceability with no database. |
| D9 | **Identify is segment → orient → read → match**, not "send frame to a model". | Rotated spines and flat books need an upright crop; OCR alone does not fix orientation. |
| D10 | **Capture problems are detected by code; the agent only speaks them.** A Coach module emits `CAPTURE_ISSUE` with a reason code and a suggested ask. | Agent cannot skip or invent a reading. Behaviour is testable. |
| D11 | **Give-up rule:** max 2 asks per region, then `unidentified` with a reason. | No-guess guarantee; keeps the sweep moving. |
| D12 | **No AR/LiDAR dependency.** Any phone browser camera; marker for scale; reconstruction from RGB. LiDAR/WebXR only as optional upgrade. | Works on cheap phones; laptop webcam is a last resort. |
| D13 | **Take perception and lookup from open/hosted components; build only glue and honesty rules.** | See section 5.1. |
| D15 | **Voice over WebRTC, server sideband.** The phone sends its WebRTC offer to our server, which creates the OpenAI call (key stays on the laptop) and attaches to the same call to run tools and stream captions. | Raw audio over our WebSocket bypassed the browser's echo cancellation and noise suppression: the agent heard itself and the room. WebRTC also works on any network, mobile data included. |
| D16 | **Dedupe by frame-to-frame tracking** (homography between overlapping keyframes). | Linking by identified neighbours fails when few books are identified (a real sweep turned 97 sightings into 97 books). |
| D17 | **Prompts are code constants in one package** (`backend/prompts/`); **mini models by default.** | One place to change what any model is told; lower cost per sweep. |
| D14 | **Phone is the capture device; the laptop runs the server.** The phone opens the web app in its browser over HTTPS (tunnel or local HTTPS). Laptop webcam is not the primary camera. | Walking a room needs a handheld rear camera; browsers allow camera/mic only on HTTPS, so access topology is part of the design. |

### 5.1 Build vs take

| Problem | Take (free / open / hosted) | We build |
|---|---|---|
| Live voice | OpenAI Realtime (`gpt-realtime-mini`) over WebRTC; browser echo cancellation / noise suppression | Signalling endpoint, sideband controller, prompt, tools, coaching messages |
| Blur / glare / brightness | OpenCV | Thresholds, per-region aggregation |
| Find and read spines | Vision LLM on upscaled crops; optional detector/segmenter (YOLO, Grounding DINO, SAM2) | Segment → orient → read flow, structured output, reason codes |
| Title to real book | Open Library, Google Books APIs | Fuzzy match + accept/reject threshold |
| Metric scale | OpenCV ArUco | px to cm per shelf plane, cross-checks |
| Room dimensions | RGB reconstruction model (e.g. VGGT, COLMAP); optional monocular metric depth as a cross-check | Plane fitting, scaling with marker, area math, fallbacks |
| Prices | eBay Browse API, shopping search API, Frankfurter FX | Listing-to-book matching, source/URL/date logging, `converted` flag, appraisal rules |
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
┌──────────────────────── PHONE BROWSER ─────────────────────────┐
│  Mic ─────────────► WebRTC audio ◄──────────────────────────────┼───────────► OpenAI Realtime
│  Speaker ◄──────── (echo cancel + noise suppression) ───────────┼──── voice ◄──  (gpt-realtime-mini)
│  Camera ─┬─► quality check (320 px) ─► keyframes hi-res (video frames) ──► POST /api/sweeps/<id>/frames
│          └─► preview                                            │                     ▲
│  Transcript + inventory panel ◄── /ws/live (down only) ◄────────┼──┐                  │ sideband (same call)
└─────────────────────────────────────────────────────────────────┘  │                  │
                         │ HTTPS/WSS (tunnel)                        │                  │
┌────────────────────────▼────────────────────────── BACKEND (FastAPI) ─────────────────┴────────┐
│  /voice: WebRTC signalling (creates the call; API key stays here)                               │
│  Voice agent (sideband): tools record_user_fact, mark_region, finish_sweep; captions + state ──┘│
│  Coach: reason codes -> CAPTURE_ISSUE text for the agent                                        │
│  Sweep Store (disk):  sweeps/<id>/ frames/ frames.json sightings/ links.json user_facts.json …  │
│                                                                                                 │
│  Pipeline (each stage: JSON in → JSON out)                                                      │
│   ┌──────────┐  ┌─────────────┐  ┌─────────┐  ┌────────┐  ┌──────────┐                          │
│   │ IDENTIFY │─►│ TRACK +     │─►│ MEASURE │─►│ PRICE  │─►│ ASSEMBLE │─► packet + report        │
│   │ spines + │  │ DEDUPE      │  │ scale + │  │ sourced│  │ totals + │                          │
│   │ items    │  │             │  │ room 3D │  │ only   │  │ review q │                          │
│   └────┬─────┘  └─────────────┘  └────┬────┘  └───┬────┘  └──────────┘                          │
└────────┼──────────────────────────────┼───────────┼─────────────────────────────────────────────┘
         ▼                              ▼           ▼
   OpenAI vision (gpt-4.1-mini)   ArUco (OpenCV)   eBay Browse, FX rates
   Open Library / Google Books    3D reconstruction (not built yet)
```

### 6.2 Responsibilities

| Component | Responsibility | Never does |
|---|---|---|
| Browser quality checker | Blur, glare, brightness scores per frame; "which shelf" hint from motion | Identify content |
| Live server + voice signalling | Create the WebRTC call, attach sideband, run tools, push captions / agent state / inventory to the UI | Carry voice audio; write inventory facts itself |
| Live voice agent | Converse, coach, ask questions, relay user corrections via tools | State prices, titles, or dimensions as fact |
| Identify | Per keyframe: segment each book, classify orientation, rotate/upscale crop, read text, catalogue match + score; non-book classification; emit reason code on failure | Guess below threshold |
| Coach | Per-region state (`ask_count`, `resolved`); turns reason codes into one rate-limited `CAPTURE_ISSUE` message; closes region after 2 asks | Read, name, or judge content |
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
| (inbound) `CAPTURE_ISSUE{region, reason, ask}` | Sent by Coach, not called by the agent. Agent speaks the ask in one sentence. |
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
- `identified` if catalogue match score ≥ T and spine text agrees
- `unidentified` otherwise (dims still recorded)
- `needs_appraisal` if signed/rare flag, art without print confirmation, or estimated value > threshold

### 6.7 Identify detail

```
keyframe (hi-res)
 → segment: one region per book (box/mask); non-book objects separate
 → classify layout: upright | rotated | flat | stacked | cover_only
 → crop + rectify (marker homography when available) + upscale
 → orient: try 2 rotations for tall boxes (4 for unknown); keep best catalogue score
 → read: vision model returns {title, author, publisher} JSON from the crop only
 → match: Open Library / Google Books; accept if score ≥ T, else unidentified
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

### 6.8 Coach

Reason codes (computed in code):

| Code | Detector | Suggested ask |
|---|---|---|
| `too_far` | Spine text height < ~12 px or tiny box | "Step a bit closer to shelf 2." |
| `blur` | Laplacian variance below threshold | "Hold steady for a second." |
| `glare` | Blown-out pixel patch over region | "Tilt the phone or step to the side." |
| `too_dark` | Low mean brightness | "Can you turn on a light?" |
| `blocked` | Non-book object overlaps region | "Could you move the lamp, or shift so I can see past it?" |
| `partial` | Box cut by frame edge | "Pan a bit further right." |
| `stack_hidden` | Spine not visible | No ask; logged as unidentified. |

Behaviour:
- Group by shelf region; one message at a time; ≥8 s between messages; wait ~2 s for the next frame to fix it.
- Max 2 asks per region; then close as `unidentified` (reason kept) and say so once.
- Never ask the user to pull out, open, rearrange, or individually photograph books. Moving an obstruction or moving closer is allowed.
- Agent has no tool that writes a title, price, or measurement.

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
 │ web app + panel  │  via tunnel or   │ frames API, pipeline, caches │
 └──────────────────┘  local HTTPS     │ API keys (never sent to phone)│
                                       └──────────────┬───────────────┘
                                                      ▼  OpenAI (vision; Realtime call setup + sideband), catalogue, price APIs
 Phone ◄──── WebRTC voice ────► OpenAI Realtime (direct; audio never passes through the laptop)
```

| Aspect | Decision |
|---|---|
| Serving | Laptop serves the static web app and API on one origin, so no CORS and one WSS endpoint. |
| HTTPS | Required for camera/mic on the phone. Default: ngrok on a fixed domain (`NGROK_DOMAIN`, same link and QR every run), else a quick tunnel (cloudflared or ngrok). Alternative on same Wi-Fi: `mkcert` local HTTPS (lower latency, CA must be trusted on the phone). |
| Access control | Random per-sweep token in the URL; server rejects WS/API calls without it. No user accounts (out of scope). |
| Secrets | API keys stay in laptop `.env`; the phone only receives the page and media endpoints. |
| Bandwidth | ~2 hi-res keyframes/s ≈ 1 to 2 MB/s upstream. Browser adapts JPEG quality/fps if the upload queue grows. |
| Keep-alive | Screen Wake Lock on the phone; foreground tab required; reconnect with transcript seed if the socket drops. |
| Privacy | With a tunnel, media transits the tunnel provider; use local HTTPS for sensitive spaces. Documented in README. |
| Reference change | Remove its localhost-only middleware and origin checks; replace with token + allowed-origin config. |

## 7. Failure and degradation matrix

| Failure | Behaviour |
|---|---|
| Marker not visible on a shelf | Fall back to standard-format heuristic; lower confidence; add to review queue |
| 3D reconstruction fails or is implausible | Room dims from marker-scaled plane fits; flag low confidence; review queue |
| Catalogue API down / no match | `unidentified`, never a guessed title |
| Price source down / no listing | blank price, counted in `excluded_from_totals` |
| Live model session drops | Phone re-establishes the WebRTC call; sideband re-attaches; frames already saved so pipeline unaffected |
| Pipeline exceeds 3 min budget | Stages emit partial results; packet built with what exists; unfinished lines go to review queue |
| Low-res camera / spine too small | `unidentified` + `too_far`/`blur`; dims kept; honest count in results |
| Region still failing after 2 asks | Closed as `unidentified`, reason in review queue |
| Spine not facing camera (stacked) | Counted, dims recorded, `stack_hidden`, no ask |
| Phone upload stalls (weak Wi-Fi / tunnel) | Browser lowers keyframe quality/fps; Coach tells user to slow down; keyframes queue and flush; sweep continues |
| Background voices (TV, other people) | Heard as speech in hands-free mode (noise reduction removes only hiss/hum); user switches to hold-to-talk |
| Voice call drops | Browser re-offers (up to 5 times with backoff); the server hangs up the old call and attaches to the new one; capture continues meanwhile |
| Phone sleeps or tab backgrounded | Wake Lock requested; on disconnect the app shows a resume prompt and reconnects; frames already saved are kept |
| Tunnel URL changes between runs | Page and API share one origin and the QR/URL is printed at server start |

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
| Duplicate counting across frames | Blows 5% count bar | Position-based dedupe + evaluation per shelf |
| Spine OCR wrong on small/rotated text | Confident-wrong titles | Catalogue match threshold, precision-first tuning, high-res crops |
| Used-price sources thin in some countries | Many blanks/converted | Accept and label honestly; second country chosen for good coverage |
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

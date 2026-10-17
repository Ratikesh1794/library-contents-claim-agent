"""Every tunable lives here (one frozen dataclass). Stages take `cfg=CFG` so tests can
override with `dataclasses.replace(CFG, title_match_T=0.9)`. Nothing is hard-coded inline.

Country and currency are NOT config: they come per sweep from meta.json.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent


def _opt_float(name: str) -> float | None:
    v = os.environ.get(name, "").strip()
    return float(v) if v else None


@dataclass(frozen=True)
class Config:
    # --- paths
    sweeps_dir: Path = Path(os.environ.get("SWEEPS_DIR") or ROOT / "sweeps")
    cache_dir: Path = Path(os.environ.get("CACHE_DIR") or ROOT / "cache")

    # --- models (override via env)
    # Mini models by default (lower cost). Full-size alternatives: VISION_MODEL=gpt-4.1, LIVE_MODEL=gpt-realtime
    vision_model: str = os.environ.get("VISION_MODEL", "gpt-4.1-mini")   # any vision + structured-output model
    live_model: str = os.environ.get("LIVE_MODEL", "gpt-realtime-mini")   # OpenAI Realtime model
    transcribe_model: str = os.environ.get("TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
    voice: str = os.environ.get("VOICE", "alloy")
    # Voice engine: "pipecat" (default; laptop filters the audio and decides turns, backend/live/voice_pipecat.py;
    # needs the phone on the same Wi-Fi) or "direct" (phone straight to OpenAI over WebRTC, backend/live/agent.py).
    voice_engine: str = os.environ.get("VOICE_ENGINE", "pipecat")
    noise_filter: str = os.environ.get("NOISE_FILTER", "rnnoise")          # pipecat engine: rnnoise | off
    # pipecat engine: loudness (Pipecat's 0..1 scale, compressed) needed to count as speech. Measured: user near the phone
    # 0.84-0.91 (0.78 at half volume); a TV ~22 dB quieter 0.64-0.67. 0.72 sits between; raise if a TV still triggers it.
    vad_min_volume: float = float(os.environ.get("VAD_MIN_VOLUME", "0.72"))
    vad_confidence: float = 0.7          # Silero: how sure it must be that the sound is speech
    vad_start_secs: float = 0.3          # speech must last this long before it counts (coughs, bangs don't interrupt)
    vad_stop_secs: float = 0.5           # silence that ends the user's turn
    ice_servers: tuple[str, ...] = tuple(s.strip() for s in os.environ.get("ICE_SERVERS", "stun:stun.l.google.com:19302").split(",") if s.strip())
    # Voice input in noisy rooms (see agent.session_config). far_field = phone held away from the mouth.
    noise_reduction: str = os.environ.get("NOISE_REDUCTION", "far_field")   # far_field | near_field | off
    vad_threshold: float = float(os.environ.get("VAD_THRESHOLD", "0.7"))    # 0..1; higher = ignores more background noise
    vad_silence_ms: int = 700           # pause that ends the user's turn
    vad_prefix_ms: int = 300            # audio kept from just before speech was detected
    usd_per_mtok: float = 0.6  # rough blended estimate for gpt-4.1-mini, cost logging only (check current pricing)

    # --- quality gating (see quality.py; values from real sweeps, tune on your own footage)
    blur_norm_px: int = 128      # blur is measured on a copy this size (long side), so crops of any size compare
    blur_min: float = 80.0       # readable spines measured 160-2000 at 128 px; plain/blurred boxes fall below ~80
    glare_max: float = 0.04
    dark_min: float = 60.0       # 99th-percentile brightness; below this even the lettering is too dark
    min_text_px: int = 12      # text height proxy = 0.6 * spine thickness in px

    # --- detect (backend/adapters/local_detect.py). "local" = open-source models on this laptop, no prompt, no cost;
    # "openai" = the DETECT prompt. Weights live in models/ (downloaded on first use, not in git).
    # Compared on real sweep frames: COCO YOLO11 splits stacked/packed books best (e.g. 25 vs 16 books on one shelf);
    # open-vocabulary YOLOE finds room items COCO doesn't know (cabinet, rug, picture frame) or mislabels (cat, suitcase).
    detector: str = os.environ.get("DETECTOR", "local")                    # local | openai
    models_dir: Path = Path(os.environ.get("MODELS_DIR") or ROOT / "models")
    book_model: str = "yolo11m-seg.pt"      # COCO; only class 73 "book" is used
    item_model: str = "yoloe-11l-seg.pt"    # open vocabulary: finds whatever item_vocab names
    item_vocab: tuple[str, ...] = (
        "chair", "sofa", "table", "desk", "bed", "lamp", "television", "computer monitor", "laptop", "picture frame",
        "rug", "potted plant", "vase", "clock", "speaker", "cabinet", "bookshelf", "printer", "laundry basket", "bag",
    )
    detect_conf: float = 0.15
    detect_imgsz: int = 1280                # model input size (long side)
    detect_dup_iou: float = 0.5             # same-kind boxes overlapping this much are one object
    detect_dup_ioa: float = 0.8             # ...or a box this much inside a larger one (fragments of one book/chair)

    # --- identify
    min_box_px: int = 900
    max_spine_aspect: float = 40.0
    occl_iou: float = 0.3
    # Strict identity (catalogue.resolve): a book is identified ONLY if the title AND the author read from the
    # spine both match the same catalogue record; anything else stays undefined (None). Never a guess.
    title_match_T: float = 0.85    # read title vs record title (or its main title before a subtitle), symmetric
    author_match_T: float = 0.8    # read author vs record author (surname-only or truncated spines still match)
    publisher_match_T: float = 0.85  # edition/year/ISBN are recorded only when the spine's publisher matches too
    review_T: float = 0.9      # below this match score, an identified line also goes to review
    orient_early_exit: bool = True
    target_text_px: int = 32
    max_upscale: float = 3.0
    crop_margin: float = 0.08
    identify_workers: int = 6
    crop_workers: int = 6        # spine reads run in parallel within one frame
    reuse_iou: float = 0.5       # a box that tracks onto an already-identified box reuses that read (no new API call)
    # "Only analyse what is new": a live keyframe mostly showing the last ANALYSED frame copies its results instead of
    # calling the vision model again (identify.carry_over). Never when the user moved closer or the view had fixable failures.
    novelty_skip_coverage: float = 0.85   # skip detection if this much of the new frame was already analysed
    novelty_max_zoom: float = 1.15        # ...and things look at most this much bigger (moving closer = re-read)
    retry_reasons: tuple[str, ...] = ("blur", "glare", "too_dark", "too_far", "blocked", "partial")

    # --- coach
    asks_per_region: int = 2
    ask_gap_s: float = 8.0
    settle_s: float = 2.0
    resolve_ratio: float = 0.8

    # --- tracking / dedupe (backend/stages/track.py)
    track_max_side: int = 1024   # frames are downscaled to this for feature matching
    track_features: int = 2000   # ORB keypoints per frame
    track_min_inliers: int = 25  # fewer matches than this => frames are not linked (no overlap)
    track_span: int = 2          # link each frame to the next N frames (bridges one missed detection)
    track_iou: float = 0.3       # projected box vs box overlap needed to call two sightings the same object
    item_contain_weight: float = 0.6   # items: "one box inside the other" counts as this much overlap (0.6 * 1.0 >= track_iou)

    # --- measure
    marker_side_cm: float | None = field(default_factory=lambda: _opt_float("MARKER_SIDE_CM"))
    marker_dict: str = "DICT_4X4_50"
    marker_max_skew: float = 1.25      # reject markers whose opposite sides differ more than this
    spine_height_sane_cm: tuple[float, float] = (10.0, 40.0)

    # --- price
    list_T: float = 0.8
    appraisal_threshold_usd: float = 100.0  # converted to the sweep currency via FX at runtime
    antiquarian_year: int = 1900
    used_min_listings: int = 3
    diverge_ratio: float = 5.0
    condition_assumed: str = "good"
    http_timeout_s: float = 8.0
    price_workers: int = 6
    listing_blocklist: tuple[str, ...] = (
        "lot of", "set of", "bundle", "collection", "box set", "ebook", "kindle",
        "audiobook", "dvd", "poster", "summary of", "study guide", "pdf",
    )
    listing_noise: tuple[str, ...] = (   # words in listing titles that are not part of the book's title
        "paperback", "hardcover", "hardback", "edition", "book", "books", "novel", "new", "used", "by", "mass",
        "market", "trade", "pb", "hc", "first", "printing", "good", "very", "like", "fine", "vintage", "the", "a",
    )
    item_categories: tuple[str, ...] = (
        "furniture", "shelving", "lamp", "appliance", "art", "rug", "electronics", "decor", "other",
    )

    # --- serving
    keyframe_max_bytes: int = 3_000_000
    audio_chunk_max_bytes: int = 128_000
    ws_message_max_bytes: int = 4_000_000
    public_origin: str = os.environ.get("PUBLIC_ORIGIN", "")
    allowed_origins: tuple[str, ...] = ()

    # --- rate limits per sweep: (max requests, per seconds); over the limit => HTTP 429, the phone retries
    rate_frames: tuple[int, float] = (5, 1.0)
    rate_voice: tuple[int, float] = (6, 60.0)
    rate_finish: tuple[int, float] = (3, 60.0)
    rate_new_sweep: tuple[int, float] = (10, 60.0)

    # --- pipeline budgets (seconds); stages that overrun mark themselves incomplete
    budget_identify_s: float = 60.0
    budget_price_s: float = 60.0


CFG = Config()

"""Every tunable lives here (one frozen dataclass). Stages take `cfg=CFG` so tests can
override with `dataclasses.replace(CFG, accept_T=0.9)`. Nothing is hard-coded inline.

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

    # --- identify
    min_box_px: int = 900
    max_spine_aspect: float = 40.0
    occl_iou: float = 0.3
    accept_T: float = 0.85     # catalogue match needed to call a book "identified"
    review_T: float = 0.9      # below this, an identified line also goes to review
    ambiguity_gap: float = 0.05
    ambiguity_penalty: float = 0.2
    orient_early_exit: bool = True
    target_text_px: int = 32
    max_upscale: float = 3.0
    crop_margin: float = 0.08
    identify_workers: int = 6
    crop_workers: int = 6        # spine reads run in parallel within one frame
    reuse_iou: float = 0.5       # a box that tracks onto an already-identified box reuses that read (no new API call)

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

    # --- pipeline budgets (seconds); stages that overrun mark themselves incomplete
    budget_identify_s: float = 60.0
    budget_price_s: float = 60.0


CFG = Config()

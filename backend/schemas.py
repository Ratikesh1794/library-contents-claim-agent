"""Single source of truth for every shape that crosses a stage boundary (JSON on disk).

Rule: a field the system does not know is None, never "" or 0.
Units: cm for objects, m for room, px for image space.
IDs: f_0012 frame, s_0012_03 sighting, b_017 book, i_003 item.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

Status = Literal["identified", "unidentified", "needs_appraisal"]
Reason = Literal["too_far", "blur", "glare", "too_dark", "blocked", "partial", "stack_hidden",
                 "no_catalogue_match", "low_confidence", "user_skipped", "author_not_visible", "ambiguous_match"]
Layout = Literal["upright", "flat", "stacked", "cover_only", "unknown"]
BBox = tuple[int, int, int, int]  # x0, y0, x1, y1 in frame px


# ---------------------------------------------------------------- capture / identify
class Frame(BaseModel):
    id: str
    path: str                 # relative to the sweep dir, e.g. "frames/f_0012.jpg"
    t: float                  # seconds from sweep start
    w: int
    h: int
    blur: float
    glare: float
    dark: float
    seq: int | None = None    # browser sequence number (dedupes re-uploads)


class Candidate(BaseModel):
    title: str
    author: str | None = None
    isbn: str | None = None
    publishers: list[str] = []
    year: int | None = None   # EDITION year only (used for the antiquarian rule)
    edition_level: bool = False   # True if this record is ONE edition (its ISBN/year belong to that edition);
                                  # False for work-level records that merge many editions (Open Library search)
    edition: str | None = None
    source_url: str
    score: float = 0.0


class ItemGuess(BaseModel):
    category: str | None = None
    material: str | None = None
    brand_model: str | None = None   # only if text/logo visible in the crop
    dims_hint: str | None = None     # free text from the vision model, never used as a number


class Sighting(BaseModel):
    id: str
    frame_id: str
    kind: Literal["book", "item"]
    bbox: BBox
    layout: Layout | None = None
    rotation_deg: int | None = None
    raw_text: str | None = None         # what the vision model read; NEVER promoted to a title
    legible: bool | None = None
    accepted: Candidate | None = None   # the only source of a title
    candidates: list[Candidate] = []
    score: float | None = None
    reason: Reason | None = None
    shelf_hint: str | None = None       # "row1", "row2" ... top-down within the frame
    crop_blur: float | None = None
    item: ItemGuess | None = None
    carried_from: str | None = None     # set when copied from an earlier analysed frame (view already seen), not detected here


class UserFact(BaseModel):
    ref: str | None = None    # object id, shelf/region id, or None
    text: str                 # "first edition", "signed", "print", "skip"
    turn_id: str | None = None
    t: float = 0.0


# ---------------------------------------------------------------- dedupe
class Object(BaseModel):
    id: str
    kind: Literal["book", "item"]
    sighting_ids: list[str]
    shelf: str | None = None            # "<frame_id>:<row>" of the best sighting
    position: int | None = None         # index within that row, left to right
    status: Status
    id_confidence: float | None = None
    reason: Reason | None = None
    frame_ref: str                      # best frame, relative path
    bbox: BBox
    layout: Layout | None = None
    rotation_deg: int | None = None
    title: str | None = None
    author: str | None = None
    edition: str | None = None
    isbn: str | None = None
    year: int | None = None
    catalogue_match_score: float | None = None
    source_url: str | None = None
    category: str | None = None
    material: str | None = None
    brand_model: str | None = None
    dims_hint: str | None = None


# ---------------------------------------------------------------- measure
class MarkerObs(BaseModel):
    marker_id: int
    center: tuple[float, float]
    side_px: float
    px_per_cm: float
    skew: float


class BookDims(BaseModel):
    height_cm: float | None = None
    thickness_cm: float | None = None
    unc_cm: float | None = None
    scale_frame_id: str | None = None
    confidence: float | None = None
    reason: str | None = None           # "no_scale" when dims are None


class Geometry(BaseModel):
    scale: dict[str, list[MarkerObs]] = {}   # frame_id -> markers seen
    books: dict[str, BookDims] = {}          # object id -> dims
    shelf_run_m: float | None = None
    shelved_wall_area_m2: float | None = None
    scale_suspect: bool = False
    notes: list[str] = []


class Room(BaseModel):
    length_m: float | None = None
    width_m: float | None = None
    height_m: float | None = None
    floor_area_m2: float | None = None
    floor_area_ft2: float | None = None
    wall_area_m2: float | None = None        # gross: openings are NOT subtracted
    wall_area_ft2: float | None = None
    shelved_wall_area_m2: float | None = None
    shelved_wall_area_ft2: float | None = None
    scale_method: str | None = None
    confidence: float | None = None
    notes: list[str] = []


# ---------------------------------------------------------------- price
class Price(BaseModel):
    kind: Literal["replacement", "used"]
    amount: float | None = None
    low: float | None = None
    high: float | None = None
    currency: str
    source: str | None = None
    url: str | None = None
    retrieved_at: str | None = None
    condition: str | None = None
    count: int | None = None            # listings behind the number
    converted: bool = False
    fx_rate: float | None = None
    fx_date: str | None = None

    @model_validator(mode="after")
    def _sourced(self):
        if any(v is not None for v in (self.amount, self.low, self.high)):
            if not (self.source and self.url and self.retrieved_at):
                raise ValueError("a price needs source, url and retrieved_at (NFR-I1)")
        return self


class PriceResult(BaseModel):
    object_id: str
    status: Literal["priced", "range", "needs_appraisal", "unpriced"]
    replacement: Price | None = None
    used: Price | None = None
    reason: str | None = None           # why needs_appraisal / unpriced


# ---------------------------------------------------------------- packet (the contract)
def _check_frame_ref(v: str, info: ValidationInfo) -> str:
    from pathlib import Path
    sd = (info.context or {}).get("sweep_dir")
    if sd is not None and not (Path(sd) / v).is_file():
        raise ValueError(f"frame_ref {v} does not exist on disk")
    return v


class BookLine(BaseModel):
    id: str
    status: Status
    title: str | None = None
    author: str | None = None
    edition: str | None = None
    isbn: str | None = None
    catalogue_match_score: float | None = None
    id_confidence: float | None = None
    reason: str | None = None
    spine_height_cm: float | None = None
    spine_thickness_cm: float | None = None
    dims_unc_cm: float | None = None
    frame_ref: str
    bbox: BBox
    replacement: Price | None = None
    used: Price | None = None
    user_notes: list[str] = []

    _fr = field_validator("frame_ref")(_check_frame_ref)


class ItemLine(BaseModel):
    id: str
    status: Literal["priced", "range", "needs_appraisal", "unpriced"]
    category: str | None = None
    material: str | None = None
    brand_model: str | None = None
    dims_hint: str | None = None
    id_confidence: float | None = None
    frame_ref: str
    bbox: BBox
    price: Price | None = None          # low/high (low == high for a single price)
    reason: str | None = None
    user_notes: list[str] = []

    _fr = field_validator("frame_ref")(_check_frame_ref)


class Totals(BaseModel):
    currency: str
    book_count: int
    books_identified: int
    books_unidentified: int
    books_needs_appraisal: int
    item_count: int
    shelf_run_m: float | None
    books_replacement_cost: float
    books_used_value: float
    items_replacement_cost_low: float
    items_replacement_cost_high: float
    excluded_from_totals: int


class ReviewRef(BaseModel):
    ref_id: str
    reason: str
    detail: str | None = None


class Packet(BaseModel):
    sweep_id: str
    country: str | None
    currency: str
    generated_at: str
    room: Room | None
    totals: Totals
    books: list[BookLine]
    items: list[ItemLine]
    review_queue: list[ReviewRef]
    assumptions: list[str] = Field(default_factory=list)

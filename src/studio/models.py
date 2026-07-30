"""Versioned, strict models for the Product Image Studio."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Generic, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid4().hex


class SourceKind(str, Enum):
    OWN_CAPTURE = "own_capture"
    COMPETITOR_REFERENCE = "competitor_reference"
    GENERATED_RESULT = "generated_result"
    UNKNOWN = "unknown"


class ContentKind(str, Enum):
    PRODUCT_FULL_FRONT = "product_full_front"
    PRODUCT_FULL_BACK = "product_full_back"
    PRODUCT_SIDE = "product_side"
    FLAT_LAY = "flat_lay"
    MODEL_FRONT = "model_front"
    MODEL_BACK = "model_back"
    DETAIL = "detail"
    COLLAGE = "collage"
    SIZE_CHART = "size_chart"
    TEXT_REFERENCE = "text_reference"
    UNKNOWN = "unknown"


class Importance(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class StudioPlatform(str, Enum):
    TEMU = "temu"
    TIKTOK_SHOP = "tiktok_shop"


T = TypeVar("T")


class OverrideValue(BaseModel, Generic[T]):
    """A model suggestion and an optional human replacement."""

    model_config = ConfigDict(extra="forbid")
    model_value: T
    user_override: T | None = None

    @property
    def effective_value(self) -> T:
        return self.user_override if self.user_override is not None else self.model_value


class StudioProject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    id: str = Field(default_factory=new_id)
    name: str = Field(min_length=1, max_length=200)
    status: str = "draft"
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    target_platform: StudioPlatform = StudioPlatform.TEMU
    selected_style_pack_id: str | None = None


class Asset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=new_id)
    project_id: str
    original_filename: str = Field(min_length=1, max_length=255)
    stored_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    exif_summary: dict[str, str] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now)
    thumbnail_path: str | None = None
    annotation_path: str | None = None


class NormalizedBBox(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @field_validator("width")
    @classmethod
    def valid_width(cls, value: float, info: ValidationInfo) -> float:
        if info.data.get("x", 0) + value > 1:
            raise ValueError("bbox must stay within image bounds")
        return value

    @field_validator("height")
    @classmethod
    def valid_height(cls, value: float, info: ValidationInfo) -> float:
        if info.data.get("y", 0) + value > 1:
            raise ValueError("bbox must stay within image bounds")
        return value


class DetailRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=new_id)
    asset_id: str
    detail_type: OverrideValue[str]
    importance: OverrideValue[Importance]
    normalized_bbox: NormalizedBBox | None = None
    polygon: list[tuple[float, float]] | None = None
    label: OverrideValue[str]
    visual_facts: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    user_confirmed: bool = False


class AssetAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=new_id)
    asset_id: str
    source_kind: OverrideValue[SourceKind]
    content_kind: OverrideValue[ContentKind]
    detail_types: OverrideValue[list[str]]
    detail_regions: list[DetailRegion] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    reason: str = ""
    visual_facts: list[str] = Field(default_factory=list)
    analyzer_name: str
    analyzer_version: str
    schema_version: str
    source_image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    analyzed_at: str = Field(default_factory=utc_now)


class ProductFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=new_id)
    key: str = Field(min_length=1)
    value: str = Field(min_length=1)
    description: str = ""
    priority: Importance = Importance.MEDIUM
    evidence_asset_ids: list[str] = Field(default_factory=list)
    user_confirmed: bool = False
    status: str = "review"


class CanonicalProductSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    facts: list[ProductFact] = Field(default_factory=list)
    compiled_at: str = Field(default_factory=utc_now)
    schema_version: int = 1


class StylePack(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    platform: StudioPlatform
    version: str
    name: str
    composition: str
    lighting: str
    background: str
    visual_tone: str
    product_preservation_rules: list[str]
    forbidden_elements: list[str]
    output_aspect_ratio: str
    reference_asset_ids: list[str] = Field(default_factory=list)


class ReferenceBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    product_asset_ids: list[str] = Field(default_factory=list)
    style_asset_ids: list[str] = Field(default_factory=list)
    detail_board_path: str | None = None
    product_spec: CanonicalProductSpec
    style_pack: StylePack | None = None
    compiled_at: str = Field(default_factory=utc_now)


class StudioRecord(BaseModel):
    """Single atomically-written aggregate per Studio project."""

    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    project: StudioProject
    assets: list[Asset] = Field(default_factory=list)
    analyses: list[AssetAnalysis] = Field(default_factory=list)
    product_spec: CanonicalProductSpec | None = None

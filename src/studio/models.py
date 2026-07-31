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


class ReferenceRole(str, Enum):
    PRODUCT_REFERENCE_CLEAN = "product_reference_clean"
    DETAIL_REFERENCE_CLEAN = "detail_reference_clean"
    HUMAN_ANNOTATION_PREVIEW = "human_annotation_preview"
    STYLE_REFERENCE = "style_reference"
    CANONICAL_PRODUCT_SPEC = "canonical_product_spec"
    STYLE_PACK = "style_pack"


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
    selected_style_pack: StylePack | None = None


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
    config_version: str
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
    references: list[ReferenceItem] = Field(default_factory=list)
    compiled_at: str = Field(default_factory=utc_now)


class ReferenceItem(BaseModel):
    """A role-labelled item so future image providers cannot mix visual inputs."""

    model_config = ConfigDict(extra="forbid")
    role: ReferenceRole
    asset_id: str | None = None
    relative_path: str | None = None


class StudioRecord(BaseModel):
    """Single atomically-written aggregate per Studio project."""

    model_config = ConfigDict(extra="forbid")
    schema_version: int = 2
    project: StudioProject
    assets: list[Asset] = Field(default_factory=list)
    analyses: list[AssetAnalysis] = Field(default_factory=list)
    product_spec: CanonicalProductSpec | None = None
    shot_plans: list[ShotPlan] = Field(default_factory=list)
    prompt_packages: list[PromptPackage] = Field(default_factory=list)
    generation_jobs: list[GenerationJob] = Field(default_factory=list)
    generation_attempts: list[GenerationAttempt] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)


# M2 generation entities intentionally remain in the Studio aggregate.  This
# preserves project isolation and lets the existing atomic JSON/lock store be
# used without adding a database or an external worker.
class PlanStatus(str, Enum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    STALE = "stale"
    BLOCKED = "blocked"


class GenerationStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class CandidateStatus(str, Enum):
    GENERATED = "generated"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ShotSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=new_id)
    shot_type: str
    title: str
    purpose: str
    aspect_ratio: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    composition: str
    subject_requirements: list[str] = Field(default_factory=list)
    required_fact_keys: list[str] = Field(default_factory=list)
    forbidden_elements: list[str] = Field(default_factory=list)
    reference_policy: str = "clean_product_and_detail"
    user_instruction: str = ""
    sequence: int = Field(ge=1)
    enabled: bool = True


class ShotPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=new_id)
    project_id: str
    platform: StudioPlatform
    style_pack_id: str
    style_pack_version: str
    product_spec_version: str
    version: int = 1
    status: PlanStatus = PlanStatus.DRAFT
    shots: list[ShotSpec] = Field(default_factory=list)
    content_hash: str
    blocking_reasons: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    confirmed_at: str | None = None
    confirmed_by: str | None = None


class PromptPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=new_id)
    project_id: str
    shot_id: str
    compiler_name: str = "studio-prompt-compiler"
    compiler_version: str = "m2a-1"
    schema_version: str = "1"
    rendered_prompt: str
    negative_prompt: str
    structured_product_facts: list[dict[str, str]] = Field(default_factory=list)
    structured_style_rules: list[str] = Field(default_factory=list)
    structured_composition: dict[str, str] = Field(default_factory=dict)
    product_reference_ids: list[str] = Field(default_factory=list)
    detail_reference_ids: list[str] = Field(default_factory=list)
    style_reference_ids: list[str] = Field(default_factory=list)
    annotation_preview_ids: list[str] = Field(default_factory=list)
    content_hash: str
    stale: bool = False
    created_at: str = Field(default_factory=utc_now)


class ProviderCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    model: str
    supports_image_references: bool = False
    supports_multiple_references: bool = False
    max_reference_images: int = Field(default=0, ge=0)
    supports_edit: bool = False
    supports_mask: bool = False
    supports_seed: bool = False
    supports_negative_prompt: bool = True
    supported_aspect_ratios: list[str] = Field(default_factory=list)
    supported_output_sizes: list[str] = Field(default_factory=list)
    pricing_version: str | None = None


class BudgetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_limit: float | None = Field(default=None, ge=0)
    job_limit: float | None = Field(default=None, ge=0)
    shot_limit: float | None = Field(default=None, ge=0)
    currency: str = "USD"
    require_confirmation: bool = True
    pricing_version: str | None = None
    allow_unknown_pricing: bool = False


class GenerationJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=new_id)
    project_id: str
    shot_plan_id: str
    status: GenerationStatus = GenerationStatus.QUEUED
    mode: str = "mock"
    provider: str
    model: str
    budget_policy: BudgetPolicy
    estimated_total_cost: float | None = 0.0
    reserved_cost: float | None = 0.0
    actual_total_cost: float | None = None
    created_at: str = Field(default_factory=utc_now)
    confirmed_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error_summary: str | None = None
    generation_intent: str = "initial"
    confirmed_by: str | None = None


class GenerationAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=new_id)
    job_id: str
    shot_id: str
    attempt_number: int = Field(ge=1)
    provider_request_id: str | None = None
    status: GenerationStatus = GenerationStatus.QUEUED
    request_hash: str
    prompt_package_id: str
    reference_asset_ids: list[str] = Field(default_factory=list)
    estimated_cost: float | None = 0.0
    actual_cost: float | None = None
    idempotency_key: str
    claimed_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error_code: str | None = None
    error_message_safe: str | None = None
    generation_intent: str = "initial"
    generation_nonce: str | None = None
    confirmed_by: str | None = None


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=new_id)
    project_id: str
    shot_id: str
    attempt_id: str
    stored_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    mime_type: str
    status: CandidateStatus = CandidateStatus.GENERATED
    created_at: str = Field(default_factory=utc_now)
    accepted_at: str | None = None
    rejected_at: str | None = None
    rejection_reason: str | None = None

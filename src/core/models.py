"""Pydantic data models for TEMU Image Factory."""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TaskType(str, Enum):
    DETERMINISTIC = "deterministic"
    AI_GENERATE = "ai_generate"
    AI_EDIT = "ai_edit"


class TaskStatus(str, Enum):
    PENDING = "pending"
    GENERATED = "generated"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FAILED = "failed"
    SKIPPED = "skipped"


class ProductFeature(BaseModel):
    id: str
    title: str
    subtitle: str


class SizeInfo(BaseModel):
    chest_cm: float | None = None
    length_cm: float | None = None
    sleeve_cm: float | None = None
    shoulder_cm: float | None = None


class ProductInfo(BaseModel):
    sku: str
    product: dict[str, Any] = Field(default_factory=dict)
    fabric: dict[str, Any] = Field(default_factory=dict)
    features: list[ProductFeature] = Field(default_factory=list)
    images: dict[str, str] = Field(default_factory=dict)
    sizes: dict[str, SizeInfo] = Field(default_factory=dict)

    @field_validator("sku")
    @classmethod
    def validate_sku(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("SKU cannot be empty")
        if "/" in v or "\\" in v or ".." in v:
            raise ValueError("SKU contains invalid characters")
        return v


class PlatformTask(BaseModel):
    id: str
    name: str
    type: TaskType
    description: str = ""
    task_category: str | None = None
    required_images: list[str] = Field(default_factory=list)


class PlatformConfig(BaseModel):
    platform: str
    ratio: str
    width: int
    height: int
    background_color: str = "#FFFFFF"
    tasks: list[PlatformTask] = Field(default_factory=list)
    content_rules: dict[str, Any] = Field(default_factory=dict)
    model_direction: dict[str, Any] = Field(default_factory=dict)
    garment_preservation: list[str] = Field(default_factory=list)


class ModelCapabilities(BaseModel):
    text_to_image: bool = False
    image_edit: bool = False
    multi_image: bool = False
    exact_size: bool = False
    mask: bool = False
    inpaint: bool = False
    upscale: bool = False


class PricingContract(BaseModel):
    """Versioned, evidence-backed pricing contract for one provider model.

    ``pricing_status="exact"`` is only valid when every evidence field is
    present; anything less stays ``unknown`` and can never unlock Live.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    provider_model_id: str
    pricing_status: str = "unknown"
    pricing_version: str | None = None
    pricing_source: str | None = None
    source_type: str | None = None
    effective_at: str | None = None
    retrieved_at: str | None = None
    currency: str = "USD"
    unit: str | None = None
    amount: float | None = Field(default=None, gt=0)
    request_mode: str | None = None
    supported_output_sizes: list[str] = Field(default_factory=list)
    supported_resolutions: list[str] = Field(default_factory=list)
    supported_aspect_ratios: list[str] = Field(default_factory=list)
    supported_quality_levels: list[str] = Field(default_factory=list)
    reference_policy: str | None = None
    output_count: int = Field(default=1, ge=1)
    evidence_digest: str | None = None
    expires_at: str | None = None
    revoked: bool = False

    @model_validator(mode="after")
    def exact_requires_full_evidence(self) -> PricingContract:
        if self.pricing_status != "exact":
            return self
        missing = [
            field
            for field, value in (
                ("pricing_version", self.pricing_version),
                ("pricing_source", self.pricing_source),
                ("source_type", self.source_type),
                ("effective_at", self.effective_at),
                ("retrieved_at", self.retrieved_at),
                ("unit", self.unit),
                ("amount", self.amount),
                ("request_mode", self.request_mode),
                ("evidence_digest", self.evidence_digest),
            )
            if value in (None, "")
        ]
        if missing:
            raise ValueError(f"exact pricing contract is missing required evidence fields: {', '.join(missing)}")
        return self


class ModelConfig(BaseModel):
    name: str
    provider: str
    model: str
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    image_size: str | None = None
    default_image_size: str | None = None
    candidate_image_size: str | None = None
    default_aspect_ratio: str | None = None
    supported_sizes: list[str] = Field(default_factory=list)
    default_size: str | None = None
    supported_resolutions: list[str] = Field(default_factory=list)
    default_resolution: str | None = None
    size_map: dict[str, dict[str, str]] = Field(default_factory=dict)
    supports_n: bool = True
    timeout: int = 300
    estimated_cost_mode: str = "request"
    estimated_cost_usd: float = 0.0
    role: str = "draft"


class RoutingRule(BaseModel):
    primary: str
    fallback: list[str] = Field(default_factory=list)


class CandidateInfo(BaseModel):
    index: int
    filename: str
    path: Path
    status: TaskStatus = TaskStatus.PENDING
    estimated_cost_usd: float = 0.0


class TaskManifest(BaseModel):
    task_id: str
    status: TaskStatus
    model: str | None = None
    provider: str | None = None
    candidates: list[CandidateInfo] = Field(default_factory=list)
    accepted_candidate: int | None = None
    estimated_cost_usd: float = 0.0
    actual_cost_usd: float | None = None
    error: str | None = None
    prompt_hash: str | None = None
    prompt_template: str | None = None
    prompt_version: str | None = None


class BuildManifest(BaseModel):
    sku: str
    platform: str
    created_at: str
    tasks: list[TaskManifest] = Field(default_factory=list)
    total_estimated_cost_usd: float = 0.0
    total_actual_cost_usd: float = 0.0

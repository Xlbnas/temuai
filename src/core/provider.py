"""Abstract ImageProvider interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.models import ModelConfig


@dataclass
class GenerateRequest:
    prompt: str
    sku: str
    task_id: str
    platform: str
    aspect_ratio: str | None = None
    size: str | None = None
    n: int = 1
    negative_prompt: str | None = None


@dataclass
class EditRequest:
    prompt: str
    sku: str
    task_id: str
    platform: str
    reference_images: list[Path]
    aspect_ratio: str | None = None
    size: str | None = None
    n: int = 1
    mask: Path | None = None


@dataclass
class GeneratedImage:
    path: Path
    format: str
    width: int
    height: int
    request_id: str | None = None
    actual_cost_usd: float | None = None
    usage: dict[str, Any] | None = None


@dataclass
class ProviderResult:
    images: list[GeneratedImage]
    provider: str
    model: str
    request_id: str | None = None
    duration_seconds: float = 0.0
    estimated_cost_usd: float = 0.0
    actual_cost_usd: float | None = None
    raw_response: Any = None


class ImageProvider(ABC):
    """Abstract interface for AI image providers."""

    name: str = "abstract"

    def __init__(self, model_config: ModelConfig, api_key: str, base_url: str) -> None:
        self.model_config = model_config
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    @abstractmethod
    def capabilities(self) -> dict[str, bool]:
        """Return supported capabilities."""
        raise NotImplementedError

    def supports(self, capability: str) -> bool:
        return self.capabilities().get(capability, False)

    @abstractmethod
    def generate(self, request: GenerateRequest, output_dir: Path) -> ProviderResult:
        """Generate images from text prompt."""
        raise NotImplementedError

    @abstractmethod
    def edit(self, request: EditRequest, output_dir: Path) -> ProviderResult:
        """Edit images based on reference images and prompt."""
        raise NotImplementedError

    def inpaint(self, request: EditRequest, output_dir: Path) -> ProviderResult:
        """Optional inpaint capability."""
        raise NotImplementedError(f"{self.name} does not support inpaint")

    def upscale(self, image_path: Path, output_dir: Path) -> ProviderResult:
        """Optional upscale capability."""
        raise NotImplementedError(f"{self.name} does not support upscale")

    def validate_request(self, request: GenerateRequest | EditRequest) -> None:
        """Validate request against model capabilities before calling API."""
        if isinstance(request, GenerateRequest) and not self.supports("text_to_image"):
            raise ValueError(f"{self.name}:{self.model_config.model} does not support text_to_image")
        if isinstance(request, EditRequest):
            if not self.supports("image_edit"):
                raise ValueError(f"{self.name}:{self.model_config.model} does not support image_edit")
            if len(request.reference_images) > 1 and not self.supports("multi_image"):
                raise ValueError(f"{self.name}:{self.model_config.model} does not support multi_image")
            if request.mask and not self.supports("mask"):
                raise ValueError(f"{self.name}:{self.model_config.model} does not support mask")

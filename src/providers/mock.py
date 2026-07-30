"""Mock provider for tests and dry-runs. Generates deterministic placeholder images."""
from __future__ import annotations

import time
import uuid
from pathlib import Path

from src.core.models import ModelConfig
from src.core.provider import EditRequest, GeneratedImage, GenerateRequest, ImageProvider, ProviderResult
from src.utils.image import create_placeholder_image, validate_saved_image
from src.utils.paths import safe_filename


class MockProvider(ImageProvider):
    """Mock provider that never calls real APIs."""

    name = "mock"

    def __init__(self, model_config: ModelConfig, api_key: str = "mock", base_url: str = "") -> None:
        super().__init__(model_config, api_key, base_url)

    def capabilities(self) -> dict[str, bool]:
        caps = self.model_config.capabilities
        return {
            "text_to_image": caps.text_to_image,
            "image_edit": caps.image_edit,
            "multi_image": caps.multi_image,
            "exact_size": caps.exact_size,
            "mask": caps.mask,
            "inpaint": caps.inpaint,
            "upscale": caps.upscale,
        }

    def _make_placeholder(
        self,
        request: GenerateRequest | EditRequest,
        output_dir: Path,
        index: int,
    ) -> GeneratedImage:
        output_dir.mkdir(parents=True, exist_ok=True)
        label = f"{request.sku}\n{request.task_id}\nMOCK {index}"
        width, height = self._resolve_size(request)
        filename = f"candidate_{index:03d}.png"
        dest = output_dir / filename
        create_placeholder_image(dest, width, height, label)
        if not validate_saved_image(dest):
            raise RuntimeError(f"Mock failed to create valid image at {dest}")
        return GeneratedImage(
            path=dest,
            format="png",
            width=width,
            height=height,
            request_id=f"mock-{uuid.uuid4().hex[:12]}",
            actual_cost_usd=0.0,
        )

    def _resolve_size(self, request: GenerateRequest | EditRequest) -> tuple[int, int]:
        size = getattr(request, "size", None) or self.model_config.default_image_size or "1K"
        ratio = getattr(request, "aspect_ratio", None) or self.model_config.default_aspect_ratio or "3:4"
        if ratio == "3:4":
            if size == "2K":
                return (1500, 2000)
            return (750, 1000)
        if ratio == "1:1":
            if size == "2K":
                return (1500, 1500)
            return (1000, 1000)
        return (750, 1000)

    def generate(self, request: GenerateRequest, output_dir: Path) -> ProviderResult:
        self.validate_request(request)
        start = time.time()
        images = [
            self._make_placeholder(request, output_dir, i + 1)
            for i in range(request.n)
        ]
        return ProviderResult(
            images=images,
            provider=self.name,
            model=self.model_config.model,
            request_id=f"mock-{uuid.uuid4().hex[:12]}",
            duration_seconds=time.time() - start,
            estimated_cost_usd=0.0,
            actual_cost_usd=0.0,
        )

    def edit(self, request: EditRequest, output_dir: Path) -> ProviderResult:
        self.validate_request(request)
        start = time.time()
        images = [
            self._make_placeholder(request, output_dir, i + 1)
            for i in range(request.n)
        ]
        return ProviderResult(
            images=images,
            provider=self.name,
            model=self.model_config.model,
            request_id=f"mock-{uuid.uuid4().hex[:12]}",
            duration_seconds=time.time() - start,
            estimated_cost_usd=0.0,
            actual_cost_usd=0.0,
        )

    def inpaint(self, request: EditRequest, output_dir: Path) -> ProviderResult:
        return self.edit(request, output_dir)

    def upscale(self, image_path: Path, output_dir: Path) -> ProviderResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        dest = output_dir / f"upscaled_{safe_filename(image_path.stem)}.png"
        create_placeholder_image(dest, 1500, 2000, "UPSCALED")
        return ProviderResult(
            images=[GeneratedImage(path=dest, format="png", width=1500, height=2000)],
            provider=self.name,
            model=self.model_config.model,
            request_id=f"mock-{uuid.uuid4().hex[:12]}",
            duration_seconds=0.0,
            estimated_cost_usd=0.0,
            actual_cost_usd=0.0,
        )

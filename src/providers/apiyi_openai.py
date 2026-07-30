"""APIYI gateway provider for OpenAI Images API models (gpt-image-2, etc.)."""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from src.core.models import ModelConfig
from src.core.provider import EditRequest, GeneratedImage, GenerateRequest, ImageProvider, ProviderResult
from src.utils.image import (
    detect_image_format_from_bytes,
    download_image,
    save_base64_image,
    validate_saved_image,
)
from src.utils.paths import safe_filename
from src.utils.secrets import mask_message
from src.utils.size import is_resolution_token, resolve_image_size


class ApiYiOpenAIImageProvider(ImageProvider):
    """Provider for APIYI OpenAI-compatible image endpoints."""

    name = "apiyi_openai"

    def capabilities(self) -> dict[str, bool]:
        caps = self.model_config.capabilities
        return {
            "text_to_image": caps.text_to_image,
            "image_edit": caps.image_edit,
            "multi_image": caps.multi_image,
            "exact_size": getattr(caps, "exact_size", False),
            "mask": getattr(caps, "mask", False),
            "inpaint": False,
            "upscale": False,
        }

    def _build_generate_payload(self, request: GenerateRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model_config.model,
            "prompt": request.prompt,
        }
        # n is only sent when the model supports it (gpt-image-2-vip does NOT:
        # APIYI bills n but still returns 1 image). Multi-image counts are
        # executed as separate calls by generate()/edit() instead.
        if self.model_config.supports_n:
            payload["n"] = request.n
        size = self._gate_size(request.size, request.aspect_ratio)
        if size:
            payload["size"] = size
        # Never send "quality" — not supported by the APIYI gpt-image-2 endpoints.
        return payload

    def _build_edit_payload(self, request: EditRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model_config.model,
            "prompt": request.prompt,
        }
        if self.model_config.supports_n:
            payload["n"] = request.n
        size = self._gate_size(request.size, request.aspect_ratio)
        if size:
            payload["size"] = size
        return payload

    def _gate_size(self, size: str | None, aspect_ratio: str | None = None) -> str | None:
        """Capability gating for size against the model's supported size table.

        ``size`` may be an exact "WxH" string or a resolution token
        ("1K"/"2K"/"4K"). Returns the API-ready size string, or None when the
        model does not support exact sizes. Raises ValueError for sizes
        outside the model's supported table.
        """
        if not self.supports("exact_size"):
            return None
        supported = list(self.model_config.supported_sizes)
        if size and "x" in size:
            # Explicit exact size: must be in the supported table
            if supported and size not in supported:
                raise ValueError(
                    f"Size '{size}' is not supported by {self.model_config.model}. "
                    f"Supported: {', '.join(supported)}"
                )
            return size
        resolution = size if is_resolution_token(size) else None
        return resolve_image_size(self.model_config, aspect_ratio, resolution)

    def _exponential_backoff(self, attempt: int) -> float:
        return [2, 5, 10][min(attempt, 2)]

    def _is_retryable(self, status: int) -> bool:
        if status == 429:
            return True
        if status >= 500:
            return True
        return False

    def _call_post_json(self, endpoint: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with httpx.Client(timeout=timeout) as client:
                    response = client.post(url, headers=headers, json=payload)
                    response.raise_for_status()
                    return response.json()
            except httpx.TimeoutException as e:
                last_error = e
                time.sleep(self._exponential_backoff(attempt))
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                body_text = e.response.text
                if self._is_retryable(status):
                    last_error = e
                    time.sleep(self._exponential_backoff(attempt))
                else:
                    raise RuntimeError(
                        f"API error {status} (non-retryable): {mask_message(body_text)}"
                    ) from e
            except Exception as e:
                raise RuntimeError(mask_message(str(e))) from e
        raise RuntimeError(f"API call failed after retries: {mask_message(str(last_error))}")

    def _call_post_multipart(
        self, endpoint: str, data: dict[str, Any], files: dict[str, Any], timeout: int
    ) -> dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with httpx.Client(timeout=timeout) as client:
                    response = client.post(url, headers=headers, data=data, files=files)
                    response.raise_for_status()
                    return response.json()
            except httpx.TimeoutException as e:
                last_error = e
                time.sleep(self._exponential_backoff(attempt))
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                body_text = e.response.text
                if self._is_retryable(status):
                    last_error = e
                    time.sleep(self._exponential_backoff(attempt))
                else:
                    raise RuntimeError(
                        f"API error {status} (non-retryable): {mask_message(body_text)}"
                    ) from e
            except Exception as e:
                raise RuntimeError(mask_message(str(e))) from e
        raise RuntimeError(f"API call failed after retries: {mask_message(str(last_error))}")

    def _save_remote_image(self, url: str, output_dir: Path, index: int) -> GeneratedImage:
        output_dir.mkdir(parents=True, exist_ok=True)
        dest = output_dir / f"candidate_{index + 1:03d}.png"
        with httpx.Client(timeout=120) as client:
            download_image(url, client, dest)
        if not validate_saved_image(dest):
            raise RuntimeError(f"Downloaded image failed validation: {dest}")
        from PIL import Image

        with Image.open(dest) as img:
            width, height = img.size
        return GeneratedImage(path=dest, format="png", width=width, height=height)

    def _save_b64_image(self, b64: str, output_dir: Path, index: int) -> GeneratedImage:
        output_dir.mkdir(parents=True, exist_ok=True)
        dest = output_dir / f"candidate_{index + 1:03d}.png"
        # APIYI may return pure base64 or a data URI depending on channel version
        if b64.startswith("data:"):
            b64 = b64.split(",", 1)[-1]
        save_base64_image(b64, dest)
        if not validate_saved_image(dest):
            raise RuntimeError(f"Base64 image failed validation: {dest}")
        from PIL import Image

        with Image.open(dest) as img:
            width, height = img.size
        return GeneratedImage(path=dest, format="png", width=width, height=height)

    def _parse_result(
        self, response_data: dict[str, Any], output_dir: Path, start_index: int = 0
    ) -> list[GeneratedImage]:
        images: list[GeneratedImage] = []
        for idx, item in enumerate(response_data.get("data", [])):
            if "url" in item:
                images.append(self._save_remote_image(item["url"], output_dir, start_index + idx))
            elif "b64_json" in item:
                images.append(self._save_b64_image(item["b64_json"], output_dir, start_index + idx))
            else:
                raise ValueError("API response item contains neither url nor b64_json")
        if not images:
            raise ValueError("API response contains no image data")
        return images

    def generate(self, request: GenerateRequest, output_dir: Path) -> ProviderResult:
        start = time.time()
        self.validate_request(request)
        # Models without n support (gpt-image-2-vip): one call per image.
        calls = 1 if self.model_config.supports_n else request.n
        images: list[GeneratedImage] = []
        response_data: dict[str, Any] = {}
        for _ in range(calls):
            payload = self._build_generate_payload(request)
            response_data = self._call_post_json(
                "/images/generations", payload, self.model_config.timeout
            )
            images.extend(self._parse_result(response_data, output_dir, start_index=len(images)))
        return ProviderResult(
            images=images,
            provider=self.name,
            model=self.model_config.model,
            request_id=response_data.get("id") or f"apiyi-openai-{uuid.uuid4().hex[:12]}",
            duration_seconds=time.time() - start,
            estimated_cost_usd=self.model_config.estimated_cost_usd * request.n,
        )

    def edit(self, request: EditRequest, output_dir: Path) -> ProviderResult:
        start = time.time()
        self.validate_request(request)
        files: dict[str, Any] = {}
        for i, img_path in enumerate(request.reference_images[: min(4, len(request.reference_images))]):
            files[f"image[{i}]"] = (safe_filename(img_path.name), img_path.read_bytes(), "image/png")
        if request.mask:
            files["mask"] = (safe_filename(request.mask.name), request.mask.read_bytes(), "image/png")
        calls = 1 if self.model_config.supports_n else request.n
        images: list[GeneratedImage] = []
        response_data: dict[str, Any] = {}
        for _ in range(calls):
            payload = self._build_edit_payload(request)
            response_data = self._call_post_multipart(
                "/images/edits", payload, files, self.model_config.timeout
            )
            images.extend(self._parse_result(response_data, output_dir, start_index=len(images)))
        return ProviderResult(
            images=images,
            provider=self.name,
            model=self.model_config.model,
            request_id=response_data.get("id") or f"apiyi-openai-{uuid.uuid4().hex[:12]}",
            duration_seconds=time.time() - start,
            estimated_cost_usd=self.model_config.estimated_cost_usd * request.n,
        )

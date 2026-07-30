"""APIYI gateway provider for Gemini / Nano Banana image models."""
from __future__ import annotations

import base64
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from src.core.models import ModelConfig
from src.core.provider import EditRequest, GeneratedImage, GenerateRequest, ImageProvider, ProviderResult
from src.utils.image import detect_image_format_from_bytes, save_base64_image, validate_saved_image
from src.utils.secrets import mask_message


class ApiYiGeminiImageProvider(ImageProvider):
    """Provider for APIYI Gemini / Nano Banana image models using generateContent API."""

    name = "apiyi_gemini"

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

    def _build_request_body(
        self,
        prompt: str,
        reference_images: list[Path] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
    ) -> dict[str, Any]:
        parts: list[dict[str, Any]] = []
        if reference_images:
            for img_path in reference_images:
                data = img_path.read_bytes()
                # mimeType must reflect the real image format
                mime = f"image/{detect_image_format_from_bytes(data)}"
                # Pure base64 payload, no "data:image/...;base64," prefix
                b64 = base64.b64encode(data).decode("utf-8")
                parts.append({"inlineData": {"mimeType": mime, "data": b64}})
        parts.append({"text": prompt})
        generation_config: dict[str, Any] = {
            "responseModalities": ["IMAGE"],
        }
        image_config: dict[str, Any] = {}
        if aspect_ratio:
            image_config["aspectRatio"] = aspect_ratio
        if image_size:
            image_config["imageSize"] = image_size
        if image_config:
            generation_config["imageConfig"] = image_config
        return {
            "contents": [{"parts": parts}],
            "generationConfig": generation_config,
        }

    def _extract_image_data(self, response_data: dict[str, Any]) -> list[tuple[str, bytes]]:
        """Extract (format, bytes) image data from Gemini response."""
        results: list[tuple[str, bytes]] = []
        text_parts: list[str] = []
        for candidate in response_data.get("candidates", []):
            parts = candidate.get("content", {}).get("parts", [])
            for part in parts:
                if "inlineData" in part or "inline_data" in part:
                    inline = part.get("inlineData") or part.get("inline_data")
                    b64 = inline.get("data")
                    mime = inline.get("mimeType") or inline.get("mime_type", "image/png")
                    fmt = mime.split("/")[-1]
                    if b64:
                        results.append((fmt, base64.b64decode(b64)))
                elif "text" in part:
                    text_parts.append(part["text"])
        if not results and text_parts:
            raise ValueError(f"API returned text but no image: {' '.join(text_parts)}")
        return results

    def _save_images(self, image_data: list[tuple[str, bytes]], output_dir: Path) -> list[GeneratedImage]:
        output_dir.mkdir(parents=True, exist_ok=True)
        saved: list[GeneratedImage] = []
        for idx, (fmt, raw) in enumerate(image_data):
            dest = output_dir / f"candidate_{idx + 1:03d}.{fmt}"
            with open(dest, "wb") as f:
                f.write(raw)
            if not validate_saved_image(dest):
                raise RuntimeError(f"Generated image failed validation: {dest}")
            from PIL import Image

            with Image.open(dest) as img:
                width, height = img.size
            saved.append(
                GeneratedImage(
                    path=dest,
                    format=fmt,
                    width=width,
                    height=height,
                )
            )
        return saved

    def _resolve_image_size(self, request: GenerateRequest | EditRequest) -> str | None:
        """imageSize for imageConfig: request override -> model default.

        Models with ``supported_resolutions`` (e.g. Nano Banana 2 Lite = 1K
        only) reject anything outside that list before it reaches the API.
        """
        size = request.size or self.model_config.default_image_size or self.model_config.image_size
        allowed = list(self.model_config.supported_resolutions)
        if allowed and size and size not in allowed:
            raise ValueError(
                f"Resolution '{size}' is not supported by {self.model_config.model}. "
                f"Supported: {', '.join(allowed)}"
            )
        return size

    def _exponential_backoff(self, attempt: int) -> float:
        return [2, 5, 10][min(attempt, 2)]

    def _is_retryable(self, status: int, body: str) -> bool:
        if status == 429:
            return True
        if status >= 500:
            return True
        # Don't retry auth/balance/param errors blindly
        if status in (401, 402, 403, 400):
            return False
        return False

    def _call_api(self, endpoint: str, body: dict[str, Any], timeout: int) -> dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with httpx.Client(timeout=timeout) as client:
                    response = client.post(url, headers=headers, json=body)
                    response.raise_for_status()
                    return response.json()
            except httpx.TimeoutException as e:
                last_error = e
                time.sleep(self._exponential_backoff(attempt))
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                body_text = e.response.text
                if self._is_retryable(status, body_text):
                    last_error = e
                    time.sleep(self._exponential_backoff(attempt))
                else:
                    raise RuntimeError(
                        f"API error {status} (non-retryable): {mask_message(body_text)}"
                    ) from e
            except Exception as e:
                raise RuntimeError(mask_message(str(e))) from e
        raise RuntimeError(f"API call failed after retries: {mask_message(str(last_error))}")

    def generate(self, request: GenerateRequest, output_dir: Path) -> ProviderResult:
        start = time.time()
        self.validate_request(request)
        body = self._build_request_body(
            request.prompt,
            aspect_ratio=request.aspect_ratio,
            image_size=self._resolve_image_size(request),
        )
        endpoint = f"/v1beta/models/{self.model_config.model}:generateContent"
        response_data = self._call_api(endpoint, body, self.model_config.timeout)
        image_data = self._extract_image_data(response_data)
        saved = self._save_images(image_data, output_dir)
        duration = time.time() - start
        return ProviderResult(
            images=saved,
            provider=self.name,
            model=self.model_config.model,
            request_id=f"apiyi-gemini-{uuid.uuid4().hex[:12]}",
            duration_seconds=duration,
            estimated_cost_usd=self.model_config.estimated_cost_usd * request.n,
        )

    def edit(self, request: EditRequest, output_dir: Path) -> ProviderResult:
        start = time.time()
        self.validate_request(request)
        body = self._build_request_body(
            request.prompt,
            request.reference_images,
            aspect_ratio=request.aspect_ratio,
            image_size=self._resolve_image_size(request),
        )
        endpoint = f"/v1beta/models/{self.model_config.model}:generateContent"
        response_data = self._call_api(endpoint, body, self.model_config.timeout)
        image_data = self._extract_image_data(response_data)
        saved = self._save_images(image_data, output_dir)
        return ProviderResult(
            images=saved,
            provider=self.name,
            model=self.model_config.model,
            request_id=f"apiyi-gemini-{uuid.uuid4().hex[:12]}",
            duration_seconds=time.time() - start,
            estimated_cost_usd=self.model_config.estimated_cost_usd * request.n,
        )

"""Verified, synchronous APIYI image adapter used by Product Image Studio.

The wire shapes in this module are deliberately limited to the two APIYI
contracts already exercised by the legacy providers in this repository:

* Gemini ``/v1beta/models/{model}:generateContent`` with ``inlineData`` parts.
* OpenAI-compatible ``/v1/images/edits`` multipart requests and ``data`` results.

Neither verified contract exposes an asynchronous task-status endpoint.  A
successful submit is therefore a terminal status; an interrupted submit is
never replayed and instead requires reconciliation.
"""
from __future__ import annotations

import base64
import binascii
import ipaddress
import os
import socket
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.core.config import AppConfig
from src.core.models import ModelConfig
from src.studio.models import ProviderCapability
from src.utils.image import detect_image_format_from_bytes
from src.utils.secrets import mask_message
from src.utils.size import resolve_image_size


class APIYIProviderErrorCode(str, Enum):
    NOT_CONFIGURED = "provider_not_configured"
    AUTHENTICATION_FAILED = "authentication_failed"
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_MODEL = "unsupported_model"
    UNSUPPORTED_REFERENCES = "unsupported_references"
    RATE_LIMITED = "rate_limited"
    TIMEOUT_BEFORE_SUBMISSION = "timeout_before_submission"
    TIMEOUT_AFTER_SUBMISSION = "timeout_after_submission"
    PROVIDER_FAILED = "provider_failed"
    MALFORMED_RESPONSE = "malformed_response"
    UNSAFE_RESULT = "unsafe_result"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class APIYIProviderError(RuntimeError):
    """Safe typed provider error; never contains request bodies or credentials."""

    def __init__(self, code: APIYIProviderErrorCode, message: str) -> None:
        self.code = code
        self.safe_message = message[:300]
        super().__init__(self.safe_message)


class APIYIReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    asset_id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: str
    path: Path


class APIYIGenerationRequest(BaseModel):
    """Studio request model, compiled only from persisted inputs.

    ``references`` retain roles even when the provider receives a flat array.
    Paths are local implementation details and never serialized to a provider.
    """

    model_config = ConfigDict(extra="forbid")
    provider: str = "apiyi"
    model: str
    prompt: str = Field(min_length=1)
    negative_prompt: str = ""
    references: list[APIYIReference] = Field(default_factory=list)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    aspect_ratio: str
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")


class APIYIGenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str | None = None
    b64_json: str | None = None

    @field_validator("url")
    @classmethod
    def only_https_urls(cls, value: str | None) -> str | None:
        if value is not None and urlparse(value).scheme != "https":
            raise ValueError("Provider result URL must use HTTPS")
        return value


class APIYIGenerationSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_request_id: str | None = None
    status: str
    results: list[APIYIGenerationResult] = Field(default_factory=list)
    actual_cost_usd: float | None = Field(default=None, ge=0)


class APIYIGenerationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_request_id: str
    status: str
    results: list[APIYIGenerationResult] = Field(default_factory=list)
    actual_cost_usd: float | None = Field(default=None, ge=0)


def _model_config(config: AppConfig, model: str) -> ModelConfig:
    for name, raw in config.models.get("models", {}).items():
        parsed = ModelConfig(name=name, **raw)
        if model in {name, parsed.model}:
            if parsed.provider not in {"apiyi_gemini", "apiyi_openai"}:
                break
            return parsed
    raise APIYIProviderError(APIYIProviderErrorCode.UNSUPPORTED_MODEL, "Unsupported APIYI model")


def _base_url(provider: str) -> str:
    if provider == "apiyi_gemini":
        return os.getenv("APIYI_GEMINI_BASE_URL", "https://api.apiyi.com")
    return os.getenv("APIYI_OPENAI_BASE_URL", "https://api.apiyi.com/v1")


class APIYIClient:
    """One-shot APIYI HTTP client.  It intentionally has no retry loop."""

    MAX_RESULT_BYTES = 25 * 1024 * 1024
    MAX_REDIRECTS = 2

    def __init__(self, api_key: str, base_url: str, timeout: int) -> None:
        if not api_key:
            raise APIYIProviderError(APIYIProviderErrorCode.NOT_CONFIGURED, "APIYI is not configured")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(
        self, method: str, endpoint: str, *, json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None, files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if json is not None:
            headers["Content-Type"] = "application/json"
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
                response = client.request(
                    method, f"{self.base_url}{endpoint}", headers=headers, json=json, data=data, files=files
                )
        except httpx.TimeoutException as exc:
            raise APIYIProviderError(
                APIYIProviderErrorCode.TIMEOUT_AFTER_SUBMISSION,
                "Provider request timed out; reconciliation is required before any resend.",
            ) from exc
        except httpx.HTTPError as exc:
            raise APIYIProviderError(APIYIProviderErrorCode.PROVIDER_FAILED, "Provider connection failed") from exc
        if response.status_code == 401 or response.status_code == 403:
            raise APIYIProviderError(APIYIProviderErrorCode.AUTHENTICATION_FAILED, "Provider authentication failed")
        if response.status_code == 429:
            raise APIYIProviderError(APIYIProviderErrorCode.RATE_LIMITED, "Provider rate limit reached")
        if response.status_code >= 400:
            raise APIYIProviderError(APIYIProviderErrorCode.INVALID_REQUEST, "Provider rejected the request")
        try:
            data_value = response.json()
        except ValueError as exc:
            raise APIYIProviderError(APIYIProviderErrorCode.MALFORMED_RESPONSE, "Provider returned invalid JSON") from exc
        if not isinstance(data_value, dict):
            raise APIYIProviderError(APIYIProviderErrorCode.MALFORMED_RESPONSE, "Provider returned an invalid response")
        return data_value

    @staticmethod
    def _result_url_is_safe(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise APIYIProviderError(APIYIProviderErrorCode.UNSAFE_RESULT, "Provider result URL is unsafe")
        try:
            addresses = socket.getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM)
            for item in addresses:
                address = ipaddress.ip_address(item[4][0])
                if not address.is_global:
                    raise APIYIProviderError(APIYIProviderErrorCode.UNSAFE_RESULT, "Provider result URL is unsafe")
        except socket.gaierror as exc:
            raise APIYIProviderError(APIYIProviderErrorCode.UNSAFE_RESULT, "Provider result host could not be resolved") from exc

    def download_result(self, result: APIYIGenerationResult) -> bytes:
        if result.b64_json is not None:
            raw = result.b64_json.split(",", 1)[-1] if result.b64_json.startswith("data:") else result.b64_json
            if len(raw) > self.MAX_RESULT_BYTES * 2:
                raise APIYIProviderError(APIYIProviderErrorCode.UNSAFE_RESULT, "Provider image is too large")
            try:
                content = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise APIYIProviderError(APIYIProviderErrorCode.MALFORMED_RESPONSE, "Provider image is invalid") from exc
            if len(content) > self.MAX_RESULT_BYTES:
                raise APIYIProviderError(APIYIProviderErrorCode.UNSAFE_RESULT, "Provider image is too large")
            return content
        if result.url is None:
            raise APIYIProviderError(APIYIProviderErrorCode.MALFORMED_RESPONSE, "Provider returned no image result")
        url = result.url
        for _ in range(self.MAX_REDIRECTS + 1):
            self._result_url_is_safe(url)
            try:
                with httpx.Client(timeout=min(self.timeout, 120), follow_redirects=False) as client:
                    response = client.get(url)
            except httpx.HTTPError as exc:
                raise APIYIProviderError(APIYIProviderErrorCode.UNSAFE_RESULT, "Provider image download failed") from exc
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    break
                url = str(response.next_request.url) if response.next_request else location
                continue
            if response.status_code != 200 or len(response.content) > self.MAX_RESULT_BYTES:
                break
            return response.content
        raise APIYIProviderError(APIYIProviderErrorCode.UNSAFE_RESULT, "Provider image download was rejected")


class APIYIImageGenerationProvider:
    """Studio adapter for the repository-verified synchronous APIYI contracts."""

    name = "apiyi"

    def __init__(self, config: AppConfig, model: str, api_key: str) -> None:
        self.config = config
        self.model_config = _model_config(config, model)
        self.client = APIYIClient(api_key, _base_url(self.model_config.provider), self.model_config.timeout)

    @staticmethod
    def capability_for(config: AppConfig, model: str) -> ProviderCapability:
        parsed = _model_config(config, model)
        raw = config.get_model_config(parsed.name)
        # Existing repository values are estimates, not an APIYI price contract.
        return ProviderCapability(
            provider="apiyi", model=parsed.model,
            supports_image_references=parsed.capabilities.image_edit,
            supports_multiple_references=parsed.capabilities.multi_image,
            max_reference_images=4 if parsed.capabilities.multi_image else 1,
            supports_edit=parsed.capabilities.image_edit,
            supports_mask=parsed.capabilities.mask,
            supports_negative_prompt=False,
            supported_aspect_ratios=list(parsed.size_map.keys()) or ([parsed.default_aspect_ratio] if parsed.default_aspect_ratio else []),
            supported_output_sizes=list(parsed.supported_sizes or parsed.supported_resolutions),
            synchronous=True,
            pricing_version=raw.get("pricing_version"),
            estimated_price_usd=parsed.estimated_cost_usd,
            pricing_status=raw.get("pricing_status", "unknown"),
            pricing_source=raw.get("pricing_source", "config/models.yaml estimated_cost_usd (not a verified price contract)"),
        )

    def estimate_cost(self, request: APIYIGenerationRequest) -> float | None:
        capability = self.capability_for(self.config, self.model_config.model)
        return capability.estimated_price_usd if capability.pricing_status != "unknown" else None

    def compile_request(self, request: APIYIGenerationRequest) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
        references = request.references
        if references and not self.model_config.capabilities.image_edit:
            raise APIYIProviderError(APIYIProviderErrorCode.UNSUPPORTED_REFERENCES, "Model does not support references")
        if len(references) > 4:
            raise APIYIProviderError(APIYIProviderErrorCode.UNSUPPORTED_REFERENCES, "Too many references for model")
        if self.model_config.provider == "apiyi_gemini":
            parts: list[dict[str, Any]] = []
            for reference in references:
                content = reference.path.read_bytes()
                parts.append({"inlineData": {"mimeType": f"image/{detect_image_format_from_bytes(content)}", "data": base64.b64encode(content).decode("ascii")}})
            parts.append({"text": request.prompt})
            image_size = self.model_config.default_image_size
            body: dict[str, Any] = {"contents": [{"parts": parts}], "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": request.aspect_ratio, "imageSize": image_size}}}
            return (f"/v1beta/models/{self.model_config.model}:generateContent", body, None)
        payload: dict[str, Any] = {"model": self.model_config.model, "prompt": request.prompt}
        size = self._openai_size(request)
        if size:
            payload["size"] = size
        files: dict[str, Any] = {}
        for index, reference in enumerate(references):
            files[f"image[{index}]"] = (f"reference-{index + 1}", reference.path.read_bytes(), reference.mime_type)
        return ("/images/edits" if files else "/images/generations", payload, files or None)

    def _openai_size(self, request: APIYIGenerationRequest) -> str | None:
        if not self.model_config.capabilities.exact_size:
            return None
        requested = f"{request.width}x{request.height}"
        if requested in self.model_config.supported_sizes:
            return requested
        if self.model_config.size_map:
            return resolve_image_size(self.model_config, request.aspect_ratio, self.model_config.default_resolution)
        raise APIYIProviderError(APIYIProviderErrorCode.INVALID_REQUEST, "Requested output size is unsupported")

    def submit(self, request: APIYIGenerationRequest) -> APIYIGenerationSubmission:
        endpoint, payload, files = self.compile_request(request)
        response = self.client._request("POST", endpoint, json=None if files else payload, data=payload if files else None, files=files)
        if self.model_config.provider == "apiyi_gemini":
            results: list[APIYIGenerationResult] = []
            for candidate in response.get("candidates", []):
                for part in candidate.get("content", {}).get("parts", []):
                    inline = part.get("inlineData") or part.get("inline_data")
                    if isinstance(inline, dict) and isinstance(inline.get("data"), str):
                        results.append(APIYIGenerationResult(b64_json=inline["data"]))
            request_id = response.get("id")
        else:
            data = response.get("data")
            if not isinstance(data, list):
                raise APIYIProviderError(APIYIProviderErrorCode.MALFORMED_RESPONSE, "Provider response has no image data")
            results = [APIYIGenerationResult(url=item.get("url"), b64_json=item.get("b64_json")) for item in data if isinstance(item, dict)]
            request_id = response.get("id")
        if not results:
            raise APIYIProviderError(APIYIProviderErrorCode.MALFORMED_RESPONSE, "Provider response has no image result")
        return APIYIGenerationSubmission(provider_request_id=request_id, status="succeeded", results=results)

    def get_generation_status(self, provider_request_id: str) -> APIYIGenerationStatus:
        # No polling endpoint has been verified for either supported sync contract.
        raise APIYIProviderError(
            APIYIProviderErrorCode.RECONCILIATION_REQUIRED,
            "No verified APIYI status endpoint exists for this synchronous request; reconcile manually.",
        )

    def reconcile(self, provider_request_id: str) -> APIYIGenerationStatus:
        return self.get_generation_status(provider_request_id)


def safe_provider_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, APIYIProviderError):
        return exc.code.value, exc.safe_message
    return (APIYIProviderErrorCode.PROVIDER_FAILED.value, mask_message(str(exc))[:300] or "Provider request failed")

"""Offline M2B fixtures for the repository-verified APIYI Studio contracts."""
from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from src.core.config import AppConfig
from src.studio.apiyi import (
    APIYIGenerationRequest,
    APIYIImageGenerationProvider,
    APIYIProviderError,
    APIYIProviderErrorCode,
    APIYIReference,
)
from src.studio.models import BudgetPolicy, GenerationStatus
from src.studio.service import StudioService
from tests.test_studio_m2 import _confirmed_plan


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (16, 24), "orange").save(output, format="PNG")
    return output.getvalue()


def _request(tmp_path: Path) -> APIYIGenerationRequest:
    image = tmp_path / "clean-product.png"
    image.write_bytes(_png_bytes())
    return APIYIGenerationRequest(
        model="nano_banana_2", prompt="clean product image", width=1500, height=2000,
        aspect_ratio="3:4", idempotency_key="a" * 64,
        references=[APIYIReference(
            role="product_reference_clean", asset_id="product", sha256="b" * 64,
            mime_type="image/png", path=image,
        )],
    )


def test_gemini_compile_uses_verified_inline_data_shape(temp_config: AppConfig, tmp_path: Path) -> None:
    adapter = APIYIImageGenerationProvider(temp_config, "nano_banana_2", "fixture-key")
    endpoint, body, files = adapter.compile_request(_request(tmp_path))
    assert endpoint.endswith(":generateContent")
    assert files is None
    inline = body["contents"][0]["parts"][0]["inlineData"]
    assert inline["mimeType"] == "image/png"
    assert base64.b64decode(inline["data"]) == _png_bytes()
    assert "negative_prompt" not in body


def test_openai_compile_uses_verified_multipart_edit_shape(temp_config: AppConfig, tmp_path: Path) -> None:
    adapter = APIYIImageGenerationProvider(temp_config, "gpt_image_2_vip", "fixture-key")
    endpoint, payload, files = adapter.compile_request(_request(tmp_path).model_copy(update={"model": "gpt_image_2_vip"}))
    assert endpoint == "/images/edits"
    assert payload["model"] == "gpt-image-2-vip"
    assert payload["size"] == "1536x2048"
    assert files and "image[0]" in files


def test_submit_fixture_persists_real_response_id_shape(
    temp_config: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = APIYIImageGenerationProvider(temp_config, "nano_banana_2", "fixture-key")
    encoded = base64.b64encode(_png_bytes()).decode("ascii")
    monkeypatch.setattr(adapter.client, "_request", lambda *args, **kwargs: {
        "id": "gemini-request-123", "candidates": [{"content": {"parts": [{"inlineData": {"data": encoded}}]}}],
    })
    result = adapter.submit(_request(tmp_path))
    assert result.provider_request_id == "gemini-request-123"
    assert result.status == "succeeded"
    assert adapter.client.download_result(result.results[0]) == _png_bytes()


def test_sync_status_lookup_requires_manual_reconciliation(temp_config: AppConfig) -> None:
    adapter = APIYIImageGenerationProvider(temp_config, "nano_banana_2", "fixture-key")
    with pytest.raises(APIYIProviderError) as captured:
        adapter.get_generation_status("known-request")
    assert captured.value.code == APIYIProviderErrorCode.RECONCILIATION_REQUIRED


def test_live_gate_rejects_unknown_pricing_even_with_key(
    temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    monkeypatch.setenv("LIVE_GENERATION_ENABLED", "true")
    monkeypatch.setenv("APIYI_API_KEY", "fixture-key")
    with pytest.raises(ValueError, match="pricing_unknown"):
        service.create_generation_job(
            project.id, plan.id, mode="live", provider="apiyi", model="nano_banana_2",
            shot_id=plan.shots[0].id, paid_confirmation=True, confirmed_by="tester",
            budget_policy=BudgetPolicy(project_limit=1, job_limit=1, shot_limit=1),
        )


def test_reconcile_marks_persisted_attempt_without_resend(temp_config: AppConfig) -> None:
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    job = service.create_generation_job(project.id, plan.id, shot_id=plan.shots[0].id)
    with service.store.lock(project.id):
        record = service.get_record(project.id)
        attempt = next(item for item in record.generation_attempts if item.job_id == job.id)
        attempt.status = GenerationStatus.UNKNOWN
        service.store.save(record)
    reconciled = service.reconcile_attempt(project.id, attempt.id)
    assert reconciled.status == GenerationStatus.RECONCILE_REQUIRED
    assert reconciled.provider_request_id is None

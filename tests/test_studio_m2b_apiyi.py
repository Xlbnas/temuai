"""Offline M2B fixtures for the repository-verified APIYI Studio contracts."""
from __future__ import annotations

import base64
import socket
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from src.core.config import AppConfig
from src.studio.apiyi import (
    APIYIClient,
    APIYIGenerationRequest,
    APIYIGenerationResult,
    APIYIImageGenerationProvider,
    APIYIProviderError,
    APIYIProviderErrorCode,
    APIYIReference,
    safe_provider_error,
)
from src.studio.models import BudgetPolicy, GenerationStatus
from src.studio.service import StudioService
from tests.test_studio_m2 import _confirmed_plan

REAL_APIYI_HOSTS = {"api.apiyi.com", "b.apiyi.com"}


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch):
    """Hermetic suite: any real APIYI hostname (or unexpected host) fails fast."""
    original_send = httpx.Client.send

    def guarded_send(self, request, *args, **kwargs):
        host = request.url.host
        if host in REAL_APIYI_HOSTS:
            raise AssertionError(f"real APIYI network call attempted: {request.url}")
        if host != "testserver":
            raise AssertionError(f"unexpected real network call in tests: {request.url}")
        return original_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", guarded_send)

    def guarded_socket(address, *args, **kwargs):
        raise AssertionError(f"unexpected raw socket connection in tests: {address!r}")

    monkeypatch.setattr(socket, "create_connection", guarded_socket)


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (16, 24), "orange").save(output, format="PNG")
    return output.getvalue()


def _request(tmp_path: Path) -> APIYIGenerationRequest:
    image = tmp_path / "clean-product.png"
    image.write_bytes(_png_bytes())
    return APIYIReference(
        role="product_reference_clean", asset_id="product", sha256="b" * 64,
        mime_type="image/png", path=image,
    )


def _generation_request(tmp_path: Path) -> APIYIGenerationRequest:
    return APIYIGenerationRequest(
        model="nano_banana_2", prompt="clean product image", width=1500, height=2000,
        aspect_ratio="3:4", idempotency_key="a" * 64, references=[_request(tmp_path)],
    )


def _fake_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, json=payload,
        request=httpx.Request("POST", "https://api.apiyi.com/v1beta/models/x:generateContent"),
    )


def _enable_live(temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch, model: str = "nano_banana_2") -> None:
    # The shipped nano_banana_2 pricing contract already provides exact pricing;
    # env switches are the only thing Live still needs here.
    monkeypatch.setenv("LIVE_GENERATION_ENABLED", "true")
    monkeypatch.setenv("APIYI_API_KEY", "fixture-key")


def _force_unknown_pricing(temp_config: AppConfig, model: str = "nano_banana_2") -> None:
    """Pin a model to pricing-unknown regardless of any shipped pricing contract."""
    raw = temp_config.models["models"][model]
    raw.pop("pricing_contract", None)
    raw["pricing_status"] = "unknown"


def _force_exact_pricing(temp_config: AppConfig, model: str = "nano_banana_2") -> None:
    """Restore an exact model with a full validated contract (flat flip is not enough)."""
    raw = temp_config.models["models"][model]
    raw["pricing_status"] = "exact"
    raw["pricing_contract"] = {
        "provider": "apiyi",
        "provider_model_id": "gemini-3.1-flash-image",
        "pricing_status": "exact",
        "pricing_version": "fixture-exact-v1",
        "pricing_source": "fixture official source",
        "source_type": "public_official",
        "effective_at": "2026-03-01",
        "retrieved_at": "2026-08-01T00:00:00Z",
        "currency": "USD",
        "unit": "per_request",
        "amount": 0.055,
        "request_mode": "generation_or_edit",
        "supported_resolutions": ["512px", "1K", "2K", "4K"],
        "supported_aspect_ratios": ["3:4"],
        "supported_quality_levels": [],
        "reference_policy": "price_unchanged",
        "output_count": 1,
        "evidence_digest": "sha256:" + "0" * 64,
    }


def _live_job(service: StudioService, project, plan):
    return service.create_generation_job(
        project.id, plan.id, mode="live", provider="apiyi", model="nano_banana_2",
        shot_id=plan.shots[0].id, paid_confirmation=True, confirmed_by="tester",
        budget_policy=BudgetPolicy(project_limit=1, job_limit=1, shot_limit=1),
    )


def _attempt_of(service: StudioService, project_id: str, job_id: str):
    record = service.get_record(project_id)
    return next(item for item in record.generation_attempts if item.job_id == job_id)


def test_gemini_compile_uses_verified_inline_data_shape(temp_config: AppConfig, tmp_path: Path) -> None:
    adapter = APIYIImageGenerationProvider(temp_config, "nano_banana_2", "fixture-key")
    endpoint, body, files = adapter.compile_request(_generation_request(tmp_path))
    assert endpoint.endswith(":generateContent")
    assert files is None
    inline = body["contents"][0]["parts"][0]["inlineData"]
    assert inline["mimeType"] == "image/png"
    assert base64.b64decode(inline["data"]) == _png_bytes()
    assert "negative_prompt" not in body


def test_openai_compile_uses_verified_multipart_edit_shape(temp_config: AppConfig, tmp_path: Path) -> None:
    adapter = APIYIImageGenerationProvider(temp_config, "gpt_image_2_vip", "fixture-key")
    endpoint, payload, files = adapter.compile_request(
        _generation_request(tmp_path).model_copy(update={"model": "gpt_image_2_vip"})
    )
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
    result = adapter.submit(_generation_request(tmp_path))
    assert result.provider_request_id == "gemini-request-123"
    assert result.status == "succeeded"
    assert adapter.client.download_result(result.results[0]) == _png_bytes()


def test_sync_status_lookup_requires_manual_reconciliation(temp_config: AppConfig) -> None:
    adapter = APIYIImageGenerationProvider(temp_config, "nano_banana_2", "fixture-key")
    with pytest.raises(APIYIProviderError) as captured:
        adapter.get_generation_status("known-request")
    assert captured.value.code == APIYIProviderErrorCode.RECONCILIATION_REQUIRED


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (httpx.ConnectTimeout("connect"), APIYIProviderErrorCode.TIMEOUT_AFTER_SUBMISSION),
        (httpx.ReadTimeout("read"), APIYIProviderErrorCode.TIMEOUT_AFTER_SUBMISSION),
        (httpx.WriteTimeout("write"), APIYIProviderErrorCode.TIMEOUT_AFTER_SUBMISSION),
        (httpx.PoolTimeout("pool"), APIYIProviderErrorCode.TIMEOUT_AFTER_SUBMISSION),
        (httpx.ConnectError("refused"), APIYIProviderErrorCode.PROVIDER_FAILED),
        (httpx.WriteError("broken pipe"), APIYIProviderErrorCode.RECONCILIATION_REQUIRED),
        (httpx.ReadError("reset"), APIYIProviderErrorCode.RECONCILIATION_REQUIRED),
        (httpx.CloseError("closed"), APIYIProviderErrorCode.RECONCILIATION_REQUIRED),
        (httpx.DecodingError("bad gzip"), APIYIProviderErrorCode.MALFORMED_RESPONSE),
    ],
)
def test_transport_error_classification_matrix(
    monkeypatch: pytest.MonkeyPatch, raised: httpx.HTTPError, expected: APIYIProviderErrorCode
) -> None:
    client = APIYIClient("fixture-key", "https://api.apiyi.com", 10)

    def fail(*args, **kwargs):
        raise raised

    monkeypatch.setattr(httpx.Client, "request", fail)
    with pytest.raises(APIYIProviderError) as captured:
        client._request("POST", "/v1beta/models/x:generateContent", json={})
    assert captured.value.code == expected


def test_live_gate_rejects_unknown_pricing_even_with_key(
    temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_unknown_pricing(temp_config)
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


def test_live_gate_matrix_blocks_every_missing_requirement(
    temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    _force_exact_pricing(temp_config)
    policy = BudgetPolicy(project_limit=999, job_limit=999, shot_limit=999)
    base = {
        "mode": "live", "provider": "apiyi", "model": "nano_banana_2", "shot_id": plan.shots[0].id,
        "budget_policy": policy, "paid_confirmation": True, "confirmed_by": "tester",
    }
    # LIVE_GENERATION_ENABLED=false blocks even with key + exact pricing + high budget.
    monkeypatch.setenv("APIYI_API_KEY", "fixture-key")
    monkeypatch.delenv("LIVE_GENERATION_ENABLED", raising=False)
    with pytest.raises(ValueError, match="LIVE_GENERATION_ENABLED=false"):
        service.create_generation_job(project.id, plan.id, **base)
    # Missing API key blocks even with LIVE=true.
    monkeypatch.setenv("LIVE_GENERATION_ENABLED", "true")
    monkeypatch.delenv("APIYI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="NotConfigured"):
        service.create_generation_job(project.id, plan.id, **base)
    monkeypatch.setenv("APIYI_API_KEY", "fixture-key")
    # Missing paid confirmation.
    with pytest.raises(ValueError, match="paid confirmation"):
        service.create_generation_job(project.id, plan.id, **{**base, "paid_confirmation": False})
    # Missing explicit user confirmation.
    with pytest.raises(ValueError, match="explicit user confirmation"):
        service.create_generation_job(project.id, plan.id, **{**base, "confirmed_by": None})
    # Whole-plan live is forbidden: exactly one shot is required.
    with pytest.raises(ValueError, match="exactly one --shot-id"):
        service.create_generation_job(project.id, plan.id, **{**base, "shot_id": None})
    # Non-positive budget is rejected before any other consideration.
    with pytest.raises(ValueError, match="positive max cost"):
        service.create_generation_job(
            project.id, plan.id, **{**base, "budget_policy": BudgetPolicy(project_limit=0, job_limit=0, shot_limit=0)}
        )
    assert not service.get_record(project.id).generation_jobs


def test_live_success_without_provider_request_id_succeeds(
    temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither verified sync contract guarantees a response id; success must not require one."""
    _enable_live(temp_config, monkeypatch)
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    job = _live_job(service, project, plan)
    encoded = base64.b64encode(_png_bytes()).decode("ascii")
    monkeypatch.setattr(
        httpx.Client, "request",
        lambda *a, **k: _fake_response({"candidates": [{"content": {"parts": [{"inlineData": {"data": encoded}}]}}]}),
    )
    finished = service.run_apiyi_generation_job(project.id, job.id)
    attempt = _attempt_of(service, project.id, job.id)
    assert finished.status == GenerationStatus.SUCCEEDED
    assert attempt.status == GenerationStatus.SUCCEEDED
    assert attempt.provider_request_id is None
    record = service.get_record(project.id)
    assert len(record.candidates) == 1
    assert attempt.actual_cost is None  # unknown cost is never coerced to 0
    ledger = [item for item in service.ledger.read_all() if item.task == job.id]
    assert ledger[-1].status == "succeeded" and ledger[-1].actual_cost_usd is None


def test_live_timeout_after_submission_never_retries_and_stays_unknown(
    temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_live(temp_config, monkeypatch)
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    job = _live_job(service, project, plan)
    calls = 0

    def raise_timeout(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(httpx.Client, "request", raise_timeout)
    finished = service.run_apiyi_generation_job(project.id, job.id)
    attempt = _attempt_of(service, project.id, job.id)
    assert calls == 1  # no automatic retry of a possibly-submitted paid request
    assert attempt.status == GenerationStatus.RECONCILE_REQUIRED
    assert finished.status == GenerationStatus.RECONCILE_REQUIRED
    assert attempt.actual_cost is None and finished.actual_total_cost is None
    assert attempt.reconciliation_note
    # A second dispatcher pass must not resend the in-flight attempt.
    again = service.run_apiyi_generation_job(project.id, job.id)
    assert calls == 1 and again.status == GenerationStatus.RECONCILE_REQUIRED
    ledger = [item for item in service.ledger.read_all() if item.task == job.id]
    assert ledger[-1].status == "reconcile_required" and ledger[-1].actual_cost_usd is None
    # The identical request can never be created again while the outcome is unknown.
    with pytest.raises(ValueError, match="uncertain outcome"):
        _live_job(service, project, plan)


def test_live_write_error_is_treated_as_possible_submission(
    temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_live(temp_config, monkeypatch)
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    job = _live_job(service, project, plan)

    def raise_write_error(*args, **kwargs):
        raise httpx.WriteError("request body may have been sent")

    monkeypatch.setattr(httpx.Client, "request", raise_write_error)
    service.run_apiyi_generation_job(project.id, job.id)
    attempt = _attempt_of(service, project.id, job.id)
    assert attempt.status == GenerationStatus.RECONCILE_REQUIRED
    assert attempt.actual_cost is None


def test_live_connect_failure_is_safe_failed_and_rerunnable(
    temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_live(temp_config, monkeypatch)
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    job = _live_job(service, project, plan)

    def raise_connect(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.Client, "request", raise_connect)
    service.run_apiyi_generation_job(project.id, job.id)
    attempt = _attempt_of(service, project.id, job.id)
    assert attempt.status == GenerationStatus.FAILED
    assert attempt.error_code == APIYIProviderErrorCode.PROVIDER_FAILED.value
    # A pre-submission failure never reached the provider, so a new job is safe.
    encoded = base64.b64encode(_png_bytes()).decode("ascii")
    monkeypatch.setattr(
        httpx.Client, "request",
        lambda *a, **k: _fake_response({"candidates": [{"content": {"parts": [{"inlineData": {"data": encoded}}]}}]}),
    )
    retry = _live_job(service, project, plan)
    finished = service.run_apiyi_generation_job(project.id, retry.id)
    assert finished.status == GenerationStatus.SUCCEEDED


def test_live_malformed_success_response_requires_reconciliation(
    temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_live(temp_config, monkeypatch)
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    job = _live_job(service, project, plan)
    # 2xx but no usable image data: the provider processed the request.
    monkeypatch.setattr(httpx.Client, "request", lambda *a, **k: _fake_response({"candidates": []}))
    service.run_apiyi_generation_job(project.id, job.id)
    attempt = _attempt_of(service, project.id, job.id)
    assert attempt.status == GenerationStatus.RECONCILE_REQUIRED
    assert attempt.error_code == APIYIProviderErrorCode.MALFORMED_RESPONSE.value
    assert attempt.actual_cost is None


def test_live_download_failure_after_success_requires_reconciliation(
    temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_live(temp_config, monkeypatch)
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    job = _live_job(service, project, plan)
    monkeypatch.setattr(
        httpx.Client, "request",
        lambda *a, **k: _fake_response({"id": "req-1", "candidates": [{"content": {"parts": [{"inlineData": {"data": "not-an-image"}}]}}]}),
    )
    service.run_apiyi_generation_job(project.id, job.id)
    attempt = _attempt_of(service, project.id, job.id)
    # The provider generated (and billed) but the result was unusable locally.
    assert attempt.status == GenerationStatus.RECONCILE_REQUIRED
    assert attempt.provider_request_id == "req-1"
    assert not service.get_record(project.id).candidates


def test_live_candidate_save_failure_cleans_orphan_and_requires_reconciliation(
    temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_live(temp_config, monkeypatch)
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    job = _live_job(service, project, plan)
    encoded = base64.b64encode(_png_bytes()).decode("ascii")
    monkeypatch.setattr(
        httpx.Client, "request",
        lambda *a, **k: _fake_response({"candidates": [{"content": {"parts": [{"inlineData": {"data": encoded}}]}}]}),
    )
    original_save = service.store.save
    saves = 0

    def fail_final_save(record):
        nonlocal saves
        saves += 1
        if saves == 3:  # submitting, downloading, then the settling save
            raise OSError("metadata write unavailable")
        return original_save(record)

    monkeypatch.setattr(service.store, "save", fail_final_save)
    service.run_apiyi_generation_job(project.id, job.id)
    attempt = _attempt_of(service, project.id, job.id)
    assert attempt.status == GenerationStatus.RECONCILE_REQUIRED
    assert not service.get_record(project.id).candidates
    assert not list(service.store.project_dir(project.id).glob("generation/candidates/**/*.*"))


def test_duplicate_guard_blocks_creation_while_first_request_in_flight(
    temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_live(temp_config, monkeypatch)
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    job = _live_job(service, project, plan)
    # A second identical request is already blocked while the first is QUEUED.
    with pytest.raises(ValueError, match="already queued"):
        _live_job(service, project, plan)
    # Simulate a dispatcher mid-submission; creation must still be blocked.
    with service.store.lock(project.id):
        record = service.store.load(project.id)
        stored = next(item for item in record.generation_attempts if item.job_id == job.id)
        stored.status = GenerationStatus.SUBMITTING
        stored.submitted_at = stored.claimed_at = "2026-07-31T00:00:00+00:00"
        service.store.save(record)
    with pytest.raises(ValueError, match="already queued"):
        _live_job(service, project, plan)
    with service.store.lock(project.id):
        record = service.store.load(project.id)
        stored = next(item for item in record.generation_attempts if item.job_id == job.id)
        stored.status = GenerationStatus.DOWNLOADING
        service.store.save(record)
    with pytest.raises(ValueError, match="already queued"):
        _live_job(service, project, plan)


def test_reconcile_attempt_states(temp_config: AppConfig) -> None:
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    job = service.create_generation_job(project.id, plan.id, shot_id=plan.shots[0].id)
    attempt = _attempt_of(service, project.id, job.id)
    # A never-dispatched attempt is a safe final failure, not an uncertain paid state.
    reconciled = service.reconcile_attempt(project.id, attempt.id)
    assert reconciled.status == GenerationStatus.FAILED
    assert reconciled.error_code == "never_submitted"
    # A fresh confirmed job for the same shot is safe after never_submitted.
    second = service.create_generation_job(project.id, plan.id, shot_id=plan.shots[0].id)
    service.run_generation_job(project.id, second.id)
    succeeded = _attempt_of(service, project.id, second.id)
    assert succeeded.status == GenerationStatus.SUCCEEDED
    # A settled attempt can never be reconciled into an uncertain state.
    with pytest.raises(ValueError, match="unresolved"):
        service.reconcile_attempt(project.id, succeeded.id)


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


def test_reconcile_job_covers_stranded_queued_attempts(temp_config: AppConfig) -> None:
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    job = service.create_generation_job(project.id, plan.id, shot_id=plan.shots[0].id)
    reconciled = service.reconcile_job(project.id, job.id)
    assert len(reconciled) == 1
    assert reconciled[0].status == GenerationStatus.FAILED
    assert reconciled[0].error_code == "never_submitted"


def test_provider_status_reports_locked_until_gate_satisfied(
    temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = StudioService(temp_config)
    monkeypatch.delenv("APIYI_API_KEY", raising=False)
    assert service.provider_status("nano_banana_2")["status"] == "not_configured"
    # Key configured but pricing unknown: locked, never "ready".
    _force_unknown_pricing(temp_config)
    monkeypatch.setenv("APIYI_API_KEY", "fixture-key")
    locked = service.provider_status("nano_banana_2")
    assert locked["status"] == "locked"
    assert locked["capability"]["pricing_status"] == "unknown"
    # Exact pricing + LIVE=true makes it ready.
    _force_exact_pricing(temp_config)
    monkeypatch.setenv("LIVE_GENERATION_ENABLED", "true")
    assert service.provider_status("nano_banana_2")["status"] == "ready"
    # Key configured + exact pricing but LIVE=false: still locked.
    monkeypatch.delenv("LIVE_GENERATION_ENABLED", raising=False)
    assert service.provider_status("nano_banana_2")["status"] == "locked"


def test_provider_payload_excludes_annotations_and_foreign_assets(temp_config: AppConfig) -> None:
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    record = service.get_record(project.id)
    package = next(item for item in record.prompt_packages if item.shot_id == plan.shots[0].id)
    assert package.annotation_preview_ids  # the fixture project has rendered annotations
    shot = plan.shots[0]
    job = service.create_generation_job(project.id, plan.id, shot_id=shot.id)
    record = service.get_record(project.id)
    attempt = next(item for item in record.generation_attempts if item.job_id == job.id)
    request = service._apiyi_request(record, package, shot, attempt, "nano_banana_2")
    roles = [reference.role for reference in request.references]
    assert set(roles) <= {"product_reference_clean", "detail_reference_clean", "style_reference"}
    # Deterministic role order: product, detail, style.
    order = {"product_reference_clean": 0, "detail_reference_clean": 1, "style_reference": 2}
    assert roles == sorted(roles, key=order.__getitem__)
    # Payload bytes come only from clean stored originals; an annotation preview
    # path is never referenced even when its underlying asset is a clean detail.
    assets = {asset.id: asset for asset in record.assets}
    annotation_paths = {asset.annotation_path for asset in record.assets if asset.annotation_path}
    assert annotation_paths  # the fixture project has rendered annotations
    for reference in request.references:
        asset = assets[reference.asset_id]
        assert reference.path == service._project_path(project.id, asset.stored_path)
        assert asset.stored_path not in annotation_paths
    # Cross-project assets are rejected even if the ID is grafted into a package.
    other = service.create_project("foreign")
    foreign, _ = service.import_asset(other.id, "foreign.png", _png_bytes(), 5_000_000)
    tampered = package.model_copy(update={"product_reference_ids": [foreign.id]})
    with pytest.raises(ValueError, match="missing"):
        service._apiyi_request(record, tampered, shot, attempt, "nano_banana_2")


def test_request_hash_binds_mode_provider_model_references_and_nonce(temp_config: AppConfig) -> None:
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    record = service.get_record(project.id)
    package = next(item for item in record.prompt_packages if item.shot_id == plan.shots[0].id)
    shot = plan.shots[0]
    base = service._request_hash(record, shot, package, "mock", "mock", "mock-image-v1", None)
    assert base != service._request_hash(record, shot, package, "live", "apiyi", "mock-image-v1", None)
    assert base != service._request_hash(record, shot, package, "mock", "mock", "nano_banana_2", None)
    assert base != service._request_hash(record, shot, package, "mock", "mock", "mock-image-v1", "nonce-1")
    tampered = record.model_copy(deep=True)
    tampered.assets[0].sha256 = "f" * 64
    assert base != service._request_hash(tampered, shot, package, "mock", "mock", "mock-image-v1", None)


def test_safe_provider_error_withholds_secret_matrix() -> None:
    secrets = (
        "Authorization: Bearer secret",
        "Authorization=secret",
        '{"Authorization": "Bearer secret"}',
        "api_key=secret",
        "API-KEY: secret",
        "APIYI_API_KEY=secret",
        "?api_key=secret",
        "token=secret",
        "access_token=secret",
        "cookie: session=secret",
        "https://cdn.example.test/image.png?sig=secret",
        "https://cdn.example.test/image.png?X-Amz-Signature=secret",
        "Authorization:\n Bearer secret",
        "https://user:password@example.test",
        "data:image/png;base64,secret",
    )
    for text in secrets:
        code, message = safe_provider_error(RuntimeError(text))
        assert code == APIYIProviderErrorCode.PROVIDER_FAILED.value
        assert "secret" not in message.lower()
        assert len(message) <= 300
    # Benign diagnostics still pass through, masked and truncated.
    code, message = safe_provider_error(ValueError("disk full"))
    assert message == "disk full"


def _public_dns(monkeypatch: pytest.MonkeyPatch, ip: str = "93.184.216.34") -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))],
    )


def _download_client(monkeypatch: pytest.MonkeyPatch, ip: str = "93.184.216.34") -> APIYIClient:
    _public_dns(monkeypatch, ip)
    return APIYIClient(
        "fixture-key", "https://api.apiyi.com", 10,
        allowed_result_hosts={"api.apiyi.com", "b.apiyi.com"},
    )


class _FakeStreamResponse:
    def __init__(self, status_code: int = 200, headers: dict | None = None, chunks: tuple = ()) -> None:
        self.status_code = status_code
        self.headers = httpx.Headers(headers or {})
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    @property
    def is_redirect(self) -> bool:
        return self.status_code in (301, 302, 303, 307, 308)

    def iter_bytes(self, chunk_size: int):
        yield from self._chunks


@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "100.64.0.1"],
)
def test_result_url_rejects_non_global_addresses(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    client = _download_client(monkeypatch)
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *args, **kwargs: [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))],
    )
    with pytest.raises(APIYIProviderError) as captured:
        client._result_url_is_safe("https://api.apiyi.com/result.png")
    assert captured.value.code == APIYIProviderErrorCode.UNSAFE_RESULT


def test_result_url_rejects_non_https_userinfo_and_unknown_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _download_client(monkeypatch)
    for url in (
        "http://api.apiyi.com/result.png",
        "https://user:password@api.apiyi.com/result.png",
        "https://evil-cdn.example.net/result.png",
        "https://localhost.evil.example.net/result.png",
    ):
        with pytest.raises(APIYIProviderError) as captured:
            client._result_url_is_safe(url)
        assert captured.value.code == APIYIProviderErrorCode.UNSAFE_RESULT


def test_result_url_allowlisted_host_with_public_ip_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _download_client(monkeypatch)
    client._result_url_is_safe("https://api.apiyi.com/result.png")
    client._result_url_is_safe("https://b.apiyi.com/result.png")


def test_download_streams_content_without_content_length(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _download_client(monkeypatch)
    payload = _png_bytes()
    chunks = (payload[:10], payload[10:])
    monkeypatch.setattr(
        httpx.Client, "stream",
        lambda self, method, url, **kwargs: _FakeStreamResponse(200, {}, chunks),
    )
    result = APIYIGenerationResult(url="https://api.apiyi.com/result.png")
    assert client.download_result(result) == payload


def test_download_rejects_oversized_body_while_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _download_client(monkeypatch)
    monkeypatch.setattr(APIYIClient, "MAX_RESULT_BYTES", 32)
    # A lying small Content-Length does not matter: the cap is enforced on bytes read.
    monkeypatch.setattr(
        httpx.Client, "stream",
        lambda self, method, url, **kwargs: _FakeStreamResponse(200, {"Content-Length": "8"}, (b"x" * 16, b"y" * 32)),
    )
    with pytest.raises(APIYIProviderError) as captured:
        client.download_result(APIYIGenerationResult(url="https://api.apiyi.com/big.png"))
    assert captured.value.code == APIYIProviderErrorCode.UNSAFE_RESULT


def test_download_follows_relative_redirect_but_revalidates_host(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _download_client(monkeypatch)
    payload = _png_bytes()
    responses = [
        _FakeStreamResponse(302, {"location": "/final.png"}),
        _FakeStreamResponse(200, {}, (payload,)),
    ]
    monkeypatch.setattr(httpx.Client, "stream", lambda self, method, url, **kwargs: responses.pop(0))
    result = APIYIGenerationResult(url="https://api.apiyi.com/redirect")
    assert client.download_result(result) == payload


def test_download_rejects_redirect_to_non_allowlisted_or_private(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _download_client(monkeypatch)
    # Public allowlisted host redirects to a non-allowlisted host.
    responses = [_FakeStreamResponse(302, {"location": "https://169.254.169.254/latest/meta-data"})]
    monkeypatch.setattr(httpx.Client, "stream", lambda self, method, url, **kwargs: responses.pop(0))
    with pytest.raises(APIYIProviderError) as captured:
        client.download_result(APIYIGenerationResult(url="https://api.apiyi.com/redirect"))
    assert captured.value.code == APIYIProviderErrorCode.UNSAFE_RESULT


def test_download_rejects_redirect_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _download_client(monkeypatch)
    monkeypatch.setattr(
        httpx.Client, "stream",
        lambda self, method, url, **kwargs: _FakeStreamResponse(302, {"location": "https://api.apiyi.com/loop"}),
    )
    with pytest.raises(APIYIProviderError) as captured:
        client.download_result(APIYIGenerationResult(url="https://api.apiyi.com/loop"))
    assert captured.value.code == APIYIProviderErrorCode.UNSAFE_RESULT


def test_web_live_post_is_rejected_by_core_gate_when_pricing_unknown(
    temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import re

    from fastapi.testclient import TestClient

    from src.web.app import create_app

    _force_unknown_pricing(temp_config)
    monkeypatch.setenv("LIVE_GENERATION_ENABLED", "true")
    monkeypatch.setenv("APIYI_API_KEY", "fixture-key")
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    client = TestClient(create_app(temp_config))
    login = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', login.text).group(1)
    client.post("/login", data={"username": "admin", "password": "test-password", "csrf_token": csrf})
    page = client.get(f"/studio/{project.id}/generation")
    assert "locked" in page.text or "not_configured" in page.text
    page_csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = client.post(
        f"/studio/{project.id}/plans/{plan.id}/generate/live",
        data={
            "shot_id": plan.shots[0].id, "model": "nano_banana_2", "max_cost": "999",
            "confirm_paid_generation": "true", "csrf_token": page_csrf,
        },
    )
    assert response.status_code == 200
    assert "pricing_unknown" in response.text
    assert not service.get_record(project.id).generation_jobs


def test_cli_live_is_rejected_by_core_gate_when_pricing_unknown(
    temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from src.cli import cli

    _force_unknown_pricing(temp_config)
    monkeypatch.setenv("LIVE_GENERATION_ENABLED", "true")
    monkeypatch.setenv("APIYI_API_KEY", "fixture-key")
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    monkeypatch.setattr("src.cli.get_config", lambda: temp_config)
    result = CliRunner().invoke(
        cli,
        [
            "studio", "generate-live", project.id, plan.id,
            "--mode", "live", "--provider", "apiyi", "--model", "nano_banana_2",
            "--shot-id", plan.shots[0].id, "--max-cost", "999", "--confirm-paid-generation",
        ],
    )
    assert result.exit_code != 0
    assert "pricing_unknown" in result.output
    assert not service.get_record(project.id).generation_jobs

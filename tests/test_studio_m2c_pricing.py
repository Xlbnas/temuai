"""M2C verified pricing contract tests — offline only, mocked transports.

The shipped config pins Nano Banana 2 (gemini-3.1-flash-image) to a verified
per-request price; every other model must stay pricing-unknown and locked.
"""
from __future__ import annotations

import base64
import json
import socket
from io import BytesIO

import httpx
import pytest
from PIL import Image

from src.core.config import AppConfig
from src.studio.apiyi import APIYIImageGenerationProvider, _model_config, load_pricing_contract
from src.studio.models import BudgetPolicy, GenerationStatus
from src.studio.service import StudioService
from tests.test_studio_m2 import _confirmed_plan

REAL_APIYI_HOSTS = {"api.apiyi.com", "b.apiyi.com"}
CONTRACT_VERSION = "apiyi-nb2-per-request-2026-03-01"
CONTRACT_DIGEST = "sha256:0c4dec06c0d2420f22d19d7183e9250c20f041ad955cf430faa2d421f467df0f"


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch):
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


def _fake_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, json=payload,
        request=httpx.Request("POST", "https://api.apiyi.com/v1beta/models/x:generateContent"),
    )


def _contract_block(**overrides) -> dict:
    block = {
        "provider": "apiyi",
        "provider_model_id": "gemini-3.1-flash-image",
        "pricing_status": "exact",
        "pricing_version": CONTRACT_VERSION,
        "pricing_source": "fixture official source",
        "source_type": "public_official",
        "effective_at": "2026-03-01",
        "retrieved_at": "2026-08-01T01:59:39Z",
        "currency": "USD",
        "unit": "per_request",
        "amount": 0.055,
        "request_mode": "generation_or_edit",
        "supported_resolutions": ["512px", "1K", "2K", "4K"],
        "supported_aspect_ratios": ["3:4"],
        "supported_quality_levels": [],
        "reference_policy": "price_unchanged",
        "output_count": 1,
        "evidence_digest": CONTRACT_DIGEST,
    }
    block.update(overrides)
    return block


def _set_contract(temp_config: AppConfig, block: dict | None, model: str = "nano_banana_2") -> None:
    raw = temp_config.models["models"][model]
    if block is None:
        raw.pop("pricing_contract", None)
        raw["pricing_status"] = "unknown"
    else:
        raw["pricing_contract"] = block
        raw["pricing_status"] = block.get("pricing_status", "unknown")


def _enable_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVE_GENERATION_ENABLED", "true")
    monkeypatch.setenv("APIYI_API_KEY", "fixture-key")


def _live_job(service: StudioService, project, plan):
    return service.create_generation_job(
        project.id, plan.id, mode="live", provider="apiyi", model="nano_banana_2",
        shot_id=plan.shots[0].id, paid_confirmation=True, confirmed_by="tester",
        budget_policy=BudgetPolicy(project_limit=1, job_limit=1, shot_limit=1),
    )


def test_shipped_nano_banana_2_contract_is_exact_and_valid(temp_config: AppConfig) -> None:
    contract = load_pricing_contract(temp_config, _model_config(temp_config, "nano_banana_2"))
    assert contract is not None
    assert contract.pricing_status == "exact"
    assert contract.amount == pytest.approx(0.055)
    assert contract.recommended_hard_max_usd == pytest.approx(0.06)
    assert contract.unit == "per_request"
    assert contract.currency == "USD"
    assert contract.pricing_version == CONTRACT_VERSION
    assert contract.evidence_digest == CONTRACT_DIGEST
    capability = APIYIImageGenerationProvider.capability_for(temp_config, "nano_banana_2")
    assert capability.pricing_status == "exact"
    assert capability.estimated_price_usd == pytest.approx(0.055)
    assert capability.pricing_version == CONTRACT_VERSION
    assert capability.pricing_effective_at == "2026-03-01"
    assert capability.pricing_digest == CONTRACT_DIGEST


def test_other_models_remain_unknown_and_locked(temp_config: AppConfig) -> None:
    for model in ("nano_banana_lite", "gpt_image_2_vip", "gpt_image_2"):
        capability = APIYIImageGenerationProvider.capability_for(temp_config, model)
        assert capability.pricing_status == "unknown", model
        assert load_pricing_contract(temp_config, _model_config(temp_config, model)) is None


@pytest.mark.parametrize(
    "missing_field",
    ["pricing_version", "source_type", "effective_at", "retrieved_at", "unit", "amount", "request_mode", "evidence_digest"],
)
def test_exact_contract_missing_evidence_field_fails_closed(temp_config: AppConfig, missing_field: str) -> None:
    block = _contract_block(**{missing_field: None})
    _set_contract(temp_config, block)
    capability = APIYIImageGenerationProvider.capability_for(temp_config, "nano_banana_2")
    assert capability.pricing_status == "unknown"
    assert "invalid pricing contract" in (capability.pricing_source or "")


def test_contract_model_id_mismatch_fails_closed(temp_config: AppConfig) -> None:
    _set_contract(temp_config, _contract_block(provider_model_id="gemini-3.1-flash-image-preview"))
    capability = APIYIImageGenerationProvider.capability_for(temp_config, "nano_banana_2")
    assert capability.pricing_status == "unknown"
    assert "model ID" in (capability.pricing_source or "")


def test_contract_wrong_provider_fails_closed(temp_config: AppConfig) -> None:
    _set_contract(temp_config, _contract_block(provider="other_provider"))
    capability = APIYIImageGenerationProvider.capability_for(temp_config, "nano_banana_2")
    assert capability.pricing_status == "unknown"


def test_contract_resolution_out_of_scope_fails_closed(temp_config: AppConfig) -> None:
    # The configured model default is 2K; a contract that excludes it is invalid.
    _set_contract(temp_config, _contract_block(supported_resolutions=["1K"]))
    capability = APIYIImageGenerationProvider.capability_for(temp_config, "nano_banana_2")
    assert capability.pricing_status == "unknown"


def test_contract_unsupported_request_mode_fails_closed(temp_config: AppConfig) -> None:
    _set_contract(temp_config, _contract_block(request_mode="batch_offline"))
    capability = APIYIImageGenerationProvider.capability_for(temp_config, "nano_banana_2")
    assert capability.pricing_status == "unknown"


def test_contract_hard_max_below_unit_price_fails_closed(temp_config: AppConfig) -> None:
    _set_contract(temp_config, _contract_block(recommended_hard_max_usd=0.05))
    capability = APIYIImageGenerationProvider.capability_for(temp_config, "nano_banana_2")
    assert capability.pricing_status == "unknown"
    assert "hard max" in (capability.pricing_source or "")


def test_expired_or_revoked_contract_fails_closed(temp_config: AppConfig) -> None:
    _set_contract(temp_config, _contract_block(expires_at="2020-01-01T00:00:00Z"))
    capability = APIYIImageGenerationProvider.capability_for(temp_config, "nano_banana_2")
    assert capability.pricing_status == "unknown"
    assert "expired" in (capability.pricing_source or "")
    _set_contract(temp_config, _contract_block(revoked=True))
    capability = APIYIImageGenerationProvider.capability_for(temp_config, "nano_banana_2")
    assert capability.pricing_status == "unknown"
    assert "revoked" in (capability.pricing_source or "")


def test_live_gate_matrix_with_exact_contract(temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    policy = BudgetPolicy(project_limit=999, job_limit=999, shot_limit=999)
    base = {
        "mode": "live", "provider": "apiyi", "model": "nano_banana_2", "shot_id": plan.shots[0].id,
        "budget_policy": policy, "paid_confirmation": True, "confirmed_by": "tester",
    }
    # A. exact pricing + Live=false -> rejected before any network.
    monkeypatch.delenv("LIVE_GENERATION_ENABLED", raising=False)
    monkeypatch.setenv("APIYI_API_KEY", "fixture-key")
    with pytest.raises(ValueError, match="LIVE_GENERATION_ENABLED=false"):
        service.create_generation_job(project.id, plan.id, **base)
    # B. exact pricing + fake key + Live=false -> rejected (key alone never unlocks).
    # (identical to A by construction; keep explicit for the record)
    with pytest.raises(ValueError, match="LIVE_GENERATION_ENABLED=false"):
        service.create_generation_job(project.id, plan.id, **base)
    # C. unknown pricing + Live=true + fake key -> rejected.
    _set_contract(temp_config, None)
    monkeypatch.setenv("LIVE_GENERATION_ENABLED", "true")
    with pytest.raises(ValueError, match="pricing_unknown"):
        service.create_generation_job(project.id, plan.id, **base)
    # D. exact pricing + Live=true + no key -> rejected.
    _set_contract(temp_config, _contract_block())
    monkeypatch.delenv("APIYI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="NotConfigured"):
        service.create_generation_job(project.id, plan.id, **base)
    # E. exact + Live=true + fake key + whole plan (no shot id) -> rejected.
    monkeypatch.setenv("APIYI_API_KEY", "fixture-key")
    with pytest.raises(ValueError, match="exactly one --shot-id"):
        service.create_generation_job(project.id, plan.id, **{**base, "shot_id": None})
    assert not service.get_record(project.id).generation_jobs


def test_live_job_creation_snapshots_pricing_contract(temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_live(monkeypatch)
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    job = _live_job(service, project, plan)
    assert job.pricing_version == CONTRACT_VERSION
    assert job.pricing_digest == CONTRACT_DIGEST
    assert job.estimated_total_cost == pytest.approx(0.055)
    record = service.get_record(project.id)
    attempt = next(item for item in record.generation_attempts if item.job_id == job.id)
    assert attempt.pricing_version == CONTRACT_VERSION
    assert attempt.pricing_digest == CONTRACT_DIGEST
    assert attempt.unit_price_usd == pytest.approx(0.055)
    assert attempt.estimated_cost == pytest.approx(0.055)


def test_live_dispatch_with_exact_contract_and_fake_transport(
    temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F. exact + Live=true + fake key + single shot — works only on fake transport."""
    _enable_live(monkeypatch)
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    job = _live_job(service, project, plan)
    encoded = base64.b64encode(_png_bytes()).decode("ascii")
    monkeypatch.setattr(
        httpx.Client, "request",
        lambda *a, **k: _fake_response({"candidates": [{"content": {"parts": [{"inlineData": {"data": encoded}}]}}]}),
    )
    finished = service.run_apiyi_generation_job(project.id, job.id)
    assert finished.status == GenerationStatus.SUCCEEDED
    ledger = [item for item in service.ledger.read_all() if item.task == job.id]
    assert ledger[-1].pricing_version == CONTRACT_VERSION
    assert ledger[-1].estimated_cost_usd == pytest.approx(0.055)
    assert ledger[-1].actual_cost_usd is None


def test_live_dispatch_without_mock_is_stopped_by_network_guard(
    temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unmocked live dispatch must die on the real-APIYI network guard."""
    _enable_live(monkeypatch)
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    job = _live_job(service, project, plan)
    with pytest.raises(AssertionError, match="real APIYI network call attempted"):
        service.run_apiyi_generation_job(project.id, job.id)


def test_flat_pricing_status_flip_without_contract_stays_locked(
    temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare pricing_status=exact flip (no validated contract) must not unlock Live."""
    temp_config.models["models"]["gpt_image_2_vip"]["pricing_status"] = "exact"
    capability = APIYIImageGenerationProvider.capability_for(temp_config, "gpt_image_2_vip")
    assert capability.pricing_status == "unknown"
    assert "pricing_contract" in (capability.pricing_source or "")
    _enable_live(monkeypatch)
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    with pytest.raises(ValueError, match="pricing_unknown"):
        service.create_generation_job(
            project.id, plan.id, mode="live", provider="apiyi", model="gpt_image_2_vip",
            shot_id=plan.shots[0].id, paid_confirmation=True, confirmed_by="tester",
            budget_policy=BudgetPolicy(project_limit=999, job_limit=999, shot_limit=999),
        )


def test_max_cost_is_only_a_ceiling_never_a_price(temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Users cannot override the server-side contract price via budget inputs."""
    _enable_live(monkeypatch)
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    job = service.create_generation_job(
        project.id, plan.id, mode="live", provider="apiyi", model="nano_banana_2",
        shot_id=plan.shots[0].id, paid_confirmation=True, confirmed_by="tester",
        budget_policy=BudgetPolicy(project_limit=500, job_limit=500, shot_limit=500),
    )
    assert job.estimated_total_cost == pytest.approx(0.055)
    # A ceiling below the contract price is rejected, not negotiated.
    with pytest.raises(ValueError, match="budget_rejected"):
        service.create_generation_job(
            project.id, plan.id, mode="live", provider="apiyi", model="nano_banana_2",
            shot_id=plan.shots[1].id, paid_confirmation=True, confirmed_by="tester",
            budget_policy=BudgetPolicy(project_limit=0.01, job_limit=0.01, shot_limit=0.01),
        )


def test_cli_has_no_price_override_option(temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from src.cli import cli

    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    monkeypatch.setattr("src.cli.get_config", lambda: temp_config)
    result = CliRunner().invoke(
        cli,
        [
            "studio", "generate-live", project.id, plan.id,
            "--mode", "live", "--provider", "apiyi", "--model", "nano_banana_2",
            "--shot-id", plan.shots[0].id, "--max-cost", "1", "--confirm-paid-generation",
            "--price", "0.01",
        ],
    )
    assert result.exit_code != 0
    assert "No such option" in result.output


def test_request_hash_binds_pricing_contract_for_live_only(temp_config: AppConfig) -> None:
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    record = service.get_record(project.id)
    package = next(item for item in record.prompt_packages if item.shot_id == plan.shots[0].id)
    shot = plan.shots[0]
    mock_hash = service._request_hash(record, shot, package, "mock", "mock", "mock-image-v1", None)
    live_hash = service._request_hash(
        record, shot, package, "live", "apiyi", "nano_banana_2", None,
        pricing_version=CONTRACT_VERSION, pricing_digest=CONTRACT_DIGEST,
    )
    other_contract = service._request_hash(
        record, shot, package, "live", "apiyi", "nano_banana_2", None,
        pricing_version="other-version", pricing_digest=CONTRACT_DIGEST,
    )
    assert mock_hash != live_hash
    assert live_hash != other_contract
    # Mock hash formula is unchanged by the M2C pricing fields.
    legacy = service._request_hash(record, shot, package, "mock", "mock", "mock-image-v1", None)
    assert mock_hash == legacy


def test_config_change_does_not_rewrite_historical_job(temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_live(monkeypatch)
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    job = _live_job(service, project, plan)
    _set_contract(temp_config, _contract_block(amount=9.99, pricing_version="hacked-version"))
    record = service.get_record(project.id)
    stored_job = next(item for item in record.generation_jobs if item.id == job.id)
    attempt = next(item for item in record.generation_attempts if item.job_id == job.id)
    assert stored_job.pricing_version == CONTRACT_VERSION
    assert stored_job.estimated_total_cost == pytest.approx(0.055)
    assert attempt.unit_price_usd == pytest.approx(0.055)
    # New capability reads the new config, history keeps its snapshot.
    capability = APIYIImageGenerationProvider.capability_for(temp_config, "nano_banana_2")
    assert capability.estimated_price_usd == pytest.approx(9.99)


def test_core_cost_preview_exact_and_unknown(temp_config: AppConfig) -> None:
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    shot_id = plan.shots[0].id
    exact = service.cost_preview(project.id, plan.id, "apiyi", "nano_banana_2", shot_id)
    assert exact["pricing_status"] == "exact"
    assert exact["unit_price"] == pytest.approx(0.055)
    assert exact["estimated_total"] == pytest.approx(0.055)
    assert exact["currency"] == "USD"
    assert exact["quantity"] == 1
    assert exact["pricing_version"] == CONTRACT_VERSION
    assert exact["effective_at"] == "2026-03-01"
    assert exact["hard_max_recommendation"] == pytest.approx(0.06)
    assert exact["reference_price_policy"] == "price_unchanged"
    assert "no provider call" in exact["note"]
    unknown = service.cost_preview(project.id, plan.id, "apiyi", "gpt_image_2_vip", shot_id)
    assert unknown["pricing_status"] == "unknown"
    assert unknown["estimated_cost"] is None
    assert unknown["display"] == "Pricing unavailable / Live locked"
    assert "unit_price" not in unknown


def test_cli_cost_preview_exact(temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from src.cli import cli

    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    monkeypatch.setattr("src.cli.get_config", lambda: temp_config)
    result = CliRunner().invoke(
        cli,
        ["studio", "cost-preview", project.id, plan.id, "--provider", "apiyi", "--model", "nano_banana_2", "--shot-id", plan.shots[0].id],
    )
    assert result.exit_code == 0, result.output
    preview = json.loads(result.output)
    assert preview["pricing_status"] == "exact"
    assert preview["unit_price"] == pytest.approx(0.055)
    assert preview["pricing_version"] == CONTRACT_VERSION


def test_web_generation_page_shows_exact_pricing_version(
    temp_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import re

    from fastapi.testclient import TestClient

    from src.web.app import create_app

    service = StudioService(temp_config)
    project, _plan = _confirmed_plan(service)
    client = TestClient(create_app(temp_config))
    login = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', login.text).group(1)
    client.post("/login", data={"username": "admin", "password": "test-password", "csrf_token": csrf})
    page = client.get(f"/studio/{project.id}/generation")
    assert page.status_code == 200
    assert CONTRACT_VERSION in page.text
    assert "effective 2026-03-01" in page.text
    # Live stays disabled without LIVE_GENERATION_ENABLED, even with exact pricing.
    assert "LIVE_GENERATION_ENABLED=false" in page.text


def test_old_project_json_without_pricing_fields_still_loads(temp_config: AppConfig) -> None:
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    job = service.create_generation_job(project.id, plan.id, shot_id=plan.shots[0].id)
    service.run_generation_job(project.id, job.id)
    path = service.store.record_path(project.id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    for job_payload in payload["generation_jobs"]:
        job_payload.pop("pricing_version", None)
        job_payload.pop("pricing_digest", None)
    for attempt_payload in payload["generation_attempts"]:
        for key in ("pricing_version", "pricing_digest", "unit_price_usd"):
            attempt_payload.pop(key, None)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    record = service.get_record(project.id)
    assert len(record.generation_jobs) == 1
    assert record.generation_jobs[0].pricing_version is None
    assert record.generation_attempts[0].unit_price_usd is None
    # Mock accounting semantics are untouched.
    assert record.generation_jobs[0].actual_total_cost == 0.0


def test_old_ledger_lines_without_pricing_version_still_read(temp_config: AppConfig) -> None:
    service = StudioService(temp_config)
    service.ledger.ledger_path.parent.mkdir(parents=True, exist_ok=True)
    service.ledger.ledger_path.write_text(
        json.dumps(
            {
                "sku": "studio-x", "platform": "studio", "task": "t", "provider": "mock", "model": "m",
                "request_id": None, "timestamp": "2026-07-01T00:00:00+00:00", "attempt": 1,
                "input_images": [], "requested_size": None, "aspect_ratio": None,
                "estimated_cost_usd": 0.0, "actual_cost_usd": 0.0, "status": "succeeded",
                "accepted": False, "error": None, "duration_seconds": 0.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    records = service.ledger.read_all()
    assert len(records) == 1
    assert records[0].pricing_version is None

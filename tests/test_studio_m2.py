from __future__ import annotations

import re
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.studio.analyzers import MockAssetAnalyzer
from src.studio.models import CandidateStatus, ContentKind, SourceKind, StudioPlatform
from src.studio.service import StudioService
from src.web.app import create_app


def _image_bytes(color: str) -> bytes:
    image = Image.new("RGB", (800, 1000), color=color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _ready_project(service: StudioService, platform: StudioPlatform = StudioPlatform.TEMU):
    project = service.create_project("M2 jacket", platform)
    front, _ = service.import_asset(project.id, "front.png", _image_bytes("red"), 5_000_000)
    back, _ = service.import_asset(project.id, "back.png", _image_bytes("blue"), 5_000_000)
    detail, _ = service.import_asset(project.id, "detail.png", _image_bytes("green"), 5_000_000)
    for asset, content_kind in ((front, ContentKind.PRODUCT_FULL_FRONT), (back, ContentKind.PRODUCT_FULL_BACK), (detail, ContentKind.DETAIL)):
        service.analyze_asset(project.id, asset.id, MockAssetAnalyzer())
        service.update_analysis(project.id, asset.id, SourceKind.OWN_CAPTURE, content_kind)
    service.render_annotations(project.id, detail.id)
    service.compile_product_spec(project.id)
    pack = service.style_packs(platform)[0]
    service.select_style_pack(project.id, pack.id)
    return project, front, back, detail


def test_temu_plan_prompt_reference_isolation_and_mock_e2e(temp_config) -> None:
    service = StudioService(temp_config)
    project, _, _, detail = _ready_project(service)
    plan = service.compile_shot_plan(project.id)
    assert len(plan.shots) == 5
    assert plan.shots[0].shot_type == "temu_hero"
    assert not plan.blocking_reasons
    # Disable and reorder as an operator would; the plan hash must change.
    old_hash = plan.content_hash
    plan.shots[0].enabled = False
    plan.shots[1].sequence = 1
    updated = service.update_shot_plan(project.id, plan.id, plan.shots)
    assert updated.content_hash != old_hash
    packages = service.compile_prompt_packages(project.id, plan.id)
    assert len(packages) == 4
    assert all(detail.id not in package.product_reference_ids for package in packages)
    assert any(detail.id in package.annotation_preview_ids for package in packages)
    assert all(detail.id in package.detail_reference_ids for package in packages)
    assert all("watermark" in package.negative_prompt for package in packages)
    assert all(package.content_hash for package in packages)
    repeat = service.compile_prompt_packages(project.id, plan.id)
    assert [item.content_hash for item in repeat] == [item.content_hash for item in packages]
    service.confirm_shot_plan(project.id, plan.id, "tester")
    job = service.create_generation_job(project.id, plan.id)
    completed = service.run_generation_job(project.id, job.id)
    assert completed.status.value == "succeeded"
    record = service.get_record(project.id)
    assert len(record.candidates) == 4
    assert all(service.resolve_candidate_path(project.id, candidate.id).is_file() for candidate in record.candidates)
    accepted = service.accept_candidate(project.id, record.candidates[0].id)
    rejected = service.reject_candidate(project.id, record.candidates[1].id, "Wrong styling")
    assert accepted.status == CandidateStatus.ACCEPTED
    assert rejected.status == CandidateStatus.REJECTED


def test_tiktok_plan_stale_and_blocked_requirements(temp_config) -> None:
    service = StudioService(temp_config)
    project, *_ = _ready_project(service, StudioPlatform.TIKTOK_SHOP)
    plan = service.compile_shot_plan(project.id)
    assert [shot.shot_type for shot in plan.shots] == [
        "tiktok_hook_cover", "tiktok_lifestyle", "tiktok_motion", "tiktok_product_front", "tiktok_product_back"
    ]
    service.compile_prompt_packages(project.id, plan.id)
    service.import_asset(project.id, "later.png", _image_bytes("yellow"), 5_000_000)
    record = service.get_record(project.id)
    assert record.shot_plans[0].status.value == "stale"
    assert all(package.stale for package in record.prompt_packages)


def test_plan_blocks_missing_back_and_live_never_calls_provider(temp_config) -> None:
    service = StudioService(temp_config)
    project = service.create_project("blocked")
    front, _ = service.import_asset(project.id, "front.png", _image_bytes("red"), 5_000_000)
    service.analyze_asset(project.id, front.id, MockAssetAnalyzer())
    service.update_analysis(project.id, front.id, SourceKind.OWN_CAPTURE, ContentKind.PRODUCT_FULL_FRONT)
    service.compile_product_spec(project.id)
    service.select_style_pack(project.id, service.style_packs()[0].id)
    plan = service.compile_shot_plan(project.id)
    assert plan.status.value == "blocked"
    assert "full-back" in plan.blocking_reasons[0] or "full-back" in plan.blocking_reasons[-1]
    with pytest.raises(ValueError, match="Live generation is disabled"):
        service.create_generation_job(project.id, plan.id, mode="live", provider="apiyi", paid_confirmation=True)


def test_partial_failure_and_recovery_do_not_retry(temp_config) -> None:
    service = StudioService(temp_config)
    project, *_ = _ready_project(service)
    plan = service.compile_shot_plan(project.id)
    service.compile_prompt_packages(project.id, plan.id)
    service.confirm_shot_plan(project.id, plan.id, "tester")
    job = service.create_generation_job(project.id, plan.id)
    service.run_generation_job(project.id, job.id, fail_shot_id=plan.shots[0].id)
    record = service.get_record(project.id)
    attempts = [item for item in record.generation_attempts if item.job_id == job.id]
    assert sum(item.status.value == "failed" for item in attempts) == 1
    assert sum(item.status.value == "succeeded" for item in attempts) == 4
    # Manual recovery means a new attempt/job, never an invisible retry.
    retry = service.create_generation_job(project.id, plan.id, shot_id=plan.shots[0].id)
    service.run_generation_job(project.id, retry.id)
    assert len(service.get_record(project.id).candidates) == 5


def test_generation_web_page_and_candidate_media_are_authenticated_and_isolated(temp_config) -> None:
    service = StudioService(temp_config)
    project, *_ = _ready_project(service)
    plan = service.compile_shot_plan(project.id)
    service.compile_prompt_packages(project.id, plan.id)
    service.confirm_shot_plan(project.id, plan.id, "tester")
    job = service.create_generation_job(project.id, plan.id, shot_id=plan.shots[0].id)
    service.run_generation_job(project.id, job.id)
    candidate_id = service.get_record(project.id).candidates[0].id
    other = service.create_project("other")
    client = TestClient(create_app(temp_config))
    assert client.get(f"/studio/{project.id}/generation", follow_redirects=False).status_code == 307
    login = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', login.text).group(1)
    client.post("/login", data={"username": "admin", "password": "test-password", "csrf_token": csrf})
    page = client.get(f"/studio/{project.id}/generation")
    assert page.status_code == 200
    assert "Prompt Preview" in page.text
    assert client.get(f"/studio/{project.id}/candidates/{candidate_id}/image").status_code == 200
    assert client.get(f"/studio/{other.id}/candidates/{candidate_id}/image").status_code == 404

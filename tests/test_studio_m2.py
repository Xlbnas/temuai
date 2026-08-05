from __future__ import annotations

import re
import threading
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.studio.analyzers import MockAssetAnalyzer
from src.studio.generation import safe_error, select_references
from src.studio.models import (
    BudgetPolicy,
    CandidateStatus,
    ContentKind,
    DetailRegion,
    GenerationStatus,
    Importance,
    OverrideValue,
    ProviderCapability,
    SourceKind,
    StudioPlatform,
)
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
    updated = service.update_single_shot(
        project.id,
        plan.id,
        plan.shots[0].id,
        sequence=2,
        composition=plan.shots[0].composition,
        user_instruction="",
        enabled=False,
    )
    assert updated.content_hash != old_hash
    packages = service.compile_prompt_packages(project.id, plan.id)
    assert len(packages) == 4
    assert all(detail.id not in package.product_reference_ids for package in packages)
    assert any(detail.id in package.annotation_preview_ids for package in packages)
    # Unlabelled details are not silently used for a shot that requires a
    # specific fact; the capability-policy test below covers a matching detail.
    assert any(not package.detail_reference_ids for package in packages)
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
    accepted = service.accept_candidate(project.id, record.candidates[0].id, "tester")
    rejected = service.reject_candidate(project.id, record.candidates[1].id, "Wrong styling")
    assert accepted.status == CandidateStatus.ACCEPTED
    assert rejected.status == CandidateStatus.REJECTED


def test_temu_prompt_avoids_restricted_compliance_lexicon(temp_config) -> None:
    service = StudioService(temp_config)
    project, *_ = _ready_project(service)
    plan = service.compile_shot_plan(project.id)
    packages = service.compile_prompt_packages(project.id, plan.id)
    restricted = {
        "tactical",
        "military",
        "combat",
        "army",
        "soldier",
        "uniform",
        "gear",
        "protection",
        "weapon",
        "national flag",
        "rank",
        "helmet",
        "battlefield",
    }
    for package in packages:
        compiled = " ".join(
            [
                package.rendered_prompt,
                package.negative_prompt,
                *package.structured_style_rules,
            ]
        ).lower()
        assert all(term not in compiled for term in restricted)


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
        service.create_generation_job(
            project.id,
            plan.id,
            mode="live",
            provider="apiyi",
            paid_confirmation=True,
            budget_policy=BudgetPolicy(job_limit=1),
        )


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


def test_generation_rejects_duplicate_queued_request_and_requires_an_enabled_shot(temp_config) -> None:
    service = StudioService(temp_config)
    project, *_ = _ready_project(service)
    plan = service.compile_shot_plan(project.id)
    service.compile_prompt_packages(project.id, plan.id)
    service.confirm_shot_plan(project.id, plan.id, "tester")
    service.create_generation_job(project.id, plan.id)
    with pytest.raises(ValueError, match="already queued"):
        service.create_generation_job(project.id, plan.id)
    replacement = service.compile_shot_plan(project.id)
    for shot in replacement.shots:
        service.update_single_shot(
            project.id,
            replacement.id,
            shot.id,
            sequence=shot.sequence,
            composition=shot.composition,
            user_instruction=shot.user_instruction,
            enabled=False,
        )
    service.confirm_shot_plan(project.id, replacement.id, "tester")
    with pytest.raises(ValueError, match="Enable at least one Shot"):
        service.create_generation_job(project.id, replacement.id)


def test_candidate_output_uses_decoded_format_and_safe_error_redacts_credentials(temp_config) -> None:
    service = StudioService(temp_config)
    project, *_ = _ready_project(service)
    plan = service.compile_shot_plan(project.id)
    service.compile_prompt_packages(project.id, plan.id)
    service.confirm_shot_plan(project.id, plan.id, "tester")
    job = service.create_generation_job(project.id, plan.id, shot_id=plan.shots[0].id)
    record = service.get_record(project.id)
    attempt = next(item for item in record.generation_attempts if item.job_id == job.id)
    image = Image.new("RGB", (200, 300), "purple")
    output = BytesIO()
    image.save(output, format="WEBP")
    with pytest.raises(ValueError, match="not a valid safe image"):
        service._persist_candidate(record, project.id, plan.shots[0], attempt, b"not an image")
    service._persist_candidate(record, project.id, plan.shots[0], attempt, output.getvalue())
    candidate = record.candidates[-1]
    assert candidate.mime_type == "image/webp"
    assert candidate.stored_path.endswith(".webp")
    _, message = safe_error(RuntimeError("Authorization: Bearer secret API_KEY=also-secret"))
    assert "secret" not in message


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
    page_csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    rejected = client.post(
        f"/studio/{project.id}/candidates/{candidate_id}/reject",
        data={"reason": "regenerate through the explicit web flow", "csrf_token": page_csrf},
        follow_redirects=False,
    )
    assert rejected.status_code == 303
    refreshed = client.get(f"/studio/{project.id}/generation")
    refreshed_csrf = re.search(r'name="csrf_token" value="([^"]+)"', refreshed.text).group(1)
    regenerated = client.post(
        f"/studio/{project.id}/candidates/{candidate_id}/regenerate/mock",
        data={"confirm_regeneration": "true", "csrf_token": refreshed_csrf},
        follow_redirects=False,
    )
    assert regenerated.status_code == 303
    assert len(service.get_record(project.id).candidates) == 2


def _confirmed_plan(service: StudioService):
    project, *_ = _ready_project(service)
    plan = service.compile_shot_plan(project.id)
    service.compile_prompt_packages(project.id, plan.id)
    service.confirm_shot_plan(project.id, plan.id, "tester")
    return project, plan


def test_queued_recovery_is_durable_and_running_recovery_is_interrupted(temp_config) -> None:
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    queued = service.create_generation_job(project.id, plan.id, shot_id=plan.shots[0].id)
    assert (project.id, queued.id) in service.recover_pending_mock_jobs()
    completed = service.resume_generation_job(project.id, queued.id)
    assert completed.status == GenerationStatus.SUCCEEDED

    running = service.create_generation_job(
        project.id, plan.id, shot_id=plan.shots[1].id
    )
    with service.store.lock(project.id):
        record = service.get_record(project.id)
        attempt = next(item for item in record.generation_attempts if item.job_id == running.id)
        attempt.status = GenerationStatus.RUNNING
        record.generation_jobs[-1].status = GenerationStatus.RUNNING
        service.store.save(record)
    assert service.recover_interrupted_jobs(project.id) == 1
    record = service.get_record(project.id)
    recovered = next(item for item in record.generation_attempts if item.job_id == running.id)
    assert recovered.status == GenerationStatus.INTERRUPTED
    assert next(item for item in record.generation_jobs if item.id == running.id).status == GenerationStatus.INTERRUPTED


def test_stale_queued_attempt_is_never_dispatched(temp_config) -> None:
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    job = service.create_generation_job(project.id, plan.id, shot_id=plan.shots[0].id)
    service.import_asset(project.id, "stales-plan.png", _image_bytes("black"), 5_000_000)
    final = service.run_generation_job(project.id, job.id)
    attempt = next(item for item in service.get_record(project.id).generation_attempts if item.job_id == job.id)
    assert attempt.status == GenerationStatus.FAILED
    assert attempt.error_code == "stale_prompt"
    assert final.status == GenerationStatus.FAILED
    assert not service.get_record(project.id).candidates


def test_two_runners_claim_each_attempt_once_and_do_not_finish_while_active(temp_config, monkeypatch) -> None:
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    job = service.create_generation_job(project.id, plan.id)
    from src.studio.generation import MockImageGenerationProvider

    original_generate = MockImageGenerationProvider.generate
    barrier = threading.Barrier(2)
    generate_calls = 0
    call_lock = threading.Lock()

    def delayed_generate(*args, **kwargs):
        nonlocal generate_calls
        with call_lock:
            generate_calls += 1
            should_wait = generate_calls <= 2
        if should_wait:
            barrier.wait(timeout=5)
        return original_generate(*args, **kwargs)

    monkeypatch.setattr(MockImageGenerationProvider, "generate", delayed_generate)
    workers = [threading.Thread(target=service.run_generation_job, args=(project.id, job.id)) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert not worker.is_alive()
    record = service.get_record(project.id)
    attempts = [item for item in record.generation_attempts if item.job_id == job.id]
    assert [item.status for item in attempts].count(GenerationStatus.SUCCEEDED) == len(plan.shots)
    assert len([item for item in record.candidates if item.attempt_id in {attempt.id for attempt in attempts}]) == len(attempts)
    assert next(item for item in record.generation_jobs if item.id == job.id).status == GenerationStatus.SUCCEEDED

    later = service.create_generation_job(project.id, plan.id, shot_id=plan.shots[0].id, manual_regeneration=True, confirmed_by="tester")
    with service.store.lock(project.id):
        record = service.get_record(project.id)
        attempt = next(item for item in record.generation_attempts if item.job_id == later.id)
        attempt.status = GenerationStatus.RUNNING
        service.store.save(record)
    assert service.run_generation_job(project.id, later.id).status == GenerationStatus.RUNNING


def test_concurrent_single_shot_updates_preserve_both_edits(temp_config) -> None:
    service = StudioService(temp_config)
    project, *_ = _ready_project(service)
    plan = service.compile_shot_plan(project.id)
    first, second = plan.shots[:2]
    barrier = threading.Barrier(2)
    failures: list[Exception] = []

    def edit(shot_id: str, composition: str) -> None:
        try:
            barrier.wait(timeout=5)
            service.update_single_shot(
                project.id, plan.id, shot_id, sequence=1 if shot_id == first.id else 2,
                composition=composition, user_instruction="specific", enabled=True,
            )
        except (KeyError, ValueError) as exc:  # pragma: no cover - assertion below exposes failures
            failures.append(exc)

    workers = [
        threading.Thread(target=edit, args=(first.id, "first concurrent composition")),
        threading.Thread(target=edit, args=(second.id, "second concurrent composition")),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    assert not failures
    final = service.get_record(project.id)
    shots = {shot.id: shot for shot in final.shot_plans[0].shots}
    assert shots[first.id].composition == "first concurrent composition"
    assert shots[second.id].composition == "second concurrent composition"
    assert sorted(shot.sequence for shot in shots.values()) == [1, 2, 3, 4, 5]


def test_duplicate_click_is_blocked_and_explicit_regeneration_is_audited(temp_config) -> None:
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    results: list[object] = []
    barrier = threading.Barrier(2)

    def submit() -> None:
        try:
            barrier.wait(timeout=5)
            results.append(service.create_generation_job(project.id, plan.id, shot_id=plan.shots[0].id))
        except ValueError as exc:
            results.append(exc)

    workers = [threading.Thread(target=submit), threading.Thread(target=submit)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    assert sum(not isinstance(result, Exception) for result in results) == 1
    job = next(result for result in results if not isinstance(result, Exception))
    assert service.run_generation_job(project.id, job.id).status == GenerationStatus.SUCCEEDED
    candidate = service.get_record(project.id).candidates[0]
    service.reject_candidate(project.id, candidate.id, "Needs a new mock candidate")
    with pytest.raises(ValueError, match="already queued"):
        service.create_generation_job(project.id, plan.id, shot_id=plan.shots[0].id)
    regenerated = service.create_generation_job(
        project.id,
        plan.id,
        shot_id=plan.shots[0].id,
        manual_regeneration=True,
        confirmed_by="tester",
        generation_nonce="operator-confirmation-1",
    )
    service.run_generation_job(project.id, regenerated.id)
    attempts = [item for item in service.get_record(project.id).generation_attempts if item.shot_id == plan.shots[0].id]
    assert attempts[-1].generation_intent == "manual_regeneration"
    assert attempts[-1].confirmed_by == "tester"


def test_candidate_metadata_failure_cleans_orphan_and_redacts_matrix(temp_config, monkeypatch) -> None:
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    job = service.create_generation_job(project.id, plan.id, shot_id=plan.shots[0].id)
    original_save = service.store.save
    calls = 0

    def fail_candidate_metadata(record):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("metadata write unavailable")
        return original_save(record)

    monkeypatch.setattr(service.store, "save", fail_candidate_metadata)
    service.run_generation_job(project.id, job.id)
    record = service.get_record(project.id)
    assert not [candidate for candidate in record.candidates if candidate.attempt_id.startswith("")]
    attempt = next(item for item in record.generation_attempts if item.job_id == job.id)
    assert attempt.status == GenerationStatus.FAILED
    assert not list(service.store.project_dir(project.id).glob("generation/candidates/**/*.*"))
    for secret in (
        "Authorization: Bearer secret", "Authorization=secret", '{"Authorization": "Bearer secret"}',
        "api_key=secret", "API-KEY: secret", "?api_key=secret", "token=secret",
        "access_token=secret", "Authorization:\n Bearer secret", "https://user:password@example.test",
    ):
        _, message = safe_error(RuntimeError(secret))
        assert "secret" not in message.lower()
        assert "password" not in message.lower()
        assert len(message) <= 300


def test_reference_capability_policy_prioritizes_product_detail_and_style(temp_config) -> None:
    service = StudioService(temp_config)
    project, front, back, detail = _ready_project(service)
    style, _ = service.import_asset(project.id, "style.png", _image_bytes("yellow"), 5_000_000)
    service.analyze_asset(project.id, style.id, MockAssetAnalyzer())
    service.update_analysis(project.id, style.id, SourceKind.COMPETITOR_REFERENCE, ContentKind.COLLAGE)
    with service.store.lock(project.id):
        record = service.get_record(project.id)
        analysis = next(item for item in record.analyses if item.asset_id == detail.id)
        analysis.detail_regions.append(
            DetailRegion(
                asset_id=detail.id,
                detail_type=OverrideValue(model_value="pocket"),
                importance=OverrideValue(model_value=Importance.HIGH),
                label=OverrideValue(model_value="zip pocket"),
                confidence=1,
                user_confirmed=True,
            )
        )
        service.store.save(record)
    service.compile_product_spec(project.id)
    service.select_style_pack(project.id, service.style_packs()[0].id)
    plan = service.compile_shot_plan(project.id)
    detail_shot = next(shot for shot in plan.shots if shot.required_fact_keys)
    back_shot = next(shot for shot in plan.shots if "back" in shot.shot_type)
    record = service.get_record(project.id)
    for limit in (1, 2, 3, 4):
        capability = ProviderCapability(provider="mock", model="test", max_reference_images=limit)
        selected = select_references(record, detail_shot, capability)
        sent = selected["product"] + selected["detail"] + selected["style"]
        assert len(sent) <= limit
        assert selected["product"]
        if limit >= 2:
            assert detail.id in selected["detail"]
        else:
            assert not selected["detail"]
        if limit >= 3:
            assert style.id in selected["style"]
        assert len(sent) == len(set(sent))
    assert select_references(record, back_shot, ProviderCapability(provider="mock", model="test", max_reference_images=1))["product"] == [back.id]
    assert front.id in select_references(record, plan.shots[0], ProviderCapability(provider="mock", model="test", max_reference_images=1))["product"]


def test_two_authenticated_web_requests_update_different_shots_without_lost_update(temp_config) -> None:
    service = StudioService(temp_config)
    project, *_ = _ready_project(service)
    plan = service.compile_shot_plan(project.id)
    first, second = plan.shots[:2]
    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def post_update(shot_id: str, composition: str, sequence: int) -> None:
        with TestClient(create_app(temp_config)) as client:
            login = client.get("/login")
            login_csrf = re.search(r'name="csrf_token" value="([^"]+)"', login.text).group(1)
            client.post("/login", data={"username": "admin", "password": "test-password", "csrf_token": login_csrf})
            page = client.get(f"/studio/{project.id}/generation")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
            barrier.wait(timeout=5)
            response = client.post(
                f"/studio/{project.id}/plans/{plan.id}/shots/{shot_id}",
                data={
                    "sequence": sequence,
                    "composition": composition,
                    "user_instruction": "web concurrent update",
                    "enabled": "true",
                    "csrf_token": csrf,
                },
                follow_redirects=False,
            )
            statuses.append(response.status_code)

    workers = [
        threading.Thread(target=post_update, args=(first.id, "web first", 1)),
        threading.Thread(target=post_update, args=(second.id, "web second", 2)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert not worker.is_alive()
    assert statuses == [303, 303]
    final = service.get_record(project.id)
    shots = {shot.id: shot for shot in final.shot_plans[0].shots}
    assert shots[first.id].composition == "web first"
    assert shots[second.id].composition == "web second"


def test_live_gate_requires_positive_cost_before_provider_configuration(temp_config, monkeypatch) -> None:
    service = StudioService(temp_config)
    project, plan = _confirmed_plan(service)
    monkeypatch.setenv("LIVE_GENERATION_ENABLED", "true")
    with pytest.raises(ValueError, match="positive max cost"):
        service.create_generation_job(
            project.id,
            plan.id,
            mode="live",
            provider="apiyi",
            paid_confirmation=True,
            budget_policy=BudgetPolicy(job_limit=0),
        )
    with pytest.raises(ValueError, match="NotConfigured"):
        service.create_generation_job(
            project.id,
            plan.id,
            mode="live",
            provider="apiyi",
            paid_confirmation=True,
            budget_policy=BudgetPolicy(job_limit=1),
        )

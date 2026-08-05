"""M2C-B derived export, provenance, acceptance, and scope regressions — offline only."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest
from PIL import Image

from src.studio.exports import fit_pad_recipe, render_fit_pad
from src.studio.generation import default_shots
from src.studio.models import BudgetPolicy, CandidateStatus, StudioPlatform
from src.studio.service import StudioService
from tests.test_studio_m2 import _ready_project


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _front_candidate(service: StudioService):
    project, *_ = _ready_project(service)
    plan = service.compile_shot_plan(project.id)
    service.compile_prompt_packages(project.id, plan.id)
    service.confirm_shot_plan(project.id, plan.id, "reviewer")
    shot = next(item for item in plan.shots if item.shot_type == "temu_model_full_front")
    job = service.create_generation_job(project.id, plan.id, shot_id=shot.id)
    service.run_generation_job(project.id, job.id)
    candidate = next(item for item in service.get_record(project.id).candidates if item.shot_id == shot.id)
    return project, candidate


def test_export_is_independent_idempotent_and_never_auto_accepts(temp_config) -> None:
    service = StudioService(temp_config)
    project, candidate = _front_candidate(service)
    source = service.resolve_candidate_path(project.id, candidate.id)
    source_before, bytes_before = _sha(source), source.read_bytes()

    first = service.create_derived_export(project.id, candidate.id, actor="reviewer")
    second = service.create_derived_export(project.id, candidate.id, actor="reviewer")

    assert first.id == second.id
    assert _sha(source) == source_before and source.read_bytes() == bytes_before
    assert first.source_candidate_sha256 == source_before
    assert first.publishable is False
    assert service.get_record(project.id).candidates[0].status == CandidateStatus.GENERATED
    output = service.resolve_export_path(project.id, first.id)
    with Image.open(output) as image:
        assert image.size == (1350, 1800)
        assert image.mode == "RGB" and image.format == "JPEG"
    manifest = (temp_config.output_dir / first.manifest_path).read_text(encoding="utf-8")
    manifest_payload = json.loads(manifest)
    assert first.sha256 == _sha(output)
    assert "APIYI_API_KEY" not in manifest and "Authorization" not in manifest and "Cookie" not in manifest
    assert manifest_payload["export"]["sha256"] == _sha(output)
    assert '"actual_cost_usd": null' in manifest


def test_human_acceptance_is_required_and_promotes_existing_export_without_duplicate(temp_config) -> None:
    service = StudioService(temp_config)
    project, candidate = _front_candidate(service)
    export = service.create_derived_export(project.id, candidate.id, actor="reviewer")
    with pytest.raises(ValueError, match="Human acceptance actor"):
        service.accept_candidate(project.id, candidate.id)
    accepted = service.accept_candidate(project.id, candidate.id, "human-reviewer")
    promoted = service.create_derived_export(project.id, candidate.id, actor="reviewer")
    assert accepted.accepted_by == "human-reviewer"
    assert promoted.id == export.id
    assert promoted.publishable is True
    assert promoted.acceptance is not None and promoted.acceptance.decided_by == "human-reviewer"
    assert len(service.get_record(project.id).derived_exports) == 1


def test_concurrent_duplicate_export_is_single_flight(temp_config) -> None:
    service = StudioService(temp_config)
    project, candidate = _front_candidate(service)
    service.accept_candidate(project.id, candidate.id, "human-reviewer")
    barrier = threading.Barrier(2)
    results: list[object] = []

    def create() -> None:
        try:
            barrier.wait(timeout=5)
            results.append(service.create_derived_export(project.id, candidate.id, actor="reviewer"))
        except (KeyError, OSError, RuntimeError, ValueError) as exc:  # pragma: no cover - assertion exposes failures
            results.append(exc)

    workers = [threading.Thread(target=create), threading.Thread(target=create)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    assert all(not isinstance(item, Exception) for item in results)
    assert len({item.id for item in results}) == 1
    assert len(service.get_record(project.id).derived_exports) == 1


def test_resize_padding_recipe_is_exact_and_does_not_modify_source(tmp_path: Path) -> None:
    source, output = tmp_path / "source.png", tmp_path / "derived.jpg"
    Image.new("RGB", (1792, 2400), "navy").save(source, format="PNG")
    before = source.read_bytes()
    transforms = fit_pad_recipe(1792, 2400, 1350, 1800)
    render_fit_pad(source, output, transforms)
    assert source.read_bytes() == before
    assert transforms[0].parameters["width"] == 1344
    assert [item.operation for item in transforms] == ["resize_fit", "pad_canvas", "convert_color_mode", "encode"]
    assert transforms[1].parameters == {"width": 1350, "height": 1800, "background": "#ffffff", "left": 3, "right": 3, "top": 0, "bottom": 0}
    with Image.open(output) as image:
        assert image.size == (1350, 1800) and image.mode == "RGB" and image.format == "JPEG"


def test_existing_accepted_export_can_be_backfilled_without_fabricating_decision_time(temp_config) -> None:
    service = StudioService(temp_config)
    project, candidate = _front_candidate(service)
    service.accept_candidate(project.id, candidate.id, "human-reviewer")
    created = service.create_derived_export(project.id, candidate.id, actor="reviewer")
    legacy_relative = f"legacy/{Path(created.stored_path).name}"
    legacy = temp_config.output_dir / legacy_relative
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(service.resolve_export_path(project.id, created.id).read_bytes())
    with service.store.lock(project.id):
        record = service.get_record(project.id)
        record.derived_exports = []
        service.store.save(record)
    migrated = service.backfill_derived_export(
        project.id, candidate.id, output_relative_path=legacy_relative, actor="migration",
        acceptance_source="migrated_existing_explicit_user_acceptance",
    )
    assert migrated.publishable is True
    assert migrated.acceptance is not None
    assert migrated.acceptance.decided_at is None
    assert migrated.acceptance.source == "migrated_existing_explicit_user_acceptance"
    assert migrated.actual_cost_usd is None


def test_schema_v2_record_loads_with_empty_derived_exports_and_upgrades_on_save(temp_config) -> None:
    service = StudioService(temp_config)
    project = service.create_project("Legacy studio")
    record_path = service.store.record_path(project.id)
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 2
    payload.pop("derived_exports", None)
    record_path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = service.get_record(project.id)
    assert loaded.schema_version == 3 and loaded.derived_exports == []
    service.store.save(loaded)
    assert json.loads(record_path.read_text(encoding="utf-8"))["schema_version"] == 3


def test_full_body_failure_constraints_are_scoped_to_temu_front_white_background(temp_config) -> None:
    service = StudioService(temp_config)
    temu_pack = service.style_packs(StudioPlatform.TEMU)[0]
    tiktok_pack = service.style_packs(StudioPlatform.TIKTOK_SHOP)[0]
    temu = default_shots(StudioPlatform.TEMU, temu_pack)
    tiktok = default_shots(StudioPlatform.TIKTOK_SHOP, tiktok_pack)
    scoped = next(item for item in temu if item.shot_type == "temu_model_full_front")
    assert scoped.scene == "white_background_full_body_model"
    compiled = " ".join([scoped.composition, *scoped.forbidden_elements]).lower()
    for term in ("head to soles", "both shoes completely visible", "white margin below feet", "body not touching canvas edges", "cropped feet", "cut-off shoes", "missing shoes", "tight bottom crop", "feet outside frame"):
        assert term in compiled
    for shot in [item for item in temu if item.id != scoped.id] + tiktok:
        unrelated = " ".join([shot.composition, *shot.forbidden_elements]).lower()
        assert shot.scene != "white_background_full_body_model"
        assert "cropped feet" not in unrelated and "both shoes completely visible" not in unrelated


def test_live_generation_fails_closed_without_creating_an_attempt(temp_config, monkeypatch) -> None:
    monkeypatch.setenv("LIVE_GENERATION_ENABLED", "false")
    service = StudioService(temp_config)
    project, _candidate = _front_candidate(service)
    record = service.get_record(project.id)
    attempt_count = len(record.generation_attempts)
    plan_id = record.shot_plans[0].id
    with pytest.raises(ValueError, match="LIVE_GENERATION_ENABLED=false"):
        service.create_generation_job(
            project.id, plan_id, mode="live",
            budget_policy=BudgetPolicy(project_limit=1, job_limit=1, shot_limit=1),
        )
    assert len(service.get_record(project.id).generation_attempts) == attempt_count

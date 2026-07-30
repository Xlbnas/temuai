from __future__ import annotations

from pathlib import Path

import pytest

from src.core.config import AppConfig
from src.core.models import TaskStatus
from src.core.pipeline import Pipeline
from src.utils.paths import safe_filename


def test_validate_success(temp_config: AppConfig, sample_sku: str) -> None:
    pipeline = Pipeline(temp_config, live=False)
    result = pipeline.validate(sample_sku, "temu")
    assert result["valid"] is True
    assert result["errors"] == []


def test_validate_missing_sku(temp_config: AppConfig) -> None:
    pipeline = Pipeline(temp_config, live=False)
    result = pipeline.validate("NONEXISTENT", "temu")
    assert result["valid"] is False
    assert len(result["errors"]) > 0


def test_build_dry_run(temp_config: AppConfig, sample_sku: str) -> None:
    pipeline = Pipeline(temp_config, live=False)
    manifest = pipeline.build(sample_sku, "temu")
    assert manifest.sku == sample_sku
    assert manifest.platform == "temu"
    assert len(manifest.tasks) > 0
    # Deterministic tasks should have no cost
    for task in manifest.tasks:
        if task.task_id in ("01_main", "08_size_guide"):
            assert task.estimated_cost_usd == 0.0


def test_generate_dry_run(temp_config: AppConfig, sample_sku: str) -> None:
    pipeline = Pipeline(temp_config, live=False)
    task = pipeline.run_task(sample_sku, "temu", "02_model_front", count=2)
    assert task.status == TaskStatus.GENERATED
    assert len(task.candidates) == 2
    assert task.estimated_cost_usd == 0.0
    cand_dir = temp_config.output_dir / sample_sku / "temu" / "candidates" / "02_model_front"
    assert (cand_dir / "candidate_001.png").exists()
    assert (cand_dir / "candidate_002.png").exists()


def test_accept_candidate(temp_config: AppConfig, sample_sku: str) -> None:
    pipeline = Pipeline(temp_config, live=False)
    task = pipeline.run_task(sample_sku, "temu", "02_model_front", count=2)
    pipeline.manifest_manager.update_task(sample_sku, "temu", task)
    dest = pipeline.accept_candidate(sample_sku, "temu", "02_model_front", 2)
    assert dest.exists()
    assert dest.name == "02_model_front.png"
    # Other candidates still exist
    cand_dir = temp_config.output_dir / sample_sku / "temu" / "candidates" / "02_model_front"
    assert (cand_dir / "candidate_001.png").exists()


def test_manifest_prompt_hash(temp_config: AppConfig, sample_sku: str) -> None:
    pipeline = Pipeline(temp_config, live=False)
    task = pipeline.run_task(sample_sku, "temu", "02_model_front", count=1)
    assert task.prompt_hash is not None
    assert task.prompt_template == "model_front"
    prompt_path = temp_config.output_dir / sample_sku / "temu" / "metadata" / "prompts" / "02_model_front_attempt_01.txt"
    assert prompt_path.exists()

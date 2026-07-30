from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.config import AppConfig
from src.core.costing import CostGuard
from src.core.ledger import CostLedger


def test_cost_guard_live_disabled(temp_config: AppConfig) -> None:
    guard = CostGuard(temp_config)
    assert guard.is_live_allowed(False) is False
    assert guard.is_live_allowed(True) is False


def test_cost_guard_task_budget_ok(temp_config: AppConfig) -> None:
    guard = CostGuard(temp_config)
    check = guard.check_task_budget("TEST-SKU", "temu", "01_main", 0.05)
    assert check.allowed is True
    assert check.estimated_cost == 0.05


def test_cost_guard_task_budget_exceed(temp_config: AppConfig) -> None:
    guard = CostGuard(temp_config)
    check = guard.check_task_budget("TEST-SKU", "temu", "01_main", 0.50)
    assert check.allowed is False
    assert "exceeds task budget" in check.reason


def test_cost_guard_sku_budget_exceed(temp_config: AppConfig) -> None:
    guard = CostGuard(temp_config)
    # Write fake cost report
    report_dir = temp_config.output_dir / "TEST-SKU" / "temu"
    report_dir.mkdir(parents=True, exist_ok=True)
    report = {"actual_cost_usd": 1.99}
    (report_dir / "cost-report.json").write_text(json.dumps(report), encoding="utf-8")
    check = guard.check_task_budget("TEST-SKU", "temu", "01_main", 0.02)
    assert check.allowed is False
    assert "SKU budget" in check.reason


def test_cost_ledger_record(temp_config: AppConfig) -> None:
    ledger = CostLedger(temp_config.logs_dir)
    record = ledger.record_call(
        sku="TEST-SKU",
        platform="temu",
        task="02_model_front",
        provider="mock",
        model="gemini-3.1-flash-image",
        request_id="req-123",
        attempt=1,
        input_images=["front.png"],
        requested_size="2K",
        aspect_ratio="3:4",
        estimated_cost_usd=0.055,
        actual_cost_usd=0.055,
        status="success",
        accepted=False,
        error=None,
        duration_seconds=1.5,
    )
    assert record.sku == "TEST-SKU"
    assert ledger.ledger_path.exists()
    loaded = ledger.read_all()
    assert len(loaded) == 1
    assert loaded[0].model == "gemini-3.1-flash-image"

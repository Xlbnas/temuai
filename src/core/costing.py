"""Budget and cost protection."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from src.core.config import AppConfig
from src.utils.paths import safe_filename


@dataclass
class BudgetCheck:
    allowed: bool
    estimated_cost: float
    remaining_task_budget: float
    remaining_sku_budget: float
    reason: str = ""


class CostGuard:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.budget = config.budget
        self.max_per_task = float(self.budget.get("max_cost_per_task", 0.30))
        self.max_per_sku = float(self.budget.get("max_cost_per_sku", 2.00))
        self.max_attempts = int(self.budget.get("max_attempts_per_task", 3))
        self.live_api_enabled = bool(self.budget.get("live_api_enabled", False))

    def is_live_allowed(self, explicit_live: bool) -> bool:
        return explicit_live and self.live_api_enabled

    def get_sku_actual_cost(self, sku: str, platform: str) -> float:
        report_path = (
            self.config.output_dir / safe_filename(sku) / safe_filename(platform) / "cost-report.json"
        )
        if not report_path.exists():
            return 0.0
        try:
            data = yaml.safe_load(report_path.read_text(encoding="utf-8")) or {}
            return float(data.get("actual_cost_usd", data.get("estimated_cost_usd", 0.0)))
        except Exception:
            return 0.0

    def check_task_budget(
        self,
        sku: str,
        platform: str,
        task_id: str,
        estimated_cost: float,
        attempts: int = 1,
    ) -> BudgetCheck:
        if estimated_cost <= 0:
            return BudgetCheck(
                allowed=True,
                estimated_cost=0.0,
                remaining_task_budget=self.max_per_task,
                remaining_sku_budget=self.max_per_sku,
            )

        sku_cost = self.get_sku_actual_cost(sku, platform)
        remaining_sku = max(0.0, self.max_per_sku - sku_cost)
        remaining_task = max(0.0, self.max_per_task - estimated_cost)

        if attempts > self.max_attempts:
            return BudgetCheck(
                allowed=False,
                estimated_cost=estimated_cost,
                remaining_task_budget=remaining_task,
                remaining_sku_budget=remaining_sku,
                reason=f"Task attempts {attempts} exceeds max_attempts {self.max_attempts}",
            )

        if estimated_cost > self.max_per_task:
            return BudgetCheck(
                allowed=False,
                estimated_cost=estimated_cost,
                remaining_task_budget=remaining_task,
                remaining_sku_budget=remaining_sku,
                reason=f"Estimated cost ${estimated_cost:.4f} exceeds task budget ${self.max_per_task:.4f}",
            )

        if sku_cost + estimated_cost > self.max_per_sku:
            return BudgetCheck(
                allowed=False,
                estimated_cost=estimated_cost,
                remaining_task_budget=remaining_task,
                remaining_sku_budget=remaining_sku,
                reason=f"Estimated cost would exceed SKU budget ${self.max_per_sku:.4f} (already ${sku_cost:.4f})",
            )

        return BudgetCheck(
            allowed=True,
            estimated_cost=estimated_cost,
            remaining_task_budget=remaining_task,
            remaining_sku_budget=remaining_sku,
        )

    def format_cost_preview(
        self,
        sku: str,
        platform: str,
        task_id: str,
        model: str,
        n: int,
        estimated_cost: float,
    ) -> str:
        return (
            f"Estimated generation:\n"
            f"  SKU: {sku}\n"
            f"  Platform: {platform}\n"
            f"  Task: {task_id}\n"
            f"  Model: {model}\n"
            f"  Count: {n}\n"
            f"  Estimated single cost: ${estimated_cost / max(n, 1):.4f}\n"
            f"  Estimated total cost: ${estimated_cost:.4f}\n"
            f"  Max per task: ${self.max_per_task:.4f}\n"
            f"  Max per SKU: ${self.max_per_sku:.4f}"
        )

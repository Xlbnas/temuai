"""Task routing: select model and fallback chain."""
from __future__ import annotations

from dataclasses import dataclass

from src.core.config import AppConfig
from src.core.models import ModelConfig, RoutingRule


@dataclass
class RoutingDecision:
    task_category: str
    primary: str
    fallback: list[str]
    max_attempts: int
    max_cost_usd: float
    fallback_enabled: bool


class TaskRouter:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.routes = config.routing.get("routes", {})
        self.global_fallback = config.routing.get("global_fallback", {})

    def decide(self, task_category: str) -> RoutingDecision:
        rule = self.routes.get(task_category, {"primary": "nano_banana_2"})
        if isinstance(rule, list):
            primary = rule[0]
            fallback = rule[1:] if len(rule) > 1 else []
        else:
            primary = rule.get("primary", "nano_banana_2")
            fallback = rule.get("fallback", [])

        return RoutingDecision(
            task_category=task_category,
            primary=primary,
            fallback=fallback,
            max_attempts=self.global_fallback.get("max_attempts", 3),
            max_cost_usd=self.global_fallback.get("max_cost_usd", 0.30),
            fallback_enabled=self.global_fallback.get("enabled", True),
        )

    def get_model_config(self, model_name: str) -> ModelConfig:
        raw = self.config.get_model_config(model_name)
        return ModelConfig(name=model_name, **raw)

    def model_chain(self, task_category: str) -> list[ModelConfig]:
        decision = self.decide(task_category)
        names = [decision.primary] + (decision.fallback if decision.fallback_enabled else [])
        configs = []
        for name in names:
            try:
                configs.append(self.get_model_config(name))
            except KeyError:
                continue
        return configs

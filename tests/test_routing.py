from __future__ import annotations

import pytest

from src.core.config import AppConfig
from src.core.routing import TaskRouter


def test_router_default_draft(temp_config: AppConfig) -> None:
    router = TaskRouter(temp_config)
    decision = router.decide("draft")
    assert decision.primary == "nano_banana_lite"
    assert decision.fallback == ["gpt_image_2_vip"]


def test_router_model_front_fallback(temp_config: AppConfig) -> None:
    router = TaskRouter(temp_config)
    decision = router.decide("model_front")
    assert decision.primary == "nano_banana_2"
    assert "gpt_image_2_vip" in decision.fallback
    assert "nano_banana_pro" not in decision.fallback
    assert decision.max_attempts == 3
    assert decision.max_cost_usd == 0.30


def test_router_masked_inpaint(temp_config: AppConfig) -> None:
    router = TaskRouter(temp_config)
    decision = router.decide("masked_inpaint")
    assert decision.primary == "gpt_image_2"


def test_router_premium_final(temp_config: AppConfig) -> None:
    router = TaskRouter(temp_config)
    decision = router.decide("premium_final")
    assert decision.primary == "nano_banana_2"
    assert decision.fallback == ["gpt_image_2"]


def test_router_unknown_category_fallback(temp_config: AppConfig) -> None:
    router = TaskRouter(temp_config)
    decision = router.decide("nonexistent_task")
    assert decision.primary == "nano_banana_2"


def test_router_model_chain(temp_config: AppConfig) -> None:
    router = TaskRouter(temp_config)
    chain = router.model_chain("model_front")
    assert len(chain) >= 2
    assert chain[0].name == "nano_banana_2"

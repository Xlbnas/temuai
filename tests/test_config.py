from __future__ import annotations

import pytest

from src.core.config import AppConfig


def test_load_models_yaml(temp_config: AppConfig) -> None:
    models = temp_config.models.get("models", {})
    # Production model set: lite draft + three main series
    assert "nano_banana_lite" in models
    assert "nano_banana_2" in models
    assert "gpt_image_2_vip" in models
    assert "gpt_image_2" in models
    assert "nano_banana_pro" not in models


def test_nano_banana_2_model_id(temp_config: AppConfig) -> None:
    """Nano Banana 2 must use the GA model id (preview IDs were retired 2026-06-25)."""
    models = temp_config.models.get("models", {})
    nb2 = models["nano_banana_2"]
    assert nb2["provider"] == "apiyi_gemini"
    assert nb2["model"] == "gemini-3.1-flash-image"
    assert "lite" not in nb2["model"]
    assert "preview" not in nb2["model"]


def test_nano_banana_lite_model_id(temp_config: AppConfig) -> None:
    """Nano Banana 2 Lite image model id (not the text/multimodal flash-lite)."""
    models = temp_config.models.get("models", {})
    lite = models["nano_banana_lite"]
    assert lite["provider"] == "apiyi_gemini"
    assert lite["model"] == "gemini-3.1-flash-lite-image"
    assert lite["model"] != "gemini-3.1-flash-lite"
    assert lite["estimated_cost_usd"] == 0.034
    assert lite["role"] == "draft"


def test_nano_banana_lite_only_1k(temp_config: AppConfig) -> None:
    models = temp_config.models.get("models", {})
    lite = models["nano_banana_lite"]
    assert lite["supported_resolutions"] == ["1K"]
    assert lite["default_image_size"] == "1K"


def test_nano_banana_lite_supports_3_4(temp_config: AppConfig) -> None:
    models = temp_config.models.get("models", {})
    lite = models["nano_banana_lite"]
    assert lite["default_aspect_ratio"] == "3:4"
    caps = lite["capabilities"]
    assert caps["text_to_image"] is True
    assert caps["image_edit"] is True
    assert caps["multi_image"] is True


def test_load_routing_yaml(temp_config: AppConfig) -> None:
    routes = temp_config.routing.get("routes", {})
    assert "model_front" in routes
    assert "masked_inpaint" in routes
    assert "draft" in routes


def test_load_budget_yaml(temp_config: AppConfig) -> None:
    budget = temp_config.budget
    assert budget["max_cost_per_task"] == 0.30
    assert budget["max_cost_per_sku"] == 2.00
    assert budget["live_api_enabled"] is False


def test_load_platform_temu(temp_config: AppConfig) -> None:
    platform = temp_config.platforms.get("temu")
    assert platform is not None
    assert platform["ratio"] == "3:4"
    assert platform["width"] == 1500
    assert platform["height"] == 2000


def test_load_prompt_templates(temp_config: AppConfig) -> None:
    prompts = temp_config.prompts
    assert "model_front" in prompts
    assert "lifestyle" in prompts
    assert "product_scene" in prompts

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.models import ModelConfig
from src.core.provider import EditRequest, GenerateRequest
from src.providers.mock import MockProvider


def _model_config(name: str = "test_model", caps: dict | None = None) -> ModelConfig:
    return ModelConfig(
        name=name,
        provider="mock",
        model=name,
        capabilities=caps or {
            "text_to_image": True,
            "image_edit": True,
            "multi_image": True,
            "exact_size": True,
            "mask": True,
        },
        estimated_cost_usd=0.01,
    )


def test_mock_provider_generate(temp_config) -> None:
    provider = MockProvider(_model_config())
    request = GenerateRequest(
        prompt="test",
        sku="TEST-SKU",
        task_id="01_main",
        platform="temu",
        n=2,
    )
    result = provider.generate(request, temp_config.cache_dir / "mock")
    assert len(result.images) == 2
    assert all(p.path.exists() for p in result.images)
    assert result.actual_cost_usd == 0.0


def test_mock_provider_edit(temp_config) -> None:
    provider = MockProvider(_model_config())
    request = EditRequest(
        prompt="test",
        sku="TEST-SKU",
        task_id="05_feature_pocket",
        platform="temu",
        reference_images=[temp_config.cache_dir / "ref1.png"],
        n=1,
    )
    result = provider.edit(request, temp_config.cache_dir / "mock")
    assert len(result.images) == 1


def test_capability_gating_text_to_image() -> None:
    provider = MockProvider(_model_config(caps={"text_to_image": False}))
    request = GenerateRequest(prompt="x", sku="s", task_id="t", platform="temu")
    with pytest.raises(ValueError, match="does not support text_to_image"):
        provider.generate(request, Path("/tmp/mock"))


def test_capability_gating_multi_image() -> None:
    provider = MockProvider(_model_config(caps={"text_to_image": True, "image_edit": True, "multi_image": False}))
    request = EditRequest(
        prompt="x",
        sku="s",
        task_id="t",
        platform="temu",
        reference_images=[Path("a.png"), Path("b.png")],
    )
    with pytest.raises(ValueError, match="does not support multi_image"):
        provider.edit(request, Path("/tmp/mock"))


def test_capability_gating_mask() -> None:
    provider = MockProvider(_model_config(caps={"text_to_image": True, "image_edit": True, "mask": False}))
    request = EditRequest(
        prompt="x",
        sku="s",
        task_id="t",
        platform="temu",
        reference_images=[Path("a.png")],
        mask=Path("mask.png"),
    )
    with pytest.raises(ValueError, match="does not support mask"):
        provider.edit(request, Path("/tmp/mock"))

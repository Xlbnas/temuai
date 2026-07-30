"""Tests for unified aspect-ratio + resolution -> exact size mapping.

All expectations come from the APIYI official gpt-image-2-vip size table
(10 ratios x 1K/2K/4K = 30 fixed sizes) and the Nano Banana 2 Lite spec.
"""
from __future__ import annotations

import pytest

from src.core.config import AppConfig
from src.core.models import ModelConfig
from src.core.provider import GenerateRequest
from src.providers.apiyi_gemini import ApiYiGeminiImageProvider
from src.utils.size import resolve_image_size

# APIYI official gpt-image-2-vip 30-size table (docs.apiyi.com, 2026-07)
OFFICIAL_VIP_SIZE_MAP = {
    "1:1": {"1K": "1280x1280", "2K": "2048x2048", "4K": "2880x2880"},
    "2:3": {"1K": "848x1280", "2K": "1360x2048", "4K": "2336x3520"},
    "3:2": {"1K": "1280x848", "2K": "2048x1360", "4K": "3520x2336"},
    "3:4": {"1K": "960x1280", "2K": "1536x2048", "4K": "2480x3312"},
    "4:3": {"1K": "1280x960", "2K": "2048x1536", "4K": "3312x2480"},
    "4:5": {"1K": "1024x1280", "2K": "1632x2048", "4K": "2560x3216"},
    "5:4": {"1K": "1280x1024", "2K": "2048x1632", "4K": "3216x2560"},
    "9:16": {"1K": "720x1280", "2K": "1152x2048", "4K": "2160x3840"},
    "16:9": {"1K": "1280x720", "2K": "2048x1152", "4K": "3840x2160"},
    "21:9": {"1K": "1280x544", "2K": "2048x864", "4K": "3840x1632"},
}
OFFICIAL_VIP_SIZES = {s for levels in OFFICIAL_VIP_SIZE_MAP.values() for s in levels.values()}


def _vip_cfg(temp_config: AppConfig) -> ModelConfig:
    raw = temp_config.get_model_config("gpt_image_2_vip")
    return ModelConfig(name="gpt_image_2_vip", **raw)


def _lite_cfg(temp_config: AppConfig) -> ModelConfig:
    raw = temp_config.get_model_config("nano_banana_lite")
    return ModelConfig(name="nano_banana_lite", **raw)


# ---------------- 30-size table validity ----------------

def test_vip_has_30_supported_sizes(temp_config: AppConfig) -> None:
    cfg = _vip_cfg(temp_config)
    assert len(cfg.supported_sizes) == 30
    assert len(set(cfg.supported_sizes)) == 30


def test_vip_size_map_matches_official_table(temp_config: AppConfig) -> None:
    cfg = _vip_cfg(temp_config)
    assert cfg.size_map == OFFICIAL_VIP_SIZE_MAP
    flattened = {s for levels in cfg.size_map.values() for s in levels.values()}
    assert flattened == set(cfg.supported_sizes) == OFFICIAL_VIP_SIZES


# ---------------- TEMU 3:4 mapping ----------------

def test_temu_3_4_1k(temp_config: AppConfig) -> None:
    assert resolve_image_size(_vip_cfg(temp_config), "3:4", "1K") == "960x1280"


def test_temu_3_4_2k(temp_config: AppConfig) -> None:
    assert resolve_image_size(_vip_cfg(temp_config), "3:4", "2K") == "1536x2048"


def test_temu_3_4_4k(temp_config: AppConfig) -> None:
    assert resolve_image_size(_vip_cfg(temp_config), "3:4", "4K") == "2480x3312"


def test_temu_default_resolution_is_2k(temp_config: AppConfig) -> None:
    """TEMU formal output defaults to 2K; deterministic post-scaling handles 1500x2000."""
    cfg = _vip_cfg(temp_config)
    assert cfg.default_aspect_ratio == "3:4"
    assert cfg.default_resolution == "2K"
    assert resolve_image_size(cfg) == "1536x2048"


def test_1024x1536_is_not_3_4(temp_config: AppConfig) -> None:
    """1024x1536 is a 2:3 size and must never resolve for TEMU's 3:4."""
    cfg = _vip_cfg(temp_config)
    assert "1024x1536" not in cfg.supported_sizes
    for res in ("1K", "2K", "4K"):
        assert resolve_image_size(cfg, "3:4", res) != "1024x1536"
    # 2:3 mapping is a different, official size
    assert resolve_image_size(cfg, "2:3", "2K") == "1360x2048"


def test_resolve_unknown_ratio_raises(temp_config: AppConfig) -> None:
    with pytest.raises(ValueError, match="Aspect ratio"):
        resolve_image_size(_vip_cfg(temp_config), "5:7", "2K")


def test_resolve_unknown_resolution_raises(temp_config: AppConfig) -> None:
    with pytest.raises(ValueError, match="Resolution"):
        resolve_image_size(_vip_cfg(temp_config), "3:4", "8K")


# ---------------- Nano Banana 2 Lite: 1K only ----------------

def test_lite_rejects_non_1k_resolution(temp_config: AppConfig) -> None:
    cfg = _lite_cfg(temp_config)
    provider = ApiYiGeminiImageProvider(cfg, api_key="k", base_url="https://example.com")
    request = GenerateRequest(prompt="x", sku="S", task_id="t", platform="temu", size="2K")
    with pytest.raises(ValueError, match="not supported"):
        provider._resolve_image_size(request)


def test_lite_accepts_1k(temp_config: AppConfig) -> None:
    cfg = _lite_cfg(temp_config)
    provider = ApiYiGeminiImageProvider(cfg, api_key="k", base_url="https://example.com")
    request = GenerateRequest(prompt="x", sku="S", task_id="t", platform="temu")
    assert provider._resolve_image_size(request) == "1K"

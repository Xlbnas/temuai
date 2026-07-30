"""Tests for APIYI providers (no real API calls)."""
from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from src.core.config import AppConfig
from src.core.models import ModelConfig
from src.core.provider import EditRequest, GenerateRequest
from src.providers.apiyi_gemini import ApiYiGeminiImageProvider
from src.providers.apiyi_openai import ApiYiOpenAIImageProvider


def _png_bytes(color: str = "red") -> bytes:
    buf = BytesIO()
    Image.new("RGB", (8, 8), color=color).save(buf, format="PNG")
    return buf.getvalue()


def _gemini_provider() -> ApiYiGeminiImageProvider:
    cfg = ModelConfig(
        name="nano_banana_2",
        provider="apiyi_gemini",
        model="gemini-3.1-flash-image",
        capabilities={"text_to_image": True, "image_edit": True, "multi_image": True},
        default_image_size="2K",
        default_aspect_ratio="3:4",
        estimated_cost_usd=0.055,
    )
    return ApiYiGeminiImageProvider(cfg, api_key="test-key", base_url="https://example.com")


def _vip_provider(temp_config: AppConfig) -> ApiYiOpenAIImageProvider:
    raw = temp_config.get_model_config("gpt_image_2_vip")
    cfg = ModelConfig(name="gpt_image_2_vip", **raw)
    return ApiYiOpenAIImageProvider(cfg, api_key="test-key", base_url="https://example.com")


# ---------------- Gemini / Nano Banana 2 ----------------

def test_gemini_pure_base64_input(tmp_path: Path) -> None:
    """Reference images must be sent as pure base64 with a real mimeType."""
    img = tmp_path / "ref.png"
    raw = _png_bytes()
    img.write_bytes(raw)

    provider = _gemini_provider()
    body = provider._build_request_body("edit this", [img], aspect_ratio="3:4", image_size="2K")

    parts = body["contents"][0]["parts"]
    inline = parts[0]["inlineData"]
    assert inline["mimeType"] == "image/png"
    # Pure base64: no data URI prefix, decodes back to the original bytes
    assert not inline["data"].startswith("data:")
    assert base64.b64decode(inline["data"]) == raw
    assert parts[1] == {"text": "edit this"}


def test_gemini_generation_config(tmp_path: Path) -> None:
    provider = _gemini_provider()
    body = provider._build_request_body("a model photo", aspect_ratio="3:4", image_size="2K")
    gen_cfg = body["generationConfig"]
    assert gen_cfg["responseModalities"] == ["IMAGE"]
    assert gen_cfg["imageConfig"]["aspectRatio"] == "3:4"
    assert gen_cfg["imageConfig"]["imageSize"] == "2K"


def test_gemini_parse_image_response() -> None:
    """Images are read from candidates[].content.parts[].inlineData with mimeType."""
    provider = _gemini_provider()
    raw = _png_bytes("blue")
    response = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "here is your image"},
                        {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(raw).decode()}},
                    ]
                }
            }
        ]
    }
    results = provider._extract_image_data(response)
    assert len(results) == 1
    fmt, data = results[0]
    assert fmt == "png"
    assert data == raw


def test_gemini_parse_text_only_response_raises() -> None:
    provider = _gemini_provider()
    response = {"candidates": [{"content": {"parts": [{"text": "cannot generate"}]}}]}
    with pytest.raises(ValueError, match="no image"):
        provider._extract_image_data(response)


# ---------------- gpt-image-2-vip payload rules ----------------

def test_vip_payload_has_no_quality_no_n(temp_config: AppConfig) -> None:
    """APIYI: vip accepts neither quality nor n — both must be absent."""
    provider = _vip_provider(temp_config)
    request = GenerateRequest(
        prompt="product photo", sku="S", task_id="t", platform="temu",
        aspect_ratio="3:4", size="2K", n=3,
    )
    payload = provider._build_generate_payload(request)
    assert "quality" not in payload
    assert "n" not in payload
    assert payload["model"] == "gpt-image-2-vip"
    assert payload["size"] == "1536x2048"

    edit_payload = provider._build_edit_payload(
        EditRequest(
            prompt="edit", sku="S", task_id="t", platform="temu",
            reference_images=[Path("a.png")], aspect_ratio="1:1", size="1K", n=2,
        )
    )
    assert "quality" not in edit_payload
    assert "n" not in edit_payload
    assert edit_payload["size"] == "1280x1280"


def test_vip_default_size_is_temu_3_4_2k(temp_config: AppConfig) -> None:
    provider = _vip_provider(temp_config)
    request = GenerateRequest(prompt="x", sku="S", task_id="t", platform="temu", aspect_ratio="3:4")
    payload = provider._build_generate_payload(request)
    assert payload["size"] == "1536x2048"


def test_vip_resolution_token_override(temp_config: AppConfig) -> None:
    provider = _vip_provider(temp_config)
    assert provider._gate_size("1K", "3:4") == "960x1280"
    assert provider._gate_size("2K", "3:4") == "1536x2048"
    assert provider._gate_size("4K", "3:4") == "2480x3312"


def test_vip_size_gating_rejects_unsupported(temp_config: AppConfig) -> None:
    provider = _vip_provider(temp_config)
    # 1024x1536 is the old 2:3 default and is NOT in the official 30-size table
    with pytest.raises(ValueError, match="not supported"):
        provider._gate_size("1024x1536", "3:4")
    with pytest.raises(ValueError, match="not supported"):
        provider._gate_size("4096x4096", "1:1")


def test_vip_size_gating_allows_official_sizes(temp_config: AppConfig) -> None:
    provider = _vip_provider(temp_config)
    assert provider._gate_size("960x1280", "3:4") == "960x1280"
    assert provider._gate_size("3840x2160", "16:9") == "3840x2160"


def test_size_ignored_without_exact_size_capability(temp_config: AppConfig) -> None:
    provider = _vip_provider(temp_config)
    provider.model_config.capabilities.exact_size = False
    request = GenerateRequest(prompt="x", sku="S", task_id="t", platform="temu", size="2K")
    payload = provider._build_generate_payload(request)
    assert "size" not in payload


# ---------------- count -> N independent API calls ----------------

def _fake_b64_response() -> dict:
    return {
        "id": "img-fake-1",
        "data": [{"b64_json": base64.b64encode(_png_bytes()).decode()}],
    }


def test_vip_count_3_executes_3_independent_calls(
    temp_config: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--count 3 must run 3 separate API calls; n never enters the body."""
    provider = _vip_provider(temp_config)
    payloads: list[dict] = []

    def fake_call(endpoint: str, payload: dict, timeout: int) -> dict:
        payloads.append(payload)
        return _fake_b64_response()

    monkeypatch.setattr(provider, "_call_post_json", fake_call)
    request = GenerateRequest(
        prompt="product photo", sku="S", task_id="t", platform="temu",
        aspect_ratio="3:4", size="1K", n=3,
    )
    result = provider.generate(request, tmp_path / "out")

    assert len(payloads) == 3
    for payload in payloads:
        assert "n" not in payload
        assert "quality" not in payload
        assert payload["size"] == "960x1280"
    assert len(result.images) == 3
    assert all(img.path.exists() for img in result.images)
    # 3 images x $0.03 uniform price
    assert result.estimated_cost_usd == pytest.approx(0.09)


def test_supports_n_model_sends_single_call(
    temp_config: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gpt-image-2 (supports n) keeps one call with n in the body."""
    raw = temp_config.get_model_config("gpt_image_2")
    cfg = ModelConfig(name="gpt_image_2", **raw)
    provider = ApiYiOpenAIImageProvider(cfg, api_key="k", base_url="https://example.com")
    payloads: list[dict] = []

    def fake_call(endpoint: str, payload: dict, timeout: int) -> dict:
        payloads.append(payload)
        return {
            "id": "img-fake-2",
            "data": [
                {"b64_json": base64.b64encode(_png_bytes()).decode()},
                {"b64_json": base64.b64encode(_png_bytes("green")).decode()},
                {"b64_json": base64.b64encode(_png_bytes("blue")).decode()},
            ],
        }

    monkeypatch.setattr(provider, "_call_post_json", fake_call)
    request = GenerateRequest(
        prompt="x", sku="S", task_id="t", platform="temu", size="1024x1024", n=3
    )
    result = provider.generate(request, tmp_path / "out")
    assert len(payloads) == 1
    assert payloads[0]["n"] == 3
    assert len(result.images) == 3

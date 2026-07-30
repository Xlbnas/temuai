"""Provider registry for instantiating image providers."""
from __future__ import annotations

import os
from typing import Type

from src.core.models import ModelConfig
from src.core.provider import ImageProvider
from src.providers.apiyi_gemini import ApiYiGeminiImageProvider
from src.providers.apiyi_openai import ApiYiOpenAIImageProvider
from src.providers.mock import MockProvider

_PROVIDER_MAP: dict[str, Type[ImageProvider]] = {
    "mock": MockProvider,
    "apiyi_gemini": ApiYiGeminiImageProvider,
    "apiyi_openai": ApiYiOpenAIImageProvider,
}


def register_provider(name: str, cls: Type[ImageProvider]) -> None:
    _PROVIDER_MAP[name] = cls


def get_provider_class(name: str) -> Type[ImageProvider]:
    if name not in _PROVIDER_MAP:
        raise KeyError(f"Unknown provider: {name}")
    return _PROVIDER_MAP[name]


def create_provider(model_config: ModelConfig, force_mock: bool = False) -> ImageProvider:
    if force_mock:
        return MockProvider(model_config)

    provider_name = model_config.provider
    cls = get_provider_class(provider_name)

    if provider_name == "mock":
        return cls(model_config)

    api_key = os.getenv("APIYI_API_KEY", "")
    if not api_key:
        raise RuntimeError(f"Provider {provider_name} requires APIYI_API_KEY environment variable")

    if provider_name == "apiyi_gemini":
        base_url = os.getenv("APIYI_GEMINI_BASE_URL", "https://api.apiyi.com")
    elif provider_name == "apiyi_openai":
        base_url = os.getenv("APIYI_OPENAI_BASE_URL", "https://api.apiyi.com/v1")
    else:
        base_url = os.getenv("APIYI_BACKUP_BASE_URL", "https://b.apiyi.com")

    return cls(model_config, api_key=api_key, base_url=base_url)

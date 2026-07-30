"""TEMU-specific layout utilities."""
from __future__ import annotations

from src.core.config import AppConfig
from src.core.models import PlatformConfig


class TemuLayout:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def get_platform_config(self) -> PlatformConfig:
        return PlatformConfig(**self.config.get_platform_config("temu"))

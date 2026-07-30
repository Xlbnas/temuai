"""Configuration loading for TEMU Image Factory."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.utils.paths import DEFAULT_DIRS, PROJECT_ROOT
from src.utils.secrets import load_env_file, mask_env_value


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@dataclass(frozen=True)
class AppConfig:
    project_root: Path
    input_dir: Path
    output_dir: Path
    cache_dir: Path
    logs_dir: Path
    config_dir: Path
    templates_dir: Path
    data_dir: Path
    models: dict[str, Any]
    routing: dict[str, Any]
    budget: dict[str, Any]
    platforms: dict[str, Any]
    prompts: dict[str, Any]

    @classmethod
    def load(cls, env_file: str | None = None) -> "AppConfig":
        load_env_file(env_file)
        # All config/prompt loading goes through CONFIG_DIR / TEMPLATE_DIR.
        config_dir = Path(os.getenv("CONFIG_DIR", DEFAULT_DIRS["config"]))
        templates_dir = Path(os.getenv("TEMPLATE_DIR", DEFAULT_DIRS["templates"]))
        models = _load_yaml(config_dir / "models.yaml")
        routing = _load_yaml(config_dir / "routing.yaml")
        budget = _load_yaml(config_dir / "budget.yaml")

        platforms: dict[str, Any] = {}
        platform_dir = config_dir / "platforms"
        if platform_dir.exists():
            for p in sorted(platform_dir.glob("*.yaml")):
                data = _load_yaml(p)
                platforms[data.get("platform", p.stem)] = data

        prompts: dict[str, Any] = {}
        prompt_dir = templates_dir / "prompts"
        if prompt_dir.exists():
            for p in sorted(prompt_dir.glob("*.yaml")):
                data = _load_yaml(p)
                prompts[data.get("name", p.stem)] = data

        return cls(
            project_root=PROJECT_ROOT,
            input_dir=Path(os.getenv("INPUT_DIR", DEFAULT_DIRS["input"])),
            output_dir=Path(os.getenv("OUTPUT_DIR", DEFAULT_DIRS["output"])),
            cache_dir=Path(os.getenv("CACHE_DIR", DEFAULT_DIRS["cache"])),
            logs_dir=Path(os.getenv("LOGS_DIR", DEFAULT_DIRS["logs"])),
            config_dir=config_dir,
            templates_dir=templates_dir,
            data_dir=Path(os.getenv("DATA_DIR", DEFAULT_DIRS["data"])),
            models=models,
            routing=routing,
            budget=budget,
            platforms=platforms,
            prompts=prompts,
        )

    def get_model_config(self, name: str) -> dict[str, Any]:
        models = self.models.get("models", {})
        if name not in models:
            raise KeyError(f"Model '{name}' not found in config/models.yaml")
        return models[name]

    def get_route(self, task_category: str) -> dict[str, Any]:
        routes = self.routing.get("routes", {})
        return routes.get(task_category, {"primary": "nano_banana_2"})

    def get_platform_config(self, platform: str) -> dict[str, Any]:
        if platform not in self.platforms:
            raise KeyError(f"Platform '{platform}' not found in config/platforms/")
        return self.platforms[platform]

    def get_prompt_template(self, name: str) -> dict[str, Any]:
        if name not in self.prompts:
            raise KeyError(f"Prompt template '{name}' not found in templates/prompts/")
        return self.prompts[name]

    def safe_env(self, key: str, default: str | None = None) -> str:
        value = os.getenv(key, default)
        return mask_env_value(key, value)


# Lazy singleton
_CONFIG: AppConfig | None = None


def get_config() -> AppConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = AppConfig.load()
    return _CONFIG


def reload_config() -> AppConfig:
    global _CONFIG
    _CONFIG = AppConfig.load()
    return _CONFIG

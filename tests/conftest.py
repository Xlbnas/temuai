from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import yaml

from src.core.config import AppConfig

# Ensure environment is ready before any test module imports FastAPI app
os.environ.setdefault("SESSION_SECRET", "test-secret-key-at-least-32-chars-long-123456")
os.environ.setdefault("APP_USERNAME", "admin")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("MAX_UPLOAD_MB", "5")


@pytest.fixture(scope="session", autouse=True)
def test_env():
    """Set up test environment variables."""
    import argon2

    os.environ.setdefault("TEST_ADMIN_PASSWORD", "test-password")
    os.environ.setdefault(
        "APP_PASSWORD_HASH",
        argon2.PasswordHasher().hash("test-password"),
    )
    yield


@pytest.fixture
def temp_config(tmp_path: Path) -> AppConfig:
    """Create an isolated AppConfig with temp directories and bundled test configs."""
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    cache_dir = tmp_path / "cache"
    logs_dir = tmp_path / "logs"
    config_dir = tmp_path / "config"
    templates_dir = tmp_path / "templates"

    for d in (input_dir, output_dir, cache_dir, logs_dir, config_dir / "platforms", templates_dir / "prompts"):
        d.mkdir(parents=True, exist_ok=True)

    project_root = Path(__file__).parent.parent
    shutil.copy2(project_root / "config" / "models.yaml", config_dir / "models.yaml")
    shutil.copy2(project_root / "config" / "routing.yaml", config_dir / "routing.yaml")
    shutil.copy2(project_root / "config" / "budget.yaml", config_dir / "budget.yaml")
    shutil.copytree(project_root / "config" / "platforms", config_dir / "platforms", dirs_exist_ok=True)
    shutil.copytree(project_root / "templates" / "prompts", templates_dir / "prompts", dirs_exist_ok=True)

    def _load(path: Path) -> dict:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    platforms = {}
    for p in (config_dir / "platforms").glob("*.yaml"):
        data = _load(p)
        platforms[data.get("platform", p.stem)] = data

    prompts = {}
    for p in (templates_dir / "prompts").glob("*.yaml"):
        data = _load(p)
        prompts[data.get("name", p.stem)] = data

    config = AppConfig(
        project_root=tmp_path,
        input_dir=input_dir,
        output_dir=output_dir,
        cache_dir=cache_dir,
        logs_dir=logs_dir,
        config_dir=config_dir,
        templates_dir=templates_dir,
        data_dir=tmp_path / "data",
        models=_load(config_dir / "models.yaml"),
        routing=_load(config_dir / "routing.yaml"),
        budget=_load(config_dir / "budget.yaml"),
        platforms=platforms,
        prompts=prompts,
    )
    return config


@pytest.fixture
def sample_sku(temp_config: AppConfig) -> str:
    """Create a sample SKU with placeholder images in temp config."""
    sku = "TEST-SKU"
    sku_dir = temp_config.input_dir / sku
    originals_dir = sku_dir / "originals"
    originals_dir.mkdir(parents=True, exist_ok=True)

    from PIL import Image, ImageDraw

    for name in ["front", "back", "pocket", "fabric"]:
        img = Image.new("RGB", (800, 1000), color=(220, 220, 220))
        draw = ImageDraw.Draw(img)
        draw.text((100, 100), f"{sku}\n{name}", fill=(80, 80, 80))
        img.save(originals_dir / f"{name}.png")

    product_yaml = f"""sku: {sku}
product:
  category: jacket
  color: black
fabric:
  name: Ripstop Fabric
features:
  - id: pocket
    title: Multiple Utility Pockets
    subtitle: Secure Storage & Easy Access
images:
  front: originals/front.png
  back: originals/back.png
  pocket: originals/pocket.png
  fabric: originals/fabric.png
sizes:
  M:
    chest_cm: 116
    length_cm: 74
"""
    (sku_dir / "product.yaml").write_text(product_yaml, encoding="utf-8")
    return sku

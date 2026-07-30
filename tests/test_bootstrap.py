"""Tests for default config/template bootstrap (Docker first-start seeding)."""
from __future__ import annotations

import shutil
from pathlib import Path

from src.utils.bootstrap import (
    REQUIRED_CONFIG_FILES,
    ensure_config_defaults,
    ensure_template_defaults,
)

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DEFAULTS = PROJECT_ROOT / "config"
TEMPLATE_DEFAULTS = PROJECT_ROOT / "templates"


def test_config_first_init_copies_defaults(tmp_path: Path) -> None:
    target = tmp_path / "config"
    copied = ensure_config_defaults(target, CONFIG_DEFAULTS)
    assert set(copied) == set(REQUIRED_CONFIG_FILES)
    for rel in REQUIRED_CONFIG_FILES:
        assert (target / rel).exists()


def test_config_existing_not_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "config"
    # First init seeds everything
    ensure_config_defaults(target, CONFIG_DEFAULTS)
    # User edits their config
    models_file = target / "models.yaml"
    models_file.write_text("# user customized\nmodels: {}\n", encoding="utf-8")
    # Second init (e.g. after image upgrade) must not clobber user edits
    copied = ensure_config_defaults(target, CONFIG_DEFAULTS)
    assert copied == []
    assert models_file.read_text(encoding="utf-8") == "# user customized\nmodels: {}\n"


def test_config_partial_init_only_missing(tmp_path: Path) -> None:
    target = tmp_path / "config"
    target.mkdir()
    (target / "models.yaml").write_text("# custom\n", encoding="utf-8")
    copied = ensure_config_defaults(target, CONFIG_DEFAULTS)
    assert "models.yaml" not in copied
    assert "routing.yaml" in copied
    assert (target / "models.yaml").read_text(encoding="utf-8") == "# custom\n"


def test_templates_first_init_copies_defaults(tmp_path: Path) -> None:
    target = tmp_path / "templates"
    copied = ensure_template_defaults(target, TEMPLATE_DEFAULTS)
    assert copied
    assert (target / "prompts" / "model_front.yaml").exists()


def test_templates_existing_not_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "templates"
    ensure_template_defaults(target, TEMPLATE_DEFAULTS)
    prompt_file = target / "prompts" / "model_front.yaml"
    prompt_file.write_text("# user prompt v2\n", encoding="utf-8")
    copied = ensure_template_defaults(target, TEMPLATE_DEFAULTS)
    assert copied == []
    assert prompt_file.read_text(encoding="utf-8") == "# user prompt v2\n"

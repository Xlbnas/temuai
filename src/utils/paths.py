"""Path utilities for TEMU Image Factory."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

DEFAULT_DIRS = {
    "input": PROJECT_ROOT / "input",
    "output": PROJECT_ROOT / "output",
    "cache": PROJECT_ROOT / "cache",
    "logs": PROJECT_ROOT / "logs",
    "config": PROJECT_ROOT / "config",
    "templates": PROJECT_ROOT / "templates",
    "data": PROJECT_ROOT / "data",
}


def get_project_root() -> Path:
    return PROJECT_ROOT


def safe_filename(name: str) -> str:
    """Convert arbitrary string to English-safe filename, stripping path traversal."""
    name = name.strip().replace(" ", "_")
    # Remove path traversal and dot segments entirely
    name = name.replace("..", "").replace(".", "")
    name = re.sub(r"[^a-zA-Z0-9_\-]", "", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "unnamed"


def sku_path(sku: str) -> Path:
    return DEFAULT_DIRS["input"] / safe_filename(sku)


def sku_output_path(sku: str, platform: str) -> Path:
    return DEFAULT_DIRS["output"] / safe_filename(sku) / safe_filename(platform)


def sku_cache_path(sku: str, platform: str) -> Path:
    return DEFAULT_DIRS["cache"] / safe_filename(sku) / safe_filename(platform)


def candidate_dir(sku: str, platform: str, task_id: str) -> Path:
    return sku_output_path(sku, platform) / "candidates" / safe_filename(task_id)


def task_output_path(sku: str, platform: str, task_id: str, ext: str = "png") -> Path:
    return sku_output_path(sku, platform) / f"{safe_filename(task_id)}.{ext}"


def resolve_within(base: Path, *parts: str) -> Path:
    """Resolve parts under base, rejecting any path that escapes base.

    Protects against "../" path traversal: the resolved path must be base
    itself or a descendant of base.
    """
    base_resolved = base.resolve()
    candidate = base_resolved.joinpath(*parts).resolve()
    if candidate != base_resolved and base_resolved not in candidate.parents:
        raise ValueError(f"Path escapes allowed directory: {candidate}")
    return candidate

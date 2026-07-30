"""Default config/template bootstrap for persistent volumes.

Used by the Docker entrypoint (and unit tests) to seed a fresh persistent
directory with bundled defaults. NEVER overwrites files that already exist,
so image upgrades cannot clobber user-modified configs or prompt templates.
"""
from __future__ import annotations

import shutil
from pathlib import Path

# Files that must exist in CONFIG_DIR for the app to run.
REQUIRED_CONFIG_FILES = (
    "models.yaml",
    "routing.yaml",
    "budget.yaml",
    "platforms/temu.yaml",
)


def _copy_missing(defaults_dir: Path, target_dir: Path, rel_path: str) -> bool:
    """Copy defaults_dir/rel_path to target_dir/rel_path if missing. Returns True if copied."""
    src = defaults_dir / rel_path
    dest = target_dir / rel_path
    if dest.exists():
        return False
    if not src.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return True


def ensure_config_defaults(config_dir: Path, defaults_dir: Path) -> list[str]:
    """Seed missing required config files from defaults. Returns copied relative paths."""
    copied: list[str] = []
    for rel in REQUIRED_CONFIG_FILES:
        if _copy_missing(defaults_dir, config_dir, rel):
            copied.append(rel)
    return copied


def ensure_template_defaults(templates_dir: Path, defaults_dir: Path) -> list[str]:
    """Seed missing prompt templates from defaults (per-file, never overwrite)."""
    copied: list[str] = []
    src_prompts = defaults_dir / "prompts"
    if not src_prompts.exists():
        return copied
    for src in sorted(src_prompts.glob("*.yaml")):
        rel = f"prompts/{src.name}"
        if _copy_missing(defaults_dir, templates_dir, rel):
            copied.append(rel)
    return copied


def ensure_persistent_dirs(dirs: list[Path]) -> None:
    """Create persistent directories if they do not exist."""
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def main() -> None:
    """CLI entry: seed defaults using CONFIG_DIR/TEMPLATE_DIR-style env vars."""
    import os

    config_dir = Path(os.getenv("CONFIG_DIR", "/app/config"))
    config_defaults = Path(os.getenv("CONFIG_DEFAULTS_DIR", "/app/config-defaults"))
    templates_dir = Path(os.getenv("TEMPLATE_DIR", "/app/templates"))
    template_defaults = Path(os.getenv("TEMPLATE_DEFAULTS_DIR", "/app/templates-defaults"))
    data_dir = Path(os.getenv("DATA_DIR", "/app/data"))

    ensure_persistent_dirs(
        [
            config_dir,
            templates_dir,
            data_dir,
            Path(os.getenv("INPUT_DIR", "/app/input")),
            Path(os.getenv("OUTPUT_DIR", "/app/output")),
            Path(os.getenv("CACHE_DIR", "/app/cache")),
            Path(os.getenv("LOGS_DIR", "/app/logs")),
        ]
    )
    copied_config = ensure_config_defaults(config_dir, config_defaults)
    copied_templates = ensure_template_defaults(templates_dir, template_defaults)
    for rel in copied_config:
        print(f"initialized config: {rel}")
    for rel in copied_templates:
        print(f"initialized template: {rel}")
    if not copied_config and not copied_templates:
        print("config/templates already present, nothing to initialize")


if __name__ == "__main__":
    main()

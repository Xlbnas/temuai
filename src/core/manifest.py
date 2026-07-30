"""Manifest management for output/<SKU>/<platform>/manifest.json."""
from __future__ import annotations

import json
from pathlib import Path

from src.core.models import BuildManifest, TaskManifest
from src.utils.paths import safe_filename


class ManifestManager:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    def manifest_path(self, sku: str, platform: str) -> Path:
        return self.output_dir / safe_filename(sku) / safe_filename(platform) / "manifest.json"

    def load(self, sku: str, platform: str) -> BuildManifest | None:
        path = self.manifest_path(sku, platform)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return BuildManifest(**data)
        except Exception:
            return None

    def save(self, manifest: BuildManifest) -> Path:
        path = self.manifest_path(manifest.sku, manifest.platform)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        return path

    def update_task(self, sku: str, platform: str, task: TaskManifest) -> BuildManifest:
        manifest = self.load(sku, platform)
        if manifest is None:
            from datetime import datetime, timezone

            manifest = BuildManifest(
                sku=sku,
                platform=platform,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        # Replace or append
        manifest.tasks = [t for t in manifest.tasks if t.task_id != task.task_id]
        manifest.tasks.append(task)
        manifest.total_estimated_cost_usd = sum(t.estimated_cost_usd for t in manifest.tasks)
        manifest.total_actual_cost_usd = sum(
            (t.actual_cost_usd or 0.0) for t in manifest.tasks
        )
        self.save(manifest)
        return manifest

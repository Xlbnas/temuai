"""Atomic JSON persistence for independent Studio projects."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from src.studio.models import StudioProject, StudioRecord
from src.utils.paths import resolve_within


class StudioStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "studio"

    def project_dir(self, project_id: str) -> Path:
        return resolve_within(self.root, project_id)

    def record_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "project.json"

    def list_projects(self) -> list[StudioProject]:
        if not self.root.exists():
            return []
        projects: list[StudioProject] = []
        for path in self.root.glob("*/project.json"):
            try:
                projects.append(
                    StudioRecord.model_validate_json(path.read_text(encoding="utf-8")).project
                )
            except (OSError, ValueError):
                continue
        return sorted(projects, key=lambda project: project.updated_at, reverse=True)

    def load(self, project_id: str) -> StudioRecord:
        path = self.record_path(project_id)
        if not path.exists():
            raise KeyError("Studio project not found")
        return StudioRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, record: StudioRecord) -> Path:
        path = self.record_path(record.project.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(path, record.model_dump_json(indent=2))
        return path

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

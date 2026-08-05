"""Atomic JSON persistence for independent Studio projects."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - M1 is supported only on POSIX Docker/NAS hosts.
    fcntl = None  # type: ignore[assignment]

from src.studio.models import StudioProject, StudioRecord
from src.utils.paths import resolve_within


class StudioStore:
    CURRENT_SCHEMA_VERSION = 3

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
                projects.append(self._read_record(path).project)
            except (OSError, ValueError):
                continue
        return sorted(projects, key=lambda project: project.updated_at, reverse=True)

    def project_ids(self) -> list[str]:
        """Return only valid project directory names for startup recovery."""
        if not self.root.exists():
            return []
        return sorted(path.parent.name for path in self.root.glob("*/project.json"))

    def load(self, project_id: str) -> StudioRecord:
        path = self.record_path(project_id)
        if not path.exists():
            raise KeyError("Studio project not found")
        return self._read_record(path)

    @contextmanager
    def lock(self, project_id: str) -> Iterator[None]:
        """Serialize a project's complete read-modify-write operation across workers."""
        if fcntl is None:
            raise RuntimeError("Studio requires POSIX advisory file locking")
        project_dir = self.project_dir(project_id)
        project_dir.mkdir(parents=True, exist_ok=True)
        lock_path = project_dir / ".project.lock"
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

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
            StudioStore._fsync_directory(path.parent)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    @classmethod
    def _read_record(cls, path: Path) -> StudioRecord:
        payload = json.loads(path.read_text(encoding="utf-8"))
        version = payload.get("schema_version")
        if version in {1, 2}:
            # Studio schema upgrades are additive and happen only on the next
            # atomic write, preserving a rollback-friendly JSON migration path.
            payload["schema_version"] = cls.CURRENT_SCHEMA_VERSION
        elif version != cls.CURRENT_SCHEMA_VERSION:
            raise ValueError(f"Unsupported Studio schema version: {version!r}")
        return StudioRecord.model_validate(payload)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """Persist the rename itself, not merely the temporary file contents."""
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

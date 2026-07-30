"""Job data model and lifecycle states."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# States from which a job can no longer transition.
TERMINAL_STATES = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Job(BaseModel):
    """A unit of work: generate `count` candidates for one SKU task.

    Everything the Pipeline needs is captured here, so a future background
    worker can execute jobs without any HTTP request context.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    sku: str
    platform: str = "temu"
    task: str
    model: str | None = None
    count: int = 1
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    estimated_cost_usd: float = 0.0
    actual_cost_usd: float | None = None
    created_at: str = Field(default_factory=_utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    def mark_running(self) -> None:
        if self.is_terminal:
            raise ValueError(f"Cannot start a job in state {self.status}")
        self.status = JobStatus.RUNNING
        self.started_at = _utc_now()

    def mark_completed(self, actual_cost_usd: float | None = None) -> None:
        self.status = JobStatus.COMPLETED
        self.progress = 1.0
        self.actual_cost_usd = actual_cost_usd
        self.finished_at = _utc_now()

    def mark_failed(self, error: str) -> None:
        self.status = JobStatus.FAILED
        self.error = error
        self.finished_at = _utc_now()

    def mark_cancelled(self) -> None:
        if self.is_terminal:
            raise ValueError(f"Cannot cancel a job in state {self.status}")
        self.status = JobStatus.CANCELLED
        self.finished_at = _utc_now()

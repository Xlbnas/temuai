"""Job store interface and synchronous runner.

The store is an interface only: the current implementation is in-memory,
the next iteration will be SQLite at DATA_DIR/app.db. The runner shows how
the Pipeline is invoked from a Job without any HTTP request context.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from src.jobs.models import Job, JobStatus


class JobStore(ABC):
    """Persistence interface for jobs (SQLite implementation comes later)."""

    @abstractmethod
    def add(self, job: Job) -> Job: ...

    @abstractmethod
    def get(self, job_id: str) -> Job | None: ...

    @abstractmethod
    def update(self, job: Job) -> None: ...

    @abstractmethod
    def list(self, sku: str | None = None) -> list[Job]: ...


class InMemoryJobStore(JobStore):
    """Minimal non-persistent store, useful for tests and early wiring."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def add(self, job: Job) -> Job:
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def update(self, job: Job) -> None:
        self._jobs[job.id] = job

    def list(self, sku: str | None = None) -> list[Job]:
        jobs = list(self._jobs.values())
        if sku is not None:
            jobs = [j for j in jobs if j.sku == sku]
        return jobs


def run_job(pipeline, job: Job, store: JobStore | None = None) -> Job:
    """Execute a job synchronously against a Pipeline.

    Works with any Pipeline instance (dry-run or live) and never touches the
    HTTP layer, so the same function can later run inside a background worker.
    """
    job.mark_running()
    if store:
        store.update(job)
    try:
        task_manifest = pipeline.run_task(
            job.sku, job.platform, job.task, model_override=job.model, count=job.count
        )
        if task_manifest.status.value == "failed":
            job.mark_failed(task_manifest.error or "Task failed")
        else:
            job.actual_cost_usd = task_manifest.actual_cost_usd
            job.mark_completed(task_manifest.actual_cost_usd)
    except Exception as e:
        job.mark_failed(str(e))
    if store:
        store.update(job)
    return job

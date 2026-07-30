"""Tests for the Job abstraction (no background worker yet)."""
from __future__ import annotations

import pytest

from src.core.config import AppConfig
from src.core.pipeline import Pipeline
from src.jobs import Job, JobStatus, JobStore
from src.jobs.store import InMemoryJobStore, run_job


def test_job_defaults() -> None:
    job = Job(sku="TEST-SKU", task="02_model_front")
    assert job.id
    assert job.platform == "temu"
    assert job.status == JobStatus.QUEUED
    assert job.progress == 0.0
    assert job.count == 1
    assert job.created_at
    assert job.started_at is None
    assert job.finished_at is None
    assert job.error is None
    assert not job.is_terminal


def test_job_lifecycle_completed() -> None:
    job = Job(sku="S", task="t")
    job.mark_running()
    assert job.status == JobStatus.RUNNING
    assert job.started_at
    job.mark_completed(actual_cost_usd=0.03)
    assert job.status == JobStatus.COMPLETED
    assert job.progress == 1.0
    assert job.actual_cost_usd == 0.03
    assert job.finished_at
    assert job.is_terminal


def test_job_lifecycle_failed() -> None:
    job = Job(sku="S", task="t")
    job.mark_running()
    job.mark_failed("boom")
    assert job.status == JobStatus.FAILED
    assert job.error == "boom"
    assert job.is_terminal


def test_job_cancel_not_allowed_after_terminal() -> None:
    job = Job(sku="S", task="t")
    job.mark_running()
    job.mark_completed()
    with pytest.raises(ValueError):
        job.mark_cancelled()
    with pytest.raises(ValueError):
        job.mark_running()


def test_in_memory_store() -> None:
    store: JobStore = InMemoryJobStore()
    job = store.add(Job(sku="A", task="t"))
    store.add(Job(sku="B", task="t"))
    assert store.get(job.id) is job
    assert store.get("missing") is None
    assert len(store.list()) == 2
    assert len(store.list(sku="A")) == 1
    job.mark_running()
    store.update(job)
    assert store.get(job.id).status == JobStatus.RUNNING


def test_run_job_executes_pipeline(temp_config: AppConfig, sample_sku: str) -> None:
    """A Job drives the Pipeline without any HTTP request context (dry-run)."""
    pipeline = Pipeline(temp_config, live=False)
    store = InMemoryJobStore()
    job = store.add(Job(sku=sample_sku, task="02_model_front", count=1))
    result = run_job(pipeline, job, store)
    assert result.status == JobStatus.COMPLETED
    assert result.started_at and result.finished_at
    assert store.get(job.id).status == JobStatus.COMPLETED


def test_run_job_failure_recorded(temp_config: AppConfig) -> None:
    pipeline = Pipeline(temp_config, live=False)
    job = Job(sku="NO-SUCH-SKU", task="02_model_front")
    result = run_job(pipeline, job)
    assert result.status == JobStatus.FAILED
    assert result.error

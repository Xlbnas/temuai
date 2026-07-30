"""Job abstraction for future asynchronous task execution.

This package deliberately avoids Redis/Celery/RabbitMQ for now. It defines
the Job data model, its lifecycle states, and a minimal store interface so
that a later SQLite-backed worker (app.db under DATA_DIR) can plug in without
changing the Pipeline or the Web layer.

The Pipeline never depends on the HTTP request lifecycle: a Job carries
everything needed to invoke `Pipeline.run_task` from any execution context
(CLI, web request, or a future background worker).
"""
from src.jobs.models import Job, JobStatus
from src.jobs.store import JobStore

__all__ = ["Job", "JobStatus", "JobStore"]

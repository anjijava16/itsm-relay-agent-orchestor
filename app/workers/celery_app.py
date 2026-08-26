"""Celery application.

Queues are split so a burst of large PDF ingests can never starve the
short-lived maintenance jobs.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "itsm",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.tasks.ingestion", "app.workers.tasks.maintenance"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,                 # redeliver if a worker dies mid-parse
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,        # long tasks, fair dispatch
    task_track_started=True,
    task_time_limit=1800,
    task_soft_time_limit=1500,
    result_expires=86400,
    broker_transport_options={"visibility_timeout": 3600},
    task_routes={
        "ingestion.*": {"queue": "ingestion"},
        "maintenance.*": {"queue": "maintenance"},
    },
    beat_schedule={
        "reindex-stale-chunks": {
            "task": "maintenance.reindex_stale_chunks",
            "schedule": crontab(minute="*/15"),
        },
        "detect-problem-candidates": {
            "task": "maintenance.detect_problems",
            "schedule": crontab(hour="*/6", minute=5),
        },
        "expire-dead-jobs": {
            "task": "maintenance.expire_stuck_jobs",
            "schedule": crontab(minute="*/30"),
        },
    },
)

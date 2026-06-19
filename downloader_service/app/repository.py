from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from .models import DownloadJob, JobStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, job: DownloadJob) -> DownloadJob:
        self.session.add(job)
        self.session.commit()
        self.session.refresh(job)
        return job

    def get(self, job_id: str) -> Optional[DownloadJob]:
        return self.session.get(DownloadJob, job_id)

    def list(self, limit: int = 100, offset: int = 0) -> list[DownloadJob]:
        stmt = (
            select(DownloadJob)
            .order_by(DownloadJob.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(self.session.exec(stmt).all())

    def claim_next_runnable(self) -> Optional[DownloadJob]:
        stmt = (
            select(DownloadJob)
            .where(DownloadJob.status == JobStatus.queued)
            .order_by(DownloadJob.created_at.asc())
        )
        job = self.session.exec(stmt).first()
        if not job:
            return None

        job.status = JobStatus.running
        job.updated_at = utcnow()
        self.session.add(job)
        self.session.commit()
        self.session.refresh(job)
        return job

    def mark_completed(
        self,
        job_id: str,
        download_path: str | None = None,
        mixer_import_path: str | None = None,
        manifest_path: str | None = None,
    ) -> Optional[DownloadJob]:
        job = self.get(job_id)
        if not job:
            return None

        job.status = JobStatus.completed
        job.progress = 100.0
        job.download_path = download_path
        job.mixer_import_path = mixer_import_path
        job.manifest_path = manifest_path
        job.updated_at = utcnow()

        self.session.add(job)
        self.session.commit()
        self.session.refresh(job)
        return job

    def mark_failed(self, job_id: str, error_message: str) -> Optional[DownloadJob]:
        job = self.get(job_id)
        if not job:
            return None

        job.attempts += 1
        if job.attempts >= job.max_retries:
            job.status = JobStatus.failed
        else:
            job.status = JobStatus.queued

        job.error_message = error_message
        job.updated_at = utcnow()

        self.session.add(job)
        self.session.commit()
        self.session.refresh(job)
        return job

    def update_progress(self, job_id: str, progress: float) -> Optional[DownloadJob]:
        job = self.get(job_id)
        if not job:
            return None

        job.progress = progress
        job.updated_at = utcnow()
        self.session.add(job)
        self.session.commit()
        self.session.refresh(job)
        return job
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlmodel import Session

from .config import settings
from .db import get_session, init_db
from .models import DownloadJobCreate, DownloadJobRead, JobStatus, QueueStats
from .repository import JobRepository
from .schemas import CreateJobRequest, CreateJobResponse, RetryJobRequest
from .worker import WorkerManager
from .models import DownloadJob, DownloadJobCreate, DownloadJobRead, JobStatus, QueueStats, utcnow
from datetime import datetime, timezone

worker_manager = WorkerManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    worker_manager.start()
    yield
    worker_manager.stop()


app = FastAPI(title=settings.app_name, version='1.0.0', lifespan=lifespan)


@app.get('/health')
def health() -> dict:
    return {'ok': True, 'service': settings.app_name}


@app.post("/jobs", response_model=DownloadJobRead)
def create_job(payload: DownloadJobCreate, session: Session = Depends(get_session)):
    meta = dict(payload.job_metadata)
    meta["generate_lrc"] = bool(payload.generate_synced_lyrics)
    if payload.lyrics_provider:
        meta["lyrics_provider"] = str(payload.lyrics_provider)
    else:
        meta.pop("lyrics_provider", None)
    meta["audio_provider"] = payload.audio_provider
    meta["output_format"] = payload.output_format
    meta["threads"] = int(payload.threads)

    job = DownloadJob(
        source_url=payload.source_url,
        requested_by=payload.requested_by,
        output_subdir=payload.output_subdir,
        mixer_profile=payload.mixer_profile,
        dedupe_key=payload.dedupe_key,
        callback_url=payload.callback_url,
        job_metadata=meta,
        status=JobStatus.queued,
        attempts=0,
        max_retries=settings.max_attempts,
        progress=0.0,
        next_run_at=datetime.now(timezone.utc),
        # next_run_at=utcnow(),
        priority=100,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


@app.get('/jobs', response_model=list[DownloadJobRead])
def list_jobs(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
):
    repo = JobRepository(session)
    return [DownloadJobRead.model_validate(job) for job in repo.list(limit=limit, offset=offset)]


@app.get('/jobs/stats', response_model=QueueStats)
def queue_stats(session: Session = Depends(get_session)):
    repo = JobRepository(session)
    return repo.stats()


@app.get("/jobs/{job_id}", response_model=DownloadJobRead)
def get_job(job_id: str, session: Session = Depends(get_session)):
    job = session.get(DownloadJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post('/jobs/{job_id}/cancel', response_model=DownloadJobRead)
def cancel_job(job_id: int, session: Session = Depends(get_session)):
    repo = JobRepository(session)
    job = repo.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    if job.status == JobStatus.running:
        raise HTTPException(status_code=409, detail='Running jobs cannot be force-canceled by this lightweight worker')
    job.status = JobStatus.canceled
    return DownloadJobRead.model_validate(repo.update(job))


@app.post('/jobs/{job_id}/retry', response_model=DownloadJobRead)
def retry_job(job_id: int, payload: RetryJobRequest, session: Session = Depends(get_session)):
    repo = JobRepository(session)
    job = repo.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    if payload.reset_attempts:
        job.attempt = 0
    job.error_message = None
    job.status = JobStatus.queued
    return DownloadJobRead.model_validate(repo.update(job))

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


@app.post('/jobs', response_model=CreateJobResponse)
def create_job(payload: CreateJobRequest, session: Session = Depends(get_session)):
    repo = JobRepository(session)
    if payload.dedupe_key:
        existing = repo.find_existing(payload.dedupe_key)
        if existing and existing.status in {JobStatus.queued, JobStatus.running, JobStatus.retry_wait, JobStatus.completed}:
            return CreateJobResponse(id=existing.id, status=existing.status.value, message='Reused existing job by dedupe key')

    job = repo.create(
        DownloadJobCreate(
            **payload.model_dump(),
            max_attempts=payload.max_attempts or settings.max_attempts,
        )
    )
    return CreateJobResponse(id=job.id, status=job.status.value, message='Job queued')


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


@app.get('/jobs/{job_id}', response_model=DownloadJobRead)
def get_job(job_id: int, session: Session = Depends(get_session)):
    repo = JobRepository(session)
    job = repo.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    return DownloadJobRead.model_validate(job)


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

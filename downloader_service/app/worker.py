from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from sqlmodel import Session

from .config import settings
from .db import engine
from .downloader import DownloadExecutionError, DownloaderService
from .models import JobStatus
from .repository import JobRepository

logger = logging.getLogger(__name__)


class WorkerManager:
    def __init__(self):
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.downloader = DownloaderService()

    def start(self) -> None:
        if self.threads:
            return
        for index in range(settings.max_workers):
            thread = threading.Thread(target=self._run_loop, name=f'download-worker-{index}', daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=2)
        self.threads.clear()

    def _run_loop(self) -> None:
        while not self.stop_event.is_set():
            claimed = False
            with Session(engine) as session:
                repo = JobRepository(session)
                job = repo.claim_next_runnable()
                if job:
                    claimed = True
                    self._process_job(repo, job)
            if not claimed:
                time.sleep(settings.worker_poll_seconds)

    def _process_job(self, repo: JobRepository, job):
        try:
            files, manifest_path, stdout, stderr = self.downloader.execute(job)
            job.status = JobStatus.completed
            job.progress = 100.0
            job.error_message = None
            job.last_stdout = stdout
            job.last_stderr = stderr
            job.resolved_output_dir = str(self.downloader._safe_job_dir(job).resolve())
            job.manifest_path = manifest_path
            job.finished_at = datetime.now(timezone.utc)
            repo.update(job)
            self.downloader._notify_webhook(job, 'download.completed', files, manifest_path)
        except DownloadExecutionError as exc:
            job.attempt += 1
            job.error_message = str(exc)
            job.finished_at = None
            if job.attempt < job.max_attempts:
                job.status = JobStatus.retry_wait
                job.next_run_at = self.downloader.next_retry_at(job.attempt)
            else:
                job.status = JobStatus.failed
                job.finished_at = datetime.now(timezone.utc)
            repo.update(job)
            try:
                self.downloader._notify_webhook(job, 'download.failed' if job.status == JobStatus.failed else 'download.retry_wait', [], job.manifest_path)
            except Exception:
                logger.exception('Webhook notification failed')
        except Exception as exc:  # pragma: no cover
            logger.exception('Unexpected worker error')
            job.attempt += 1
            job.error_message = f'unexpected error: {exc}'
            job.status = JobStatus.failed
            job.finished_at = datetime.now(timezone.utc)
            repo.update(job)

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from .config import settings
from .models import DownloadJob, JobStatus, MixerImportManifest


class DownloadExecutionError(RuntimeError):
    pass


class DownloaderService:
    def _build_command(self, job: DownloadJob, output_dir: Path) -> list[str]:
        meta = job.job_metadata if isinstance(job.job_metadata, dict) else {}
        audio_provider = meta.get("audio_provider") or settings.default_audio_provider
        threads = int(meta.get("threads", 4))
        output_format = str(meta.get("output_format", "mp3"))
        lyrics_provider = meta.get("lyrics_provider")
        generate_lrc = bool(meta.get("generate_lrc", True))
        extra_args = meta.get("additional_args") or []

        command = [
            "spotdl",
            "download",
            job.source_url,
            "--audio",
            str(audio_provider),
            "--threads",
            str(threads),
            "--output",
            str(output_dir),
            "--format",
            output_format,
        ]
        if lyrics_provider:
            command += ["--lyrics", str(lyrics_provider)]
        if generate_lrc:
            command.append("--generate-lrc")
        if settings.spotify_cookie_file:
            command += ["--cookie-file", settings.spotify_cookie_file]
        if settings.youtube_cookie_file:
            command += ["--ytm-data", settings.youtube_cookie_file]
        if isinstance(extra_args, list):
            command += [str(a) for a in extra_args]
        return command

    def _safe_job_dir(self, job: DownloadJob) -> Path:
        leaf = ''.join(c if c.isalnum() or c in ('-', '_') else '_' for c in job.output_subdir)[:80] or 'default'
        return settings.download_root / leaf / f'job_{job.id}'

    def _collect_files(self, output_dir: Path) -> list[str]:
        if not output_dir.exists():
            return []
        return [str(p.resolve()) for p in output_dir.rglob('*') if p.is_file()]

    def _copy_into_mixer_import(self, job: DownloadJob, files: list[str]) -> list[str]:
        target_dir = settings.mixer_import_root / (job.mixer_profile or 'default') / f'job_{job.id}'
        target_dir.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for src in files:
            src_path = Path(src)
            if src_path.suffix.lower() not in {'.mp3', '.wav', '.m4a', '.flac', '.ogg', '.opus', '.lrc', '.jpg', '.jpeg', '.png'}:
                continue
            dest = target_dir / src_path.name
            shutil.copy2(src_path, dest)
            copied.append(str(dest.resolve()))
        return copied

    def _write_manifest(self, job: DownloadJob, copied_files: list[str]) -> Path:
        manifest_dir = settings.mixer_import_root / (job.mixer_profile or 'default')
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / f'job_{job.id}_manifest.json'
        payload = MixerImportManifest(
            job_id=job.id or 0,
            source_url=job.source_url,
            output_dir=str((settings.mixer_import_root / (job.mixer_profile or 'default') / f'job_{job.id}').resolve()),
            imported_at=datetime.now(timezone.utc),
            files=copied_files,
            job_metadata=job.job_metadata or {},
        )
        manifest_path.write_text(payload.model_dump_json(indent=2), encoding='utf-8')
        return manifest_path

    def _notify_webhook(self, job: DownloadJob, event: str, files: list[str], manifest_path: str | None) -> None:
        webhook_url = job.webhook_url or settings.mixer_webhook_url
        if not webhook_url:
            return
        payload: dict[str, Any] = {
            'event': event,
            'job_id': job.id,
            'status': job.status.value,
            'source_url': job.source_url,
            'files': files,
            'manifest_path': manifest_path,
            'job_metadata': job.job_metadata or {},
        }
        requests.post(webhook_url, json=payload, timeout=20)

    def next_retry_at(self, attempt: int) -> datetime:
        seconds = min(settings.retry_base_seconds * (2 ** max(attempt - 1, 0)), settings.retry_max_seconds)
        return datetime.now(timezone.utc) + timedelta(seconds=seconds)

    def execute(self, job: DownloadJob) -> tuple[list[str], str | None, str, str]:
        output_dir = self._safe_job_dir(job)
        output_dir.mkdir(parents=True, exist_ok=True)
        command = self._build_command(job, output_dir)
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=settings.request_timeout_seconds,
            check=False,
        )
        stdout = completed.stdout[-20000:]
        stderr = completed.stderr[-20000:]
        files = self._collect_files(output_dir)

        if completed.returncode != 0:
            raise DownloadExecutionError(stderr or stdout or f'spotdl exited with code {completed.returncode}')
        if not files:
            raise DownloadExecutionError('spotdl returned success but no output files were found')

        copied_files = self._copy_into_mixer_import(job, files)
        manifest_path = self._write_manifest(job, copied_files)
        return copied_files, str(manifest_path.resolve()), stdout, stderr

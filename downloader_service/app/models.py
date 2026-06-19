from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import Column
from sqlalchemy.dialects.sqlite import JSON as SQLITE_JSON
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    retry_wait = "retry_wait"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class DownloadJobBase(SQLModel):
    source_url: str
    requested_by: str = "user"
    output_subdir: Optional[str] = None
    mixer_profile: Optional[str] = None
    dedupe_key: Optional[str] = None
    callback_url: Optional[str] = None


class DownloadJob(DownloadJobBase, table=True):
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    status: JobStatus = Field(default=JobStatus.queued, index=True)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    next_run_at: datetime = Field(default_factory=utcnow, index=True)
    priority: int = Field(default=100, index=True)

    attempts: int = Field(default=0)
    max_retries: int = Field(default=3)
    progress: float = Field(default=0.0)

    error_message: Optional[str] = None
    download_path: Optional[str] = None
    mixer_import_path: Optional[str] = None
    manifest_path: Optional[str] = None

    job_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(SQLITE_JSON, nullable=False),
    )


class DownloadJobCreate(DownloadJobBase):
    job_metadata: dict[str, Any] = Field(default_factory=dict)
    generate_synced_lyrics: bool = True
    lyrics_provider: Optional[str] = "genius"
    audio_provider: str = "youtube-music"
    output_format: str = "mp3"
    threads: int = Field(default=4, ge=1, le=8)


class DownloadJobRead(DownloadJobBase):
    id: str
    status: JobStatus

    created_at: datetime
    updated_at: datetime

    attempts: int
    max_retries: int
    progress: float

    error_message: Optional[str] = None
    download_path: Optional[str] = None
    mixer_import_path: Optional[str] = None
    manifest_path: Optional[str] = None

    job_metadata: dict[str, Any] = Field(default_factory=dict)


class MixerImportTrack(SQLModel):
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    file_path: str
    duration: Optional[float] = None
    source_url: Optional[str] = None


class MixerImportManifest(SQLModel):
    job_id: str
    mixer_profile: Optional[str] = None
    output_subdir: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    tracks: list[MixerImportTrack] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class QueueStats(SQLModel):
    queued: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    total: int = 0
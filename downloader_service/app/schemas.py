from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class CreateJobRequest(BaseModel):
    source_url: str
    requested_by: str = 'local-user'
    source_type: str = 'spotify'
    output_subdir: str = 'default'
    audio_provider: str = 'youtube-music'
    lyrics_provider: str | None = 'genius'
    generate_synced_lyrics: bool = Field(
        default=True,
        description='When True, spotdl writes timestamped .lrc files next to audio (synced lyrics).',
    )
    threads: int = Field(default=4, ge=1, le=8)
    output_format: str = 'mp3'
    additional_args: list[str] = Field(default_factory=list)
    mixer_profile: str | None = None
    webhook_url: str | None = None
    dedupe_key: str | None = None
    priority: int = 100
    job_metadata: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int | None = Field(default=None, ge=1, le=20)


class CreateJobResponse(BaseModel):
    id: int
    status: str
    message: str


class RetryJobRequest(BaseModel):
    reset_attempts: bool = False


class WebhookPayload(BaseModel):
    event: str
    job_id: int
    status: str
    source_url: str
    files: list[str] = Field(default_factory=list)
    manifest_path: str | None = None
    job_metadata: dict[str, Any] = Field(default_factory=dict)

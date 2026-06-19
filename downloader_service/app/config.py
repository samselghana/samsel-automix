from __future__ import annotations

from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'AutoMix Downloader Service'
    app_host: str = '0.0.0.0'
    app_port: int = 8010
    database_url: str = 'sqlite:///./jobs.db'
    download_root: Path = Path('./downloads')
    temp_root: Path = Path('./temp')
    worker_poll_seconds: float = 1.0
    max_workers: int = 2
    max_attempts: int = 4
    retry_base_seconds: int = 30
    retry_max_seconds: int = 1800
    default_audio_provider: str = 'youtube-music'
    default_output_template: str = '{artist} - {title}.{output-ext}'
    mixer_import_root: Path = Path('./mixer_import')
    spotify_cookie_file: str | None = None
    youtube_cookie_file: str | None = None
    mixer_webhook_url: str | None = None
    request_timeout_seconds: int = 1800
    use_spotdl_cli: bool = True


settings = Settings()
settings.download_root.mkdir(parents=True, exist_ok=True)
settings.temp_root.mkdir(parents=True, exist_ok=True)
settings.mixer_import_root.mkdir(parents=True, exist_ok=True)

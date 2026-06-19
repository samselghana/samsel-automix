# AutoMix Downloader Service

A lightweight download microservice for your mixer using **FastAPI + SQLite + a built-in worker pool**.

It accepts Spotify or other source URLs, queues them, runs `spotdl`, retries with exponential backoff, copies finished media into a mixer import folder, and writes a JSON manifest your mixer can consume.

## Why this design

- **Simple deployment**: no Redis or Celery required.
- **Persistent queue**: jobs are stored in SQLite, so restarts do not lose queued work.
- **Retries**: failed jobs are rescheduled automatically.
- **Mixer integration**: completed files are copied into a dedicated import folder and a manifest is written per job.
- **Webhook support**: your mixer can receive completion or failure callbacks.

## Architecture

1. `POST /jobs` queues a download request.
2. Worker threads claim queued jobs from SQLite.
3. Each worker runs `spotdl download ...` in a subprocess.
4. On success:
   - output files are copied into `mixer_import/<profile>/job_<id>/`
   - `job_<id>_manifest.json` is written
   - optional webhook is sent
5. On failure:
   - the job is rescheduled until `max_attempts` is reached
   - exponential backoff is applied

## Install

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Make sure these commands work in the same shell:

```bash
spotdl --version
ffmpeg -version
```

## Run

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8010 --reload
```

## Example API usage

### Queue a job

```bash
curl -X POST http://127.0.0.1:8010/jobs \
  -H "Content-Type: application/json" \
  -d "{\"source_url\":\"https://open.spotify.com/playlist/2npdvbKcNnqknx0qQz5M6H\",\"requested_by\":\"sam\",\"output_subdir\":\"party_set\",\"mixer_profile\":\"deck_a\",\"dedupe_key\":\"playlist:party_set\"}"
```

### Check a job

```bash
curl http://127.0.0.1:8010/jobs/1
```

### Queue stats

```bash
curl http://127.0.0.1:8010/jobs/stats
```

## Request payload

```json
{
  "source_url": "https://open.spotify.com/playlist/...",
  "requested_by": "sam",
  "source_type": "spotify",
  "output_subdir": "party_set",
  "audio_provider": "youtube-music",
  "lyrics_provider": "genius",
  "generate_lrc": true,
  "threads": 1,
  "output_format": "mp3",
  "additional_args": ["--print-errors"],
  "mixer_profile": "deck_a",
  "webhook_url": "http://127.0.0.1:9000/api/download-events",
  "dedupe_key": "playlist:party_set",
  "priority": 100,
  "metadata": {
    "collection": "wedding",
    "import_to_crates": ["warmup", "afrobeats"]
  },
  "max_attempts": 4
}
```

## Mixer integration contract

When a job completes, a manifest like this is written:

```json
{
  "job_id": 1,
  "source_url": "https://open.spotify.com/playlist/...",
  "output_dir": "C:/path/to/mixer_import/deck_a/job_1",
  "imported_at": "2026-03-17T12:00:00Z",
  "files": [
    "C:/path/to/mixer_import/deck_a/job_1/Artist - Song.mp3",
    "C:/path/to/mixer_import/deck_a/job_1/Artist - Song.lrc"
  ],
  "metadata": {
    "collection": "wedding",
    "import_to_crates": ["warmup", "afrobeats"]
  }
}
```

Your mixer can either:

- watch the `mixer_import` directory for new manifests, or
- expose a webhook endpoint and receive a `download.completed` callback.

## Suggested webhook payload

```json
{
  "event": "download.completed",
  "job_id": 1,
  "status": "completed",
  "source_url": "https://open.spotify.com/playlist/...",
  "files": ["...mp3", "...lrc"],
  "manifest_path": "...job_1_manifest.json",
  "metadata": {
    "collection": "wedding"
  }
}
```

## Windows notes

- Put `spotdl`, `ffmpeg`, and Python on `PATH`.
- Use `threads=1` when Spotify or YouTube is rate-limiting.
- Put your exported cookies file path into `.env` if needed.
- Run the API and your mixer under the same user if both access OneDrive folders.

## Production upgrades

This version is intentionally simple. For heavier scale, upgrade to:

- PostgreSQL instead of SQLite
- Redis + Celery/RQ for distributed workers
- per-track progress parsing from `spotdl` logs
- auth tokens for API calls
- structured logging and Sentry
- file integrity checks and duplicate hashing

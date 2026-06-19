from __future__ import annotations

import csv
import importlib
import json
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Any, Tuple
from urllib.parse import urlparse

# How to use the new parts:

# For a playlist URL:
# pipeline.enqueue_playlist_url("https://www.youtube.com/playlist?list=...", playlist_name="My Playlist")

# For a CSV:
# pipeline.enqueue_csv(r"C:\Users\pc\Downloads\tracks.csv", playlist_name="CSV Import")

# Custom callable:
# genre_model_type="callable",
# genre_model_callable="genre_model:predict_genre",


# Torch checkpoint:
# genre_model_type="torch",
# genre_model_checkpoint=str(base_dir / "models" / "genre_model.pt"),
# genre_labels_path=str(base_dir / "models" / "genre_labels.json"),

try:
    import numpy as np
except Exception:
    np = None

try:
    import librosa
except Exception:
    librosa = None

try:
    import syncedlyrics
except Exception:
    syncedlyrics = None

try:
    import torch
    import torch.nn as nn
except Exception:
    torch = None
    nn = None

try:
    from mutagen.id3 import ID3, APIC, ID3NoHeaderError, TIT2, TPE1, TALB, TCON, TBPM
    from mutagen.mp3 import MP3
except Exception:
    ID3 = None
    APIC = None
    ID3NoHeaderError = Exception
    TIT2 = TPE1 = TALB = TCON = TBPM = None
    MP3 = None


ProgressCallback = Callable[[Dict[str, Any]], None]
GenrePredictor = Callable[[Path], Dict[str, Any]]


@dataclass
class PipelineConfig:
    workspace_dir: Path
    downloads_dir: Path
    library_dir: Path
    temp_dir: Path
    db_path: Path

    yt_dlp_path: str = "yt-dlp"
    ffmpeg_path: str = "ffmpeg"
    max_workers: int = 2
    write_playlist_m3u: bool = True
    organize_pattern: str = "{genre}/{artist}/{album}"
    unknown_genre_name: str = "Unknown"
    unknown_artist_name: str = "Unknown Artist"
    unknown_album_name: str = "Unknown Album"
    default_csv_playlist_name: str = "Imported CSV"

    # Real genre model integration
    genre_model_type: str = "callable"   # callable | torch
    genre_model_callable: str = ""        # e.g. genre_model:predict_genre
    genre_model_checkpoint: str = ""      # e.g. C:/models/genre_model.pt
    genre_labels_path: str = ""           # e.g. C:/models/genre_labels.json
    genre_model_input_seconds: float = 30.0
    genre_model_sample_rate: int = 22050
    genre_model_n_mels: int = 128
    genre_model_device: str = "cpu"


@dataclass
class TrackRecord:
    source_query: str
    title: str = ""
    artist: str = ""
    album: str = ""
    genre: str = ""
    bpm: Optional[float] = None
    duration_sec: Optional[float] = None
    playlist_name: str = ""
    mp3_path: str = ""
    lrc_path: str = ""
    artwork_path: str = ""
    source_url: str = ""
    provider: str = ""
    status: str = "pending"
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


class LibraryDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tracks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_query TEXT,
                    title TEXT,
                    artist TEXT,
                    album TEXT,
                    genre TEXT,
                    bpm REAL,
                    duration_sec REAL,
                    playlist_name TEXT,
                    mp3_path TEXT UNIQUE,
                    lrc_path TEXT,
                    artwork_path TEXT,
                    source_url TEXT,
                    provider TEXT,
                    status TEXT,
                    error TEXT,
                    extra_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    def upsert_track(self, record: TrackRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tracks (
                    source_query, title, artist, album, genre, bpm, duration_sec,
                    playlist_name, mp3_path, lrc_path, artwork_path, source_url,
                    provider, status, error, extra_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mp3_path) DO UPDATE SET
                    source_query=excluded.source_query,
                    title=excluded.title,
                    artist=excluded.artist,
                    album=excluded.album,
                    genre=excluded.genre,
                    bpm=excluded.bpm,
                    duration_sec=excluded.duration_sec,
                    playlist_name=excluded.playlist_name,
                    lrc_path=excluded.lrc_path,
                    artwork_path=excluded.artwork_path,
                    source_url=excluded.source_url,
                    provider=excluded.provider,
                    status=excluded.status,
                    error=excluded.error,
                    extra_json=excluded.extra_json
                """,
                (
                    record.source_query,
                    record.title,
                    record.artist,
                    record.album,
                    record.genre,
                    record.bpm,
                    record.duration_sec,
                    record.playlist_name,
                    record.mp3_path,
                    record.lrc_path,
                    record.artwork_path,
                    record.source_url,
                    record.provider,
                    record.status,
                    record.error,
                    json.dumps(record.extra, ensure_ascii=False),
                ),
            )
            conn.commit()


class CallableGenrePredictor:
    def __init__(self, callable_spec: str) -> None:
        if not callable_spec or ":" not in callable_spec:
            raise ValueError("genre_model_callable must look like 'module:function'")
        module_name, func_name = callable_spec.split(":", 1)
        module = importlib.import_module(module_name)
        func = getattr(module, func_name)
        if not callable(func):
            raise TypeError(f"{callable_spec} is not callable")
        self._func = func

    def __call__(self, mp3_path: Path) -> Dict[str, Any]:
        result = self._func(mp3_path)
        if not isinstance(result, dict):
            raise TypeError("Custom genre callable must return a dict")
        return result


class SimpleTorchGenreCNN(nn.Module if nn else object):
    def __init__(self, num_classes: int):
        if nn is None:
            raise RuntimeError("torch is not installed")
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(1, 16, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(16, 32, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.AdaptiveAvgPool2d((8, 8)),
        )
        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.Linear(64 * 8 * 8, 128),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(128, num_classes),
        )

    def forward(self, x):
        x = self.features(x)
        return self.classifier(x)


class TorchGenrePredictor:
    def __init__(self, cfg: PipelineConfig) -> None:
        if torch is None or librosa is None or np is None:
            raise RuntimeError("torch, librosa and numpy are required for torch genre inference")
        if not cfg.genre_model_checkpoint:
            raise ValueError("genre_model_checkpoint is required for torch predictor")
        if not cfg.genre_labels_path:
            raise ValueError("genre_labels_path is required for torch predictor")

        self.cfg = cfg
        self.device = torch.device(cfg.genre_model_device)
        self.labels = self._load_labels(Path(cfg.genre_labels_path))
        self.model = self._load_model(Path(cfg.genre_model_checkpoint), len(self.labels)).to(self.device)
        self.model.eval()

    @staticmethod
    def _load_labels(labels_path: Path) -> List[str]:
        text = labels_path.read_text(encoding="utf-8")
        data = json.loads(text)
        if isinstance(data, dict):
            # Supports {"0": "Pop", ...}
            return [data[str(i)] for i in range(len(data))]
        if isinstance(data, list):
            return [str(x) for x in data]
        raise ValueError("genre labels file must be a JSON list or dict")

    def _load_model(self, checkpoint_path: Path, num_classes: int):
        checkpoint = torch.load(str(checkpoint_path), map_location=self.device)
        if isinstance(checkpoint, nn.Module):
            return checkpoint

        if isinstance(checkpoint, dict) and "model" in checkpoint and isinstance(checkpoint["model"], nn.Module):
            return checkpoint["model"]

        model = SimpleTorchGenreCNN(num_classes=num_classes)
        state_dict = checkpoint.get("state_dict", checkpoint) if isinstance(checkpoint, dict) else checkpoint
        model.load_state_dict(state_dict)
        return model

    def _build_input(self, mp3_path: Path):
        y, sr = librosa.load(
            str(mp3_path),
            sr=self.cfg.genre_model_sample_rate,
            mono=True,
            duration=self.cfg.genre_model_input_seconds,
        )
        target_len = int(self.cfg.genre_model_input_seconds * self.cfg.genre_model_sample_rate)
        if len(y) < target_len:
            y = np.pad(y, (0, target_len - len(y)))
        else:
            y = y[:target_len]

        mel = librosa.feature.melspectrogram(
            y=y,
            sr=sr,
            n_mels=self.cfg.genre_model_n_mels,
            hop_length=512,
            n_fft=2048,
        )
        mel_db = librosa.power_to_db(mel, ref=np.max)
        mel_norm = (mel_db - mel_db.mean()) / (mel_db.std() + 1e-8)
        tensor = torch.tensor(mel_norm, dtype=torch.float32).unsqueeze(0).unsqueeze(0)
        return tensor.to(self.device)

    def __call__(self, mp3_path: Path) -> Dict[str, Any]:
        with torch.no_grad():
            x = self._build_input(mp3_path)
            logits = self.model(x)
            probs = torch.softmax(logits, dim=1)[0]
            idx = int(torch.argmax(probs).item())
            return {
                "genre": self.labels[idx],
                "confidence": float(probs[idx].item()),
                "all_probs": {self.labels[i]: float(probs[i].item()) for i in range(len(self.labels))},
            }


class AutoMixDownloaderPipeline:
    def __init__(self, config: PipelineConfig, progress_cb: Optional[ProgressCallback] = None) -> None:
        self.config = config
        self.progress_cb = progress_cb or (lambda payload: None)
        self.db = LibraryDB(config.db_path)
        self.job_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.workers: List[threading.Thread] = []
        self.stop_event = threading.Event()
        self.genre_predictor = self._build_genre_predictor()

        for path in [
            self.config.workspace_dir,
            self.config.downloads_dir,
            self.config.library_dir,
            self.config.temp_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def _build_genre_predictor(self) -> Optional[GenrePredictor]:
        try:
            if self.config.genre_model_type == "callable" and self.config.genre_model_callable:
                return CallableGenrePredictor(self.config.genre_model_callable)
            if self.config.genre_model_type == "torch" and self.config.genre_model_checkpoint:
                return TorchGenrePredictor(self.config)
        except Exception as exc:
            self._emit("genre_model_error", error=str(exc))
        return None

    def start(self) -> None:
        if self.workers:
            return
        for _ in range(self.config.max_workers):
            t = threading.Thread(target=self._worker_loop, daemon=True)
            t.start()
            self.workers.append(t)

    def stop(self) -> None:
        self.stop_event.set()

    def enqueue_track(self, query: str, playlist_name: str = "") -> str:
        job_id = f"job-{int(time.time() * 1000)}"
        self.job_queue.put({
            "job_id": job_id,
            "type": "track",
            "source": query,
            "playlist_name": playlist_name,
        })
        self._emit("queued", job_id=job_id, source=query)
        return job_id

    def enqueue_playlist_url(self, playlist_url: str, playlist_name: str = "") -> str:
        job_id = f"job-{int(time.time() * 1000)}"
        self.job_queue.put({
            "job_id": job_id,
            "type": "playlist_url",
            "source": playlist_url,
            "playlist_name": playlist_name,
        })
        self._emit("queued", job_id=job_id, source=playlist_url)
        return job_id

    def enqueue_csv(self, csv_path: str | Path, playlist_name: str = "") -> str:
        job_id = f"job-{int(time.time() * 1000)}"
        self.job_queue.put({
            "job_id": job_id,
            "type": "csv",
            "source": str(csv_path),
            "playlist_name": playlist_name or self.config.default_csv_playlist_name,
        })
        self._emit("queued", job_id=job_id, source=str(csv_path))
        return job_id

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                job = self.job_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._handle_job(job)
            except Exception as exc:
                self._emit("job_error", job_id=job.get("job_id"), error=str(exc))
            finally:
                self.job_queue.task_done()

    def _handle_job(self, job: Dict[str, Any]) -> None:
        job_id = job["job_id"]
        source = job["source"]
        playlist_name = job.get("playlist_name", "")
        self._emit("job_started", job_id=job_id, source=source)

        if job["type"] == "track":
            records = [self._process_downloaded_file(self._download_single(source, job_id), source, playlist_name)]
        elif job["type"] == "playlist_url":
            records = self._process_playlist_url(source, job_id, playlist_name)
        elif job["type"] == "csv":
            records = self._process_csv(source, job_id, playlist_name)
        else:
            raise RuntimeError(f"Unsupported job type: {job['type']}")

        if self.config.write_playlist_m3u and records:
            self._write_playlist_file(records)

        self._emit(
            "job_finished",
            job_id=job_id,
            source=source,
            created=len(records),
            failed=len([r for r in records if r.status == "failed"]),
        )

    def _process_playlist_url(self, playlist_url: str, job_id: str, playlist_name: str) -> List[TrackRecord]:
        before = set(self.config.downloads_dir.rglob("*.mp3"))
        self._run_yt_dlp_download(playlist_url, playlist_mode=True, job_id=job_id)
        after = set(self.config.downloads_dir.rglob("*.mp3"))
        new_files = sorted(list(after - before), key=lambda p: p.stat().st_mtime)
        if not new_files:
            raise RuntimeError("No tracks were created from the playlist URL")
        records = []
        for mp3 in new_files:
            records.append(self._process_downloaded_file(mp3, playlist_url, playlist_name))
        return records

    def _process_csv(self, csv_path: str, job_id: str, playlist_name: str) -> List[TrackRecord]:
        csv_file = Path(csv_path)
        if not csv_file.exists():
            raise FileNotFoundError(f"CSV file not found: {csv_file}")

        queries = self._read_queries_from_csv(csv_file)
        if not queries:
            raise RuntimeError("No valid rows found in CSV")

        records: List[TrackRecord] = []
        for idx, query in enumerate(queries, start=1):
            sub_job_id = f"{job_id}-row-{idx}"
            self._emit("csv_row_started", job_id=job_id, row=idx, query=query)
            try:
                mp3 = self._download_single(query, sub_job_id)
                record = self._process_downloaded_file(mp3, query, playlist_name)
                records.append(record)
            except Exception as exc:
                failed = TrackRecord(
                    source_query=query,
                    playlist_name=playlist_name,
                    status="failed",
                    error=str(exc),
                )
                self.db.upsert_track(failed)
                records.append(failed)
                self._emit("csv_row_failed", job_id=job_id, row=idx, query=query, error=str(exc))
        return records

    def _process_downloaded_file(self, mp3_path: Path, source_query: str, playlist_name: str) -> TrackRecord:
        record = TrackRecord(
            source_query=source_query,
            playlist_name=playlist_name,
            mp3_path=str(mp3_path),
            provider="yt-dlp",
            status="downloaded",
        )
        try:
            self._emit("track_processing", mp3_path=str(mp3_path), source=source_query)
            self._hydrate_metadata(record)
            self._ensure_synced_lyrics(record)
            self._analyze(record)
            self._detect_genre(record)
            self._rewrite_id3_tags(record)
            self._organize(record)
            record.status = "ready"
        except Exception as exc:
            record.status = "failed"
            record.error = str(exc)
        self.db.upsert_track(record)
        self._emit("track_done", record=asdict(record))
        return record

    def _download_single(self, source: str, job_id: str) -> Path:
        before = set(self.config.downloads_dir.rglob("*.mp3"))
        self._run_yt_dlp_download(source, playlist_mode=False, job_id=job_id)
        after = set(self.config.downloads_dir.rglob("*.mp3"))
        new_files = sorted(list(after - before), key=lambda p: p.stat().st_mtime)
        if not new_files:
            raise RuntimeError("No MP3 file was created.")
        return new_files[-1]

    def _run_yt_dlp_download(self, source: str, playlist_mode: bool, job_id: str) -> None:
        query = source if self._looks_like_url(source) else f"ytsearch1:{source}"
        output_pattern = "%(playlist_index|NA)s - %(title)s.%(ext)s" if playlist_mode else "%(title)s.%(ext)s"
        cmd = [
            self.config.yt_dlp_path,
            query,
            "-f", "bestaudio/best",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--embed-thumbnail",
            "--add-metadata",
            "--no-warnings",
            "--yes-playlist" if playlist_mode else "--no-playlist",
            "-o", str(self.config.downloads_dir / output_pattern),
        ]
        self._emit("download_command", job_id=job_id, provider="yt-dlp", cmd=cmd)
        result = self._run_command(cmd)
        if result["returncode"] != 0:
            raise RuntimeError(result["stderr"] or result["stdout"] or "yt-dlp download failed")

    def _read_queries_from_csv(self, csv_path: Path) -> List[str]:
        rows: List[str] = []
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                normalized = {self._normalize_header(h): h for h in reader.fieldnames if h}
                artist_col = self._first_match(normalized, ["artist", "artists", "artist_name", "main_artist"])
                title_col = self._first_match(normalized, ["title", "track", "song", "name", "track_name"])
                query_col = self._first_match(normalized, ["query", "search", "search_query", "url", "link"])

                for row in reader:
                    if query_col and row.get(query_col):
                        q = row.get(query_col, "").strip()
                        if q:
                            rows.append(q)
                            continue
                    artist = row.get(artist_col, "").strip() if artist_col else ""
                    title = row.get(title_col, "").strip() if title_col else ""
                    if artist and title:
                        rows.append(f"{artist} - {title}")
                    elif title:
                        rows.append(title)
                if rows:
                    return rows

        # Fallback for CSVs without headers
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            basic_reader = csv.reader(f)
            for row in basic_reader:
                clean = [c.strip() for c in row if c and c.strip()]
                if not clean:
                    continue
                if len(clean) == 1:
                    rows.append(clean[0])
                else:
                    rows.append(f"{clean[0]} - {clean[1]}")
        return rows

    def _hydrate_metadata(self, record: TrackRecord) -> None:
        mp3_path = Path(record.mp3_path)
        artist_q, title_q = self._split_artist_title(record.source_query)

        record.artist = artist_q or self.config.unknown_artist_name
        record.title = title_q or mp3_path.stem
        record.album = self.config.unknown_album_name
        record.genre = ""
        record.artwork_path = ""
        record.source_url = record.source_query if self._looks_like_url(record.source_query) else ""

        if MP3 is None:
            return

        audio = MP3(str(mp3_path))
        if audio.info:
            record.duration_sec = float(getattr(audio.info, "length", 0.0) or 0.0)

        try:
            id3 = ID3(str(mp3_path))
        except ID3NoHeaderError:
            return

        raw_title = self._id3_text(id3, "TIT2")
        raw_artist = self._id3_text(id3, "TPE1")
        raw_album = self._id3_text(id3, "TALB")
        raw_genre = self._id3_text(id3, "TCON")

        if raw_title:
            clean_title = self._clean_title(raw_title)
            _, extracted_title = self._split_artist_title(clean_title)
            record.title = extracted_title or clean_title or record.title

        if raw_artist:
            cleaned = self._clean_artist(raw_artist)
            if self._artist_looks_valid(cleaned, artist_q):
                record.artist = cleaned

        if raw_album:
            record.album = raw_album

        if raw_genre and raw_genre.lower() not in {"music", "unknown"}:
            record.genre = raw_genre

        if id3.getall("APIC"):
            art = id3.getall("APIC")[0]
            artwork_path = self.config.temp_dir / f"{self._safe_name(record.artist)} - {self._safe_name(record.title)}.jpg"
            artwork_path.write_bytes(art.data)
            record.artwork_path = str(artwork_path)

    def _ensure_synced_lyrics(self, record: TrackRecord) -> None:
        mp3_path = Path(record.mp3_path)
        lrc_path = mp3_path.with_suffix(".lrc")
        if lrc_path.exists() and lrc_path.stat().st_size > 0:
            record.lrc_path = str(lrc_path)
            return

        if syncedlyrics is None:
            return

        try:
            lyrics = syncedlyrics.search(f"{record.artist} {record.title}", synced_only=True)
        except TypeError:
            lyrics = syncedlyrics.search(f"{record.artist} {record.title}")
        except Exception:
            lyrics = None

        if lyrics and self._looks_like_lrc(lyrics):
            lrc_path.write_text(lyrics, encoding="utf-8", errors="ignore")
            record.lrc_path = str(lrc_path)

    def _analyze(self, record: TrackRecord) -> None:
        if librosa is None or np is None:
            return
        y, sr = librosa.load(record.mp3_path, sr=None, mono=True)
        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        tempo = librosa.feature.tempo(onset_envelope=onset_env, sr=sr)
        if tempo is not None and len(tempo):
            record.bpm = round(float(tempo[0]), 2)

    def _detect_genre(self, record: TrackRecord) -> None:
        if self.genre_predictor:
            try:
                result = self.genre_predictor(Path(record.mp3_path)) or {}
                record.extra["genre_confidence"] = result.get("confidence")
                if result.get("all_probs"):
                    record.extra["genre_probs"] = result.get("all_probs")
                predicted = result.get("genre")
                if predicted and str(predicted).strip().lower() != "unknown":
                    record.genre = str(predicted).strip()
            except Exception as exc:
                record.extra["genre_model_error"] = str(exc)

        if not record.genre or record.genre.lower() in {"unknown", "music"}:
            record.genre = self._genre_heuristic(record)

    def _genre_heuristic(self, record: TrackRecord) -> str:
        text = f"{record.artist} {record.title} {record.album}".lower()
        if any(k in text for k in ["amapiano"]):
            return "Amapiano"
        if any(k in text for k in ["afrobeats", "afrobeat"]):
            return "Afrobeats"
        if any(k in text for k in ["hip hop", "hip-hop", "rap", "trap"]):
            return "Hip-Hop"
        if any(k in text for k in ["house", "deep house", "tech house"]):
            return "House"
        if any(k in text for k in ["pop", "dance pop", "teen pop"]):
            return "Pop"
        if any(k in text for k in ["r&b", "rnb", "soul"]):
            return "R&B"
        if record.bpm is not None:
            if 118 <= record.bpm <= 130:
                return "Pop"
            if 124 <= record.bpm <= 128:
                return "House"
            if record.bpm >= 150:
                return "Dance"
        return self.config.unknown_genre_name

    def _rewrite_id3_tags(self, record: TrackRecord) -> None:
        if ID3 is None:
            return
        mp3_path = Path(record.mp3_path)
        try:
            tags = ID3(str(mp3_path))
        except ID3NoHeaderError:
            tags = ID3()

        if record.title:
            tags.delall("TIT2")
            tags.add(TIT2(encoding=3, text=record.title))
        if record.artist:
            tags.delall("TPE1")
            tags.add(TPE1(encoding=3, text=record.artist))
        if record.album:
            tags.delall("TALB")
            tags.add(TALB(encoding=3, text=record.album))
        if record.genre:
            tags.delall("TCON")
            tags.add(TCON(encoding=3, text=record.genre))
        if record.bpm is not None:
            tags.delall("TBPM")
            tags.add(TBPM(encoding=3, text=str(int(round(record.bpm)))))
        if record.artwork_path and APIC is not None:
            art_path = Path(record.artwork_path)
            if art_path.exists():
                tags.delall("APIC")
                tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=art_path.read_bytes()))
        tags.save(str(mp3_path))

    def _organize(self, record: TrackRecord) -> None:
        src = Path(record.mp3_path)
        genre = self._safe_name(record.genre or self.config.unknown_genre_name)
        artist = self._safe_name(record.artist or self.config.unknown_artist_name)
        album = self._safe_name(record.album or self.config.unknown_album_name)
        rel = self.config.organize_pattern.format(genre=genre, artist=artist, album=album)
        target_dir = self.config.library_dir / rel
        target_dir.mkdir(parents=True, exist_ok=True)

        target_mp3 = target_dir / f"{self._safe_name(record.artist)} - {self._safe_name(record.title)}.mp3"
        if target_mp3.exists():
            target_mp3.unlink()
        shutil.move(str(src), str(target_mp3))
        record.mp3_path = str(target_mp3)

        if record.lrc_path:
            src_lrc = Path(record.lrc_path)
            if src_lrc.exists():
                target_lrc = target_mp3.with_suffix(".lrc")
                if target_lrc.exists():
                    target_lrc.unlink()
                shutil.move(str(src_lrc), str(target_lrc))
                record.lrc_path = str(target_lrc)

        if record.artwork_path:
            src_art = Path(record.artwork_path)
            if src_art.exists():
                target_art = target_mp3.with_suffix(".jpg")
                shutil.copy2(str(src_art), str(target_art))
                record.artwork_path = str(target_art)

    def _write_playlist_file(self, records: List[TrackRecord]) -> None:
        valid = [r for r in records if r.status == "ready"]
        if not valid:
            return
        playlist_name = valid[0].playlist_name.strip() or time.strftime("Playlist_%Y%m%d_%H%M%S")
        m3u_path = self.config.library_dir / f"{self._safe_name(playlist_name)}.m3u8"
        lines = ["#EXTM3U"]
        for r in valid:
            duration = int(r.duration_sec or -1)
            lines.append(f"#EXTINF:{duration},{r.artist} - {r.title}")
            lines.append(r.mp3_path)
        m3u_path.write_text("\n".join(lines), encoding="utf-8")

    def _run_command(self, cmd: List[str]) -> Dict[str, Any]:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        stdout, stderr = proc.communicate()
        return {"returncode": proc.returncode, "stdout": stdout, "stderr": stderr}

    def _emit(self, event: str, **payload: Any) -> None:
        self.progress_cb({"event": event, "ts": time.time(), **payload})

    @staticmethod
    def _normalize_header(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")

    @staticmethod
    def _first_match(normalized_map: Dict[str, str], names: List[str]) -> str:
        for name in names:
            if name in normalized_map:
                return normalized_map[name]
        return ""

    @staticmethod
    def _id3_text(tags: Any, key: str) -> str:
        frame = tags.get(key)
        if not frame:
            return ""
        text = getattr(frame, "text", None)
        if isinstance(text, list):
            return " ".join(str(x) for x in text if x).strip()
        return str(frame).strip()

    @staticmethod
    def _clean_artist(text: str) -> str:
        text = (text or "").strip()
        text = re.sub(r"\s*-\s*topic$", "", text, flags=re.I)
        text = re.sub(r"\s*vevo$", "", text, flags=re.I)
        text = re.sub(r"\s+", " ", text)
        return text.strip(" -") or "Unknown Artist"

    @staticmethod
    def _clean_title(text: str) -> str:
        text = (text or "").strip()
        text = re.sub(r"\s*\(official[^)]*\)", "", text, flags=re.I)
        text = re.sub(r"\s*\[official[^\]]*\]", "", text, flags=re.I)
        text = re.sub(r"\s*\(audio\)", "", text, flags=re.I)
        text = re.sub(r"\s*\(lyrics?\)", "", text, flags=re.I)
        text = re.sub(r"\s*\(video\)", "", text, flags=re.I)
        text = re.sub(r"\s+", " ", text)
        return text.strip(" -")

    def _split_artist_title(self, text: str) -> Tuple[str, str]:
        text = self._clean_title((text or "").strip())
        if " - " in text:
            artist, title = text.split(" - ", 1)
            return self._clean_artist(artist), self._clean_title(title)
        return "", self._clean_title(text)

    @staticmethod
    def _artist_looks_valid(candidate: str, query_artist: str) -> bool:
        if not candidate:
            return False
        bad = {"unknown artist", "music", "various artists"}
        if candidate.lower() in bad:
            return False
        if not query_artist:
            return True
        c = re.sub(r"[^a-z0-9]+", "", candidate.lower())
        q = re.sub(r"[^a-z0-9]+", "", query_artist.lower())
        return c == q or c.startswith(q)

    @staticmethod
    def _safe_name(text: str) -> str:
        text = (text or "").strip()
        text = re.sub(r'[\\/:*?"<>|]+', "_", text)
        text = re.sub(r"\s+", " ", text)
        return text[:180] or "Unknown"

    @staticmethod
    def _looks_like_url(text: str) -> bool:
        try:
            parsed = urlparse(text)
            return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
        except Exception:
            return False

    @staticmethod
    def _looks_like_lrc(text: str) -> bool:
        return bool(re.search(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]", text))


class TkinterPipelineBridge:
    def __init__(self, root: Any, ui_handler: Callable[[Dict[str, Any]], None]) -> None:
        self.root = root
        self.ui_handler = ui_handler

    def emit(self, payload: Dict[str, Any]) -> None:
        self.root.after(0, lambda: self.ui_handler(payload))


class AutoMixTkinterUIAdapter:
    """
    Wire these methods into your existing AutoMix Tkinter UI buttons.

    Expected widgets/callbacks:
    - root: Tk instance
    - log_callback(str): append text to your UI log panel
    - refresh_library_callback(): optional; called when jobs finish
    """

    def __init__(
        self,
        root: Any,
        pipeline: AutoMixDownloaderPipeline,
        log_callback: Callable[[str], None],
        refresh_library_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        self.root = root
        self.pipeline = pipeline
        self.log_callback = log_callback
        self.refresh_library_callback = refresh_library_callback or (lambda: None)
        self.bridge = TkinterPipelineBridge(root, self.on_pipeline_event)
        self.pipeline.progress_cb = self.bridge.emit

    def submit_track_query(self, query: str, playlist_name: str = "Manual Downloads") -> str:
        return self.pipeline.enqueue_track(query.strip(), playlist_name=playlist_name)

    def submit_playlist_url(self, playlist_url: str, playlist_name: str = "Imported Playlist") -> str:
        return self.pipeline.enqueue_playlist_url(playlist_url.strip(), playlist_name=playlist_name)

    def submit_csv(self, csv_path: str, playlist_name: str = "Imported CSV") -> str:
        return self.pipeline.enqueue_csv(csv_path.strip(), playlist_name=playlist_name)

    def on_pipeline_event(self, payload: Dict[str, Any]) -> None:
        event = payload.get("event", "event")
        if event == "track_done":
            record = payload.get("record", {})
            self.log_callback(
                f"[{event}] {record.get('artist', '')} - {record.get('title', '')} | "
                f"Genre: {record.get('genre', '')} | BPM: {record.get('bpm', '')}"
            )
        elif event == "job_finished":
            self.log_callback(
                f"[{event}] created={payload.get('created', 0)} failed={payload.get('failed', 0)}"
            )
            self.refresh_library_callback()
        elif event == "job_error":
            self.log_callback(f"[job_error] {payload.get('error', '')}")
        elif event == "genre_model_error":
            self.log_callback(f"[genre_model_error] {payload.get('error', '')}")
        else:
            self.log_callback(json.dumps(payload, ensure_ascii=False))


class FastAPIPipelineBridge:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def emit(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            self.events.append(payload)
            if len(self.events) > 5000:
                self.events = self.events[-2000:]

    def get_events(self, job_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if not job_id:
                return list(self.events)
            return [e for e in self.events if e.get("job_id") == job_id]


def _print_event(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def build_default_pipeline() -> AutoMixDownloaderPipeline:
    base_dir = Path(__file__).resolve().parent
    workspace_dir = base_dir / "automix_workspace"

    cfg = PipelineConfig(
        workspace_dir=workspace_dir,
        downloads_dir=workspace_dir / "downloads",
        library_dir=workspace_dir / "library",
        temp_dir=workspace_dir / "temp",
        db_path=workspace_dir / "automix_library.db",
        # Choose ONE of the following real model integrations:
        # 1) Custom callable in your own module:
        # genre_model_type="callable",
        # genre_model_callable="genre_model:predict_genre",
        #
        # 2) Torch checkpoint + labels JSON:
        # genre_model_type="torch",
        # genre_model_checkpoint=str(base_dir / "models" / "genre_model.pt"),
        # genre_labels_path=str(base_dir / "models" / "genre_labels.json"),
    )
    return AutoMixDownloaderPipeline(cfg, progress_cb=_print_event)


if __name__ == "__main__":
    pipeline = build_default_pipeline()
    pipeline.start()

    # CSV IMPORT
    pipeline.enqueue_csv(
        r"C:\Users\pc\Downloads\Similar_songs_to_Summer_Skies_(Chosic).csv",
        playlist_name="Summer Skies Playlist"
    )

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pipeline.stop()


    # demo = "Sean Kingston - Eenie Meenie"
    # pipeline.enqueue_track(demo, playlist_name="Demo Downloads")

    # Examples:
    # pipeline.enqueue_playlist_url("https://www.youtube.com/playlist?list=...")
    # pipeline.enqueue_csv("C:/Users/pc/Downloads/tracks.csv", playlist_name="CSV Import")
import os
import re
import csv
import sys
import json
import time
import math
import queue
import shutil
import threading
import subprocess
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any, Tuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Optional imports
try:
    import librosa
except Exception:
    librosa = None

try:
    import numpy as np
except Exception:
    np = None


APP_NAME = "AutoMix Downloader v2"
DEFAULT_OUTPUT = str(Path.home() / "Music" / "AutoMix")
SUPPORTED_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"}


# -----------------------------
# Utility helpers
# -----------------------------
def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ensure_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]+', '_', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name[:220] if len(name) > 220 else name


def run_command(command: List[str], cwd: Optional[str] = None) -> Tuple[int, str, str]:
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out, err = proc.communicate()
    return proc.returncode, out, err


def find_executable(preferred_module: Optional[str], exe_name: str) -> Optional[List[str]]:
    """
    Return a command prefix list to run an executable/module.
    Examples:
      [sys.executable, '-m', 'yt_dlp'] or ['yt-dlp']
    """
    if preferred_module:
        code, out, err = run_command([sys.executable, "-m", preferred_module, "--help"])
        if code == 0 or out or err:
            return [sys.executable, "-m", preferred_module]
    found = shutil.which(exe_name)
    if found:
        return [found]
    return None


def try_json_loads(line: str) -> Optional[dict]:
    try:
        return json.loads(line)
    except Exception:
        return None


def classify_genre_heuristic(bpm: Optional[float], energy: Optional[float], spectral_centroid: Optional[float]) -> str:
    """
    Placeholder heuristic classifier.
    Swap this later with a real ML model.
    """
    if bpm is None:
        return "Unknown"
    if bpm < 85:
        return "Hip-Hop / R&B"
    if 85 <= bpm < 105:
        return "Afrobeats / Pop"
    if 105 <= bpm < 118:
        return "Amapiano / Midtempo"
    if 118 <= bpm < 130:
        return "Pop / Dance"
    if bpm >= 130:
        return "House / EDM"
    return "Unknown"


# -----------------------------
# Data models
# -----------------------------
@dataclass
class AppConfig:
    output_dir: str = DEFAULT_OUTPUT
    audio_format: str = "mp3"
    audio_quality: str = "0"
    embed_thumbnail: bool = True
    add_metadata: bool = True
    fetch_lyrics: bool = True
    detect_bpm: bool = True
    detect_genre: bool = True
    auto_import_library: bool = True
    playlist_subfolders: bool = True
    overwrite_files: bool = False
    concurrent_downloads: int = 1
    ffmpeg_path: str = ""

    @property
    def config_path(self) -> Path:
        return Path.home() / ".automix_downloader_v2.json"

    def save(self) -> None:
        self.config_path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls) -> "AppConfig":
        path = Path.home() / ".automix_downloader_v2.json"
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(**data)
        except Exception:
            return cls()


@dataclass
class Job:
    job_id: str
    source: str
    source_type: str  # single, playlist, csv, folder_scan
    status: str = "Queued"
    progress: float = 0.0
    eta: str = ""
    output_dir: str = ""
    playlist_name: str = ""
    current_item: str = ""
    total_items: int = 0
    completed_items: int = 0
    error: str = ""


# -----------------------------
# Metadata / analysis
# -----------------------------
class AudioAnalyzer:
    def __init__(self, logger):
        self.logger = logger

    def analyze(self, filepath: str) -> Dict[str, Any]:
        result = {
            "path": filepath,
            "title": Path(filepath).stem,
            "bpm": None,
            "genre": "Unknown",
            "duration_sec": None,
            "sample_rate": None,
            "energy": None,
            "spectral_centroid": None,
        }
        if librosa is None or np is None:
            self.logger(f"[WARN] librosa/numpy not available. Skipping BPM/genre for: {filepath}")
            return result

        try:
            y, sr = librosa.load(filepath, sr=None, mono=True)
            result["sample_rate"] = sr
            result["duration_sec"] = round(len(y) / sr, 2) if sr else None

            tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
            if isinstance(tempo, np.ndarray):
                tempo = float(tempo.squeeze()) if tempo.size else None
            elif tempo is not None:
                tempo = float(tempo)
            result["bpm"] = round(tempo, 2) if tempo else None

            rms = librosa.feature.rms(y=y)
            energy = float(np.mean(rms)) if rms is not None else None
            result["energy"] = round(energy, 6) if energy else None

            centroid = librosa.feature.spectral_centroid(y=y, sr=sr)
            spectral_centroid = float(np.mean(centroid)) if centroid is not None else None
            result["spectral_centroid"] = round(spectral_centroid, 2) if spectral_centroid else None

            result["genre"] = classify_genre_heuristic(
                bpm=result["bpm"],
                energy=result["energy"],
                spectral_centroid=result["spectral_centroid"],
            )
            return result
        except Exception as e:
            self.logger(f"[WARN] Analysis failed for {filepath}: {e}")
            return result


# -----------------------------
# Lyrics
# -----------------------------
class LyricsFetcher:
    def __init__(self, logger):
        self.logger = logger
        self.synced_cmd = find_executable("syncedlyrics", "syncedlyrics")

    def available(self) -> bool:
        return self.synced_cmd is not None

    def fetch_lrc(self, query: str, output_lrc_path: str) -> bool:
        if not self.synced_cmd:
            self.logger("[WARN] syncedlyrics is not installed. Skipping lyrics.")
            return False

        command = self.synced_cmd + [query]
        code, out, err = run_command(command)

        if code == 0 and out.strip():
            Path(output_lrc_path).write_text(out, encoding="utf-8", errors="replace")
            self.logger(f"[LYRICS] Saved LRC: {output_lrc_path}")
            return True

        self.logger(f"[WARN] Lyrics not found for '{query}'. {err.strip()}")
        return False


# -----------------------------
# Downloader engine
# -----------------------------
class DownloaderEngine:
    def __init__(self, config: AppConfig, logger, progress_callback, table_update_callback):
        self.config = config
        self.logger = logger
        self.progress_callback = progress_callback
        self.table_update_callback = table_update_callback
        self.jobs: Dict[str, Job] = {}
        self.job_queue: "queue.Queue[Job]" = queue.Queue()
        self.stop_event = threading.Event()
        self.worker_thread: Optional[threading.Thread] = None
        self.ytdlp_cmd = find_executable("yt_dlp", "yt-dlp")
        self.analyzer = AudioAnalyzer(logger)
        self.lyrics = LyricsFetcher(logger)
        self.library_index_path = Path(self.config.output_dir) / "library_index.json"

    def start(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return
        self.stop_event.clear()
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()
        self.logger("[ENGINE] Worker started.")

    def stop(self) -> None:
        self.stop_event.set()
        self.logger("[ENGINE] Stop requested.")

    def enqueue(self, job: Job) -> None:
        self.jobs[job.job_id] = job
        self.job_queue.put(job)
        self.table_update_callback()
        self.logger(f"[QUEUE] Added job {job.job_id}: {job.source}")

    def enqueue_csv(self, csv_path: str) -> None:
        job = Job(
            job_id=f"job-{int(time.time() * 1000)}",
            source=csv_path,
            source_type="csv",
            output_dir=self.config.output_dir,
        )
        self.enqueue(job)

    def enqueue_source(self, source: str, source_type: str) -> None:
        job = Job(
            job_id=f"job-{int(time.time() * 1000)}",
            source=source,
            source_type=source_type,
            output_dir=self.config.output_dir,
        )
        self.enqueue(job)

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                job = self.job_queue.get(timeout=0.25)
            except queue.Empty:
                continue

            try:
                job.status = "Running"
                self.table_update_callback()
                if job.source_type == "csv":
                    self._process_csv_job(job)
                elif job.source_type in {"single", "playlist"}:
                    self._process_download_job(job, job.source)
                elif job.source_type == "folder_scan":
                    self._process_folder_scan(job)
                else:
                    raise ValueError(f"Unsupported job type: {job.source_type}")

                if not job.error:
                    job.status = "Completed"
                    job.progress = 100.0
                self.table_update_callback()
            except Exception as e:
                job.status = "Failed"
                job.error = str(e)
                self.logger(f"[ERROR] Job {job.job_id} failed: {e}")
                self.table_update_callback()
            finally:
                self.job_queue.task_done()

    def _process_csv_job(self, job: Job) -> None:
        rows = self._read_csv_queries(job.source)
        job.total_items = len(rows)
        self.logger(f"[CSV] Found {len(rows)} items in {job.source}")
        self.table_update_callback()

        for idx, query in enumerate(rows, start=1):
            if self.stop_event.is_set():
                job.status = "Stopped"
                break
            job.current_item = query
            job.completed_items = idx - 1
            base_progress = ((idx - 1) / max(len(rows), 1)) * 100
            job.progress = round(base_progress, 1)
            self.table_update_callback()
            self._process_download_job(job, query, nested=True)
            job.completed_items = idx
            job.progress = round((idx / max(len(rows), 1)) * 100, 1)
            self.table_update_callback()

    def _read_csv_queries(self, csv_path: str) -> List[str]:
        queries: List[str] = []
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                fields_lower = {name.lower(): name for name in reader.fieldnames}
                artist_col = None
                title_col = None
                for candidate in ["artist", "artists", "creator", "album artist"]:
                    if candidate in fields_lower:
                        artist_col = fields_lower[candidate]
                        break
                for candidate in ["title", "track", "name", "song"]:
                    if candidate in fields_lower:
                        title_col = fields_lower[candidate]
                        break
                if artist_col and title_col:
                    for row in reader:
                        artist = (row.get(artist_col) or "").strip()
                        title = (row.get(title_col) or "").strip()
                        if artist or title:
                            queries.append(f"{artist} - {title}".strip(" -"))
                    return queries

        with open(csv_path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line:
                    queries.append(line)
        return queries

    def _process_folder_scan(self, job: Job) -> None:
        audio_files = []
        for root, _, files in os.walk(job.source):
            for name in files:
                if Path(name).suffix.lower() in SUPPORTED_AUDIO_EXTS:
                    audio_files.append(str(Path(root) / name))

        job.total_items = len(audio_files)
        if not audio_files:
            self.logger("[SCAN] No audio files found.")
            return

        for i, filepath in enumerate(audio_files, start=1):
            if self.stop_event.is_set():
                job.status = "Stopped"
                break
            job.current_item = Path(filepath).name
            self.logger(f"[SCAN] Processing {filepath}")
            self._post_process_downloaded_file(filepath)
            job.completed_items = i
            job.progress = round((i / max(job.total_items, 1)) * 100, 1)
            self.table_update_callback()

    def _process_download_job(self, job: Job, source: str, nested: bool = False) -> None:
        if not self.ytdlp_cmd:
            raise RuntimeError("yt-dlp not found. Install with: py -3.10 -m pip install yt-dlp")

        ensure_dir(self.config.output_dir)
        before_files = self._snapshot_audio_files(self.config.output_dir)

        out_template = self._build_output_template()
        command = self.ytdlp_cmd + [
            source,
            "-x",
            "--audio-format", self.config.audio_format,
            "--audio-quality", self.config.audio_quality,
            "--ignore-errors",
            "--newline",
            "--progress",
            "--no-abort-on-error",
            "-o", out_template,
        ]

        if self.config.embed_thumbnail:
            command.append("--embed-thumbnail")
        if self.config.add_metadata:
            command.append("--add-metadata")
        if not self.config.overwrite_files:
            command.append("--no-overwrites")
        if self.config.ffmpeg_path.strip():
            command += ["--ffmpeg-location", self.config.ffmpeg_path.strip()]

        if source.lower().startswith(("http://", "https://")):
            if "playlist" in source.lower() or "list=" in source.lower():
                command.append("--yes-playlist")
            else:
                command.append("--no-playlist")
        else:
            command[1:1] = [f"ytsearch1:{source}"]
            del command[2]
            command.append("--no-playlist")

        self.logger(f"[DOWNLOAD] {source}")
        self.logger("[COMMAND] " + " ".join(f'"{c}"' if ' ' in c else c for c in command))

        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        start_time = time.time()
        try:
            while True:
                if self.stop_event.is_set():
                    proc.terminate()
                    job.status = "Stopped"
                    self.logger("[DOWNLOAD] Terminated by user.")
                    break

                line = proc.stdout.readline() if proc.stdout else ""
                if not line and proc.poll() is not None:
                    break
                if not line:
                    continue

                line = line.rstrip()
                self.logger(line)
                self._parse_progress_line(job, line, start_time)
                self.table_update_callback()

            rc = proc.wait()
            if rc != 0 and job.status != "Stopped":
                raise RuntimeError(f"yt-dlp exited with code {rc}")
        finally:
            try:
                proc.stdout.close() if proc.stdout else None
            except Exception:
                pass

        after_files = self._snapshot_audio_files(self.config.output_dir)
        new_files = sorted(after_files - before_files)
        if not new_files:
            self.logger("[WARN] No new audio files detected after download.")

        for filepath in new_files:
            self._post_process_downloaded_file(filepath)

    def _parse_progress_line(self, job: Job, line: str, start_time: float) -> None:
        m = re.search(r'\[download\]\s+(\d+(?:\.\d+)?)%', line)
        if m:
            pct = float(m.group(1))
            job.progress = pct
            elapsed = max(time.time() - start_time, 1.0)
            rate = pct / elapsed
            if rate > 0 and pct < 100:
                remain = (100 - pct) / rate
                job.eta = self._format_seconds(remain)
            self.progress_callback(job.progress, job.eta)
            return

        if "Destination:" in line:
            job.current_item = line.split("Destination:", 1)[-1].strip()
        elif "[ExtractAudio] Destination:" in line:
            job.current_item = line.split("Destination:", 1)[-1].strip()

    def _format_seconds(self, secs: float) -> str:
        secs = int(max(0, secs))
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m}m {s}s"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    def _build_output_template(self) -> str:
        if self.config.playlist_subfolders:
            return str(Path(self.config.output_dir) / "%(playlist,Unknown Playlist)s" / "%(playlist_index,0>2)s - %(title)s.%(ext)s")
        return str(Path(self.config.output_dir) / "%(title)s.%(ext)s")

    def _snapshot_audio_files(self, root: str) -> set:
        files = set()
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                if Path(name).suffix.lower() in SUPPORTED_AUDIO_EXTS:
                    files.add(str(Path(dirpath) / name))
        return files

    def _post_process_downloaded_file(self, filepath: str) -> None:
        self.logger(f"[POST] {filepath}")
        metadata: Dict[str, Any] = {
            "path": filepath,
            "filename": Path(filepath).name,
            "stem": Path(filepath).stem,
            "lrc": None,
            "analysis": None,
        }

        if self.config.fetch_lyrics:
            lrc_path = str(Path(filepath).with_suffix(".lrc"))
            query = Path(filepath).stem
            if self.lyrics.fetch_lrc(query, lrc_path):
                metadata["lrc"] = lrc_path

        if self.config.detect_bpm or self.config.detect_genre:
            analysis = self.analyzer.analyze(filepath)
            metadata["analysis"] = analysis
            json_path = str(Path(filepath).with_suffix(".analysis.json"))
            Path(json_path).write_text(json.dumps(analysis, indent=2), encoding="utf-8")
            self.logger(f"[ANALYSIS] Saved {json_path}")

        if self.config.auto_import_library:
            self._append_to_library(metadata)

    def _append_to_library(self, record: Dict[str, Any]) -> None:
        ensure_dir(str(self.library_index_path.parent))
        data = []
        if self.library_index_path.exists():
            try:
                data = json.loads(self.library_index_path.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    data = []
            except Exception:
                data = []
        data.append(record)
        self.library_index_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.logger(f"[LIBRARY] Indexed: {record['filename']}")


# -----------------------------
# UI
# -----------------------------
class AutoMixDownloaderUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1240x830")
        self.root.minsize(1080, 720)

        self.config = AppConfig.load()
        ensure_dir(self.config.output_dir)

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.engine = DownloaderEngine(
            config=self.config,
            logger=self.log,
            progress_callback=self.on_progress,
            table_update_callback=self.refresh_jobs_table,
        )
        self.engine.start()

        self._build_vars()
        self._build_ui()
        self._load_config_into_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._drain_log_queue)

    def _build_vars(self) -> None:
        self.var_source = tk.StringVar()
        self.var_output_dir = tk.StringVar(value=self.config.output_dir)
        self.var_audio_format = tk.StringVar(value=self.config.audio_format)
        self.var_audio_quality = tk.StringVar(value=self.config.audio_quality)
        self.var_embed_thumbnail = tk.BooleanVar(value=self.config.embed_thumbnail)
        self.var_add_metadata = tk.BooleanVar(value=self.config.add_metadata)
        self.var_fetch_lyrics = tk.BooleanVar(value=self.config.fetch_lyrics)
        self.var_detect_bpm = tk.BooleanVar(value=self.config.detect_bpm)
        self.var_detect_genre = tk.BooleanVar(value=self.config.detect_genre)
        self.var_auto_import_library = tk.BooleanVar(value=self.config.auto_import_library)
        self.var_playlist_subfolders = tk.BooleanVar(value=self.config.playlist_subfolders)
        self.var_overwrite_files = tk.BooleanVar(value=self.config.overwrite_files)
        self.var_ffmpeg_path = tk.StringVar(value=self.config.ffmpeg_path)
        self.var_status = tk.StringVar(value="Ready")
        self.var_eta = tk.StringVar(value="ETA: --")
        self.var_source_type = tk.StringVar(value="single")

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text=APP_NAME, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(top, text="Playlist + lyrics + BPM + genre + queue-based downloader", foreground="#555").pack(anchor="w", pady=(2, 10))

        main = ttk.Panedwindow(self.root, orient="horizontal")
        main.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        left = ttk.Frame(main, padding=8)
        right = ttk.Frame(main, padding=8)
        main.add(left, weight=3)
        main.add(right, weight=2)

        self._build_left_panel(left)
        self._build_right_panel(right)

        bottom = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        bottom.pack(fill="x")
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.pack(fill="x")
        footer = ttk.Frame(bottom)
        footer.pack(fill="x", pady=(6, 0))
        ttk.Label(footer, textvariable=self.var_status).pack(side="left")
        ttk.Label(footer, textvariable=self.var_eta).pack(side="right")

    def _build_left_panel(self, parent: ttk.Frame) -> None:
        source_card = ttk.LabelFrame(parent, text="Source", padding=10)
        source_card.pack(fill="x")

        type_row = ttk.Frame(source_card)
        type_row.pack(fill="x", pady=(0, 8))
        ttk.Radiobutton(type_row, text="Single Track / Search", variable=self.var_source_type, value="single").pack(side="left")
        ttk.Radiobutton(type_row, text="Playlist URL", variable=self.var_source_type, value="playlist").pack(side="left", padx=(10, 0))
        ttk.Radiobutton(type_row, text="CSV", variable=self.var_source_type, value="csv").pack(side="left", padx=(10, 0))
        ttk.Radiobutton(type_row, text="Scan Folder", variable=self.var_source_type, value="folder_scan").pack(side="left", padx=(10, 0))

        entry_row = ttk.Frame(source_card)
        entry_row.pack(fill="x")
        ttk.Entry(entry_row, textvariable=self.var_source).pack(side="left", fill="x", expand=True)
        ttk.Button(entry_row, text="Browse", command=self.browse_source).pack(side="left", padx=(8, 0))
        ttk.Button(entry_row, text="Add to Queue", command=self.add_job).pack(side="left", padx=(8, 0))

        output_card = ttk.LabelFrame(parent, text="Output & Processing", padding=10)
        output_card.pack(fill="x", pady=(10, 0))

        out_row = ttk.Frame(output_card)
        out_row.pack(fill="x")
        ttk.Label(out_row, text="Output Folder:").pack(side="left")
        ttk.Entry(out_row, textvariable=self.var_output_dir).pack(side="left", fill="x", expand=True, padx=(8, 8))
        ttk.Button(out_row, text="Choose", command=self.choose_output_dir).pack(side="left")

        grid = ttk.Frame(output_card)
        grid.pack(fill="x", pady=(10, 0))

        ttk.Label(grid, text="Audio Format").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(grid, textvariable=self.var_audio_format, values=["mp3", "wav", "m4a", "flac"], width=12, state="readonly").grid(row=0, column=1, sticky="w", padx=(8, 24))
        ttk.Label(grid, text="Audio Quality").grid(row=0, column=2, sticky="w", pady=4)
        ttk.Combobox(grid, textvariable=self.var_audio_quality, values=["0", "2", "5", "7", "9"], width=12, state="readonly").grid(row=0, column=3, sticky="w", padx=(8, 0))

        ttk.Checkbutton(grid, text="Embed Thumbnail", variable=self.var_embed_thumbnail).grid(row=1, column=0, sticky="w", pady=4)
        ttk.Checkbutton(grid, text="Add Metadata", variable=self.var_add_metadata).grid(row=1, column=1, sticky="w", pady=4)
        ttk.Checkbutton(grid, text="Fetch Lyrics (.lrc)", variable=self.var_fetch_lyrics).grid(row=1, column=2, sticky="w", pady=4)
        ttk.Checkbutton(grid, text="Auto Import Library", variable=self.var_auto_import_library).grid(row=1, column=3, sticky="w", pady=4)

        ttk.Checkbutton(grid, text="Detect BPM", variable=self.var_detect_bpm).grid(row=2, column=0, sticky="w", pady=4)
        ttk.Checkbutton(grid, text="Detect Genre", variable=self.var_detect_genre).grid(row=2, column=1, sticky="w", pady=4)
        ttk.Checkbutton(grid, text="Playlist Subfolders", variable=self.var_playlist_subfolders).grid(row=2, column=2, sticky="w", pady=4)
        ttk.Checkbutton(grid, text="Overwrite Files", variable=self.var_overwrite_files).grid(row=2, column=3, sticky="w", pady=4)

        ff_row = ttk.Frame(output_card)
        ff_row.pack(fill="x", pady=(8, 0))
        ttk.Label(ff_row, text="FFmpeg Path:").pack(side="left")
        ttk.Entry(ff_row, textvariable=self.var_ffmpeg_path).pack(side="left", fill="x", expand=True, padx=(8, 8))
        ttk.Button(ff_row, text="Browse", command=self.choose_ffmpeg_dir).pack(side="left")

        action_card = ttk.LabelFrame(parent, text="Actions", padding=10)
        action_card.pack(fill="both", expand=True, pady=(10, 0))

        btns = ttk.Frame(action_card)
        btns.pack(fill="x")
        ttk.Button(btns, text="Start Worker", command=self.engine.start).pack(side="left")
        ttk.Button(btns, text="Stop Worker", command=self.engine.stop).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Save Settings", command=self.save_settings).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Open Output Folder", command=self.open_output_folder).pack(side="left", padx=(8, 0))

        queue_card = ttk.LabelFrame(action_card, text="Queue", padding=8)
        queue_card.pack(fill="both", expand=True, pady=(10, 0))

        cols = ("job_id", "type", "source", "status", "progress", "item", "error")
        self.jobs_table = ttk.Treeview(queue_card, columns=cols, show="headings", height=12)
        headings = {
            "job_id": "Job ID",
            "type": "Type",
            "source": "Source",
            "status": "Status",
            "progress": "%",
            "item": "Current Item",
            "error": "Error",
        }
        widths = {"job_id": 150, "type": 80, "source": 250, "status": 90, "progress": 60, "item": 220, "error": 220}
        for c in cols:
            self.jobs_table.heading(c, text=headings[c])
            self.jobs_table.column(c, width=widths[c], anchor="w")
        self.jobs_table.pack(fill="both", expand=True)

    def _build_right_panel(self, parent: ttk.Frame) -> None:
        help_card = ttk.LabelFrame(parent, text="Quick Tips", padding=10)
        help_card.pack(fill="x")

        tips = (
            "• Single Track / Search: type artist - title\n"
            "• Playlist URL: paste YouTube playlist URL\n"
            "• CSV: should contain artist/title columns or one query per line\n"
            "• Scan Folder: runs lyrics + BPM + genre on existing audio files\n"
            "• For best results install: yt-dlp, FFmpeg, syncedlyrics, librosa, numpy"
        )
        ttk.Label(help_card, text=tips, justify="left").pack(anchor="w")

        log_card = ttk.LabelFrame(parent, text="Live Log", padding=8)
        log_card.pack(fill="both", expand=True, pady=(10, 0))

        self.txt_log = tk.Text(log_card, wrap="word", height=30, font=("Consolas", 10))
        self.txt_log.pack(fill="both", expand=True)

    def browse_source(self) -> None:
        source_type = self.var_source_type.get()
        if source_type == "csv":
            path = filedialog.askopenfilename(filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
            if path:
                self.var_source.set(path)
        elif source_type == "folder_scan":
            path = filedialog.askdirectory()
            if path:
                self.var_source.set(path)
        else:
            messagebox.showinfo("Browse", "Paste a track name/search query or playlist URL into the source box.")

    def choose_output_dir(self) -> None:
        path = filedialog.askdirectory(initialdir=self.var_output_dir.get() or DEFAULT_OUTPUT)
        if path:
            self.var_output_dir.set(path)

    def choose_ffmpeg_dir(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self.var_ffmpeg_path.set(path)

    def add_job(self) -> None:
        source = self.var_source.get().strip()
        source_type = self.var_source_type.get().strip()
        if not source:
            messagebox.showwarning("Missing source", "Enter a source first.")
            return
        self.save_settings(silent=True)
        self.engine.enqueue_source(source, source_type)
        self.var_status.set(f"Queued: {source}")

    def save_settings(self, silent: bool = False) -> None:
        self.config.output_dir = self.var_output_dir.get().strip() or DEFAULT_OUTPUT
        self.config.audio_format = self.var_audio_format.get().strip() or "mp3"
        self.config.audio_quality = self.var_audio_quality.get().strip() or "0"
        self.config.embed_thumbnail = self.var_embed_thumbnail.get()
        self.config.add_metadata = self.var_add_metadata.get()
        self.config.fetch_lyrics = self.var_fetch_lyrics.get()
        self.config.detect_bpm = self.var_detect_bpm.get()
        self.config.detect_genre = self.var_detect_genre.get()
        self.config.auto_import_library = self.var_auto_import_library.get()
        self.config.playlist_subfolders = self.var_playlist_subfolders.get()
        self.config.overwrite_files = self.var_overwrite_files.get()
        self.config.ffmpeg_path = self.var_ffmpeg_path.get().strip()
        ensure_dir(self.config.output_dir)
        self.config.save()
        self.engine.config = self.config
        if not silent:
            self.var_status.set("Settings saved")
            self.log("[CONFIG] Settings saved.")

    def open_output_folder(self) -> None:
        path = self.var_output_dir.get().strip() or DEFAULT_OUTPUT
        ensure_dir(path)
        os.startfile(path)

    def refresh_jobs_table(self) -> None:
        def _update():
            for item in self.jobs_table.get_children():
                self.jobs_table.delete(item)
            for job in self.engine.jobs.values():
                self.jobs_table.insert(
                    "", "end",
                    values=(
                        job.job_id,
                        job.source_type,
                        job.source,
                        job.status,
                        f"{job.progress:.1f}",
                        job.current_item,
                        job.error,
                    )
                )
        self.root.after(0, _update)

    def on_progress(self, pct: float, eta: str) -> None:
        def _update():
            self.progress["value"] = pct
            self.var_eta.set(f"ETA: {eta or '--'}")
            self.var_status.set(f"Running... {pct:.1f}%")
        self.root.after(0, _update)

    def log(self, message: str) -> None:
        self.log_queue.put(f"[{ts()}] {message}")

    def _drain_log_queue(self) -> None:
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.txt_log.insert("end", msg + "\n")
                self.txt_log.see("end")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log_queue)

    def _load_config_into_ui(self) -> None:
        self.var_output_dir.set(self.config.output_dir)
        self.var_audio_format.set(self.config.audio_format)
        self.var_audio_quality.set(self.config.audio_quality)
        self.var_embed_thumbnail.set(self.config.embed_thumbnail)
        self.var_add_metadata.set(self.config.add_metadata)
        self.var_fetch_lyrics.set(self.config.fetch_lyrics)
        self.var_detect_bpm.set(self.config.detect_bpm)
        self.var_detect_genre.set(self.config.detect_genre)
        self.var_auto_import_library.set(self.config.auto_import_library)
        self.var_playlist_subfolders.set(self.config.playlist_subfolders)
        self.var_overwrite_files.set(self.config.overwrite_files)
        self.var_ffmpeg_path.set(self.config.ffmpeg_path)

    def on_close(self) -> None:
        self.save_settings(silent=True)
        self.engine.stop()
        self.root.after(150, self.root.destroy)


def main() -> None:
    root = tk.Tk()
    try:
        from tkinter import TclError
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    app = AutoMixDownloaderUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()

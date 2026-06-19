"""Headless AutoMix downloader core. Shared by desktop UI and SAMSEL Web API."""

import os
import re
import csv
import sys
import json
import time
import queue
import shutil
import threading
import subprocess
from pathlib import Path
from dataclasses import dataclass, asdict, fields
from typing import Optional, List, Dict, Any, Tuple


# librosa/numpy: lazy-loaded in AudioAnalyzer.analyze (import is slow; helps cold start / Railway healthchecks)
_librosa_mod = None
_np_mod = None


def _lazy_librosa_np():
    """Return (librosa, np) or (None, None) if unavailable."""
    global _librosa_mod, _np_mod
    if _librosa_mod is False or _np_mod is False:
        return None, None
    if _librosa_mod is None:
        try:
            import librosa as _lr

            _librosa_mod = _lr
        except Exception:
            _librosa_mod = False
    if _np_mod is None:
        try:
            import numpy as _n

            _np_mod = _n
        except Exception:
            _np_mod = False
    if _librosa_mod is False or _np_mod is False:
        return None, None
    return _librosa_mod, _np_mod


try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import USLT, SYLT
except Exception:
    MP3 = None
    USLT = None
    SYLT = None



APP_NAME = "SAMSEL AutoMix Downloader v2"


DEFAULT_OUTPUT = str(Path.home() / "Music" / "AutoMix")
SUPPORTED_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac"}
# When final format is MP3, delete these if a same-stem .mp3 exists (yt-dlp/ffmpeg leftovers).
_INTERMEDIATE_AUDIO_WHEN_MP3 = frozenset({".m4a", ".webm", ".opus", ".aac", ".ogg"})


# -----------------------------
# Utility helpers
# -----------------------------
def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ensure_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def sanitize_youtube_search_query(q: str) -> str:
    """Strip characters that break Windows argv or YouTube search (Spotify titles often have quotes)."""
    q = q.replace('"', "'").replace("\r", " ").replace("\n", " ")
    q = re.sub(r"\s+", " ", q).strip()
    return q


def lrc_to_plain_text_for_uslt(lrc_text: str) -> str:
    """Strip LRC timestamps for ID3 USLT (unsynchronized lyrics) — better in most players."""
    lines: List[str] = []
    for line in lrc_text.splitlines():
        s = re.sub(r"^\[[0-9]+:[0-9]{2}(?:\.[0-9]+)?\]\s*", "", line.strip())
        if s:
            lines.append(s)
    return "\n".join(lines).strip()


def lrc_to_sylt_pairs(lrc_text: str) -> List[Tuple[str, int]]:
    """
    Parse LRC into ID3 SYLT pairs: mutagen expects (line_text, time_ms) per line.
    Skips non-timestamp lines (e.g. [ar:], [ti:]).
    """
    pairs: List[Tuple[str, int]] = []
    line_re = re.compile(
        r"^\[(\d+):(\d{2})(?:\.(\d{1,3}))?\]\s*(.*)$"
    )
    for raw in lrc_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = line_re.match(line)
        if not m:
            continue
        mm = int(m.group(1))
        ss = int(m.group(2))
        frac = m.group(3)
        text = (m.group(4) or "").strip()
        if not text:
            continue
        ms = mm * 60_000 + ss * 1_000
        if frac:
            f = frac.ljust(3, "0")[:3]
            ms += int(f)
        pairs.append((text, ms))
    return pairs


def embed_slt_mp3(mp3_path: str, lrc_text: str, logger) -> bool:
    """
    Embed synchronized lyrics as ID3v2 SYLT (Synchronized Lyrics/Text; often called SLT).
    Preserves LRC timing for players that read SYLT; requires mutagen.
    """
    if not (lrc_text or "").strip():
        return False
    if MP3 is None or SYLT is None:
        logger(
            "[WARN] mutagen is not installed; cannot embed SYLT in MP3. "
            "Install: py -3.10 -m pip install mutagen"
        )
        return False
    pairs = lrc_to_sylt_pairs(lrc_text)
    if not pairs:
        return False
    try:
        audio = MP3(mp3_path)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.delall("SYLT")
        # format=2: absolute milliseconds; type=1: lyrics (ID3v2.4 content type).
        audio.tags.add(
            SYLT(
                encoding=1,
                lang="eng",
                format=2,
                type=1,
                desc="",
                text=pairs,
            )
        )
        audio.save(v2_version=4)
        logger(f"[TAGS] Embedded synchronized lyrics (SYLT) in {Path(mp3_path).name}")
        return True
    except Exception as e:
        logger(f"[WARN] Could not embed SYLT in MP3: {e}")
        return False


def embed_lrc_uslt_and_slt_mp3(mp3_path: str, lrc_text: str, uslt_body: str, logger) -> bool:
    """
    Write USLT and SYLT from LRC in a single ID3 save.
    `uslt_body` is the exact text stored in USLT (plain lines, or full LRC with timestamps for Mp3tag).
    `lrc_text` is used to build SYLT timecodes.
    """
    if MP3 is None or USLT is None or SYLT is None:
        logger(
            "[WARN] mutagen is not installed; cannot embed lyrics in MP3. "
            "Install: py -3.10 -m pip install mutagen"
        )
        return False
    pairs = lrc_to_sylt_pairs(lrc_text or "")
    if not (uslt_body or "").strip() and not pairs:
        return False
    try:
        audio = MP3(mp3_path)
        if audio.tags is None:
            audio.add_tags()
        if (uslt_body or "").strip():
            audio.tags.delall("USLT")
            audio.tags.add(USLT(encoding=1, lang="eng", desc="", text=uslt_body.strip()))
        if pairs:
            audio.tags.delall("SYLT")
            audio.tags.add(
                SYLT(
                    encoding=1,
                    lang="eng",
                    format=2,
                    type=1,
                    desc="",
                    text=pairs,
                )
            )
        # v2.4: better SYLT interoperability; Mp3tag still maps USLT only — use full LRC in USLT to see timings there.
        audio.save(v2_version=4)
        tags = []
        if (uslt_body or "").strip():
            tags.append("USLT")
        if pairs:
            tags.append("SYLT")
        logger(f"[TAGS] Embedded lyrics ({', '.join(tags)}) in {Path(mp3_path).name}")
        return True
    except Exception as e:
        logger(f"[WARN] Could not embed lyrics in MP3: {e}")
        return False


def embed_lyrics_uslt_mp3(mp3_path: str, lyrics_plain: str, logger) -> bool:
    """Write ID3v2 USLT frame (unsynchronized lyrics). Requires mutagen."""
    if not lyrics_plain.strip():
        return False
    if MP3 is None or USLT is None:
        logger(
            "[WARN] mutagen is not installed; cannot embed lyrics in MP3. "
            "Install: py -3.10 -m pip install mutagen"
        )
        return False
    try:
        audio = MP3(mp3_path)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.delall("USLT")
        # encoding=1 (UTF-16) matches ID3v2.3 convention; many Windows apps (e.g. MusicBee) read USLT more reliably than UTF-8.
        audio.tags.add(USLT(encoding=1, lang="eng", desc="", text=lyrics_plain))
        audio.save(v2_version=3)
        logger(f"[TAGS] Embedded lyrics (USLT, UTF-16) in {Path(mp3_path).name}")
        return True
    except Exception as e:
        logger(f"[WARN] Could not embed lyrics in MP3: {e}")
        return False


def lyrics_query_from_filename_stem(stem: str) -> str:
    """
    syncedlyrics works best with 'Artist - Title', not yt-dlp filenames like
    '1 - Artist - Title (Official Music Video)'.
    """
    s = stem.strip()
    s = re.sub(r"^\d+\s*-\s*", "", s)
    tail = (
        r"\s*\(Official Music Video\)\s*$",
        r"\s*\(Official Lyric Video\)\s*$",
        r"\s*\(Official Video\)\s*$",
        r"\s*\(Lyric Video\)\s*$",
        r"\s*\(Lyrics\)\s*$",
        r"\s*\[Official Video\]\s*$",
        r"\s*\[Lyrics\]\s*$",
    )
    for _ in range(4):
        before = s
        for p in tail:
            s = re.sub(p, "", s, flags=re.IGNORECASE).strip()
        if s == before:
            break
    return s.strip() or stem.strip()


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]+', '_', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name[:220] if len(name) > 220 else name


def run_command(
    command: List[str],
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=run_env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = "", ""
        return -1, out or "", (err or "") + "\n[command timed out]"
    return proc.returncode, out, err


def resolve_ffmpeg_dir(path_str: str) -> Optional[str]:
    """
    yt-dlp needs a directory containing ffmpeg and ffprobe (Windows: .exe).
    Accepts that directory, a .../bin folder, or the path to ffmpeg.exe.
    Returns absolute path str, or None if not found (caller should use PATH).
    """
    raw = (path_str or "").strip()
    if not raw:
        return None
    p = Path(raw)
    candidates: List[Path] = []
    if p.is_file():
        if p.suffix.lower() == ".exe" and p.name.lower().startswith("ffmpeg"):
            candidates.append(p.parent)
        else:
            return None
    else:
        candidates.append(p)
        candidates.append(p / "bin")

    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    probe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    seen: set[str] = set()
    for d in candidates:
        try:
            d = d.resolve()
        except OSError:
            continue
        key = str(d)
        if key in seen:
            continue
        seen.add(key)
        if not d.is_dir():
            continue
        if (d / exe).is_file() and (d / probe).is_file():
            return str(d)
    return None


def find_executable(preferred_module: Optional[str], exe_name: str) -> Optional[List[str]]:
    """
    Return a command prefix list to run an executable/module.
    Examples:
      [sys.executable, '-m', 'yt_dlp'] or ['yt-dlp']
    """
    if preferred_module:
        code, _, _ = run_command(
            [sys.executable, "-m", preferred_module, "--help"], timeout=25.0
        )
        if code == 0:
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
    embed_lyrics_in_mp3: bool = True
    # Mp3tag and many tools only show USLT ("Unsynchronized lyrics"), not SYLT. Full LRC here = visible timings there.
    uslt_embed_full_lrc: bool = True
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
            # Ignore unknown keys from older/newer configs so new fields keep defaults.
            known = {f.name for f in fields(cls)}
            return cls(**{k: v for k, v in data.items() if k in known})
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
        librosa, np = _lazy_librosa_np()
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
        self._synced_resolved = False
        self._synced_cmd: Optional[List[str]] = None

    def _get_synced_cmd(self) -> Optional[List[str]]:
        if not self._synced_resolved:
            self._synced_cmd = find_executable("syncedlyrics", "syncedlyrics")
            self._synced_resolved = True
        return self._synced_cmd

    def available(self) -> bool:
        return self._get_synced_cmd() is not None

    def fetch_lrc(self, query: str, output_lrc_path: str) -> bool:
        synced = self._get_synced_cmd()
        if not synced:
            self.logger("[WARN] syncedlyrics is not installed. Skipping lyrics.")
            return False

        command = synced + [query]
        # syncedlyrics prints LRC to stdout; Windows cp1252 can't print some Unicode unless UTF-8 mode is on.
        code, out, err = run_command(
            command,
            env={"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )

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
        self._ytdlp_lock = threading.Lock()
        self._ytdlp_resolved = False
        self._ytdlp_cmd: Optional[List[str]] = None
        self.analyzer = AudioAnalyzer(logger)
        self.lyrics = LyricsFetcher(logger)
        self.library_index_path = Path(self.config.output_dir or DEFAULT_OUTPUT) / "library_index.json"

    def get_ytdlp_cmd(self) -> Optional[List[str]]:
        """Resolve yt-dlp lazily so the GUI can open without blocking on subprocess --help."""
        with self._ytdlp_lock:
            if not self._ytdlp_resolved:
                self._ytdlp_cmd = find_executable("yt_dlp", "yt-dlp")
                self._ytdlp_resolved = True
            return self._ytdlp_cmd

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

    def enqueue_csv(self, csv_path: str, output_dir_override: str = "") -> None:
        job = Job(
            job_id=f"job-{int(time.time() * 1000)}",
            source=csv_path,
            source_type="csv",
            output_dir=output_dir_override or self.config.output_dir,
        )
        self.enqueue(job)

    def enqueue_source(self, source: str, source_type: str, output_dir_override: str = "") -> None:
        job = Job(
            job_id=f"job-{int(time.time() * 1000)}",
            source=source,
            source_type=source_type,
            output_dir=output_dir_override or self.config.output_dir,
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

    @staticmethod
    def _resolve_csv_artist_title_columns(fieldnames: List[str]) -> Tuple[Optional[str], Optional[str]]:
        """Map headers from Spotify / Apple Music / generic exports to artist + title columns."""
        fields_lower = {name.lower(): name for name in fieldnames}
        artist_candidates = [
            "artist name(s)",
            "artist name",
            "artists",
            "artist",
            "album artist",
            "albumartist",
            "creator",
        ]
        title_candidates = [
            "track name",
            "title",
            "name",
            "track",
            "song",
        ]
        artist_col = None
        title_col = None
        for c in artist_candidates:
            if c in fields_lower:
                artist_col = fields_lower[c]
                break
        if not artist_col:
            for k, orig in fields_lower.items():
                if "artist" in k and "uri" not in k:
                    artist_col = orig
                    break
        for c in title_candidates:
            if c in fields_lower:
                title_col = fields_lower[c]
                break
        if not title_col:
            for k, orig in fields_lower.items():
                if k == "track name" or ("track" in k and "name" in k and "uri" not in k):
                    title_col = orig
                    break
        return artist_col, title_col

    @staticmethod
    def _looks_like_spotify_or_tracklist_header(line: str) -> bool:
        low = line.lower()
        return "track uri" in low or (
            "track name" in low and ("artist" in low or "album" in low)
        )

    def _read_csv_queries(self, csv_path: str) -> List[str]:
        queries: List[str] = []
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                artist_col, title_col = self._resolve_csv_artist_title_columns(
                    list(reader.fieldnames)
                )
                if artist_col and title_col:
                    for row in reader:
                        artist = (row.get(artist_col) or "").strip()
                        title = (row.get(title_col) or "").strip()
                        artist = re.sub(r"\s*;\s*", " ", artist)
                        if artist or title:
                            q = f"{artist} - {title}".strip(" -")
                            queries.append(sanitize_youtube_search_query(q))
                    return queries

        with open(csv_path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if self._looks_like_spotify_or_tracklist_header(line):
                    continue
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
        ytdlp = self.get_ytdlp_cmd()
        if not ytdlp:
            raise RuntimeError("yt-dlp not found. Install with: py -3.10 -m pip install yt-dlp")

        out_dir = str(Path(job.output_dir or self.config.output_dir or DEFAULT_OUTPUT).resolve())
        ensure_dir(out_dir)
        before_files = self._snapshot_audio_files(out_dir)

        out_template = self._build_output_template(out_dir)
        # Force a full-length audio-only format. Without this, yt-dlp can pick a
        # short DASH fragment or the wrong stream (~30–60s clips on some YouTube results).
        command = ytdlp + [
            source,
            "-f",
            "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
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
        ff_dir = resolve_ffmpeg_dir(self.config.ffmpeg_path)
        if self.config.ffmpeg_path.strip() and not ff_dir:
            self.logger(
                "[WARN] FFmpeg path is set but ffmpeg.exe + ffprobe.exe were not found there "
                f"({self.config.ffmpeg_path.strip()}). Point to the folder that contains both "
                "(often the `bin` folder inside an FFmpeg build). Trying system PATH."
            )
        if ff_dir:
            command += ["--ffmpeg-location", ff_dir]

        if source.lower().startswith(("http://", "https://")):
            if "playlist" in source.lower() or "list=" in source.lower():
                command.append("--yes-playlist")
            else:
                command.append("--no-playlist")
        else:
            # Replace the URL/query slot (immediately after yt-dlp argv prefix), not index 1 —
            # inserting at 1 breaks `python -m yt_dlp` by deleting `-m`.
            search_q = sanitize_youtube_search_query(source)
            command[len(ytdlp)] = f"ytsearch1:{search_q}"
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
                self.logger(f"[WARN] yt-dlp exited with code {rc} for: {source}")
        finally:
            try:
                proc.stdout.close() if proc.stdout else None
            except Exception:
                pass

        after_files = self._snapshot_audio_files(out_dir)
        new_files = sorted(after_files - before_files)
        new_files = self._finalize_new_audio_paths(new_files)
        if not new_files:
            if rc != 0:
                raise RuntimeError(f"yt-dlp exited with code {rc} and produced no files")
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

    def _build_output_template(self, output_dir: str = "") -> str:
        out = output_dir or self.config.output_dir or DEFAULT_OUTPUT
        if self.config.playlist_subfolders:
            return str(Path(out) / "%(playlist,Unknown Playlist)s" / "%(playlist_index,0>2)s - %(title)s.%(ext)s")
        return str(Path(out) / "%(title)s.%(ext)s")

    def _snapshot_audio_files(self, root: str) -> set:
        files = set()
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                if Path(name).suffix.lower() in SUPPORTED_AUDIO_EXTS:
                    files.add(str(Path(dirpath) / name))
        return files

    def _finalize_new_audio_paths(self, new_files: List[str]) -> List[str]:
        """
        When extracting to MP3, yt-dlp may briefly leave .m4a/.webm next to the final .mp3.
        Drop intermediates from disk and only post-process / expose the .mp3 so clients never
        pick up a raw m4a by mistake.
        """
        want = (self.config.audio_format or "mp3").lower().lstrip(".")
        if want != "mp3" or not new_files:
            return sorted(set(new_files))

        by_key: Dict[Tuple[str, str], List[str]] = {}
        for raw in new_files:
            p = Path(raw)
            if p.suffix.lower() not in SUPPORTED_AUDIO_EXTS:
                continue
            try:
                parent = str(p.parent.resolve())
            except OSError:
                parent = str(p.parent)
            key = (parent, p.stem.lower())
            by_key.setdefault(key, []).append(str(p))

        out: List[str] = []
        for _key, group in by_key.items():
            mp3s = [g for g in group if g.lower().endswith(".mp3")]
            if mp3s:
                out.extend(mp3s)
                for g in group:
                    if g in mp3s:
                        continue
                    suf = Path(g).suffix.lower()
                    if suf in _INTERMEDIATE_AUDIO_WHEN_MP3:
                        try:
                            Path(g).unlink(missing_ok=True)
                            self.logger(f"[POST] Removed intermediate audio {Path(g).name} (MP3 present)")
                        except OSError as e:
                            self.logger(f"[WARN] Could not remove intermediate {g}: {e}")
            else:
                out.extend(group)
        return sorted(set(out))

    def _post_process_downloaded_file(self, filepath: str) -> None:
        self.logger(f"[POST] {filepath}")
        metadata: Dict[str, Any] = {
            "path": filepath,
            "filename": Path(filepath).name,
            "stem": Path(filepath).stem,
            "lrc": None,
            "analysis": None,
        }

        lrc_path = Path(filepath).with_suffix(".lrc")
        if self.config.fetch_lyrics:
            query = lyrics_query_from_filename_stem(Path(filepath).stem)
            self.logger(f"[LYRICS] Searching: {query}")
            if self.lyrics.fetch_lrc(query, str(lrc_path)):
                metadata["lrc"] = str(lrc_path)
            else:
                self.logger(f"[LYRICS] No synced lyrics found for: {query}")
        lrc_text: Optional[str] = None
        if lrc_path.is_file():
            if metadata.get("lrc") is None:
                metadata["lrc"] = str(lrc_path)
            try:
                lrc_text = lrc_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                lrc_text = None

        is_mp3 = Path(filepath).suffix.lower() == ".mp3"
        if lrc_text and self.config.embed_lyrics_in_mp3 and is_mp3:
            uslt_body = (
                lrc_text.strip()
                if self.config.uslt_embed_full_lrc
                else lrc_to_plain_text_for_uslt(lrc_text)
            )
            embed_lrc_uslt_and_slt_mp3(filepath, lrc_text, uslt_body, self.logger)
        elif is_mp3 and self.config.embed_lyrics_in_mp3 and not lrc_text:
            self.logger(f"[LYRICS] No LRC text available — skipping embed for {Path(filepath).name}")

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

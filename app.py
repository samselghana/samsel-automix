from __future__ import annotations

import asyncio
import importlib.util
import os
import random
from contextlib import asynccontextmanager
import sys
import shutil
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import parse_qs

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from dj_engine_pro import DJEnginePro, make_deck

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

try:
    from tinytag import TinyTag
except Exception:  # pragma: no cover
    TinyTag = None

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
UPLOAD_DIR = BASE_DIR / "uploads"
RECORDINGS_DIR = BASE_DIR / "recordings"

for folder in (STATIC_DIR, TEMPLATES_DIR, UPLOAD_DIR, RECORDINGS_DIR):
    folder.mkdir(parents=True, exist_ok=True)

# Waveform FFT is expensive; cache by audio buffer identity + length + column count.
_waveform_spectral_cache: "OrderedDict[tuple[int, int, int, int], dict[str, list[float]]]" = OrderedDict()
_WAVEFORM_CACHE_MAX = 16

JINGLE_SLOT_DIR = BASE_DIR / "transition_jingle_uploads"
JINGLE_SLOT_DIR.mkdir(parents=True, exist_ok=True)
transition_jingle_paths: List[Optional[str]] = [None, None, None, None]
transition_jingle_order: str = "sequential"
transition_jingle_enabled: bool = False
transition_jingle_gain: float = 0.65


def _restore_transition_jingle_paths_from_disk() -> None:
    """Re-link slot paths after server restart if files still exist under transition_jingle_uploads/."""
    global transition_jingle_paths
    for i in range(4):
        matches = [p for p in JINGLE_SLOT_DIR.glob(f"jingle_slot_{i}.*") if p.is_file()]
        if matches:
            matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            transition_jingle_paths[i] = str(matches[0].resolve())
    if transition_jingle_paths[0] is None:
        legacy = [p for p in JINGLE_SLOT_DIR.glob("transition_jingle.*") if p.is_file()]
        if legacy:
            legacy.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            transition_jingle_paths[0] = str(legacy[0].resolve())


_restore_transition_jingle_paths_from_disk()

SUPPORTED_AUDIO = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"}
DEFAULT_TEMPLATE_NAME = "index.html"
MAX_UPLOAD_BYTES = 512 * 1024 * 1024  # 512 MB per file
LOG_LIMIT = 300
KEEPALIVE_SECONDS = 10.0
QUEUE_GET_TIMEOUT = 1.0

# --- Global state ---
engine: Optional[DJEnginePro] = None
deck_paths: dict[str, Optional[str]] = {"A": None, "B": None}
deck_playlists: dict[str, list[str]] = {"A": [], "B": []}
deck_playlist_index: dict[str, int] = {"A": -1, "B": -1}
deck_finished_flags: dict[str, bool] = {"A": False, "B": False}
status_log: list[str] = []
crossfader_val = 0.5
master_gain_val = 1.0
engine_eq_bands_val: list[float] = [0.0] * 10
jobs: dict[str, dict[str, Any]] = {}
_library_meta_cache: dict[str, dict[str, Any]] = {}
_track_analysis_cache: dict[str, dict[str, Any]] = {}


# --- Helpers ---
def append_status(text: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status_log.append(f"[{stamp}] {text}")
    if len(status_log) > LOG_LIMIT:
        del status_log[:-LOG_LIMIT]


def shutdown_audio_engine(*, log_reason: Optional[str] = None) -> None:
    """Release the sound device (PortAudio). Used by /stop_engine and server shutdown."""
    global engine
    if engine is None:
        return
    try:
        engine.stop()
        if log_reason:
            append_status(log_reason)
        else:
            append_status("Engine stopped")
    except Exception as exc:
        append_status(f"ERROR stopping engine: {exc}")
    finally:
        engine = None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    shutdown_audio_engine(log_reason="Engine stopped (server shutdown)")


app = FastAPI(
    title="SAMSEL DJ Engine Pro Web API",
    version="2.0.0",
    lifespan=_lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/recordings", StaticFiles(directory=str(RECORDINGS_DIR)), name="recordings")
_CAMOUFLAGE_DIR = BASE_DIR / "Camouflage_png"
if _CAMOUFLAGE_DIR.is_dir():
    app.mount("/camo", StaticFiles(directory=str(_CAMOUFLAGE_DIR)), name="camouflage")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _try_mount_samsel_web_downloader() -> None:
    """SAMSEL Web Downloader: static assets under ``/samsel_web_static`` + API ``/api/automix`` (same router as ``samsel_web``)."""
    sw = BASE_DIR / "samsel_web"
    st = sw / "static"
    if st.is_dir():
        try:
            app.mount("/samsel_web_static", StaticFiles(directory=str(st)), name="samsel_web_static")
        except Exception as exc:
            append_status(f"Downloader: static mount failed: {exc}")
    else:
        append_status("Downloader: samsel_web/static missing — embed UI unavailable.")

    if not (sw / "automix_routes.py").is_file():
        append_status("Downloader: samsel_web/automix_routes.py missing — API unavailable.")
        return
    root = str(sw.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        import automix_routes  # type: ignore

        app.include_router(automix_routes.router)
        append_status("Downloader: /api/automix API mounted (SAMSEL Web parity).")
    except Exception as exc:
        append_status(f"Downloader: API mount failed: {exc}")


_try_mount_samsel_web_downloader()

DOWNLOADER_EMBED_PATH = TEMPLATES_DIR / "downloader_embed.html"
TRIM_EMBED_PATH = TEMPLATES_DIR / "trim_embed.html"


def transition_jingle_status_payload() -> dict[str, Any]:
    """Transition jingle UI/API state (desktop + browser use the same /status)."""
    paths = (transition_jingle_paths + [None, None, None, None])[:4]
    loaded = False
    if engine:
        try:
            with engine.lock:
                slots = getattr(engine, "transition_jingle_slots", None)
                if slots:
                    loaded = any(
                        s is not None and len(s) > 0 for s in slots
                    )
        except Exception:
            pass
    slot_payload = []
    for i in range(4):
        p = paths[i]
        present = bool(p and Path(p).is_file())
        slot_payload.append(
            {
                "index": i,
                "path": p,
                "basename": os.path.basename(p) if p else "",
                "present": present,
            }
        )
    filled = [s for s in slot_payload if s["present"]]
    first_path = filled[0]["path"] if filled else None
    summary = ", ".join(s["basename"] for s in filled) if filled else ""
    return {
        "enabled": bool(transition_jingle_enabled),
        "gain": round(float(transition_jingle_gain), 4),
        "order": transition_jingle_order if transition_jingle_order in ("sequential", "random") else "sequential",
        "slots": slot_payload,
        "path": first_path,
        "basename": summary or "",
        "buffer_loaded": loaded,
    }


def push_transition_jingle_to_engine() -> None:
    if not engine:
        return
    paths = (transition_jingle_paths + [None, None, None, None])[:4]
    mode = transition_jingle_order.strip().lower() if transition_jingle_order else "sequential"
    if mode not in ("sequential", "random"):
        mode = "sequential"
    with engine.lock:
        engine.transition_jingle_enabled = bool(transition_jingle_enabled)
        engine.transition_jingle_gain = float(max(0.0, min(2.0, transition_jingle_gain)))
        engine.transition_jingle_mode = mode
    try:
        engine.set_transition_jingle_slots_from_paths(paths)
    except Exception as exc:
        append_status(f"Transition jingle load failed: {exc}")


class NoCacheHTMLResponse(HTMLResponse):
    def init_headers(self, headers: Optional[dict[str, str]] = None) -> None:
        super().init_headers(headers)
        self.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        self.headers["Pragma"] = "no-cache"
        self.headers["Expires"] = "0"


FALLBACK_INDEX_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>SAMSEL DJ Engine Pro</title>
  <style>
    body { background:#101216; color:#f3f4f6; font-family:Arial,sans-serif; margin:0; padding:24px; }
    .card { max-width:860px; margin:0 auto; background:#171a21; border:1px solid #2b3140; border-radius:16px; padding:24px; }
    code { background:#0f1117; padding:2px 6px; border-radius:6px; }
    a { color:#7dd3fc; }
  </style>
</head>
<body>
  <div class=\"card\">
    <h1>SAMSEL DJ Engine Pro</h1>
    <p>Your backend is running, but <code>templates/index.html</code> was not found.</p>
    <p>Place your professional DJ UI at <code>templates/index.html</code> beside this file, then refresh.</p>
    <p>Quick checks:</p>
    <ul>
      <li><a href=\"/status\">/status</a></li>
      <li><a href=\"/api/health\">/api/health</a></li>
      <li><a href=\"/docs\">/docs</a></li>
    </ul>
  </div>
</body>
</html>
"""


def safe_basename(filename: str) -> str:
    cleaned = Path(filename).name.replace("\x00", "").strip()
    return cleaned or "track"


def _library_cache_key(abs_path: Path) -> Optional[str]:
    try:
        st = abs_path.stat()
        # bump suffix when metadata fields change (invalidates stale cache rows)
        return f"{abs_path.resolve()}|{st.st_mtime_ns}|{st.st_size}|v4"
    except OSError:
        return None


def remember_track_analysis(path_str: str, bpm: float, duration_sec: float) -> None:
    """Cache BPM/duration from make_deck or engine (avoids re-analysis on /status)."""
    p = Path(path_str)
    if not p.is_file():
        return
    key = _library_cache_key(p)
    if key:
        _track_analysis_cache[key] = {
            "bpm": round(float(bpm), 2),
            "duration_sec": round(float(duration_sec), 2),
        }


def compute_track_analysis(abs_path: Path) -> dict[str, Any]:
    """Detect BPM + duration using librosa (first ~120s) — same approach as deck load."""
    key = _library_cache_key(abs_path)
    if key and key in _track_analysis_cache:
        return dict(_track_analysis_cache[key])

    result: dict[str, Any] = {"bpm": None, "duration_sec": None}
    if np is None:
        if key:
            _track_analysis_cache[key] = dict(result)
        return dict(result)

    try:
        import librosa
        from dj_engine_pro import detect_bpm_and_beats

        dur = float(librosa.get_duration(path=str(abs_path)))
        result["duration_sec"] = round(dur, 2)
        cap = min(120.0, dur) if dur > 0 else 120.0
        y, sr = librosa.load(str(abs_path), sr=22050, mono=False, duration=cap)
        y = np.asarray(y, dtype=np.float32)
        if y.ndim == 1:
            stereo = np.column_stack([y, y])
        else:
            stereo = y.T[:, :2]
        tempo, _ = detect_bpm_and_beats(stereo, int(sr))
        if tempo and float(tempo) > 0:
            result["bpm"] = round(float(tempo), 2)
    except Exception:
        pass

    if key:
        _track_analysis_cache[key] = dict(result)
    return dict(result)


def deck_summary_without_engine(deck_name: str) -> Optional[dict[str, Any]]:
    """Deck fields for /status when the engine is stopped but a path is loaded."""
    path = deck_paths.get(deck_name)
    if not path:
        return None
    playlist = deck_playlists.get(deck_name, [])
    idx = deck_playlist_index.get(deck_name, -1)
    p = Path(path)
    an = compute_track_analysis(p) if p.is_file() else {"bpm": None, "duration_sec": None}
    bpm_val = an.get("bpm")
    dur_val = an.get("duration_sec")
    return {
        "path": path,
        "basename": os.path.basename(path) or "No track",
        "bpm": round(float(bpm_val), 2) if bpm_val is not None else None,
        "duration_sec": round(float(dur_val), 2) if dur_val is not None else None,
        "playhead": 0,
        "playhead_sec": 0.0,
        "playing": False,
        "gain": 1.0,
        "mute": False,
        "quantize": False,
        "hot_cues": {},
        "loop": {"enabled": False, "start_sample": 0, "end_sample": 0},
        "roll": {"enabled": False, "start_sample": 0, "end_sample": 0},
        "beat_samples": [],
        "audio_len": 0,
        "sr": 0,
        "waveform": None,
        "playlist": playlist,
        "playlist_details": playlist_details_for_paths(playlist),
        "playlist_index": idx,
        "finished": bool(deck_finished_flags.get(deck_name, False)),
    }


def _bpm_from_tinytag(tag: Any) -> Optional[float]:
    """TBPM / tmpo / etc. map to TinyTag.other['other.bpm'] (list of strings)."""
    other = getattr(tag, "other", None)
    if not other:
        return None
    try:
        vals = other.get("other.bpm")
    except Exception:
        return None
    if not vals:
        return None
    raw = vals[0] if isinstance(vals, (list, tuple)) else vals
    if raw is None:
        return None
    try:
        return round(float(str(raw).strip().replace(",", ".")), 2)
    except (ValueError, TypeError):
        return None


def safe_resolve_upload_rel_path(rel: str) -> Path:
    """Resolve a path relative to UPLOAD_DIR; reject escapes."""
    rel_clean = (rel or "").replace("\\", "/").strip().lstrip("/")
    if not rel_clean or ".." in Path(rel_clean).parts:
        raise HTTPException(status_code=400, detail="Invalid library path")
    base = UPLOAD_DIR.resolve()
    full = (base / rel_clean).resolve()
    try:
        full.relative_to(base)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Path must stay under uploads") from exc
    if not full.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if full.suffix.lower() not in SUPPORTED_AUDIO:
        raise HTTPException(status_code=400, detail="Unsupported audio format")
    return full


# --- Lyrics downloader (same syncedlyrics + lrclib stack as SAMSEL Web / AutoMix) ---
_ac_lyrics_mod: Any = None
_ac_lyrics_load_attempted: bool = False
_ac_lyrics_fetcher: Any = None


def _get_automix_core_for_lyrics() -> Optional[Any]:
    """Load ``samsel_web/automix_core.py`` once (Downloader shared module; no package __init__ required)."""
    global _ac_lyrics_mod, _ac_lyrics_load_attempted
    if _ac_lyrics_load_attempted:
        return _ac_lyrics_mod
    _ac_lyrics_load_attempted = True
    path = BASE_DIR / "samsel_web" / "automix_core.py"
    if not path.is_file():
        append_status("Lyrics downloader: samsel_web/automix_core.py not found.")
        return None
    spec = importlib.util.spec_from_file_location("samsel_automix_core_lyrics", path)
    if spec is None or spec.loader is None:
        append_status("Lyrics downloader: could not create import spec for automix_core.")
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:
        append_status(f"Lyrics downloader: automix_core load failed: {exc}")
        return None
    _ac_lyrics_mod = mod
    return mod


def _get_lyrics_fetcher() -> Optional[Any]:
    global _ac_lyrics_fetcher
    mod = _get_automix_core_for_lyrics()
    if not mod:
        return None
    if _ac_lyrics_fetcher is None:
        _ac_lyrics_fetcher = mod.LyricsFetcher(append_status)
    return _ac_lyrics_fetcher


def _lyrics_search_query_for_file(full: Path, mod: Any) -> str:
    """Prefer ``Artist - Title`` from tags for lrclib; fall back to cleaned filename stem."""
    stem_q = mod.lyrics_query_from_filename_stem(full.stem)
    if TinyTag is None:
        return stem_q
    try:
        tag = TinyTag.get(str(full))
        ar = (getattr(tag, "artist", None) or "").strip()
        ti = (getattr(tag, "title", None) or "").strip()
        if ar and ti:
            return f"{ar} - {ti}"
    except Exception:
        pass
    return stem_q


def _upload_rel_posix(full: Path) -> str:
    return full.resolve().relative_to(UPLOAD_DIR.resolve()).as_posix()


def _effective_lyrics_query(full: Path, mod: Any, query_override: Optional[str]) -> str:
    o = (query_override or "").strip()
    if o:
        return o
    return _lyrics_search_query_for_file(full, mod)


def _lyrics_run_fetch_to_sidecar(
    full: Path,
    mod: Any,
    lf: Any,
    *,
    overwrite: bool,
    query_override: Optional[str],
) -> dict[str, Any]:
    """Write ``<stem>.lrc`` beside ``full`` (must live under ``UPLOAD_DIR``)."""
    lrc_path = full.with_suffix(".lrc")
    rel_s = _upload_rel_posix(full)
    if lrc_path.is_file() and not overwrite:
        append_status(f"[LYRICS] Skip (exists): {lrc_path.name}")
        return {
            "ok": True,
            "skipped": True,
            "rel_path": rel_s,
            "lrc_path": str(lrc_path),
            "query": None,
        }
    query = _effective_lyrics_query(full, mod, query_override)
    append_status(f"[LYRICS] Search: {query}")
    ok = bool(lf.fetch_lrc(query, str(lrc_path)))
    return {
        "ok": ok,
        "skipped": False,
        "rel_path": rel_s,
        "lrc_path": str(lrc_path) if ok else None,
        "query": query,
        "error": None if ok else "No synced lyrics returned for this search",
    }


def _deck_track_path_under_uploads(deck: str) -> Path:
    key = deck.strip().upper()
    if key not in ("A", "B"):
        raise HTTPException(status_code=400, detail="Deck must be A or B")
    raw = deck_paths.get(key)
    if not raw:
        raise HTTPException(status_code=400, detail=f"No audio loaded on deck {key}")
    full = Path(raw).resolve()
    base = UPLOAD_DIR.resolve()
    try:
        full.relative_to(base)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Loaded file is not under uploads/. Load from the library or uploads folder to fetch .lrc here.",
        ) from exc
    if not full.is_file():
        raise HTTPException(status_code=404, detail="Deck file not found")
    if full.suffix.lower() not in SUPPORTED_AUDIO:
        raise HTTPException(status_code=400, detail="Unsupported audio format for this deck path")
    return full


def probe_audio_metadata(abs_path: Path) -> dict[str, Any]:
    """Read tags, duration, and technical fields for one audio file (cached by mtime/size)."""
    key = _library_cache_key(abs_path)
    if key and key in _library_meta_cache:
        row = dict(_library_meta_cache[key])
        row["basename"] = abs_path.name
        if key in _track_analysis_cache:
            tb = _track_analysis_cache[key]
            if tb.get("bpm") is not None:
                row["bpm_analyzed"] = tb["bpm"]
        return row

    row: dict[str, Any] = {
        "basename": abs_path.name,
        "size_bytes": None,
        "modified_iso": None,
        "duration_sec": None,
        "channels": None,
        "samplerate": None,
        "bitrate": None,
        "bitdepth": None,
        "format": abs_path.suffix.lower().lstrip(".") or None,
        "title": None,
        "artist": None,
        "album": None,
        "track": None,
        "track_total": None,
        "disc": None,
        "disc_total": None,
        "genre": None,
        "year": None,
        "composer": None,
        "comment": None,
        "bpm_tag": None,
        "bpm_analyzed": None,
    }
    try:
        st = abs_path.stat()
        row["size_bytes"] = int(st.st_size)
        row["modified_iso"] = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
    except OSError:
        if key:
            _library_meta_cache[key] = {k: v for k, v in row.items() if k != "basename"}
        return row

    if TinyTag is not None:
        try:
            tag = TinyTag.get(str(abs_path))
            if tag.duration is not None:
                row["duration_sec"] = round(float(tag.duration), 3)
            if tag.channels is not None:
                row["channels"] = int(tag.channels)
            if tag.samplerate is not None:
                row["samplerate"] = int(tag.samplerate)
            if tag.bitrate is not None:
                row["bitrate"] = int(tag.bitrate)
            if getattr(tag, "bitdepth", None) is not None:
                row["bitdepth"] = int(tag.bitdepth)
            for src, dst in (
                ("title", "title"),
                ("artist", "artist"),
                ("album", "album"),
                ("genre", "genre"),
                ("comment", "comment"),
                ("composer", "composer"),
            ):
                val = getattr(tag, src, None)
                if val is not None and str(val).strip():
                    row[dst] = str(val).strip()
            if getattr(tag, "track", None) is not None:
                row["track"] = str(tag.track)
            if getattr(tag, "track_total", None) is not None:
                row["track_total"] = str(tag.track_total)
            if getattr(tag, "disc", None) is not None:
                row["disc"] = str(tag.disc)
            if getattr(tag, "disc_total", None) is not None:
                row["disc_total"] = str(tag.disc_total)
            if getattr(tag, "year", None) is not None:
                row["year"] = str(tag.year)
            bpm_val = _bpm_from_tinytag(tag)
            if bpm_val is not None:
                row["bpm_tag"] = bpm_val
        except Exception:
            pass

    if row["duration_sec"] is None:
        try:
            import librosa

            d = float(librosa.get_duration(path=str(abs_path)))
            row["duration_sec"] = round(d, 3)
        except Exception:
            pass

    if key and key in _track_analysis_cache:
        tb = _track_analysis_cache[key]
        if tb.get("bpm") is not None:
            row["bpm_analyzed"] = tb["bpm"]

    if key:
        _library_meta_cache[key] = {k: v for k, v in row.items() if k != "basename"}
    return row


def playlist_item_bpm_for_display(path_str: str) -> Optional[float]:
    """Cached BPM for UI only (tags + analysis cache); does not run librosa on /status."""
    p = Path(path_str)
    if not p.is_file():
        return None
    meta = probe_audio_metadata(p)
    if meta.get("bpm_tag") is not None:
        try:
            v = float(meta["bpm_tag"])
            return round(v, 2) if v > 0 else None
        except (TypeError, ValueError):
            pass
    if meta.get("bpm_analyzed") is not None:
        try:
            v = float(meta["bpm_analyzed"])
            return round(v, 2) if v > 0 else None
        except (TypeError, ValueError):
            pass
    ck = _library_cache_key(p)
    if ck and ck in _track_analysis_cache:
        b = _track_analysis_cache[ck].get("bpm")
        if b is not None:
            try:
                v = float(b)
                return round(v, 2) if v > 0 else None
            except (TypeError, ValueError):
                pass
    return None


def playlist_details_for_paths(paths: list[str]) -> list[dict[str, Any]]:
    """Per-row playlist info for the web UI (order matches deck playlist)."""
    return [
        {
            "path": pth,
            "basename": os.path.basename(pth) or pth,
            "bpm": playlist_item_bpm_for_display(pth),
        }
        for pth in paths
    ]


def effective_bpm_for_sort(path_str: str) -> Optional[float]:
    """BPM for ordering: embedded tag first, then analysis cache / librosa."""
    p = Path(path_str)
    if not p.is_file():
        return None
    meta = probe_audio_metadata(p)
    if meta.get("bpm_tag") is not None:
        try:
            v = float(meta["bpm_tag"])
            return v if v > 0 else None
        except (TypeError, ValueError):
            pass
    if meta.get("bpm_analyzed") is not None:
        try:
            v = float(meta["bpm_analyzed"])
            return v if v > 0 else None
        except (TypeError, ValueError):
            pass
    an = compute_track_analysis(p)
    if an.get("bpm") is not None:
        try:
            v = float(an["bpm"])
            return v if v > 0 else None
        except (TypeError, ValueError):
            pass
    return None


def sort_deck_playlist_by_bpm(key: str, *, raise_on_reload_error: bool = True) -> dict[str, Any]:
    """Reorder deck_playlists[key] ascending by BPM; unknown BPM last (stable by path)."""
    pl = list(deck_playlists.get(key, []))
    if len(pl) <= 1:
        return {
            "paths": pl,
            "playlist_index": deck_playlist_index.get(key, 0),
            "sorted": False,
        }

    def sort_key(path_str: str) -> tuple[float, str]:
        b = effective_bpm_for_sort(path_str)
        if b is None:
            return (float("inf"), path_str.lower())
        return (b, path_str.lower())

    new_order = sorted(pl, key=sort_key)
    previous_active = deck_paths.get(key)
    deck_playlists[key] = new_order

    if previous_active and previous_active in new_order:
        deck_playlist_index[key] = new_order.index(previous_active)
        deck_paths[key] = previous_active
    elif new_order:
        deck_playlist_index[key] = 0
        deck_paths[key] = new_order[0]
    else:
        deck_playlist_index[key] = -1

    deck_finished_flags[key] = False
    append_status(f"Deck {key} playlist sorted by BPM ↑ ({len(new_order)} tracks)")

    new_active = deck_paths.get(key)
    if engine and new_active and new_active != previous_active:
        try:
            reload_engine_deck(key, new_active)
        except Exception as exc:
            append_status(f"Deck {key} engine reload after BPM sort failed: {exc}")
            if raise_on_reload_error:
                raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "paths": new_order,
        "playlist_index": deck_playlist_index[key],
        "sorted": True,
    }


async def save_upload_file(file: UploadFile, prefix: str = "") -> tuple[str, str]:
    filename = safe_basename(file.filename or "track")
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_AUDIO:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {ext or '(none)'}")

    unique_name = f"{prefix}{uuid.uuid4().hex}_{filename}"
    path = UPLOAD_DIR / unique_name

    total = 0
    with path.open("wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                out.close()
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
                raise HTTPException(status_code=413, detail="File too large")
            await asyncio.to_thread(out.write, chunk)
    await file.close()
    return str(path), filename


async def save_many_upload_files(files: list[UploadFile], deck: str) -> list[str]:
    saved: list[str] = []
    for index, file in enumerate(files):
        if not file.filename:
            await file.close()
            continue
        try:
            path, _ = await save_upload_file(file, prefix=f"{deck}_{index:04d}_")
            saved.append(path)
        except HTTPException:
            await file.close()
            continue
    return sorted(saved, key=str.lower)


async def maybe_render_template(request: Request, template_name: str = DEFAULT_TEMPLATE_NAME) -> HTMLResponse:
    template_path = TEMPLATES_DIR / template_name
    if not template_path.exists():
        return NoCacheHTMLResponse(FALLBACK_INDEX_HTML)

    try:
        # Explicit keyword arguments avoid the TemplateResponse signature mismatch that caused the 500 crash.
        response = templates.TemplateResponse(
            request=request,
            name=template_name,
            context={
                "app_title": app.title,
                "websocket_path": "/ws/stream",
                "status_path": "/status",
                "health_path": "/api/health",
            },
        )
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response
    except Exception as exc:
        append_status(f"Template render fallback used: {exc}")
        return NoCacheHTMLResponse(FALLBACK_INDEX_HTML)



def get_deck(deck_name: str):
    if not engine:
        return None
    key = deck_name.upper()
    if key == "A":
        return engine.deck_a
    if key == "B":
        return engine.deck_b
    raise HTTPException(status_code=400, detail="Deck must be A or B")



def build_waveform_peaks(deck, width: int = 8192) -> Optional[dict[str, list[float]]]:
    """Min/max peaks plus low/mid/high band energy per column. High column count keeps max-zoom view sharp."""
    if np is None or not deck or getattr(deck, "audio", None) is None:
        return None
    if len(deck.audio) == 0:
        return None

    audio = deck.audio
    ndim = getattr(audio, "ndim", 1)
    n = int(len(audio) if ndim == 1 else len(audio))
    if n <= 0:
        return None

    cache_key = (id(audio), ndim, n, width)
    cached = _waveform_spectral_cache.get(cache_key)
    if cached is not None:
        _waveform_spectral_cache.move_to_end(cache_key)
        return cached

    if ndim == 1:
        mono = np.ascontiguousarray(audio, dtype=np.float64)
    else:
        mono = np.ascontiguousarray(np.mean(audio, axis=1), dtype=np.float64)

    sr = max(8000, int(getattr(deck, "sr", None) or 44100))
    n_fft = 1024
    samples_per_pixel = max(1, n // width)
    freqs = np.fft.rfftfreq(n_fft, 1.0 / float(sr))
    mask_low = freqs < 220.0
    mask_mid = (freqs >= 220.0) & (freqs < 5200.0)
    mask_high = freqs >= 5200.0
    win = np.hanning(n_fft)

    mins: list[float] = []
    maxs: list[float] = []
    lows: list[float] = []
    mids: list[float] = []
    highs: list[float] = []

    buf = np.zeros(n_fft, dtype=np.float64)

    for i in range(width):
        start = i * samples_per_pixel
        end = min(n, start + samples_per_pixel)
        if start >= n:
            mins.append(0.0)
            maxs.append(0.0)
            lows.append(0.0)
            mids.append(0.0)
            highs.append(0.0)
            continue
        chunk = mono[start:end]
        mins.append(float(np.min(chunk)))
        maxs.append(float(np.max(chunk)))

        nc = int(end - start)
        if nc < 4:
            lows.append(0.0)
            mids.append(0.0)
            highs.append(0.0)
            continue

        buf.fill(0.0)
        if nc >= n_fft:
            off = (nc - n_fft) // 2
            buf[:] = chunk[off : off + n_fft]
        else:
            buf[:nc] = chunk

        buf *= win
        power = np.abs(np.fft.rfft(buf)) ** 2
        lows.append(float(np.sum(power[mask_low])))
        mids.append(float(np.sum(power[mask_mid])))
        highs.append(float(np.sum(power[mask_high])))

    L = np.log1p(np.maximum(np.asarray(lows, dtype=np.float64), 0.0))
    M = np.log1p(np.maximum(np.asarray(mids, dtype=np.float64), 0.0))
    H = np.log1p(np.maximum(np.asarray(highs, dtype=np.float64), 0.0))
    peak = np.maximum(np.maximum(L, M), H)
    den = float(np.percentile(peak, 92)) if peak.size else 1.0
    if den < 1e-12:
        den = 1.0
    L = np.clip(L / den, 0, 1)
    M = np.clip(M / den, 0, 1)
    H = np.clip(H / den, 0, 1)

    result = {
        "mins": mins,
        "maxs": maxs,
        "low": L.tolist(),
        "mid": M.tolist(),
        "high": H.tolist(),
    }
    _waveform_spectral_cache[cache_key] = result
    _waveform_spectral_cache.move_to_end(cache_key)
    while len(_waveform_spectral_cache) > _WAVEFORM_CACHE_MAX:
        _waveform_spectral_cache.popitem(last=False)
    return result



def deck_to_dict(deck, deck_name: str, *, include_waveform: bool = True) -> Optional[dict[str, Any]]:
    if not deck:
        return None

    path = deck_paths.get(deck_name)
    playlist = deck_playlists.get(deck_name, [])
    idx = deck_playlist_index.get(deck_name, -1)

    audio = getattr(deck, "audio", None)
    audio_len = int(len(audio)) if audio is not None else 0
    sr = int(getattr(deck, "sr", 0) or 0)
    playhead = int(getattr(deck, "playhead", 0) or 0)
    playhead_sec = round(playhead / sr, 3) if sr else 0.0
    beat_samples = getattr(deck, "beat_samples", [])
    if hasattr(beat_samples, "tolist"):
        beat_samples = beat_samples.tolist()
    else:
        beat_samples = list(beat_samples)

    loop_obj = getattr(deck, "loop", None)
    roll_obj = getattr(deck, "roll", None)

    out: dict[str, Any] = {
        "path": path or getattr(deck, "track_path", None),
        "basename": os.path.basename(path or getattr(deck, "track_path", "") or "") or "No track",
        "bpm": round(float(getattr(deck, "bpm", 0.0) or 0.0), 2),
        "duration_sec": round(float(getattr(deck, "duration_sec", 0.0) or 0.0), 2),
        "playhead": playhead,
        "playhead_sec": playhead_sec,
        "playing": bool(getattr(deck, "playing", False)),
        "gain": float(getattr(deck, "gain", 1.0) or 1.0),
        "mute": bool(getattr(deck, "mute", False)),
        "quantize": bool(getattr(deck, "quantize", False)),
        "hot_cues": getattr(deck, "hot_cues", {}),
        "loop": {
            "enabled": bool(getattr(loop_obj, "enabled", False)),
            "start_sample": int(getattr(loop_obj, "start_sample", 0) or 0),
            "end_sample": int(getattr(loop_obj, "end_sample", 0) or 0),
        },
        "roll": {
            "enabled": bool(getattr(roll_obj, "enabled", False)),
            "start_sample": int(getattr(roll_obj, "start_sample", 0) or 0),
            "end_sample": int(getattr(roll_obj, "end_sample", 0) or 0),
        },
        "beat_samples": beat_samples,
        "audio_len": audio_len,
        "sr": sr,
        "playlist": playlist,
        "playlist_details": playlist_details_for_paths(playlist),
        "playlist_index": idx,
        "finished": bool(deck_finished_flags.get(deck_name, False)),
    }
    if include_waveform:
        out["waveform"] = build_waveform_peaks(deck)
    return out



def require_engine() -> DJEnginePro:
    if not engine:
        raise HTTPException(
            status_code=400,
            detail="Start the engine first. Load both decks, then click 'Start Engine'.",
        )
    return engine



def choose_output_sample_rate() -> int:
    try:
        import sounddevice as sd

        try:
            dev = sd.query_devices(kind="output")
            target_sr = int(dev.get("default_samplerate", 48000))
        except Exception:
            target_sr = 48000
    except Exception:
        target_sr = 48000
    if target_sr < 16000:
        target_sr = 48000
    return target_sr



def make_json_response(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    response = JSONResponse(payload, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response



def _canonical_audio_path(path: Optional[str]) -> Optional[str]:
    """Stable path identity for Auto DJ (skip same file on other deck / consecutive repeat)."""
    if not path:
        return None
    p = str(path)
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(p)))
    except Exception:
        return p


def reload_engine_deck(deck: str, path: str) -> None:
    assert engine is not None
    with engine.lock:
        old = get_deck(deck)
        target_sr = int(getattr(old, "sr", 48000) or 48000)
    new_deck = make_deck(deck, path, target_sr=target_sr)
    with engine.lock:
        old = get_deck(deck)
        new_deck.gain = getattr(old, "gain", 1.0)
        new_deck.mute = getattr(old, "mute", False)
        new_deck.quantize = getattr(old, "quantize", False)
        new_deck.playing = getattr(old, "playing", False)
        if deck == "A":
            engine.deck_a = new_deck
        else:
            engine.deck_b = new_deck
    remember_track_analysis(path, float(new_deck.bpm), float(new_deck.duration_sec))


def _blocking_finish_single_deck_upload(key: str, path: str):
    """Load track (librosa/make_deck) and hot-reload engine. Run via asyncio.to_thread."""
    deck_paths[key] = path
    deck_playlists[key] = [path]
    deck_playlist_index[key] = 0
    deck_finished_flags[key] = False
    d = make_deck(key, path)
    remember_track_analysis(path, float(d.bpm), float(d.duration_sec))
    if engine:
        reload_engine_deck(key, path)
    return d


def _blocking_finish_folder_upload_after_save(key: str, paths: list[str]) -> tuple[int, list[str], Optional[str]]:
    deck_playlists[key] = paths
    deck_playlist_index[key] = 0
    deck_paths[key] = paths[0]
    deck_finished_flags[key] = False
    append_status(f"Deck {key} folder loaded: {len(paths)} tracks")
    sort_deck_playlist_by_bpm(key, raise_on_reload_error=False)
    current = deck_paths.get(key) or (deck_playlists[key][0] if deck_playlists[key] else None)
    if engine and current:
        try:
            reload_engine_deck(key, current)
        except Exception as exc:
            append_status(f"Deck {key} hot-reload failed after folder upload: {exc}")
    return len(deck_playlists[key]), list(deck_playlists[key]), current


def _blocking_finish_load_folder(key: str, files: list[str]) -> tuple[int, Optional[str]]:
    deck_playlists[key] = files
    deck_playlist_index[key] = 0
    deck_paths[key] = files[0]
    deck_finished_flags[key] = False
    append_status(f"Deck {key} playlist set: {len(files)} tracks")
    sort_deck_playlist_by_bpm(key, raise_on_reload_error=False)
    current = deck_paths.get(key) or (deck_playlists[key][0] if deck_playlists[key] else None)
    if engine and current:
        try:
            reload_engine_deck(key, current)
        except Exception as exc:
            append_status(f"Deck {key} hot-reload failed after playlist load: {exc}")
    return len(deck_playlists[key]), current


def auto_dj_prepare_incoming_deck(deck_key: str) -> None:
    """Advance playlist for the incoming deck; skip tracks that match the other deck or repeat this deck's current file."""
    key = deck_key.upper()
    if key not in {"A", "B"}:
        return
    pl = deck_playlists.get(key, [])
    if len(pl) <= 1:
        return
    idx = deck_playlist_index.get(key, -1)
    if idx < 0:
        idx = 0
    other = "B" if key == "A" else "A"

    other_path_live: Optional[str] = deck_paths.get(other)
    self_path_live: Optional[str] = deck_paths.get(key)
    if engine:
        try:
            with engine.lock:
                odeck = engine.deck_b if other == "B" else engine.deck_a
                sdeck = engine.deck_a if key == "A" else engine.deck_b
                other_path_live = getattr(odeck, "track_path", None) or other_path_live
                self_path_live = getattr(sdeck, "track_path", None) or self_path_live
        except Exception:
            pass

    avoid_other = _canonical_audio_path(other_path_live)
    avoid_self = _canonical_audio_path(self_path_live)

    start = (idx + 1) % len(pl)
    nxt = start
    for _ in range(len(pl)):
        cand = _canonical_audio_path(pl[nxt])
        clash_other = bool(avoid_other and cand and cand == avoid_other)
        repeat_self = bool(avoid_self and cand and cand == avoid_self)
        if not clash_other and not repeat_self:
            break
        nxt = (nxt + 1) % len(pl)
    else:
        return

    deck_playlist_index[key] = nxt
    deck_paths[key] = pl[nxt]
    deck_finished_flags[key] = False
    if engine:
        reload_engine_deck(key, pl[nxt])


def parse_stream_format(websocket: WebSocket) -> str:
    query = parse_qs(websocket.scope.get("query_string", b"").decode("utf-8", errors="ignore"))
    fmt = (query.get("format", ["i16"])[0] or "i16").lower()
    return fmt if fmt in {"i16", "f32"} else "i16"



def convert_stream_bytes(raw_bytes: bytes, fmt: str) -> bytes:
    if fmt == "f32" or np is None:
        return raw_bytes
    try:
        pcm = np.frombuffer(raw_bytes, dtype=np.float32)
        pcm = np.clip(pcm, -1.0, 1.0)
        return (pcm * 32767.0).astype(np.int16).tobytes()
    except Exception:
        return raw_bytes


# --- Pydantic models ---
class LoadFolderRequest(BaseModel):
    paths: list[str]


class PlayLibraryFileRequest(BaseModel):
    deck: str
    rel_path: str


class LyricsDownloaderFetchRequest(BaseModel):
    """Fetch synced LRC via ``syncedlyrics`` (lrclib.net and other providers) next to the audio file."""

    rel_path: str
    overwrite: bool = False
    query_override: Optional[str] = None


class LyricsDownloaderFetchDeckRequest(BaseModel):
    """Fetch LRC for the file currently loaded on deck A or B (path must be under ``uploads/``)."""

    deck: str
    overwrite: bool = False
    query_override: Optional[str] = None


class LyricsDownloaderFetchBatchRequest(BaseModel):
    rel_paths: list[str] = Field(default_factory=list)
    overwrite: bool = False
    max_files: int = Field(default=80, ge=1, le=300)


class TransitionJingleConfigBody(BaseModel):
    enabled: bool = False
    gain: float = Field(default=0.65, ge=0.0, le=2.0)
    order: str = "sequential"


# --- Routes ---
@app.get("/downloader_embed", response_class=HTMLResponse)
async def downloader_embed_page():
    """Self-contained Downloader UI for the desktop engine modal (iframe). Same assets as SAMSEL Web."""
    if not DOWNLOADER_EMBED_PATH.is_file():
        return PlainTextResponse("downloader_embed.html missing", status_code=404)
    return NoCacheHTMLResponse(DOWNLOADER_EMBED_PATH.read_text(encoding="utf-8"))


@app.get("/trim_embed", response_class=HTMLResponse)
async def trim_embed_page():
    """SAMSEL Web Silence trim panel for the engine modal (iframe). Uses ``samsel.js`` trim + playlist paths."""
    if not TRIM_EMBED_PATH.is_file():
        return PlainTextResponse("trim_embed.html missing", status_code=404)
    return NoCacheHTMLResponse(TRIM_EMBED_PATH.read_text(encoding="utf-8"))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return await maybe_render_template(request)


@app.get("/api/health")
def api_health():
    return {
        "ok": True,
        "version": "2.0.0",
        "engine_running": bool(engine and getattr(engine, "running", False)),
        "deck_a_loaded": bool(deck_paths["A"]),
        "deck_b_loaded": bool(deck_paths["B"]),
        "template_present": (TEMPLATES_DIR / DEFAULT_TEMPLATE_NAME).exists(),
        "recording": bool(engine and engine.is_recording()) if engine else False,
        "ws_stream": "/ws/stream?format=i16",
    }


@app.get("/robots.txt")
def robots_txt():
    return PlainTextResponse("User-agent: *\nDisallow:\n")


@app.get("/status")
def get_status(waveforms: bool = Query(False, description="Include large per-deck waveform arrays (slow; omit for polling).")):
    if not engine:
        return {
            "engine_running": False,
            "stream_available": False,
            "crossfader": crossfader_val,
            "master_gain": master_gain_val,
            "engine_eq_db": list(engine_eq_bands_val),
            "auto_dj": False,
            "recording": False,
            "recording_path": None,
            "deck_a": deck_summary_without_engine("A"),
            "deck_b": deck_summary_without_engine("B"),
            "status_text": "\n".join(status_log[-20:]),
            "log": status_log[-30:],
            "mobile_stream": {"path": "/ws/stream?format=i16", "recommended_format": "i16"},
            "transition_jingle": transition_jingle_status_payload(),
        }

    with engine.lock:
        deck_a = engine.deck_a
        deck_b = engine.deck_b
        crossfader = float(engine.crossfader)
        master_gain = float(engine.master_gain)
        engine_eq_db = [float(x) for x in getattr(engine, "engine_eq_db", [0.0] * 10)]
        auto_dj = bool(engine.auto_dj_enabled)
        status_text = engine.status()
        running = bool(engine.running)
        stream_available = engine.stream_queue is not None
        recording = bool(engine.is_recording())
        recording_path = engine.get_recording_path()
        sr = int(getattr(engine, "sr", 48000) or 48000)

    return {
        "engine_running": running,
        "stream_available": stream_available,
        "crossfader": crossfader,
        "master_gain": master_gain,
        "engine_eq_db": engine_eq_db,
        "auto_dj": auto_dj,
        "recording": recording,
        "recording_path": recording_path,
        "engine_sr": sr,
        "deck_a": deck_to_dict(deck_a, "A", include_waveform=waveforms),
        "deck_b": deck_to_dict(deck_b, "B", include_waveform=waveforms),
        "status_text": status_text,
        "log": status_log[-30:],
        "mobile_stream": {"path": "/ws/stream?format=i16", "recommended_format": "i16"},
        "transition_jingle": transition_jingle_status_payload(),
    }


@app.get("/api/status")
def api_status_alias(waveforms: bool = Query(False)):
    return get_status(waveforms=waveforms)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if job:
        return job
    return make_json_response(
        {
            "job_id": job_id,
            "state": "missing",
            "ok": False,
            "message": "No background job exists for this id in this build.",
        },
        status_code=404,
    )


@app.get("/api/library")
def api_library():
    """All audio files under uploads/ with tag and technical metadata."""
    base = UPLOAD_DIR.resolve()
    rows: list[dict[str, Any]] = []
    if not base.is_dir():
        return {"files": []}
    for p in sorted(base.rglob("*"), key=lambda x: str(x).lower()):
        if not p.is_file() or p.suffix.lower() not in SUPPORTED_AUDIO:
            continue
        try:
            rel = p.relative_to(base)
        except ValueError:
            continue
        rel_s = rel.as_posix()
        meta = probe_audio_metadata(p)
        meta["rel_path"] = rel_s
        meta["path"] = str(p.resolve())
        rows.append(meta)
    return {"files": rows}


@app.post("/api/play_library_file")
def api_play_library_file(body: PlayLibraryFileRequest):
    """Load a file from the uploads library onto a deck (replaces that deck's playlist with this track)."""
    key = body.deck.upper()
    if key not in {"A", "B"}:
        return make_json_response({"error": "Deck must be A or B"}, status_code=400)

    full = safe_resolve_upload_rel_path(body.rel_path)
    path_str = str(full)

    deck_paths[key] = path_str
    deck_playlists[key] = [path_str]
    deck_playlist_index[key] = 0
    deck_finished_flags[key] = False
    append_status(f"Deck {key} library play: {full.name}")

    if engine:
        try:
            reload_engine_deck(key, path_str)
            with engine.lock:
                get_deck(key).play()
                deck_finished_flags[key] = False
            append_status(f"Deck {key} playing: {full.name}")
        except Exception as exc:
            append_status(f"Deck {key} engine reload failed: {exc}")
            return make_json_response({"ok": False, "error": str(exc)}, status_code=500)
    return {"ok": True, "deck": key, "path": path_str, "basename": full.name}


@app.get("/api/lyrics_downloader/status")
def api_lyrics_downloader_status():
    """Whether server-side lyrics fetch (SAMSEL Web / Downloader parity) is available."""
    mod = _get_automix_core_for_lyrics()
    if not mod:
        return {
            "module_present": False,
            "syncedlyrics": False,
            "hint": "samsel_web/automix_core.py is missing from this install.",
        }
    lf = _get_lyrics_fetcher()
    ok = bool(lf and lf.available())
    return {
        "module_present": True,
        "syncedlyrics": ok,
        "hint": "" if ok else "Install on the server: pip install syncedlyrics",
    }


@app.post("/api/lyrics_downloader/fetch")
def api_lyrics_downloader_fetch(body: LyricsDownloaderFetchRequest):
    """Write ``<same_stem>.lrc`` beside the library file under uploads/."""
    mod = _get_automix_core_for_lyrics()
    if not mod:
        return make_json_response(
            {"ok": False, "error": "Lyrics module not available (samsel_web/automix_core.py)."},
            status_code=503,
        )
    lf = _get_lyrics_fetcher()
    if not lf or not lf.available():
        return make_json_response(
            {"ok": False, "error": "syncedlyrics is not installed or not on PATH. pip install syncedlyrics"},
            status_code=503,
        )
    full = safe_resolve_upload_rel_path(body.rel_path)
    out = _lyrics_run_fetch_to_sidecar(
        full,
        mod,
        lf,
        overwrite=body.overwrite,
        query_override=body.query_override,
    )
    return out


@app.post("/api/lyrics_downloader/fetch_deck")
def api_lyrics_downloader_fetch_deck(body: LyricsDownloaderFetchDeckRequest):
    """Same as ``/fetch`` but targets the file currently on deck A or B (must be under ``uploads/``)."""
    mod = _get_automix_core_for_lyrics()
    if not mod:
        return make_json_response(
            {"ok": False, "error": "Lyrics module not available (samsel_web/automix_core.py)."},
            status_code=503,
        )
    lf = _get_lyrics_fetcher()
    if not lf or not lf.available():
        return make_json_response(
            {"ok": False, "error": "syncedlyrics is not installed or not on PATH. pip install syncedlyrics"},
            status_code=503,
        )
    try:
        full = _deck_track_path_under_uploads(body.deck)
    except HTTPException:
        raise
    return _lyrics_run_fetch_to_sidecar(
        full,
        mod,
        lf,
        overwrite=body.overwrite,
        query_override=body.query_override,
    )


@app.post("/api/lyrics_downloader/fetch_batch")
def api_lyrics_downloader_fetch_batch(body: LyricsDownloaderFetchBatchRequest):
    """Fetch LRC for many uploads rows; each path is validated like ``/api/play_library_file``."""
    mod = _get_automix_core_for_lyrics()
    if not mod:
        return make_json_response(
            {"ok": False, "error": "Lyrics module not available.", "results": []},
            status_code=503,
        )
    lf = _get_lyrics_fetcher()
    if not lf or not lf.available():
        return make_json_response(
            {"ok": False, "error": "syncedlyrics not available.", "results": []},
            status_code=503,
        )
    raw = [str(x).replace("\\", "/").strip().lstrip("/") for x in (body.rel_paths or []) if str(x).strip()]
    max_n = int(body.max_files)
    slice_paths = raw[:max_n]
    results: list[dict[str, Any]] = []
    for rel in slice_paths:
        try:
            full = safe_resolve_upload_rel_path(rel)
        except HTTPException as exc:
            det = exc.detail
            if not isinstance(det, str):
                det = str(det)
            results.append({"rel_path": rel, "ok": False, "skipped": False, "error": det})
            continue
        one = _lyrics_run_fetch_to_sidecar(
            full,
            mod,
            lf,
            overwrite=body.overwrite,
            query_override=None,
        )
        results.append(
            {
                "rel_path": rel,
                "ok": bool(one.get("ok")),
                "skipped": bool(one.get("skipped")),
                "lrc_path": one.get("lrc_path"),
                "query": one.get("query"),
                "error": one.get("error"),
            }
        )
    return {
        "ok": True,
        "results": results,
        "truncated": len(raw) > max_n,
        "processed": len(slice_paths),
    }


@app.post("/upload/{deck}")
async def upload_deck(deck: str, file: UploadFile = File(...)):
    key = deck.upper()
    if key not in {"A", "B"}:
        return make_json_response({"error": "Deck must be A or B"}, status_code=400)

    path, original_name = await save_upload_file(file, prefix=f"{key}_")
    try:
        d = await asyncio.to_thread(_blocking_finish_single_deck_upload, key, path)
        append_status(f"Deck {key} loaded: {original_name}")
        return {
            "path": path,
            "basename": original_name,
            "bpm": round(float(d.bpm), 2),
            "duration": round(float(d.duration_sec), 2),
        }
    except Exception as exc:
        return make_json_response({"error": str(exc)}, status_code=500)


@app.post("/upload_folder/{deck}")
async def upload_folder(deck: str, files: list[UploadFile] = File(...)):
    key = deck.upper()
    if key not in {"A", "B"}:
        return make_json_response({"error": "Deck must be A or B"}, status_code=400)

    paths = await save_many_upload_files(files, key)
    if not paths:
        return make_json_response({"error": "No supported audio files"}, status_code=400)

    try:
        count, pl, current = await asyncio.to_thread(_blocking_finish_folder_upload_after_save, key, paths)
    except Exception as exc:
        return make_json_response({"error": str(exc)}, status_code=500)
    return {"count": count, "paths": pl, "current": current}


@app.post("/load_folder/{deck}")
async def load_folder(deck: str, body: LoadFolderRequest):
    key = deck.upper()
    if key not in {"A", "B"}:
        return make_json_response({"error": "Deck must be A or B"}, status_code=400)

    files = [str(Path(p)) for p in body.paths if Path(p).suffix.lower() in SUPPORTED_AUDIO]
    if not files:
        return make_json_response({"error": "No supported audio files"}, status_code=400)

    try:
        count, current = await asyncio.to_thread(_blocking_finish_load_folder, key, files)
    except Exception as exc:
        return make_json_response({"error": str(exc)}, status_code=500)
    return {"count": count, "current": current}


class EngineEqPayload(BaseModel):
    bands: list[float] = Field(..., min_length=10, max_length=10)


@app.post("/init_engine")
def init_engine():
    global engine, crossfader_val, master_gain_val
    if not deck_paths["A"] or not deck_paths["B"]:
        return make_json_response({"error": "Load both Deck A and Deck B first"}, status_code=400)

    if engine:
        try:
            engine.stop()
        except Exception:
            pass
        engine = None

    try:
        target_sr = choose_output_sample_rate()
        deck_a = make_deck("A", deck_paths["A"], target_sr=target_sr)
        deck_b = make_deck("B", deck_paths["B"], target_sr=target_sr)
        remember_track_analysis(deck_paths["A"], float(deck_a.bpm), float(deck_a.duration_sec))
        remember_track_analysis(deck_paths["B"], float(deck_b.bpm), float(deck_b.duration_sec))
        engine = DJEnginePro(deck_a, deck_b, blocksize=4096)
        engine.auto_dj_prepare_incoming = auto_dj_prepare_incoming_deck
        engine.set_crossfader(crossfader_val)
        engine.set_master_gain(master_gain_val)
        engine.set_engine_eq_db(engine_eq_bands_val)
        engine.start()
        push_transition_jingle_to_engine()
        deck_finished_flags["A"] = False
        deck_finished_flags["B"] = False
        append_status(f"Engine started at {target_sr} Hz")
        return {"ok": True, "sr": target_sr}
    except Exception as exc:
        engine = None
        return make_json_response({"error": str(exc)}, status_code=500)


@app.post("/stop_engine")
def stop_engine():
    shutdown_audio_engine()
    return {"ok": True}


@app.websocket("/stream")
@app.websocket("/ws/stream")
async def audio_stream(websocket: WebSocket):
    await websocket.accept()

    current_engine = engine
    if not current_engine or not getattr(current_engine, "running", False):
        await websocket.send_json({"type": "error", "message": "Engine not running"})
        await websocket.close(code=1011)
        return

    q = current_engine.stream_queue
    if q is None:
        await websocket.send_json({"type": "error", "message": "Stream queue unavailable"})
        await websocket.close(code=1011)
        return

    fmt = parse_stream_format(websocket)
    meta = {
        "type": "stream_meta",
        "sr": int(getattr(current_engine, "sr", 48000) or 48000),
        "channels": 2,
        "format": fmt,
        "endianness": "little",
        "transport": "websocket",
        "recommended_for_mobile": fmt == "i16",
    }
    await websocket.send_json(meta)

    last_keepalive = asyncio.get_running_loop().time()
    try:
        while True:
            try:
                raw_bytes, _sr = await asyncio.wait_for(
                    asyncio.to_thread(q.get, True, QUEUE_GET_TIMEOUT),
                    timeout=QUEUE_GET_TIMEOUT + 1.0,
                )
                payload = convert_stream_bytes(raw_bytes, fmt)
                await websocket.send_bytes(payload)
            except asyncio.TimeoutError:
                now = asyncio.get_running_loop().time()
                if now - last_keepalive >= KEEPALIVE_SECONDS:
                    await websocket.send_json({"type": "keepalive"})
                    last_keepalive = now
            except WebSocketDisconnect:
                break
            except Exception as exc:
                append_status(f"WebSocket stream warning: {exc}")
                now = asyncio.get_running_loop().time()
                if now - last_keepalive >= KEEPALIVE_SECONDS:
                    await websocket.send_json({"type": "keepalive"})
                    last_keepalive = now
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@app.post("/play/{deck}")
def deck_play(deck: str):
    eng = require_engine()
    key = deck.upper()
    with eng.lock:
        get_deck(key).play()
        deck_finished_flags[key] = False
    append_status(f"Deck {key} play")
    return {"ok": True}


@app.post("/stop/{deck}")
def deck_stop(deck: str):
    eng = require_engine()
    key = deck.upper()
    with eng.lock:
        get_deck(key).stop()
    append_status(f"Deck {key} stop")
    return {"ok": True}


@app.post("/seek/{deck}")
def seek_deck(deck: str, seconds: float = Form(...)):
    eng = require_engine()
    key = deck.upper()
    d = get_deck(key)
    with eng.lock:
        d.set_playhead(int(float(seconds) * d.sr), quantize=False)
        deck_finished_flags[key] = False
    append_status(f"Deck {key} seek -> {seconds}s")
    return {"ok": True}


@app.post("/waveform_seek/{deck}")
def waveform_seek(deck: str, sample: int = Form(...)):
    eng = require_engine()
    key = deck.upper()
    with eng.lock:
        get_deck(key).set_playhead(int(sample), quantize=False)
        deck_finished_flags[key] = False
    append_status(f"Deck {key} waveform seek")
    return {"ok": True}


@app.post("/waveform_loop/{deck}")
def waveform_loop(deck: str, start_sample: int = Form(...), end_sample: int = Form(...)):
    eng = require_engine()
    key = deck.upper()
    with eng.lock:
        get_deck(key).enable_loop(int(start_sample), int(end_sample), quantize=True)
    append_status(f"Deck {key} loop set from waveform")
    return {"ok": True}


@app.post("/waveform_cue/{deck}")
def waveform_cue(deck: str, sample: int = Form(...)):
    eng = require_engine()
    key = deck.upper()
    d = get_deck(key)
    with eng.lock:
        d.set_playhead(int(sample), quantize=True)
        used = set(d.hot_cues.keys())
        next_idx = next((i for i in range(1, 9) if i not in used), 8)
        d.set_hot_cue(next_idx, quantize=True)
    append_status(f"Deck {key} set waveform cue {next_idx}")
    return {"ok": True, "cue": next_idx}


@app.post("/gain/{deck}")
def set_gain(deck: str, gain: float = Form(...)):
    eng = require_engine()
    key = deck.upper()
    with eng.lock:
        get_deck(key).set_gain(float(gain))
    return {"ok": True}


@app.post("/transition_jingle/upload")
async def transition_jingle_upload(
    file: UploadFile = File(...),
    slot: int = Form(0),
):
    global transition_jingle_paths
    if not file.filename:
        return make_json_response({"error": "No filename", "ok": False}, status_code=400)
    si = int(slot)
    if si < 0 or si > 3:
        return make_json_response({"error": "slot must be 0–3", "ok": False}, status_code=400)
    suf = Path(file.filename).suffix.lower()
    if suf not in SUPPORTED_AUDIO:
        return make_json_response({"error": f"Unsupported type {suf}", "ok": False}, status_code=400)
    dest = JINGLE_SLOT_DIR / f"jingle_slot_{si}{suf}"
    total = 0
    try:
        for old in JINGLE_SLOT_DIR.glob(f"jingle_slot_{si}.*"):
            if old.is_file():
                try:
                    old.unlink(missing_ok=True)
                except Exception:
                    pass
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    try:
                        dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return make_json_response({"error": "File too large", "ok": False}, status_code=400)
                await asyncio.to_thread(out.write, chunk)
    except Exception as exc:
        return make_json_response({"error": str(exc), "ok": False}, status_code=500)
    while len(transition_jingle_paths) < 4:
        transition_jingle_paths.append(None)
    transition_jingle_paths[si] = str(dest.resolve())
    append_status(f"Transition jingle slot {si + 1} loaded: {dest.name}")
    push_transition_jingle_to_engine()
    return {"ok": True, "transition_jingle": transition_jingle_status_payload()}


async def _drain_upload_file(uf: UploadFile) -> None:
    while await uf.read(1024 * 1024):
        pass


@app.post("/transition_jingle/upload_folder")
async def transition_jingle_upload_folder(files: List[UploadFile] = File(...)):
    """
    Browser folder picker (webkitdirectory): receives many files; keeps supported audio,
    picks up to four at random, clears all slots, and writes jingle_slot_0..3 (like V3 jingle folder).
    """
    global transition_jingle_paths
    used_ids: set[int] = set()

    async def drain_unused() -> None:
        for f in files:
            if id(f) not in used_ids:
                try:
                    await _drain_upload_file(f)
                except Exception:
                    pass

    if not files:
        return make_json_response({"error": "No files uploaded", "ok": False}, status_code=400)
    candidates: List[UploadFile] = []
    for f in files:
        if not f.filename:
            continue
        suf = Path(f.filename).suffix.lower()
        if suf in SUPPORTED_AUDIO:
            candidates.append(f)
    if not candidates:
        await drain_unused()
        return make_json_response(
            {"error": "No supported audio in selection (use mp3, wav, flac, ogg, m4a, aac)", "ok": False},
            status_code=400,
        )
    if len(candidates) > 200:
        await drain_unused()
        return make_json_response(
            {"error": "Too many audio files in one request (max 200). Choose a smaller folder.", "ok": False},
            status_code=400,
        )
    k = min(4, len(candidates))
    chosen = random.sample(candidates, k) if len(candidates) > k else list(candidates)
    for u in chosen:
        used_ids.add(id(u))

    JINGLE_SLOT_DIR.mkdir(parents=True, exist_ok=True)
    for i in range(4):
        for old in JINGLE_SLOT_DIR.glob(f"jingle_slot_{i}.*"):
            if old.is_file():
                try:
                    old.unlink(missing_ok=True)
                except Exception:
                    pass
    for old in JINGLE_SLOT_DIR.glob("transition_jingle.*"):
        if old.is_file():
            try:
                old.unlink(missing_ok=True)
            except Exception:
                pass

    new_paths: List[Optional[str]] = [None, None, None, None]
    try:
        for i, uf in enumerate(chosen):
            suf = Path(uf.filename or "").suffix.lower() or ".mp3"
            dest = JINGLE_SLOT_DIR / f"jingle_slot_{i}{suf}"
            total = 0
            with dest.open("wb") as out:
                while True:
                    chunk = await uf.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        try:
                            dest.unlink(missing_ok=True)
                        except Exception:
                            pass
                        await drain_unused()
                        return make_json_response({"error": "One jingle file is too large", "ok": False}, status_code=400)
                    await asyncio.to_thread(out.write, chunk)
            new_paths[i] = str(dest.resolve())
    except Exception as exc:
        for p in new_paths:
            if p:
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception:
                    pass
        await drain_unused()
        return make_json_response({"error": str(exc), "ok": False}, status_code=500)

    await drain_unused()

    while len(transition_jingle_paths) < 4:
        transition_jingle_paths.append(None)
    for i in range(4):
        transition_jingle_paths[i] = new_paths[i]
    append_status(f"Transition jingles from folder: {k} file(s) loaded into slots (random pick among upload)")
    push_transition_jingle_to_engine()
    return {"ok": True, "transition_jingle": transition_jingle_status_payload(), "loaded_count": k}


@app.post("/transition_jingle/config")
def transition_jingle_config(body: TransitionJingleConfigBody):
    global transition_jingle_enabled, transition_jingle_gain, transition_jingle_order
    transition_jingle_enabled = bool(body.enabled)
    transition_jingle_gain = float(max(0.0, min(2.0, body.gain)))
    o = (body.order or "sequential").strip().lower()
    transition_jingle_order = o if o in ("sequential", "random") else "sequential"
    push_transition_jingle_to_engine()
    append_status(
        f"Transition jingle {'ON' if transition_jingle_enabled else 'OFF'} | gain={transition_jingle_gain:.2f} | {transition_jingle_order}"
    )
    return {"ok": True, "transition_jingle": transition_jingle_status_payload()}


@app.post("/transition_jingle/play")
def transition_jingle_play(slot: int = Form(...)):
    """Play one loaded jingle slot immediately on the master mix (manual trigger)."""
    eng = require_engine()
    if not getattr(eng, "running", False):
        raise HTTPException(
            status_code=400,
            detail="Start the engine first so the jingle can be mixed to the main output.",
        )
    si = int(slot)
    if si < 0 or si > 3:
        raise HTTPException(status_code=400, detail="slot must be 0–3 (slots 1–4 in the UI).")
    if not eng.play_transition_jingle_slot(si):
        raise HTTPException(
            status_code=400,
            detail=f"Jingle slot {si + 1} is empty — load a file for that slot first.",
        )
    append_status(f"Manual jingle: slot {si + 1}")
    return {"ok": True, "slot": si}


@app.post("/transition_jingle/clear")
def transition_jingle_clear(slot: Optional[int] = Query(default=None)):
    global transition_jingle_paths
    while len(transition_jingle_paths) < 4:
        transition_jingle_paths.append(None)
    try:
        if slot is None:
            transition_jingle_paths = [None, None, None, None]
            for p in JINGLE_SLOT_DIR.glob("jingle_slot_*.*"):
                if p.is_file():
                    p.unlink(missing_ok=True)
            for p in JINGLE_SLOT_DIR.glob("transition_jingle.*"):
                if p.is_file():
                    p.unlink(missing_ok=True)
            append_status("All transition jingle slots cleared")
        else:
            si = int(slot)
            if si < 0 or si > 3:
                return make_json_response({"error": "slot must be 0–3", "ok": False}, status_code=400)
            transition_jingle_paths[si] = None
            for p in JINGLE_SLOT_DIR.glob(f"jingle_slot_{si}.*"):
                if p.is_file():
                    p.unlink(missing_ok=True)
            append_status(f"Transition jingle slot {si + 1} cleared")
    except Exception:
        pass
    push_transition_jingle_to_engine()
    return {"ok": True, "transition_jingle": transition_jingle_status_payload()}


@app.post("/crossfader")
def set_crossfader(x: float = Form(...)):
    global crossfader_val
    crossfader_val = max(0.0, min(1.0, float(x)))
    if engine:
        engine.set_crossfader(crossfader_val)
    return {"ok": True, "crossfader": crossfader_val}


@app.post("/master_gain")
def set_master_gain(g: float = Form(...)):
    global master_gain_val
    master_gain_val = max(0.0, float(g))
    if engine:
        engine.set_master_gain(master_gain_val)
    return {"ok": True, "master_gain": master_gain_val}


@app.post("/engine_eq")
def set_engine_eq(payload: EngineEqPayload):
    global engine_eq_bands_val
    engine_eq_bands_val = [float(x) for x in payload.bands]
    if engine:
        engine.set_engine_eq_db(engine_eq_bands_val)
    return {"ok": True, "engine_eq_db": engine_eq_bands_val}


@app.post("/loop_beats/{deck}")
def loop_beats(deck: str, beats: int = Form(...)):
    eng = require_engine()
    key = deck.upper()
    with eng.lock:
        get_deck(key).enable_loop_beats(int(beats))
    append_status(f"Deck {key} loop {beats} beats")
    return {"ok": True}


@app.post("/loop_off/{deck}")
def loop_off(deck: str):
    eng = require_engine()
    key = deck.upper()
    with eng.lock:
        get_deck(key).disable_loop()
    append_status(f"Deck {key} loop off")
    return {"ok": True}


@app.post("/roll_beats/{deck}")
def roll_beats(deck: str, beats: int = Form(...)):
    eng = require_engine()
    key = deck.upper()
    with eng.lock:
        get_deck(key).enable_roll_beats(int(beats))
    append_status(f"Deck {key} roll {beats} beats")
    return {"ok": True}


@app.post("/roll_off/{deck}")
def roll_off(deck: str):
    eng = require_engine()
    key = deck.upper()
    with eng.lock:
        get_deck(key).disable_roll()
    append_status(f"Deck {key} roll off")
    return {"ok": True}


@app.post("/cue_set/{deck}")
def cue_set(deck: str, idx: int = Form(...)):
    eng = require_engine()
    key = deck.upper()
    with eng.lock:
        get_deck(key).set_hot_cue(int(idx))
    append_status(f"Deck {key} set cue {idx}")
    return {"ok": True}


@app.post("/cue_jump/{deck}")
def cue_jump(deck: str, idx: int = Form(...)):
    eng = require_engine()
    key = deck.upper()
    with eng.lock:
        ok = get_deck(key).jump_hot_cue(int(idx))
    if ok:
        deck_finished_flags[key] = False
        append_status(f"Deck {key} jump cue {idx}")
    else:
        append_status(f"Deck {key} cue {idx} not set")
    return {"ok": ok}


@app.post("/sync/{deck}")
def sync_deck(deck: str):
    eng = require_engine()
    key = deck.upper()
    other = "B" if key == "A" else "A"
    try:
        eng.sync(key, other)
        append_status(f"Deck {key} synced to Deck {other}")
        return {"ok": True}
    except Exception as exc:
        return make_json_response({"error": str(exc), "ok": False}, status_code=500)


@app.post("/unsync/{deck}")
def unsync_deck(deck: str):
    eng = require_engine()
    key = deck.upper()
    try:
        eng.unsync(key)
        append_status(f"Deck {key} unsynced")
        return {"ok": True}
    except Exception as exc:
        return make_json_response({"error": str(exc), "ok": False}, status_code=500)


@app.post("/align/{deck}")
def align_deck(deck: str):
    eng = require_engine()
    key = deck.upper()
    other = "B" if key == "A" else "A"
    try:
        eng.align_beats(key, other)
        append_status(f"Deck {key} aligned to Deck {other}")
        return {"ok": True}
    except Exception as exc:
        return make_json_response({"error": str(exc), "ok": False}, status_code=500)


@app.post("/drop_sync/{deck}")
def drop_sync(deck: str):
    eng = require_engine()
    incoming = deck.upper()
    outgoing = "B" if incoming == "A" else "A"
    try:
        eng.drop_sync_transition(incoming, outgoing, fade_beats=8)
        append_status(f"Drop-sync: Deck {incoming} into Deck {outgoing}")
        return {"ok": True}
    except Exception as exc:
        return make_json_response({"error": str(exc), "ok": False}, status_code=500)


@app.post("/drop_manual")
def drop_manual(incoming: str = Form(...), outgoing: str = Form(...)):
    eng = require_engine()
    incoming = incoming.upper()
    outgoing = outgoing.upper()
    try:
        eng.drop_sync_transition(incoming, outgoing, fade_beats=8)
        append_status(f"Drop-sync: Deck {incoming} into Deck {outgoing}")
        return {"ok": True}
    except Exception as exc:
        return make_json_response({"error": str(exc), "ok": False}, status_code=500)


@app.post("/align_manual")
def align_manual(slave: str = Form(...), master: str = Form(...)):
    eng = require_engine()
    slave = slave.upper()
    master = master.upper()
    try:
        eng.align_beats(slave, master)
        append_status(f"Deck {slave} aligned to Deck {master}")
        return {"ok": True}
    except Exception as exc:
        return make_json_response({"error": str(exc), "ok": False}, status_code=500)


@app.post("/mute/{deck}")
def toggle_mute(deck: str):
    eng = require_engine()
    key = deck.upper()
    with eng.lock:
        d = get_deck(key)
        d.mute = not d.mute
        state = bool(d.mute)
    append_status(f"Deck {key} mute -> {state}")
    return {"ok": True, "mute": state}


@app.post("/quantize/{deck}")
def toggle_quantize(deck: str):
    eng = require_engine()
    key = deck.upper()
    with eng.lock:
        d = get_deck(key)
        d.quantize = not d.quantize
        state = bool(d.quantize)
    append_status(f"Deck {key} quantize -> {state}")
    return {"ok": True, "quantize": state}


@app.post("/auto_on")
def auto_on():
    eng = require_engine()
    try:
        eng.auto_dj_prepare_incoming = auto_dj_prepare_incoming_deck
        eng.enable_auto_dj()
        with eng.lock:
            if not eng.deck_a.playing and not eng.deck_b.playing:
                if eng.crossfader < 0.5:
                    eng.deck_a.play()
                else:
                    eng.deck_b.play()
        append_status("Auto DJ ON")
        return {"ok": True}
    except Exception as exc:
        return make_json_response({"error": str(exc)}, status_code=500)


@app.post("/auto_off")
def auto_off():
    eng = require_engine()
    try:
        eng.disable_auto_dj()
        append_status("Auto DJ OFF")
        return {"ok": True}
    except Exception as exc:
        return make_json_response({"error": str(exc)}, status_code=500)


@app.post("/record/start")
def record_start(path: Optional[str] = Form(None)):
    eng = require_engine()
    if eng.is_recording():
        return make_json_response({"error": "Already recording", "ok": False}, status_code=400)

    if path:
        out_path = str(Path(path).resolve())
    else:
        name = f"mix_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
        out_path = str(RECORDINGS_DIR / name)

    if not out_path.lower().endswith(".wav"):
        out_path += ".wav"

    try:
        eng.start_recording(out_path)
        append_status(f"Recording mix to {out_path}")
        return {"ok": True, "path": out_path, "download_url": f"/recordings/{Path(out_path).name}"}
    except Exception as exc:
        return make_json_response({"error": str(exc), "ok": False}, status_code=500)


@app.post("/record/stop")
def record_stop():
    eng = require_engine()
    if not eng.is_recording():
        return {"ok": True, "path": None, "message": "Not recording"}
    try:
        path = eng.stop_recording()
        append_status(f"Mix saved: {path}")
        return {"ok": True, "path": path, "download_url": f"/recordings/{Path(path).name}"}
    except Exception as exc:
        return make_json_response({"error": str(exc), "ok": False}, status_code=500)


@app.post("/playlist_select/{deck}")
def playlist_select(deck: str, index: int = Form(...)):
    key = deck.upper()
    pl = deck_playlists.get(key, [])
    if not pl:
        return make_json_response({"error": "No playlist"}, status_code=400)

    idx = max(0, min(len(pl) - 1, int(index)))
    deck_playlist_index[key] = idx
    deck_paths[key] = pl[idx]
    deck_finished_flags[key] = False
    append_status(f"Deck {key} selected track {idx + 1}/{len(pl)}")

    if engine:
        try:
            reload_engine_deck(key, pl[idx])
            append_status(f"Deck {key} reloaded: {os.path.basename(pl[idx])}")
        except Exception as exc:
            return make_json_response({"error": str(exc)}, status_code=500)
    return {"ok": True, "index": idx, "path": pl[idx]}


@app.post("/playlist_prev/{deck}")
def playlist_prev(deck: str):
    key = deck.upper()
    pl = deck_playlists.get(key, [])
    idx = deck_playlist_index.get(key, -1)
    if not pl or idx <= 0:
        return {"ok": False}
    return playlist_select(key, idx - 1)


@app.post("/playlist_next/{deck}")
def playlist_next(deck: str):
    key = deck.upper()
    pl = deck_playlists.get(key, [])
    idx = deck_playlist_index.get(key, -1)
    if not pl or idx >= len(pl) - 1:
        return {"ok": False}
    return playlist_select(key, idx + 1)


@app.post("/reload_deck/{deck}")
def reload_deck(deck: str):
    require_engine()
    key = deck.upper()
    path = deck_paths.get(key)
    if not path:
        return make_json_response({"error": f"No track for Deck {key}"}, status_code=400)
    try:
        reload_engine_deck(key, path)
        append_status(f"Deck {key} reloaded: {os.path.basename(path)}")
        return {"ok": True}
    except Exception as exc:
        return make_json_response({"error": str(exc)}, status_code=500)


@app.post("/playlist_sort_bpm/{deck}")
def route_playlist_sort_bpm(deck: str):
    key = deck.upper()
    if key not in {"A", "B"}:
        return make_json_response({"error": "Deck must be A or B"}, status_code=400)
    info = sort_deck_playlist_by_bpm(key)
    return {"ok": True, **info}


@app.post("/playlist_sort_bpm_all")
def route_playlist_sort_bpm_all():
    info_a = sort_deck_playlist_by_bpm("A")
    info_b = sort_deck_playlist_by_bpm("B")
    return {"ok": True, "deck_a": info_a, "deck_b": info_b}


if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="SAMSEL DJ Engine Pro Web API")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (0.0.0.0 = all interfaces, LAN)")
    parser.add_argument("--port", type=int, default=8000, help="Port")
    parser.add_argument(
        "--tunnel",
        action="store_true",
        help="Start with public URL tunnel (requires cloudflared or ngrok helper script)",
    )
    args = parser.parse_args()

    if args.tunnel:
        import subprocess
        import sys

        subprocess.run(
            [sys.executable, str(BASE_DIR / "run_public.py")],
            cwd=BASE_DIR,
            check=False,
        )
    else:
        import socket

        _probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _probe.settimeout(0.4)
        try:
            if _probe.connect_ex(("127.0.0.1", args.port)) == 0:
                print(
                    f"[ERROR] Port {args.port} is already in use (another server is listening).\n"
                    "  Stop it first, or use a free port, e.g.:\n"
                    f"    py -3.10 app.py --port {args.port + 10}\n"
                    "  Find the listener (PowerShell):\n"
                    f"    Get-NetTCPConnection -LocalPort {args.port} -State Listen\n",
                    flush=True,
                )
                raise SystemExit(1)
        finally:
            _probe.close()

        uvicorn.run(app, host=args.host, port=args.port)

"""
SAMSEL DJ Engine Pro - Full Web API
Exposes all desktop GUI features via REST endpoints.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

import asyncio
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from dj_engine_pro import DJEnginePro, make_deck

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
UPLOAD_DIR = BASE_DIR / "uploads"

STATIC_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)

SUPPORTED_AUDIO = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"}

app = FastAPI(title="SAMSEL DJ Engine Pro Web API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Global state (mirrors dj_gui_pro)
engine: Optional[DJEnginePro] = None
deck_paths = {"A": None, "B": None}
deck_playlists = {"A": [], "B": []}
deck_playlist_index = {"A": -1, "B": -1}
deck_finished_flags = {"A": False, "B": False}
status_log: list[str] = []
crossfader_val = 0.0
master_gain_val = 1.0


def append_status(text: str) -> None:
    status_log.append(text)
    if len(status_log) > 100:
        status_log.pop(0)


def get_deck(deck_name: str):
    if not engine:
        return None
    return engine.deck_a if deck_name.upper() == "A" else engine.deck_b


def build_waveform_peaks(deck, width: int = 560) -> Optional[tuple[list[float], list[float]]]:
    """Build min/max waveform peaks for display."""
    if not deck or deck.audio is None or len(deck.audio) == 0:
        return None
    import numpy as np
    mono = np.mean(deck.audio, axis=1)
    n = len(mono)
    samples_per_pixel = max(1, n // width)
    mins, maxs = [], []
    for i in range(width):
        start = i * samples_per_pixel
        end = min(n, start + samples_per_pixel)
        if start >= n:
            mins.append(0.0)
            maxs.append(0.0)
        else:
            chunk = mono[start:end]
            mins.append(float(np.min(chunk)))
            maxs.append(float(np.max(chunk)))
    return (mins, maxs)


def deck_to_dict(deck, deck_name: str):
    """Build deck dict from deck object. Pass deck from brief lock; heavy work done outside lock."""
    if not deck:
        return None
    path = deck_paths.get(deck_name)
    playlist = deck_playlists.get(deck_name, [])
    idx = deck_playlist_index.get(deck_name, -1)
    n = max(1, len(deck.audio))
    return {
        "path": path or deck.track_path,
        "basename": os.path.basename(path or deck.track_path) if path else "No track",
        "bpm": round(deck.bpm, 2),
        "duration_sec": round(deck.duration_sec, 2),
        "playhead": deck.playhead,
        "playhead_sec": round(deck.playhead / deck.sr, 2),
        "playing": deck.playing,
        "gain": deck.gain,
        "mute": deck.mute,
        "quantize": deck.quantize,
        "hot_cues": deck.hot_cues,
        "loop": {
            "enabled": deck.loop.enabled,
            "start_sample": deck.loop.start_sample,
            "end_sample": deck.loop.end_sample,
        } if deck.loop else {"enabled": False, "start_sample": 0, "end_sample": 0},
        "roll": {
            "enabled": deck.roll.enabled,
            "start_sample": deck.roll.start_sample,
            "end_sample": deck.roll.end_sample,
        } if deck.roll else {"enabled": False, "start_sample": 0, "end_sample": 0},
        "beat_samples": deck.beat_samples.tolist() if hasattr(deck.beat_samples, "tolist") else list(deck.beat_samples),
        "audio_len": n,
        "sr": deck.sr,
        "waveform": build_waveform_peaks(deck),
        "playlist": playlist,
        "playlist_index": idx,
    }


def collect_audio_files(folder: str, recursive: bool = True) -> list[str]:
    p = Path(folder)
    if not p.exists() or not p.is_dir():
        return []
    files = []
    it = p.rglob("*") if recursive else p.glob("*")
    for f in it:
        if f.is_file() and f.suffix.lower() in SUPPORTED_AUDIO:
            files.append(str(f))
    return sorted(files, key=str.lower)


# --- Routes ---

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/upload/{deck}")
async def upload_deck(deck: str, file: UploadFile = File(...)):
    if deck.upper() not in {"A", "B"}:
        return JSONResponse({"error": "Deck must be A or B"}, status_code=400)
    deck = deck.upper()
    ext = Path(file.filename or "").suffix.lower()
    if ext not in SUPPORTED_AUDIO:
        return JSONResponse({"error": f"Unsupported format. Use: {SUPPORTED_AUDIO}"}, status_code=400)
    path = UPLOAD_DIR / (file.filename or "track" + ext)
    with open(path, "wb") as buf:
        shutil.copyfileobj(file.file, buf)
    deck_paths[deck] = str(path)
    deck_playlists[deck] = [str(path)]
    deck_playlist_index[deck] = 0
    deck_finished_flags[deck] = False
    try:
        d = make_deck(deck, str(path))
        append_status(f"Deck {deck} loaded: {file.filename}")
        return {
            "path": str(path),
            "basename": file.filename,
            "bpm": round(d.bpm, 2),
            "duration": round(d.duration_sec, 2),
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


class LoadFolderRequest(BaseModel):
    paths: list[str]


@app.post("/upload_folder/{deck}")
async def upload_folder(deck: str, files: list[UploadFile] = File(...)):
    """Upload multiple files (e.g. from folder picker) and set as playlist."""
    if deck.upper() not in {"A", "B"}:
        return JSONResponse({"error": "Deck must be A or B"}, status_code=400)
    deck = deck.upper()
    paths = []
    for i, f in enumerate(files):
        if not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext not in SUPPORTED_AUDIO:
            continue
        base = Path(f.filename).name
        path = UPLOAD_DIR / f"{i:04d}_{base}"
        with open(path, "wb") as buf:
            shutil.copyfileobj(f.file, buf)
        paths.append(str(path))
    if not paths:
        return JSONResponse({"error": "No supported audio files"}, status_code=400)
    paths = sorted(paths, key=str.lower)
    deck_playlists[deck] = paths
    deck_playlist_index[deck] = 0
    deck_paths[deck] = paths[0]
    deck_finished_flags[deck] = False
    append_status(f"Deck {deck} folder loaded: {len(paths)} tracks")
    return {"count": len(paths), "paths": paths}


@app.post("/load_folder/{deck}")
async def load_folder(deck: str, body: LoadFolderRequest):
    if deck.upper() not in {"A", "B"}:
        return JSONResponse({"error": "Deck must be A or B"}, status_code=400)
    deck = deck.upper()
    files = [p for p in body.paths if Path(p).suffix.lower() in SUPPORTED_AUDIO]
    if not files:
        return JSONResponse({"error": "No supported audio files"}, status_code=400)
    deck_playlists[deck] = sorted(files, key=str.lower)
    deck_playlist_index[deck] = 0
    deck_paths[deck] = deck_playlists[deck][0]
    deck_finished_flags[deck] = False
    append_status(f"Deck {deck} playlist: {len(files)} tracks")
    return {"count": len(files), "current": deck_playlists[deck][0]}


@app.post("/init_engine")
def init_engine():
    global engine, crossfader_val, master_gain_val
    if not deck_paths["A"] or not deck_paths["B"]:
        return JSONResponse({"error": "Load both Deck A and Deck B first"}, status_code=400)
    try:
        import sounddevice as sd
        try:
            dev = sd.query_devices(kind="output")
            target_sr = int(dev.get("default_samplerate", 48000))
        except Exception:
            target_sr = 48000
        if target_sr < 16000:
            target_sr = 48000
        deck_a = make_deck("A", deck_paths["A"], target_sr=target_sr)
        deck_b = make_deck("B", deck_paths["B"], target_sr=target_sr)
        engine = DJEnginePro(deck_a, deck_b, blocksize=4096)
        engine.set_crossfader(crossfader_val)
        engine.set_master_gain(master_gain_val)
        engine.start()
        deck_finished_flags["A"] = deck_finished_flags["B"] = False
        append_status("Engine started.")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/stop_engine")
def stop_engine():
    global engine
    if engine:
        try:
            engine.stop()
            append_status("Engine stopped.")
        except Exception as e:
            append_status(f"ERROR stopping: {e}")
        engine = None
    return {"ok": True}


def require_engine():
    if not engine:
        raise HTTPException(status_code=400, detail="Start the engine first. Load both decks, then click 'Start Engine' in the center mixer.")


@app.get("/status")
def get_status():
    if not engine:
        return {
            "engine_running": False,
            "stream_available": False,
            "crossfader": crossfader_val,
            "master_gain": master_gain_val,
            "deck_a": None,
            "deck_b": None,
            "status_text": "\n".join(status_log[-20:]),
            "log": status_log[-30:],
        }
    # Grab refs under brief lock; waveform/build done outside to avoid blocking audio callback
    with engine.lock:
        deck_a = engine.deck_a
        deck_b = engine.deck_b
        crossfader = engine.crossfader
        master_gain = engine.master_gain
        auto_dj = engine.auto_dj_enabled
        status_text = engine.status()
    return {
        "engine_running": True,
        "stream_available": engine.stream_queue is not None,
        "crossfader": crossfader,
        "master_gain": master_gain,
        "auto_dj": auto_dj,
        "deck_a": deck_to_dict(deck_a, "A"),
        "deck_b": deck_to_dict(deck_b, "B"),
        "status_text": status_text,
        "log": status_log[-30:],
    }


@app.websocket("/stream")
async def audio_stream(websocket: WebSocket):
    """Stream mixed audio to remote clients (phone/tablet) for playback in browser."""
    await websocket.accept()
    if not engine or not engine.running:
        await websocket.close(code=1011, reason="Engine not running")
        return
    q = engine.stream_queue
    if q is None:
        await websocket.close(code=1011, reason="Stream not available")
        return
    try:
        await websocket.send_json({"sr": engine.sr, "channels": 2, "format": "f32"})
        loop = asyncio.get_event_loop()
        while True:
            try:
                data, _sr = await loop.run_in_executor(None, lambda: q.get(timeout=1.0))
                await websocket.send_bytes(data)
            except (TimeoutError, asyncio.TimeoutError):
                # Send keepalive or continue
                continue
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass


@app.post("/play/{deck}")
def deck_play(deck: str):
    require_engine()
    deck = deck.upper()
    with engine.lock:
        get_deck(deck).play()
        deck_finished_flags[deck] = False
    append_status(f"Deck {deck} play")
    return {"ok": True}


@app.post("/stop/{deck}")
def deck_stop(deck: str):
    require_engine()
    deck = deck.upper()
    with engine.lock:
        get_deck(deck).stop()
    append_status(f"Deck {deck} stop")
    return {"ok": True}


@app.post("/seek/{deck}")
def seek_deck(deck: str, seconds: float = Form(...)):
    require_engine()
    deck = deck.upper()
    d = get_deck(deck)
    with engine.lock:
        d.set_playhead(int(float(seconds) * d.sr), quantize=False)
        deck_finished_flags[deck] = False
    append_status(f"Deck {deck} seek -> {seconds}s")
    return {"ok": True}


@app.post("/waveform_seek/{deck}")
def waveform_seek(deck: str, sample: int = Form(...)):
    require_engine()
    deck = deck.upper()
    with engine.lock:
        get_deck(deck).set_playhead(int(sample), quantize=False)
        deck_finished_flags[deck] = False
    append_status(f"Deck {deck} waveform seek")
    return {"ok": True}


@app.post("/waveform_loop/{deck}")
def waveform_loop(deck: str, start_sample: int = Form(...), end_sample: int = Form(...)):
    require_engine()
    deck = deck.upper()
    with engine.lock:
        get_deck(deck).enable_loop(int(start_sample), int(end_sample), quantize=True)
    append_status(f"Deck {deck} loop set from waveform")
    return {"ok": True}


@app.post("/waveform_cue/{deck}")
def waveform_cue(deck: str, sample: int = Form(...)):
    require_engine()
    deck = deck.upper()
    d = get_deck(deck)
    with engine.lock:
        d.set_playhead(int(sample), quantize=True)
        used = set(d.hot_cues.keys())
        next_idx = next((i for i in range(1, 9) if i not in used), 8)
        d.set_hot_cue(next_idx, quantize=True)
    append_status(f"Deck {deck} set waveform cue {next_idx}")
    return {"ok": True}


@app.post("/gain/{deck}")
def set_gain(deck: str, gain: float = Form(...)):
    require_engine()
    deck = deck.upper()
    with engine.lock:
        get_deck(deck).set_gain(float(gain))
    return {"ok": True}


@app.post("/crossfader")
def set_crossfader(x: float = Form(...)):
    global crossfader_val
    crossfader_val = max(0, min(1, float(x)))
    if engine:
        engine.set_crossfader(crossfader_val)
    return {"ok": True}


@app.post("/master_gain")
def set_master_gain(g: float = Form(...)):
    global master_gain_val
    master_gain_val = max(0, float(g))
    if engine:
        engine.set_master_gain(master_gain_val)
    return {"ok": True}


@app.post("/loop_beats/{deck}")
def loop_beats(deck: str, beats: int = Form(...)):
    require_engine()
    deck = deck.upper()
    with engine.lock:
        get_deck(deck).enable_loop_beats(int(beats))
    append_status(f"Deck {deck} loop {beats} beats")
    return {"ok": True}


@app.post("/loop_off/{deck}")
def loop_off(deck: str):
    require_engine()
    deck = deck.upper()
    with engine.lock:
        get_deck(deck).disable_loop()
    append_status(f"Deck {deck} loop off")
    return {"ok": True}


@app.post("/roll_beats/{deck}")
def roll_beats(deck: str, beats: int = Form(...)):
    require_engine()
    deck = deck.upper()
    with engine.lock:
        get_deck(deck).enable_roll_beats(int(beats))
    append_status(f"Deck {deck} roll {beats} beats")
    return {"ok": True}


@app.post("/roll_off/{deck}")
def roll_off(deck: str):
    require_engine()
    deck = deck.upper()
    with engine.lock:
        get_deck(deck).disable_roll()
    append_status(f"Deck {deck} roll off")
    return {"ok": True}


@app.post("/cue_set/{deck}")
def cue_set(deck: str, idx: int = Form(...)):
    require_engine()
    deck = deck.upper()
    with engine.lock:
        get_deck(deck).set_hot_cue(int(idx))
    append_status(f"Deck {deck} set cue {idx}")
    return {"ok": True}


@app.post("/cue_jump/{deck}")
def cue_jump(deck: str, idx: int = Form(...)):
    require_engine()
    deck = deck.upper()
    with engine.lock:
        ok = get_deck(deck).jump_hot_cue(int(idx))
    if ok:
        deck_finished_flags[deck] = False
        append_status(f"Deck {deck} jump cue {idx}")
    else:
        append_status(f"Deck {deck} cue {idx} not set")
    return {"ok": ok}


@app.post("/sync/{deck}")
def sync_deck(deck: str):
    require_engine()
    deck = deck.upper()
    other = "B" if deck == "A" else "A"
    try:
        engine.sync(deck, other)
        append_status(f"Deck {deck} synced to Deck {other}")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e), "ok": False}, status_code=500)


@app.post("/unsync/{deck}")
def unsync_deck(deck: str):
    require_engine()
    deck = deck.upper()
    try:
        engine.unsync(deck)
        append_status(f"Deck {deck} unsynced")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/align/{deck}")
def align_deck(deck: str):
    require_engine()
    deck = deck.upper()
    other = "B" if deck == "A" else "A"
    try:
        engine.align_beats(deck, other)
        append_status(f"Deck {deck} aligned to Deck {other}")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/drop_sync/{deck}")
def drop_sync(deck: str):
    require_engine()
    incoming = deck.upper()
    outgoing = "B" if incoming == "A" else "A"
    try:
        engine.drop_sync_transition(incoming, outgoing, fade_beats=8)
        append_status(f"Drop-sync: Deck {incoming} into Deck {outgoing}")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/drop_manual")
def drop_manual(incoming: str = Form(...), outgoing: str = Form(...)):
    require_engine()
    incoming, outgoing = incoming.upper(), outgoing.upper()
    try:
        engine.drop_sync_transition(incoming, outgoing, fade_beats=8)
        append_status(f"Drop-sync: Deck {incoming} into Deck {outgoing}")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/align_manual")
def align_manual(slave: str = Form(...), master: str = Form(...)):
    require_engine()
    slave, master = slave.upper(), master.upper()
    try:
        engine.align_beats(slave, master)
        append_status(f"Deck {slave} aligned to Deck {master}")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/mute/{deck}")
def toggle_mute(deck: str):
    require_engine()
    deck = deck.upper()
    with engine.lock:
        d = get_deck(deck)
        d.mute = not d.mute
        state = d.mute
    append_status(f"Deck {deck} mute -> {state}")
    return {"ok": True, "mute": state}


@app.post("/quantize/{deck}")
def toggle_quantize(deck: str):
    require_engine()
    deck = deck.upper()
    with engine.lock:
        d = get_deck(deck)
        d.quantize = not d.quantize
        state = d.quantize
    append_status(f"Deck {deck} quantize -> {state}")
    return {"ok": True, "quantize": state}


@app.post("/auto_on")
def auto_on():
    require_engine()
    try:
        engine.enable_auto_dj()
        append_status("Auto DJ ON")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/auto_off")
def auto_off():
    require_engine()
    try:
        engine.disable_auto_dj()
        append_status("Auto DJ OFF")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/playlist_select/{deck}")
def playlist_select(deck: str, index: int = Form(...)):
    deck = deck.upper()
    pl = deck_playlists.get(deck, [])
    if not pl:
        return JSONResponse({"error": "No playlist"}, status_code=400)
    idx = max(0, min(len(pl) - 1, int(index)))
    deck_playlist_index[deck] = idx
    deck_paths[deck] = pl[idx]
    deck_finished_flags[deck] = False
    append_status(f"Deck {deck} selected track {idx + 1}/{len(pl)}")
    if engine:
        try:
            with engine.lock:
                old = get_deck(deck)
                target_sr = old.sr if old else 48000
            new_deck = make_deck(deck, pl[idx], target_sr=target_sr)
            with engine.lock:
                old = get_deck(deck)
                new_deck.gain = old.gain
                new_deck.mute = old.mute
                new_deck.quantize = old.quantize
                new_deck.playing = old.playing
                if deck == "A":
                    engine.deck_a = new_deck
                else:
                    engine.deck_b = new_deck
            append_status(f"Deck {deck} reloaded: {os.path.basename(pl[idx])}")
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    return {"ok": True, "index": idx}


@app.post("/playlist_prev/{deck}")
def playlist_prev(deck: str):
    deck = deck.upper()
    pl = deck_playlists.get(deck, [])
    idx = deck_playlist_index.get(deck, -1)
    if not pl or idx <= 0:
        return {"ok": False}
    return playlist_select(deck, idx - 1)


@app.post("/playlist_next/{deck}")
def playlist_next(deck: str):
    deck = deck.upper()
    pl = deck_playlists.get(deck, [])
    idx = deck_playlist_index.get(deck, -1)
    if not pl or idx >= len(pl) - 1:
        return {"ok": False}
    return playlist_select(deck, idx + 1)


@app.post("/reload_deck/{deck}")
def reload_deck(deck: str):
    require_engine()
    deck = deck.upper()
    path = deck_paths.get(deck)
    if not path:
        return JSONResponse({"error": f"No track for Deck {deck}"}, status_code=400)
    try:
        with engine.lock:
            old = get_deck(deck)
            target_sr = old.sr
        new_deck = make_deck(deck, path, target_sr=target_sr)
        with engine.lock:
            new_deck.gain = old.gain
            new_deck.mute = old.mute
            new_deck.quantize = old.quantize
            new_deck.playing = old.playing
            if deck == "A":
                engine.deck_a = new_deck
            else:
                engine.deck_b = new_deck
        append_status(f"Deck {deck} reloaded: {os.path.basename(path)}")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="SAMSEL DJ Engine Pro Web API")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (0.0.0.0 = all interfaces, LAN)")
    parser.add_argument("--port", type=int, default=8000, help="Port")
    parser.add_argument(
        "--tunnel",
        action="store_true",
        help="Start with public URL tunnel (requires cloudflared or ngrok)",
    )
    args = parser.parse_args()

    if args.tunnel:
        import subprocess
        import sys
        from pathlib import Path

        subprocess.run(
            [sys.executable, str(Path(__file__).parent / "run_public.py")],
            cwd=Path(__file__).resolve().parent,
        )
    else:
        uvicorn.run(app, host=args.host, port=args.port)

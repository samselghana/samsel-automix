r"""
dj_engine_pro.py

2-deck DJ engine prototype with:
- true BPM beat-sync (no pitch change) via pre-rendered time-stretch
- automatic beat alignment for mixing (phase lock to master grid; ``beatmatch`` = sync + align)
- looping and loop roll / slip loops
- hot cues
- drop-sync transitions
- automatic DJ mix mode scaffold (Auto DJ / Smart Actions: on/off, drop, align)
- stereo playback with equal-power crossfader

Dependencies:
    pip install numpy sounddevice librosa soundfile

Notes:
- Uses sounddevice OutputStream callback for playback. sounddevice supports
  callback-driven output streams on Windows/macOS/Linux. 
- Uses librosa.effects.time_stretch() for no-pitch-change sync render.
  This is not ideal for frequent live retiming, so sync is pre-rendered when enabled.
- Multi-channel time-stretch is supported by librosa.
- For a future production engine, replace sync rendering with Rubber Band or SoundTouch.

Example:
    # python dj_engine_pro.py --deck-a "C:\Users\pc\base\SAMSEL_WEB\Abronoma.mp3" --deck-b "C:\Users\pc\base\SAMSEL_WEB\Adult_Music.mp3"
    # python dj_engine_pro.py --folder-a "C:\Music\Set1" --folder-b "C:\Music\Set2"
    # python dj_engine_pro.py --folder-a "E:\SPOTIFY\SPOTIFY-A\Similar songs to Away-African Highlife_SpotifyDown_com\AWAY_3" --folder-b "E:\SPOTIFY\SPOTIFY-A\Similar songs to Away-African Highlife_SpotifyDown_com\AWAY_3"


Useful commands:
    help
    play a
    play b
    stop a
    stop b
    x 0.5
    cue set a 1
    cue jump a 1
    loop beats a 8
    roll beats a 4
    roll off a
    sync b a
    align b a
    beatmatch b a
    drop b a
    auto on
    auto off
    status
    quit
"""

from __future__ import annotations

import argparse
import inspect
import math
import queue
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import librosa
import numpy as np
import sounddevice as sd
import soundfile as sf

try:
    from scipy import signal as scipy_signal
except Exception:  # pragma: no cover
    scipy_signal = None

# True when cascaded IIR engine EQ can run (requires scipy.signal.lfilter).
ENGINE_EQ_AVAILABLE: bool = scipy_signal is not None


# ------------------------- helpers -------------------------

SUPPORTED_AUDIO = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"}


def collect_audio_files(folder: str, recursive: bool = True) -> List[str]:
    """Collect supported audio file paths from a folder (mirrors dj_gui_pro.load_folder_to_deck)."""
    p = Path(folder)
    if not p.exists() or not p.is_dir():
        return []
    files = []
    it = p.rglob("*") if recursive else p.glob("*")
    for f in it:
        if f.is_file() and f.suffix.lower() in SUPPORTED_AUDIO:
            files.append(str(f))
    return sorted(files, key=str.lower)


MAX_AUTO_UPLOADS_PLAYLIST = 768


def collect_auto_uploads_playlist(uploads_dir: Path) -> Tuple[List[str], str]:
    """
    For no-arg launch: prefer top-level files in ./uploads, else full recursive scan.
    Caps list size so huge libraries do not slow startup or inflate prev/next state.
    """
    if not uploads_dir.is_dir():
        return [], ""

    shallow = collect_audio_files(str(uploads_dir), recursive=False)
    if shallow:
        files = shallow
        mode = "top-level"
    else:
        files = collect_audio_files(str(uploads_dir), recursive=True)
        mode = "recursive"

    n = len(files)
    if n > MAX_AUTO_UPLOADS_PLAYLIST:
        print(
            f"Note: uploads/ matched {n} audio files ({mode}); playlist capped to "
            f"{MAX_AUTO_UPLOADS_PLAYLIST} for fast startup. Use folder a|b <dir> for a full folder."
        )
        return files[:MAX_AUTO_UPLOADS_PLAYLIST], mode
    return files, mode


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def ensure_stereo(y: np.ndarray) -> np.ndarray:
    y = np.asarray(y, dtype=np.float32)
    if y.ndim == 1:
        return np.repeat(y[:, None], 2, axis=1)
    if y.ndim == 2:
        if y.shape[1] == 1:
            return np.repeat(y, 2, axis=1)
        return y[:, :2].astype(np.float32)
    raise ValueError("Audio must be 1D or 2D.")


def to_mono(y: np.ndarray) -> np.ndarray:
    y = ensure_stereo(y)
    return np.mean(y, axis=1).astype(np.float32)


def normalize_peak(y: np.ndarray, peak: float = 0.98) -> np.ndarray:
    y = ensure_stereo(y)
    m = float(np.max(np.abs(y)) + 1e-9)
    if m <= 1e-9:
        return y.astype(np.float32)
    return (y * (peak / m)).astype(np.float32)


def safe_rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x)) + 1e-12))


def equal_power_gains(x: float) -> tuple[float, float]:
    x = float(clamp(x, 0.0, 1.0))
    return float(np.cos(x * np.pi / 2.0)), float(np.sin(x * np.pi / 2.0))


def engine_eq_center_frequencies() -> List[float]:
    """Ten SAMSEL Web / V3-style bands: 30 Hz … 18 kHz, log-spaced (matches browser EQ)."""
    lo = math.log10(30.0)
    hi = math.log10(18000.0)
    out: List[float] = []
    for i in range(10):
        t = i / 9.0
        hz = 10.0 ** (lo + t * (hi - lo))
        out.append(round(hz * 100.0) / 100.0)
    return out


ENGINE_EQ_BAND_FREQS: Tuple[float, ...] = tuple(engine_eq_center_frequencies())


def rbj_peaking_bilinear(sr: float, fc_hz: float, gain_db: float, q: float = 1.0) -> Tuple[np.ndarray, np.ndarray]:
    """Digital peaking EQ biquad (RBJ cookbook). Returns b, a with a[0] == 1."""
    sr = float(max(sr, 1.0))
    fc = float(max(1.0, min(fc_hz, sr * 0.45)))
    qc = float(max(0.05, q))
    w0 = 2.0 * math.pi * fc / sr
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha = sin_w0 / (2.0 * qc)
    a_lin = 10.0 ** (gain_db / 40.0)
    b0 = 1.0 + alpha * a_lin
    b1 = -2.0 * cos_w0
    b2 = 1.0 - alpha * a_lin
    a0 = 1.0 + alpha / a_lin
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha / a_lin
    b = np.array([b0 / a0, b1 / a0, b2 / a0], dtype=np.float64)
    a = np.array([1.0, a1 / a0, a2 / a0], dtype=np.float64)
    return b, a


def nearest_idx(arr: np.ndarray, value: int) -> int:
    if len(arr) == 0:
        return 0
    return int(np.argmin(np.abs(arr - value)))


def nearest_beat_sample(beat_samples: np.ndarray, sample_pos: int) -> int:
    if len(beat_samples) == 0:
        return int(sample_pos)
    return int(beat_samples[nearest_idx(beat_samples, sample_pos)])


def beat_window(beat_samples: np.ndarray, center_sample: int, beats_before: int, beats_after: int, fallback_sr: int) -> tuple[int, int]:
    if len(beat_samples) < 2:
        span = int(fallback_sr * 8.0)
        start = max(0, center_sample - span)
        end = center_sample + span
        return start, end

    idx = nearest_idx(beat_samples, center_sample)
    a = max(0, idx - beats_before)
    b = min(len(beat_samples) - 1, idx + beats_after)
    start = int(beat_samples[a])
    end = int(beat_samples[b])
    if end <= start:
        end = start + int(fallback_sr * 4.0)
    return start, end


def median_beat_spacing_samples(beat_samples: np.ndarray) -> float:
    if len(beat_samples) < 2:
        return 0.5 * 44100.0
    d = np.diff(np.asarray(beat_samples, dtype=np.float64))
    d = d[d > 1]
    return float(np.median(d)) if len(d) else float(max(1.0, beat_samples[1] - beat_samples[0]))


def compute_phase_aligned_slave_playhead(
    master_playhead: int,
    master_beats: np.ndarray,
    slave_beats: np.ndarray,
    slave_len: int,
    *,
    bars_ahead: int = 4,
    beats_per_bar: int = 4,
    slave_entry_sample: Optional[int] = None,
    min_tail_samples: int = 0,
) -> Optional[int]:
    """
    MixMeister-style beat phase alignment (after BPMs match).

    When both decks play at the same tempo, ``delta`` samples from now the master reaches
    a chosen downbeat (``bars_ahead`` bars after the next beat strictly ahead of the master
    playhead). The incoming track is cued so that after the same ``delta`` samples of playback,
    its playhead lands on ``slave_entry_sample`` (mix-in / phrase start), modulo phrase length.

    Returns quantized slave sample position, or None if grids are unusable.
    """
    mb = np.asarray(master_beats, dtype=np.int64)
    sb = np.asarray(slave_beats, dtype=np.int64)
    if len(mb) < 2 or len(sb) < 2 or slave_len <= 0:
        return None

    m_now = int(max(0, master_playhead))
    idx_next = int(np.searchsorted(mb, m_now, side="right"))
    if idx_next >= len(mb):
        return None

    span_beats = max(1, int(bars_ahead) * max(1, int(beats_per_bar)))
    target_idx = idx_next + span_beats
    if target_idx >= len(mb):
        target_idx = len(mb) - 1

    target_master_sample = int(mb[target_idx])
    delta = target_master_sample - m_now
    if delta <= 0:
        return None

    if slave_entry_sample is None:
        entry = int(sb[0])
    else:
        entry = nearest_beat_sample(sb, int(slave_entry_sample))

    spacing = median_beat_spacing_samples(sb)
    phrase = max(int(round(32.0 * spacing)), int(max(1, beats_per_bar) * 4 * max(spacing, 1.0)))

    raw = float(entry) - float(delta)
    aligned = nearest_beat_sample(sb, int(round(raw)))

    if aligned < 0:
        k = int(np.ceil(-raw / float(phrase)))
        aligned = nearest_beat_sample(sb, int(round(raw + k * float(phrase))))

    tail = max(int(min_tail_samples), int(spacing * 2))
    guard = 0
    while aligned > slave_len - tail and guard < 4096:
        aligned = nearest_beat_sample(sb, int(round(float(aligned) - float(phrase))))
        guard += 1

    if aligned < 0 or aligned > slave_len - max(1, tail // 2):
        return None
    return int(aligned)


# ------------------------- analysis -------------------------


def load_audio_stereo(path: str, target_sr: int = 44100) -> tuple[np.ndarray, int]:
    y, sr = librosa.load(path, sr=target_sr, mono=False)
    if y.ndim == 1:
        out = np.repeat(y[:, None], 2, axis=1)
    else:
        y = np.asarray(y, dtype=np.float32)
        if y.shape[0] == 1:
            out = np.repeat(y.T, 2, axis=1)
        else:
            out = y.T[:, :2]
    return out.astype(np.float32), sr


def detect_bpm_and_beats(y_stereo: np.ndarray, sr: int, hop_length: int = 512) -> tuple[float, np.ndarray]:
    y_mono = to_mono(y_stereo)
    tempo_raw, beat_frames = librosa.beat.beat_track(y=y_mono, sr=sr, hop_length=hop_length)
    tempo = float(np.ravel(tempo_raw)[0]) if np.size(tempo_raw) else 0.0
    beat_times = librosa.frames_to_time(np.asarray(beat_frames, dtype=int), sr=sr, hop_length=hop_length)
    beat_samples = np.asarray(np.round(beat_times * sr), dtype=np.int64)
    return tempo, beat_samples


def detect_drop_point(y_stereo: np.ndarray, sr: int, beat_samples: np.ndarray) -> int:
    """
    Heuristic 'drop' detector:
    prefer a strong onset / energy jump that follows a quieter region.
    """
    y_mono = to_mono(y_stereo)
    hop = 512
    onset = librosa.onset.onset_strength(y=y_mono, sr=sr, hop_length=hop).astype(np.float32)
    rms = librosa.feature.rms(y=y_mono, hop_length=hop)[0].astype(np.float32)

    if len(onset) < 16 or len(beat_samples) < 4:
        return int(beat_samples[0]) if len(beat_samples) else 0

    onset = onset / (np.max(onset) + 1e-9)
    rms = rms / (np.max(rms) + 1e-9)

    score = np.zeros_like(onset)
    for i in range(8, len(onset) - 4):
        quiet_before = float(np.mean(rms[max(0, i - 8):i]))
        loud_after = float(np.mean(rms[i:min(len(rms), i + 4)]))
        onset_now = float(onset[i])
        score[i] = 1.2 * onset_now + 0.9 * max(0.0, loud_after - quiet_before)

    best_frame = int(np.argmax(score))
    best_sample = int(librosa.frames_to_samples(best_frame, hop_length=hop))
    return nearest_beat_sample(beat_samples, best_sample)


def stretch_multichannel(y_stereo: np.ndarray, rate: float) -> np.ndarray:
    """
    No-pitch-change stretch using librosa.effects.time_stretch().
    librosa docs indicate multi-channel support.
    """
    y = np.asarray(y_stereo, dtype=np.float32).T  # shape [channels, samples]
    stretched = librosa.effects.time_stretch(y, rate=rate)
    return np.asarray(stretched.T, dtype=np.float32)


def stretch_beats(beat_samples: np.ndarray, rate: float) -> np.ndarray:
    if len(beat_samples) == 0:
        return beat_samples.astype(np.int64)
    # librosa time_stretch rate > 1 speeds up, output becomes shorter
    # new positions scale by 1 / rate
    return np.asarray(np.round(beat_samples / rate), dtype=np.int64)


def compute_sync_stretch_payload(
    original_audio: np.ndarray,
    original_beats: np.ndarray,
    original_bpm: float,
    playhead: float,
    sr: int,
    max_sync_shift: float,
    target_bpm: float,
) -> tuple[np.ndarray, np.ndarray, float, float, float]:
    """
    CPU-heavy BPM sync (time-stretch). Safe to run without holding engine.lock
    so the audio callback can keep mixing.
    Returns (audio, beat_samples, bpm, rate, new_playhead).
    """
    target_bpm = float(max(1e-6, target_bpm))
    obpm = float(original_bpm)
    if obpm <= 0 or target_bpm <= 0:
        y = ensure_stereo(np.asarray(original_audio, dtype=np.float32))
        beats_out = np.asarray(original_beats, dtype=np.int64)
        ph = float(clamp(playhead, 0, max(0, len(y) - 1)))
        return y, beats_out, max(0.0, obpm), 1.0, ph

    raw_rate = target_bpm / obpm
    rate = clamp(raw_rate, 1.0 - max_sync_shift, 1.0 + max_sync_shift)
    current_time_sec = playhead / float(sr)
    stretched = stretch_multichannel(ensure_stereo(original_audio), rate=rate)
    stretched_beats = stretch_beats(original_beats, rate=rate)
    new_bpm = float(obpm * rate)
    new_playhead = float(clamp(current_time_sec * sr, 0, max(0, len(stretched) - 1)))
    return stretched, stretched_beats, new_bpm, rate, new_playhead


# ------------------------- deck state -------------------------


@dataclass
class LoopState:
    enabled: bool = False
    start_sample: int = 0
    end_sample: int = 0

    def length(self) -> int:
        return max(0, self.end_sample - self.start_sample)


@dataclass
class RollState:
    enabled: bool = False
    start_sample: int = 0
    end_sample: int = 0
    release_playhead: float = 0.0

    def length(self) -> int:
        return max(0, self.end_sample - self.start_sample)


@dataclass
class DeckState:
    name: str
    track_path: str
    original_audio: np.ndarray
    original_sr: int
    original_bpm: float
    original_beats: np.ndarray

    audio: np.ndarray
    sr: int
    bpm: float
    beat_samples: np.ndarray

    playhead: float = 0.0
    gain: float = 1.0
    playing: bool = False
    mute: bool = False
    quantize: bool = True
    preparing: bool = False

    hot_cues: Dict[int, int] = field(default_factory=dict)
    loop: LoopState = field(default_factory=LoopState)
    roll: RollState = field(default_factory=RollState)

    sync_enabled: bool = False
    sync_target_bpm: Optional[float] = None
    sync_rate: float = 1.0
    max_sync_shift: float = 0.08

    def __post_init__(self) -> None:
        self.original_audio = ensure_stereo(self.original_audio)
        self.audio = ensure_stereo(self.audio)
        self.playhead = float(clamp(self.playhead, 0, max(0, len(self.audio) - 1)))

    @property
    def duration_sec(self) -> float:
        return len(self.audio) / float(self.sr)

    def set_gain(self, gain: float) -> None:
        self.gain = float(max(0.0, gain))

    def play(self) -> None:
        self.playing = True

    def stop(self) -> None:
        self.playing = False

    def reset_to_original(self) -> None:
        current_time_sec = self.playhead / self.sr
        self.audio = self.original_audio.copy()
        self.sr = self.original_sr
        self.bpm = float(self.original_bpm)
        self.beat_samples = self.original_beats.copy()
        self.playhead = float(clamp(current_time_sec * self.sr, 0, max(0, len(self.audio) - 1)))
        self.sync_enabled = False
        self.sync_target_bpm = None
        self.sync_rate = 1.0

    def set_playhead(self, sample_pos: int, quantize: Optional[bool] = None) -> None:
        q = self.quantize if quantize is None else quantize
        pos = int(clamp(sample_pos, 0, max(0, len(self.audio) - 1)))
        if q:
            pos = nearest_beat_sample(self.beat_samples, pos)
        self.playhead = float(pos)

    def set_hot_cue(self, idx: int, quantize: Optional[bool] = None) -> None:
        pos = int(round(self.playhead))
        if self.quantize if quantize is None else quantize:
            pos = nearest_beat_sample(self.beat_samples, pos)
        self.hot_cues[int(idx)] = pos

    def jump_hot_cue(self, idx: int, quantize: Optional[bool] = None) -> bool:
        idx = int(idx)
        if idx not in self.hot_cues:
            return False
        self.set_playhead(self.hot_cues[idx], quantize=quantize)
        return True

    def enable_loop(self, start_sample: int, end_sample: int, quantize: Optional[bool] = None) -> None:
        q = self.quantize if quantize is None else quantize
        start = int(start_sample)
        end = int(end_sample)
        if q:
            start = nearest_beat_sample(self.beat_samples, start)
            end = nearest_beat_sample(self.beat_samples, end)

        start = int(clamp(start, 0, max(0, len(self.audio) - 1)))
        end = int(clamp(end, 0, len(self.audio)))
        if end <= start:
            end = min(len(self.audio), start + max(1, int(0.5 * self.sr)))

        self.loop.enabled = True
        self.loop.start_sample = start
        self.loop.end_sample = end

    def enable_loop_beats(self, num_beats: int) -> None:
        pos = int(round(self.playhead))
        start = nearest_beat_sample(self.beat_samples, pos)
        idx = nearest_idx(self.beat_samples, start)
        end_idx = min(len(self.beat_samples) - 1, idx + max(1, int(num_beats)))
        end = int(self.beat_samples[end_idx]) if len(self.beat_samples) else start + int(self.sr)
        self.enable_loop(start, end, quantize=True)

    def disable_loop(self) -> None:
        self.loop.enabled = False

    def enable_roll_beats(self, num_beats: int) -> None:
        """
        Slip loop:
        audible output loops, but underlying timeline continues.
        """
        pos = int(round(self.playhead))
        start = nearest_beat_sample(self.beat_samples, pos)
        idx = nearest_idx(self.beat_samples, start)
        end_idx = min(len(self.beat_samples) - 1, idx + max(1, int(num_beats)))
        end = int(self.beat_samples[end_idx]) if len(self.beat_samples) else start + int(self.sr)

        self.roll.enabled = True
        self.roll.start_sample = start
        self.roll.end_sample = max(start + 1, end)
        self.roll.release_playhead = float(self.playhead)

    def disable_roll(self) -> None:
        if self.roll.enabled:
            self.playhead = float(clamp(self.roll.release_playhead, 0, max(0, len(self.audio) - 1)))
        self.roll.enabled = False

    def apply_sync_stretch_result(
        self,
        stretched: np.ndarray,
        stretched_beats: np.ndarray,
        new_bpm: float,
        rate: float,
        new_playhead: float,
        target_bpm: float,
    ) -> None:
        """Apply output of compute_sync_stretch_payload (used after stretch off engine.lock)."""
        self.audio = ensure_stereo(stretched)
        self.beat_samples = stretched_beats
        self.sr = self.original_sr
        self.bpm = float(new_bpm)
        self.playhead = float(new_playhead)
        self.sync_enabled = True
        self.sync_target_bpm = float(target_bpm)
        self.sync_rate = float(rate)
        self._rescale_loop_and_cues(rate)

    def enable_sync_to_bpm(self, target_bpm: float, max_shift: Optional[float] = None) -> None:
        """
        True BPM sync without pitch change:
        pre-render stretched audio buffer.
        """
        target_bpm = float(target_bpm)
        if self.original_bpm <= 0 or target_bpm <= 0:
            return

        if max_shift is not None:
            self.max_sync_shift = float(max(0.0, max_shift))

        audio, beats, bpm, rate, new_ph = compute_sync_stretch_payload(
            self.original_audio,
            self.original_beats,
            float(self.original_bpm),
            float(self.playhead),
            int(self.sr),
            float(self.max_sync_shift),
            target_bpm,
        )
        self.apply_sync_stretch_result(audio, beats, bpm, rate, new_ph, target_bpm)

    def disable_sync(self) -> None:
        self.reset_to_original()

    def _rescale_loop_and_cues(self, rate: float) -> None:
        if abs(rate) < 1e-9:
            return
        scale = 1.0 / rate

        if self.loop.enabled:
            self.loop.start_sample = int(round(self.loop.start_sample * scale))
            self.loop.end_sample = int(round(self.loop.end_sample * scale))
            self.loop.start_sample = int(clamp(self.loop.start_sample, 0, max(0, len(self.audio) - 1)))
            self.loop.end_sample = int(clamp(self.loop.end_sample, 0, len(self.audio)))
            if self.loop.end_sample <= self.loop.start_sample:
                self.disable_loop()

        if self.roll.enabled:
            self.roll.start_sample = int(round(self.roll.start_sample * scale))
            self.roll.end_sample = int(round(self.roll.end_sample * scale))
            self.roll.release_playhead = float(self.roll.release_playhead * scale)

        for k, v in list(self.hot_cues.items()):
            self.hot_cues[k] = int(clamp(round(v * scale), 0, max(0, len(self.audio) - 1)))

    def _sample_linear(self, pos: float) -> np.ndarray:
        if len(self.audio) == 0:
            return np.zeros(2, dtype=np.float32)
        if pos <= 0:
            return self.audio[0]
        if pos >= len(self.audio) - 1:
            return self.audio[-1]

        i0 = int(pos)
        i1 = min(i0 + 1, len(self.audio) - 1)
        frac = float(pos - i0)
        return ((1.0 - frac) * self.audio[i0] + frac * self.audio[i1]).astype(np.float32)

    def _get_block_straight_into(self, n: int, dest: np.ndarray) -> None:
        """Vectorized path when neither slip roll nor loop is active (common case)."""
        dest[:n, :] = 0.0
        la = len(self.audio)
        pos = float(self.playhead)
        if pos >= la:
            self.playing = False
            return

        remain = float(la - pos)
        count = min(n, max(0, int(np.ceil(remain - 1e-12))))
        if count <= 0:
            self.playing = False
            return

        gain = float(self.gain)
        i_start = int(math.floor(pos))
        # Integer playhead: contiguous slice — avoids several numpy temporaries per callback
        # (reduces GC pauses that showed up as intermittent dropouts under UI + engine load).
        near_int = abs(pos - float(i_start)) < 1e-4
        if near_int and i_start + count <= la:
            sl = slice(i_start, i_start + count)
            if gain == 1.0:
                np.copyto(dest[:count, :], self.audio[sl])
            else:
                np.multiply(self.audio[sl], np.float32(gain), out=dest[:count, :], casting="unsafe")
            new_pos = float(i_start + count)
            self.playhead = new_pos
            if count < n or new_pos >= float(la):
                self.playing = False
            return

        t = pos + np.arange(count, dtype=np.float64)
        i0 = np.floor(t).astype(np.int64)
        i1 = np.minimum(i0 + 1, la - 1)
        frac = (t - i0.astype(np.float64)).astype(np.float32)
        w0 = (1.0 - frac)[:, None]
        w1 = frac[:, None]
        dest[:count, :] = (w0 * self.audio[i0] + w1 * self.audio[i1]).astype(np.float32)

        new_pos = pos + float(count)
        self.playhead = new_pos
        if count < n or new_pos >= la:
            self.playing = False
        if gain != 1.0:
            dest[:count, :] *= gain

    def get_block_into(self, n: int, dest: np.ndarray) -> None:
        """Write n stereo frames into dest[:n, :]; avoids per-block allocation in the audio thread."""
        dest[:n, :] = 0.0
        if self.preparing:
            return
        if not self.playing or self.mute or len(self.audio) == 0:
            return

        if not self.roll.enabled and not self.loop.enabled:
            self._get_block_straight_into(n, dest)
            return

        pos = float(self.playhead)

        for i in range(n):
            if pos >= len(self.audio):
                self.playing = False
                break

            # roll/slip loop
            if self.roll.enabled and self.roll.end_sample > self.roll.start_sample:
                audible_pos = pos
                if audible_pos < self.roll.start_sample or audible_pos >= self.roll.end_sample:
                    loop_len = float(self.roll.end_sample - self.roll.start_sample)
                    if loop_len > 0:
                        audible_pos = self.roll.start_sample + ((audible_pos - self.roll.start_sample) % loop_len)
                dest[i] = self._sample_linear(audible_pos)
                pos += 1.0
                self.roll.release_playhead = pos
                continue

            # normal loop
            if self.loop.enabled and self.loop.end_sample > self.loop.start_sample:
                if pos < self.loop.start_sample or pos >= self.loop.end_sample:
                    pos = float(self.loop.start_sample)
                dest[i] = self._sample_linear(pos)
                pos += 1.0
                if pos >= self.loop.end_sample:
                    pos = float(self.loop.start_sample)
                continue

            dest[i] = self._sample_linear(pos)
            pos += 1.0

        self.playhead = float(pos)
        dest[:n, :] *= self.gain

    def get_block(self, n: int) -> np.ndarray:
        out = np.zeros((n, 2), dtype=np.float32)
        self.get_block_into(n, out)
        return out

    def status_text(self) -> str:
        loop_txt = "off"
        if self.loop.enabled:
            loop_txt = f"on [{self.loop.start_sample}:{self.loop.end_sample}]"
        roll_txt = "off"
        if self.roll.enabled:
            roll_txt = f"on [{self.roll.start_sample}:{self.roll.end_sample}]"
        sync_txt = "off"
        if self.sync_enabled and self.sync_target_bpm is not None:
            sync_txt = f"on target={self.sync_target_bpm:.2f} rate={self.sync_rate:.4f}"
        return (
            f"{self.name}: playing={self.playing} playhead={self.playhead/self.sr:.2f}s "
            f"bpm={self.bpm:.2f} gain={self.gain:.2f} loop={loop_txt} roll={roll_txt} sync={sync_txt}"
        )


# ------------------------- engine -------------------------


class DJEnginePro:
    def __init__(self, deck_a: DeckState, deck_b: DeckState, blocksize: int = 2048) -> None:
        if deck_a.sr != deck_b.sr:
            raise ValueError("Deck sample rates must match.")
        self.deck_a = deck_a
        self.deck_b = deck_b
        self.sr = deck_a.sr
        self.blocksize = int(blocksize)
        # Preallocated mix buffers: avoid per-callback allocations (reduces GC stutter).
        bs = self.blocksize
        self._deck_buf_a = np.zeros((bs, 2), dtype=np.float32)
        self._deck_buf_b = np.zeros((bs, 2), dtype=np.float32)
        self._mix_scale_tmp = np.zeros((bs, 2), dtype=np.float32)
        self.crossfader = 0.5
        self.master_gain = 1.0
        self.limiter = True

        # Master-bus EQ (same 10 bands as SAMSEL Web); applied after deck mix, before master_gain.
        self._engine_eq_db = np.zeros(10, dtype=np.float64)
        self._eq_ba: List[Tuple[np.ndarray, np.ndarray]] = []
        self._eq_zi = np.zeros((10, 2, 2), dtype=np.float64)
        self._rebuild_engine_eq_coefficients_unlocked()

        self.stream: Optional[sd.OutputStream] = None
        self.lock = threading.RLock()
        self.running = False

        # Optional stream queue for remote audio (WebSocket). Populated by audio_callback.
        self.stream_queue: Optional[queue.Queue] = None
        # Larger queue absorbs bursty WebSocket sends / Wi‑Fi jitter without dropping mix blocks.
        self._stream_queue_max = 128
        # When set: PortAudio buffer is zeroed after mix is copied to stream/recording (LAN/mobile-only listen).
        self._mute_host_speakers_evt = threading.Event()

        # Live microphone (full-duplex PortAudio stream when supported; falls back to output-only).
        self._live_mic_enabled = False
        self._live_mic_gain = 0.85
        self._duplex_stream_ok = False
        # When enabled: attenuate deck+jingle bus from mic envelope so voice sits above the music.
        self._live_mic_duck_enabled = False
        self._live_mic_duck_depth = 0.4
        self._live_mic_duck_env = 0.0

        self.auto_dj_enabled = False
        self.auto_thread: Optional[threading.Thread] = None
        self.auto_stop = threading.Event()
        self.auto_dj_prepare_incoming: Optional[Callable[[str], None]] = None
        self._auto_dj_cooldown_until: float = 0.0

        # Recording: capture mixed output to WAV
        self._recording = False
        self._recording_path: Optional[str] = None
        self._recording_queue: Optional[queue.Queue] = None
        self._recording_thread: Optional[threading.Thread] = None
        self._recording_stop = threading.Event()

        # Up to 4 jingle buffers; one plays per programmed crossfade (order or random).
        self.transition_jingle_slots: List[Optional[np.ndarray]] = [None, None, None, None]
        self.transition_jingle_mode: str = "sequential"
        self.transition_jingle_enabled: bool = False
        self.transition_jingle_gain: float = 0.65
        self._tj_sequential_index: int = 0
        self._tj_playhead: float = 0.0
        self._tj_active: bool = False
        self._tj_current_audio: Optional[np.ndarray] = None

    def _prepare_transition_jingle_buffer(self, path: str) -> np.ndarray:
        y, _sr = load_audio_stereo(path, target_sr=int(self.sr))
        audio = ensure_stereo(np.asarray(y, dtype=np.float32))
        fade = min(audio.shape[0] // 2, max(1, int(self.sr * 0.04)))
        if fade > 1:
            ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
            audio[:fade, :] *= ramp[:, np.newaxis]
            audio[-fade:, :] *= ramp[::-1, np.newaxis]
        return audio.astype(np.float32)

    def set_transition_jingle_slots_from_paths(self, paths: List[Optional[str]]) -> None:
        """Load up to 4 files into slots (None = empty). File I/O outside lock."""
        raw = list(paths)[:4]
        while len(raw) < 4:
            raw.append(None)
        buffers: List[Optional[np.ndarray]] = []
        for p in raw:
            if p and str(p).strip():
                pp = Path(str(p).strip())
                if pp.is_file():
                    buffers.append(self._prepare_transition_jingle_buffer(str(pp)))
                else:
                    buffers.append(None)
            else:
                buffers.append(None)
        with self.lock:
            self.transition_jingle_slots = buffers
            self._tj_active = False
            self._tj_playhead = 0.0
            self._tj_current_audio = None

    def set_transition_jingle_file(self, path: Optional[str]) -> None:
        """Legacy: load a single jingle into slot 1 only; clears slots 2–4."""
        if path and str(path).strip():
            self.set_transition_jingle_slots_from_paths([str(path).strip()])
        else:
            self.set_transition_jingle_slots_from_paths([])

    def _arm_transition_jingle_for_crossfade_unlocked(self) -> None:
        self._tj_current_audio = None
        if not self.transition_jingle_enabled:
            self._tj_active = False
            self._tj_playhead = 0.0
            return
        idxs = [
            i
            for i in range(4)
            if self.transition_jingle_slots[i] is not None and len(self.transition_jingle_slots[i]) > 0
        ]
        if not idxs:
            self._tj_active = False
            self._tj_playhead = 0.0
            return
        mode = (self.transition_jingle_mode or "sequential").strip().lower()
        if mode == "random":
            pick = int(random.choice(idxs))
        else:
            pick = int(idxs[self._tj_sequential_index % len(idxs)])
            self._tj_sequential_index += 1
        self._tj_current_audio = self.transition_jingle_slots[pick]
        self._tj_playhead = 0.0
        self._tj_active = True

    def arm_transition_jingle_for_crossfade(self) -> None:
        """Start jingle from the beginning on the next mix blocks (call when a programmed crossfade starts)."""
        with self.lock:
            self._arm_transition_jingle_for_crossfade_unlocked()

    def play_transition_jingle_slot(self, slot: int) -> bool:
        """Mix jingle from ``slot`` (0–3) from the start on top of the master output; ignores transition_jingle_enabled."""
        si = int(slot)
        if si < 0 or si > 3:
            return False
        with self.lock:
            if si >= len(self.transition_jingle_slots):
                return False
            buf = self.transition_jingle_slots[si]
            if buf is None or len(buf) == 0:
                return False
            self._tj_current_audio = buf
            self._tj_playhead = 0.0
            self._tj_active = True
        return True

    def _mix_transition_jingle_into(self, out: np.ndarray) -> None:
        if not self._tj_active or self._tj_current_audio is None:
            return
        ja = self._tj_current_audio
        n = int(out.shape[0])
        pos = int(self._tj_playhead)
        if pos >= len(ja):
            self._tj_active = False
            self._tj_current_audio = None
            self._tj_playhead = 0.0
            return
        end = min(pos + n, len(ja))
        take = end - pos
        g = float(max(0.0, self.transition_jingle_gain))
        if take > 0 and g > 0:
            out[:take, :] += ja[pos:end, :] * g
        self._tj_playhead = float(end)
        if end >= len(ja):
            self._tj_active = False
            self._tj_current_audio = None
            self._tj_playhead = 0.0

    def _deck(self, name: str) -> DeckState:
        name = name.strip().lower()
        if name in {"a", "decka", "deck_a"}:
            return self.deck_a
        if name in {"b", "deckb", "deck_b"}:
            return self.deck_b
        raise ValueError(f"Unknown deck: {name}")

    def set_crossfader(self, x: float) -> None:
        with self.lock:
            self.crossfader = float(clamp(x, 0.0, 1.0))

    def _rebuild_engine_eq_coefficients_unlocked(self) -> None:
        sr = float(self.sr)
        self._eq_ba = []
        for bi, fc in enumerate(ENGINE_EQ_BAND_FREQS):
            gdb = float(self._engine_eq_db[bi])
            # Air band (18 kHz): wider Q so the bell covers enough treble to be audible on real mixes.
            q = 0.5 if bi == 9 else 1.0
            b, a = rbj_peaking_bilinear(sr, float(fc), gdb, q)
            self._eq_ba.append((b, a))

    def set_engine_eq_gains_db(self, gains) -> None:
        """Set master-bus EQ band gains in dB (exactly 10 values). Clamped to [-12, 20]."""
        arr = np.asarray(list(gains), dtype=np.float64).reshape(-1)
        if arr.size != 10:
            raise ValueError("engine_eq requires exactly 10 band gains")
        arr = np.clip(arr, -12.0, 20.0)
        with self.lock:
            self._engine_eq_db[:] = arr
            self._rebuild_engine_eq_coefficients_unlocked()
            if float(np.max(np.abs(self._engine_eq_db))) < 1e-9:
                self._eq_zi.fill(0.0)

    def get_engine_eq_gains_db(self) -> List[float]:
        with self.lock:
            return [float(x) for x in self._engine_eq_db]

    def _apply_engine_eq(self, o: np.ndarray, n: int) -> None:
        """In-place stereo EQ on o[:n]; must be called with self.lock held."""
        if scipy_signal is None or n <= 0 or not self._eq_ba:
            return
        if float(np.max(np.abs(self._engine_eq_db))) < 1e-9:
            return
        seg = o[:n, :]
        x = seg.astype(np.float64, copy=True)
        for bi in range(10):
            b, a = self._eq_ba[bi]
            for ch in range(2):
                y, zf = scipy_signal.lfilter(b, a, x[:, ch], zi=self._eq_zi[bi, ch])
                x[:, ch] = y
                self._eq_zi[bi, ch] = zf
        seg[:, :] = x.astype(np.float32)

    def set_master_gain(self, g: float) -> None:
        with self.lock:
            self.master_gain = float(max(0.0, g))

    def sync(self, slave_name: str, master_name: str) -> None:
        """Time-stretch runs off the engine lock so audio callbacks are not starved."""
        with self.lock:
            slave = self._deck(slave_name)
            master = self._deck(master_name)
            if slave.original_bpm <= 0 or master.bpm <= 0:
                return
            target_bpm = float(master.bpm)
            oa = slave.original_audio
            obe = slave.original_beats
            obpm = float(slave.original_bpm)
            ph = float(slave.playhead)
            sr = int(slave.sr)
            msh = float(slave.max_sync_shift)

        audio, beats, bpm, rate, new_ph = compute_sync_stretch_payload(
            oa, obe, obpm, ph, sr, msh, target_bpm
        )

        with self.lock:
            slave = self._deck(slave_name)
            slave.apply_sync_stretch_result(audio, beats, bpm, rate, new_ph, target_bpm)

    def unsync(self, deck_name: str) -> None:
        with self.lock:
            self._deck(deck_name).disable_sync()

    def align_beats(
        self,
        slave_name: str,
        master_name: str,
        bars_ahead: int = 4,
        beats_per_bar: int = 4,
    ) -> None:
        """
        Phase-align the slave playhead to the master's beat grid (MixMeister-style).

        Picks a mix-out point on the master ``bars_ahead`` full bars after the next beat ahead
        of the master's playhead, then cues the slave so that after the same elapsed samples
        (matched tempo), the slave reaches its mix-in point (first hot cue, else current playhead).

        Call after ``sync`` for stable BPM match; alignment assumes both decks advance at the
        same rate (1:1 samples after time-stretch sync).
        """
        with self.lock:
            slave = self._deck(slave_name)
            master = self._deck(master_name)

            if len(master.beat_samples) < 2 or len(slave.beat_samples) < 2:
                return

            master_now = int(round(master.playhead))
            entry_hint = int(round(slave.playhead))
            if slave.hot_cues:
                entry_hint = min(slave.hot_cues.values())

            ph = compute_phase_aligned_slave_playhead(
                master_now,
                master.beat_samples,
                slave.beat_samples,
                len(slave.audio),
                bars_ahead=int(bars_ahead),
                beats_per_bar=int(beats_per_bar),
                slave_entry_sample=entry_hint,
                min_tail_samples=int(self.sr * 2),
            )
            if ph is None:
                slave.set_playhead(entry_hint, quantize=True)
            else:
                slave.set_playhead(ph, quantize=False)

    def beatmatch(
        self,
        slave_name: str,
        master_name: str,
        bars_ahead: int = 4,
        beats_per_bar: int = 4,
    ) -> None:
        """Beat-match: time-stretch slave to master BPM, then phase-align to the master's grid."""
        self.sync(slave_name, master_name)
        self.align_beats(slave_name, master_name, bars_ahead=bars_ahead, beats_per_bar=beats_per_bar)

    def drop_sync_transition(
        self,
        incoming_name: str,
        outgoing_name: str,
        fade_beats: int = 8,
        *,
        time_stretch: bool = True,
        cue_mode: str = "drop",
        align_to_outgoing: bool = True,
    ) -> None:
        """
        Optional BPM sync via time-stretch, cue incoming, optional beat-align, then crossfade.
        Auto DJ uses time_stretch=False and cue_mode='intro' for original tempo and overlap
        without long 'preparing' silence. Manual drop-sync keeps time_stretch=True.
        """
        incoming_name = incoming_name.strip()
        outgoing_name = outgoing_name.strip()
        cue_mode = (cue_mode or "drop").strip().lower()

        if not time_stretch:
            with self.lock:
                incoming = self._deck(incoming_name)
                if len(incoming.audio) == 0:
                    return
                incoming.reset_to_original()
                incoming.playing = False
                sr = int(incoming.sr)
                beats = np.asarray(incoming.beat_samples, dtype=np.int64)
                alen = len(incoming.audio)

            if cue_mode == "intro":
                anchor = int(min(max(0, sr // 8), max(0, alen - 1)))
                cue_sample = int(nearest_beat_sample(beats, anchor))
            else:
                with self.lock:
                    incoming = self._deck(incoming_name)
                    audio = ensure_stereo(np.asarray(incoming.audio, dtype=np.float32).copy())
                    beats2 = np.asarray(incoming.beat_samples, dtype=np.int64)
                cue_sample = int(detect_drop_point(audio, sr, beats2))

            with self.lock:
                incoming = self._deck(incoming_name)
                incoming.sync_enabled = False
                incoming.sync_target_bpm = None
                incoming.sync_rate = 1.0
                incoming.set_playhead(cue_sample, quantize=True)
                if align_to_outgoing:
                    self.align_beats(incoming_name, outgoing_name, bars_ahead=max(1, fade_beats // 4))
                incoming.play()

            threading.Thread(
                target=self._perform_crossfade_over_beats,
                args=(incoming_name, outgoing_name, fade_beats),
                daemon=True,
            ).start()
            return

        with self.lock:
            incoming = self._deck(incoming_name)
            outgoing = self._deck(outgoing_name)
            if len(incoming.audio) == 0:
                return

            incoming.preparing = True
            incoming.playing = False

            target_bpm = float(
                max(1e-6, outgoing.bpm if outgoing.bpm > 0 else incoming.bpm if incoming.bpm > 0 else 120.0)
            )
            stretch_ok = incoming.original_bpm > 0 and target_bpm > 0
            sr = int(incoming.sr)

            if stretch_ok:
                oa = incoming.original_audio
                obe = incoming.original_beats
                obpm = float(incoming.original_bpm)
                ph = float(incoming.playhead)
                msh = float(incoming.max_sync_shift)
            else:
                audio_ns = ensure_stereo(np.asarray(incoming.audio, dtype=np.float32).copy())
                beats_ns = np.asarray(incoming.beat_samples, dtype=np.int64).copy()

        if stretch_ok:
            audio, beats, bpm, rate, new_ph = compute_sync_stretch_payload(oa, obe, obpm, ph, sr, msh, target_bpm)
        else:
            audio = audio_ns
            beats = beats_ns
            bpm = float(max(1e-6, target_bpm))
            rate = 1.0
            new_ph = None

        drop_sample = int(detect_drop_point(audio, sr, beats))

        with self.lock:
            incoming = self._deck(incoming_name)
            outgoing = self._deck(outgoing_name)

            if stretch_ok:
                incoming.audio = ensure_stereo(audio)
                incoming.beat_samples = beats
                incoming.bpm = bpm
                incoming.sr = incoming.original_sr
                incoming.sync_enabled = True
                incoming.sync_target_bpm = target_bpm
                incoming.sync_rate = float(rate)
                incoming.playhead = new_ph
                incoming._rescale_loop_and_cues(float(rate))
            else:
                incoming.sync_enabled = False
                incoming.sync_target_bpm = None
                incoming.sync_rate = 1.0

            incoming.set_playhead(drop_sample, quantize=True)
            if align_to_outgoing:
                self.align_beats(incoming_name, outgoing_name, bars_ahead=max(1, fade_beats // 4))
            incoming.preparing = False
            incoming.play()

        threading.Thread(
            target=self._perform_crossfade_over_beats,
            args=(incoming_name, outgoing_name, fade_beats),
            daemon=True,
        ).start()

    def _perform_crossfade_over_beats(self, incoming_name: str, outgoing_name: str, fade_beats: int) -> None:
        with self.lock:
            incoming = self._deck(incoming_name)
            outgoing = self._deck(outgoing_name)
            bi = float(incoming.bpm) if incoming.bpm > 0 else 120.0
            bo = float(outgoing.bpm) if outgoing.bpm > 0 else 120.0
            bpm = max(1e-6, (bi + bo) * 0.5)
            fade_sec = max(1.0, fade_beats * (60.0 / bpm))
            start_x = self.crossfader
            self._arm_transition_jingle_for_crossfade_unlocked()

        # Update crossfader without engine.lock: mix_block holds the lock for the full
        # audio callback (two decks × get_block). Contending here caused intermittent
        # drop-outs / UI hitches during programmed crossfades. Single-attribute float
        # writes are atomic in CPython; mix_block reads crossfader under lock.
        t0 = time.time()
        inc_to_a = incoming_name.lower().startswith("a")
        while True:
            dt = time.time() - t0
            alpha = clamp(dt / fade_sec, 0.0, 1.0)

            if inc_to_a:
                # fade toward A
                self.crossfader = float((1.0 - alpha) * start_x)
            else:
                # fade toward B
                self.crossfader = float(start_x + alpha * (1.0 - start_x))

            if alpha >= 1.0:
                break
            time.sleep(0.02)

    def _apply_live_mic_additive(self, o: np.ndarray, n: int, mic_indata: np.ndarray) -> None:
        """Add microphone samples into o[:n] (float stereo). Caller holds engine.lock."""
        if not self._live_mic_enabled or self._live_mic_gain <= 0.0:
            return
        g = float(self._live_mic_gain)
        mi = np.ascontiguousarray(mic_indata[:n], dtype=np.float32)
        if mi.size == 0:
            return
        if mi.ndim == 1:
            o[:n, 0] += mi * g
            o[:n, 1] += mi * g
            return
        ch = int(mi.shape[1])
        if ch <= 0:
            return
        if ch == 1:
            c0 = mi[:, 0]
            o[:n, 0] += c0 * g
            o[:n, 1] += c0 * g
        else:
            o[:n, 0] += mi[:, 0] * g
            o[:n, 1] += mi[:, min(1, ch - 1)] * g

    def _smooth_live_mic_duck_env(self, n: int, target: float) -> None:
        """One-pole smoothing of duck sidechain (0..1). Caller holds engine.lock."""
        dt = float(n) / max(float(self.sr), 1.0)
        t = float(clamp(target, 0.0, 1.0))
        if t > self._live_mic_duck_env:
            tau = 0.012
        else:
            tau = 0.28
        coef = 1.0 - math.exp(-dt / max(tau, 1e-5))
        self._live_mic_duck_env += (t - self._live_mic_duck_env) * coef

    def _live_mic_duck_music_multiplier(self, n: int, mic_indata: Optional[np.ndarray]) -> float:
        """Linear gain for deck+jingle bus before mic is added (1.0 = no duck). Caller holds engine.lock."""
        if not self._live_mic_duck_enabled:
            self._smooth_live_mic_duck_env(n, 0.0)
            return 1.0
        if mic_indata is None or not self._live_mic_enabled:
            self._smooth_live_mic_duck_env(n, 0.0)
            return 1.0
        mi = np.ascontiguousarray(mic_indata[:n], dtype=np.float32)
        peak = float(np.max(np.abs(mi))) if mi.size else 0.0
        thresh = 0.01
        hi = 0.28
        if peak <= thresh:
            tgt = 0.0
        else:
            tgt = min(1.0, (peak - thresh) / max(hi - thresh, 1e-6))
        self._smooth_live_mic_duck_env(n, tgt)
        d = float(self._live_mic_duck_depth)
        return float(max(0.0, 1.0 - d * self._live_mic_duck_env))

    def mix_block_into(
        self, out: np.ndarray, n: int, mic_indata: Optional[np.ndarray] = None
    ) -> None:
        """Mix n frames into out[:n]; reuses deck buffers to avoid allocating in the audio callback."""
        if n > self.blocksize:
            self._mix_block_into_oversized(out, n, mic_indata)
            return

        wa = self._deck_buf_a
        wb = self._deck_buf_b
        tmp = self._mix_scale_tmp
        with self.lock:
            self.deck_a.get_block_into(n, wa)
            self.deck_b.get_block_into(n, wb)

            ga, gb = equal_power_gains(self.crossfader)
            o = out[:n]
            np.multiply(wa[:n], ga, out=o)
            np.multiply(wb[:n], gb, out=tmp[:n])
            o += tmp[:n]
            self._apply_engine_eq(o, n)
            o *= self.master_gain
            self._mix_transition_jingle_into(o)
            duck_mul = self._live_mic_duck_music_multiplier(n, mic_indata)
            if duck_mul != 1.0:
                o *= duck_mul
            if mic_indata is not None:
                self._apply_live_mic_additive(o, n, mic_indata)

            if self.limiter:
                peak = float(np.max(np.abs(o)) + 1e-9)
                if peak > 1.0:
                    o /= peak

        if out.shape[0] > n:
            out[n:, :] = 0.0

    def _mix_block_into_oversized(
        self, out: np.ndarray, n: int, mic_indata: Optional[np.ndarray] = None
    ) -> None:
        wa = np.zeros((n, 2), dtype=np.float32)
        wb = np.zeros((n, 2), dtype=np.float32)
        tmp = np.zeros((n, 2), dtype=np.float32)
        with self.lock:
            self.deck_a.get_block_into(n, wa)
            self.deck_b.get_block_into(n, wb)
            ga, gb = equal_power_gains(self.crossfader)
            o = out[:n]
            np.multiply(wa, ga, out=o)
            np.multiply(wb, gb, out=tmp)
            o += tmp
            self._apply_engine_eq(o, n)
            o *= self.master_gain
            self._mix_transition_jingle_into(o)
            duck_mul = self._live_mic_duck_music_multiplier(n, mic_indata)
            if duck_mul != 1.0:
                o *= duck_mul
            if mic_indata is not None:
                self._apply_live_mic_additive(o, n, mic_indata)
            if self.limiter:
                peak = float(np.max(np.abs(o)) + 1e-9)
                if peak > 1.0:
                    o /= peak
        if out.shape[0] > n:
            out[n:, :] = 0.0

    def mix_block(self, n: int) -> np.ndarray:
        out = np.zeros((n, 2), dtype=np.float32)
        self.mix_block_into(out, n, None)
        return out

    def set_mute_host_speakers(self, muted: bool) -> None:
        """Silence the local DAC buffer only; WebSocket stream and WAV recording still receive the full mix."""
        with self.lock:
            if muted:
                self._mute_host_speakers_evt.set()
            else:
                self._mute_host_speakers_evt.clear()

    def get_mute_host_speakers(self) -> bool:
        return self._mute_host_speakers_evt.is_set()

    def audio_callback(self, outdata, frames, _time, status) -> None:
        if status:
            pass  # Suppress output underflow spam; lock contention fix should reduce underflows
        self.mix_block_into(outdata, frames, None)
        if self.stream_queue is not None and not self.stream_queue.full():
            try:
                self.stream_queue.put_nowait((outdata[:frames].tobytes(), self.sr))
            except Exception:
                pass
        if self._recording and self._recording_queue is not None and not self._recording_queue.full():
            try:
                self._recording_queue.put_nowait(np.copy(outdata[:frames]))
            except Exception:
                pass
        if self._mute_host_speakers_evt.is_set():
            outdata[:frames].fill(0.0)

    def duplex_audio_callback(self, indata, outdata, frames, _time, status) -> None:
        if status:
            pass
        mic = np.copy(indata[:frames]) if self._live_mic_enabled else None
        self.mix_block_into(outdata, frames, mic)
        if self.stream_queue is not None and not self.stream_queue.full():
            try:
                self.stream_queue.put_nowait((outdata[:frames].tobytes(), self.sr))
            except Exception:
                pass
        if self._recording and self._recording_queue is not None and not self._recording_queue.full():
            try:
                self._recording_queue.put_nowait(np.copy(outdata[:frames]))
            except Exception:
                pass
        if self._mute_host_speakers_evt.is_set():
            outdata[:frames].fill(0.0)

    def set_live_mic(
        self,
        enabled: bool,
        gain: Optional[float] = None,
        duck_music: Optional[bool] = None,
        duck_depth: Optional[float] = None,
    ) -> None:
        with self.lock:
            self._live_mic_enabled = bool(enabled)
            if gain is not None:
                self._live_mic_gain = float(clamp(float(gain), 0.0, 2.0))
            if duck_music is not None:
                self._live_mic_duck_enabled = bool(duck_music)
            if duck_depth is not None:
                self._live_mic_duck_depth = float(clamp(float(duck_depth), 0.0, 0.95))

    def get_live_mic_enabled(self) -> bool:
        return bool(self._live_mic_enabled)

    def get_live_mic_gain(self) -> float:
        return float(self._live_mic_gain)

    def get_live_mic_duck_enabled(self) -> bool:
        return bool(self._live_mic_duck_enabled)

    def get_live_mic_duck_depth(self) -> float:
        return float(self._live_mic_duck_depth)

    def duplex_supported(self) -> bool:
        """True when PortAudio opened a full-duplex stream (live mic can be mixed)."""
        return bool(self._duplex_stream_ok)

    def start(self) -> None:
        if self.running:
            return
        self.stream_queue = queue.Queue(maxsize=self._stream_queue_max)
        self._duplex_stream_ok = False
        duplex_kw: dict = {
            "samplerate": self.sr,
            "channels": 2,
            "dtype": "float32",
            "blocksize": self.blocksize,
            "latency": "high",
            "callback": self.duplex_audio_callback,
        }
        out_kw: dict = {
            "samplerate": self.sr,
            "channels": 2,
            "dtype": "float32",
            "blocksize": self.blocksize,
            "latency": "high",
            "callback": self.audio_callback,
        }
        try:
            if "prime_output_buffers_using_stream_callback" in inspect.signature(sd.Stream).parameters:
                duplex_kw["prime_output_buffers_using_stream_callback"] = True
        except (TypeError, ValueError, AttributeError):
            pass
        try:
            if "prime_output_buffers_using_stream_callback" in inspect.signature(sd.OutputStream).parameters:
                out_kw["prime_output_buffers_using_stream_callback"] = True
        except (TypeError, ValueError, AttributeError):
            pass
        try:
            self.stream = sd.Stream(**duplex_kw)
            self.stream.start()
            self._duplex_stream_ok = True
        except Exception as duplex_err:
            self._duplex_stream_ok = False
            try:
                self.stream = sd.OutputStream(**out_kw)
                self.stream.start()
            except Exception as out_exc:
                raise RuntimeError(
                    f"PortAudio duplex failed ({duplex_err}); output-only failed ({out_exc})"
                ) from out_exc
            with self.lock:
                self._live_mic_enabled = False
                self._live_mic_duck_env = 0.0
            print(
                f"[DJEnginePro] Live microphone unavailable (duplex open failed): {duplex_err}",
                flush=True,
            )

        self.running = True

    def stop(self) -> None:
        self.disable_auto_dj()
        if self._recording:
            self.stop_recording()
        self.stream_queue = None
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self.running = False
        self._duplex_stream_ok = False
        with self.lock:
            self._live_mic_enabled = False
            self._live_mic_duck_env = 0.0

    def enable_auto_dj(self) -> None:
        if self.auto_dj_enabled:
            return
        self.auto_dj_enabled = True
        self.auto_stop.clear()
        self.auto_thread = threading.Thread(target=self._auto_dj_loop, daemon=True)
        self.auto_thread.start()

    def disable_auto_dj(self) -> None:
        self.auto_dj_enabled = False
        self.auto_stop.set()

    def is_recording(self) -> bool:
        return self._recording

    def get_recording_path(self) -> Optional[str]:
        return self._recording_path

    def start_recording(self, path: str) -> None:
        if self._recording:
            return
        path = str(Path(path).resolve())
        if not path.lower().endswith(".wav"):
            path = path + ".wav"
        self._recording_path = path
        self._recording_stop.clear()
        self._recording_queue = queue.Queue(maxsize=500)
        self._recording = True
        self._recording_thread = threading.Thread(target=self._recording_writer_loop, daemon=True)
        self._recording_thread.start()

    def stop_recording(self) -> Optional[str]:
        if not self._recording or self._recording_queue is None:
            return None
        self._recording = False
        self._recording_queue.put(None)
        self._recording_stop.set()
        if self._recording_thread is not None:
            self._recording_thread.join(timeout=10.0)
            self._recording_thread = None
        path = self._recording_path
        self._recording_queue = None
        self._recording_path = None
        return path

    def _recording_writer_loop(self) -> None:
        blocks: List[np.ndarray] = []
        try:
            while True:
                try:
                    block = self._recording_queue.get(timeout=0.5)
                except Exception:
                    if not self._recording:
                        break
                    continue
                if block is None:
                    break
                blocks.append(block)
            if blocks:
                data = np.concatenate(blocks, axis=0)
                sf.write(self._recording_path, data, self.sr, subtype="FLOAT")
        except Exception as e:
            print(f"[record] write failed: {e}")

    def _auto_dj_loop(self) -> None:
        """
        When the dominant deck is nearing its end, load the next track on the idle deck (via app callback),
        then run drop-sync + crossfade. Cooldown prevents re-entrancy while a fade is in progress.
        """
        while not self.auto_stop.is_set():
            try:
                should_transition = False
                incoming = "b"
                dominant = "a"
                with self.lock:
                    now = time.time()
                    if now < self._auto_dj_cooldown_until:
                        pass
                    else:
                        a = self.deck_a
                        b = self.deck_b

                        a_time_left = max(0.0, (len(a.audio) - a.playhead) / max(1, a.sr))
                        b_time_left = max(0.0, (len(b.audio) - b.playhead) / max(1, b.sr))

                        dominant = "a" if self.crossfader < 0.5 else "b"
                        incoming = "b" if dominant == "a" else "a"

                        dominant_deck = self._deck(dominant)
                        incoming_deck = self._deck(incoming)
                        dominant_left = a_time_left if dominant == "a" else b_time_left

                        # Fade ~16 beats; start while outgoing still has enough audio to cover the blend,
                        # or urgently if we are late (avoids silence when the dominant deck hits EOF).
                        auto_fade_beats = 16
                        bpm_dom = max(1e-6, float(dominant_deck.bpm) if dominant_deck.bpm > 0 else 120.0)
                        fade_sec_est = max(3.0, auto_fade_beats * (60.0 / bpm_dom))
                        comfortable = dominant_left >= fade_sec_est * 2.0
                        urgent = dominant_left <= fade_sec_est * 2.5
                        in_window = dominant_left <= 52.0

                        should_transition = (
                            dominant_deck.playing
                            and not incoming_deck.playing
                            and len(incoming_deck.audio) > 0
                            and in_window
                            and (comfortable or urgent)
                        )

                if should_transition:
                    cb = self.auto_dj_prepare_incoming
                    if cb:
                        try:
                            cb(incoming.upper())
                        except Exception as e:
                            print(f"[auto-dj] prepare: {e}")
                    self.drop_sync_transition(
                        incoming,
                        dominant,
                        fade_beats=16,
                        time_stretch=False,
                        cue_mode="intro",
                        align_to_outgoing=False,
                    )
                    with self.lock:
                        out = self._deck(dominant)
                        inc = self._deck(incoming)
                        bi = float(inc.bpm) if inc.bpm and inc.bpm > 0 else 120.0
                        bo = float(out.bpm) if out.bpm and out.bpm > 0 else 120.0
                        bpm_est = max(1e-6, (bi + bo) * 0.5)
                    fade_sec = max(4.0, 16.0 * (60.0 / bpm_est))
                    self._auto_dj_cooldown_until = time.time() + fade_sec + 4.0

            except Exception as e:
                print(f"[auto-dj] {e}")

            time.sleep(0.5)

    def status(self) -> str:
        with self.lock:
            return (
                f"Engine: running={self.running} crossfader={self.crossfader:.2f} master_gain={self.master_gain:.2f} auto_dj={self.auto_dj_enabled}\n"
                f"  {self.deck_a.status_text()}\n"
                f"  {self.deck_b.status_text()}"
            )


# ------------------------- factory -------------------------


def make_deck(name: str, path: str, target_sr: int = 44100) -> DeckState:
    audio, sr = load_audio_stereo(path, target_sr=target_sr)
    bpm, beats = detect_bpm_and_beats(audio, sr)

    return DeckState(
        name=name,
        track_path=path,
        original_audio=audio.copy(),
        original_sr=sr,
        original_bpm=float(bpm),
        original_beats=beats.copy(),
        audio=audio.copy(),
        sr=sr,
        bpm=float(bpm),
        beat_samples=beats.copy(),
        playhead=0.0,
        gain=1.0,
        playing=False,
    )


def make_placeholder_deck(name: str, target_sr: int = 44100, duration_sec: float = 120.0) -> DeckState:
    """Silent buffer + synthetic 120 BPM grid when no file is available (no-args / empty uploads)."""
    n = max(int(duration_sec * target_sr), target_sr * 2)
    audio = np.zeros((n, 2), dtype=np.float32)
    bpm = 120.0
    spacing = max(1, int(round((60.0 / bpm) * float(target_sr))))
    beat_samples = np.arange(0, n, spacing, dtype=np.int64)
    placeholder_path = f"<no track: {name}>"
    return DeckState(
        name=name,
        track_path=placeholder_path,
        original_audio=audio.copy(),
        original_sr=int(target_sr),
        original_bpm=float(bpm),
        original_beats=beat_samples.copy(),
        audio=audio.copy(),
        sr=int(target_sr),
        bpm=float(bpm),
        beat_samples=beat_samples.copy(),
        playhead=0.0,
        gain=1.0,
        playing=False,
    )


# ------------------------- CLI -------------------------


HELP_TEXT = """
Commands:
  help
  status
  play a|b
  stop a|b
  x <0..1>
  mgain <value>
  gain a|b <value>
  seek a|b <seconds>

  cue set a|b <1..8>
  cue jump a|b <1..8>

  loop beats a|b <n>
  loop off a|b

  roll beats a|b <n>
  roll off a|b

  sync a|b a|b
  unsync a|b
  align a|b a|b [bars_ahead]   (phase-align slave to master grid; default 4 bars)
  beatmatch a|b a|b [bars_ahead]  (sync BPM + phase-align; MixMeister-style)
  drop a|b a|b    (incoming into outgoing: drop B into A = drop b a)

  quantize a|b on|off
  mute a|b on|off

  Folder / playlist (mirrors dj_gui_pro Load Folder):
    folder a|b <dir>   - Load folder as playlist for deck, use first track
    prev a|b           - Previous track in playlist
    next a|b           - Next track in playlist

  Auto DJ / Smart Actions:
    auto on        - Auto DJ ON
    auto off       - Auto DJ OFF
    drop b a       - Drop B into A
    drop a b       - Drop A into B
    align b a      - Align B to A
    align a b      - Align A to B

  quit
"""


def _reload_deck(engine: DJEnginePro, deck_name: str, path: str, target_sr: int) -> None:
    """Replace deck with new track from path, preserving gain/mute/quantize/playing."""
    with engine.lock:
        old = engine._deck(deck_name)
        new_deck = make_deck(deck_name, path, target_sr=target_sr)
        new_deck.gain = old.gain
        new_deck.mute = old.mute
        new_deck.quantize = old.quantize
        new_deck.playing = old.playing
        if deck_name.lower() == "a":
            engine.deck_a = new_deck
        else:
            engine.deck_b = new_deck


def command_loop(
    engine: DJEnginePro,
    deck_playlists: Dict[str, List[str]],
    deck_playlist_index: Dict[str, int],
) -> None:
    print(HELP_TEXT.strip())
    while True:
        try:
            raw = input("djpro> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not raw:
            continue

        parts = raw.split()
        cmd = parts[0].lower()

        try:
            if cmd == "help":
                print(HELP_TEXT.strip())

            elif cmd == "status":
                print(engine.status())

            elif cmd == "folder" and len(parts) >= 3:
                deck_name = parts[1].upper()
                if deck_name not in ("A", "B"):
                    print("Deck must be A or B")
                else:
                    # Rejoin so paths with spaces work (e.g. "Similar songs to...")
                    folder_path = " ".join(parts[2:]).strip().strip('"').strip("'")
                    files = collect_audio_files(folder_path, recursive=True)
                    if not files:
                        print(f"No supported audio files in: {folder_path}")
                    else:
                        deck_playlists[deck_name] = files
                        deck_playlist_index[deck_name] = 0
                        path = files[0]
                        with engine.lock:
                            target_sr = engine._deck(deck_name).sr
                        _reload_deck(engine, deck_name, path, target_sr)
                        print(f"Deck {deck_name} folder: {len(files)} tracks, loaded: {Path(path).name}")

            elif cmd == "prev" and len(parts) == 2:
                deck_name = parts[1].upper()
                if deck_name not in ("A", "B"):
                    print("Deck must be A or B")
                else:
                    pl = deck_playlists.get(deck_name, [])
                    idx = deck_playlist_index.get(deck_name, 0)
                    if not pl or idx <= 0:
                        print(f"Deck {deck_name}: no previous track")
                    else:
                        new_idx = idx - 1
                        deck_playlist_index[deck_name] = new_idx
                        path = pl[new_idx]
                        with engine.lock:
                            target_sr = engine._deck(deck_name).sr
                        _reload_deck(engine, deck_name, path, target_sr)
                        print(f"Deck {deck_name} track {new_idx + 1}/{len(pl)}: {Path(path).name}")

            elif cmd == "next" and len(parts) == 2:
                deck_name = parts[1].upper()
                if deck_name not in ("A", "B"):
                    print("Deck must be A or B")
                else:
                    pl = deck_playlists.get(deck_name, [])
                    idx = deck_playlist_index.get(deck_name, 0)
                    if not pl or idx >= len(pl) - 1:
                        print(f"Deck {deck_name}: no next track")
                    else:
                        new_idx = idx + 1
                        deck_playlist_index[deck_name] = new_idx
                        path = pl[new_idx]
                        with engine.lock:
                            target_sr = engine._deck(deck_name).sr
                        _reload_deck(engine, deck_name, path, target_sr)
                        print(f"Deck {deck_name} track {new_idx + 1}/{len(pl)}: {Path(path).name}")

            elif cmd == "play" and len(parts) == 2:
                with engine.lock:
                    engine._deck(parts[1]).play()

            elif cmd == "stop" and len(parts) == 2:
                with engine.lock:
                    engine._deck(parts[1]).stop()

            elif cmd == "x" and len(parts) == 2:
                engine.set_crossfader(float(parts[1]))

            elif cmd == "mgain" and len(parts) == 2:
                engine.set_master_gain(float(parts[1]))

            elif cmd == "gain" and len(parts) == 3:
                with engine.lock:
                    engine._deck(parts[1]).set_gain(float(parts[2]))

            elif cmd == "seek" and len(parts) == 3:
                with engine.lock:
                    deck = engine._deck(parts[1])
                    deck.set_playhead(int(float(parts[2]) * deck.sr), quantize=False)

            elif cmd == "cue" and len(parts) == 4:
                action, deck_name, cue_idx = parts[1], parts[2], int(parts[3])
                with engine.lock:
                    deck = engine._deck(deck_name)
                    if action == "set":
                        deck.set_hot_cue(cue_idx)
                    elif action == "jump":
                        ok = deck.jump_hot_cue(cue_idx)
                        if not ok:
                            print(f"No cue {cue_idx} on deck {deck_name}")

            elif cmd == "loop" and len(parts) >= 3:
                sub = parts[1].lower()
                with engine.lock:
                    deck = engine._deck(parts[2])
                    if sub == "off":
                        deck.disable_loop()
                    elif sub == "beats" and len(parts) == 4:
                        deck.enable_loop_beats(int(parts[3]))
                    else:
                        print("Use: loop beats a 8  OR  loop off a")

            elif cmd == "roll" and len(parts) >= 3:
                sub = parts[1].lower()
                with engine.lock:
                    deck = engine._deck(parts[2])
                    if sub == "off":
                        deck.disable_roll()
                    elif sub == "beats" and len(parts) == 4:
                        deck.enable_roll_beats(int(parts[3]))
                    else:
                        print("Use: roll beats a 4  OR  roll off a")

            elif cmd == "sync" and len(parts) == 3:
                engine.sync(parts[1], parts[2])

            elif cmd == "unsync" and len(parts) == 2:
                engine.unsync(parts[1])

            elif cmd == "align" and len(parts) >= 3:
                bars = int(parts[3]) if len(parts) >= 4 else 4
                engine.align_beats(parts[1], parts[2], bars_ahead=bars)

            elif cmd == "beatmatch" and len(parts) >= 3:
                bars = int(parts[3]) if len(parts) >= 4 else 4
                engine.beatmatch(parts[1], parts[2], bars_ahead=bars)

            elif cmd == "drop" and len(parts) == 3:
                engine.drop_sync_transition(parts[1], parts[2], fade_beats=8)

            elif cmd == "quantize" and len(parts) == 3:
                with engine.lock:
                    engine._deck(parts[1]).quantize = parts[2].lower() == "on"

            elif cmd == "mute" and len(parts) == 3:
                with engine.lock:
                    engine._deck(parts[1]).mute = parts[2].lower() == "on"

            elif cmd == "auto" and len(parts) == 2:
                if parts[1].lower() == "on":
                    engine.enable_auto_dj()
                else:
                    engine.disable_auto_dj()

            elif cmd in {"quit", "exit"}:
                break

            else:
                print("Unknown command. Type 'help'.")
        except Exception as e:
            print(f"Error: {e}")


def main() -> None:
    repo_root = Path(__file__).resolve().parent
    uploads_dir = repo_root / "uploads"

    parser = argparse.ArgumentParser(
        description="Upgraded 2-deck DJ engine prototype.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  py -3.10 dj_engine_pro.py\n"
            "  py -3.10 dj_engine_pro.py --deck-a one.mp3 --deck-b two.mp3\n"
            "  py -3.10 dj_engine_pro.py --folder-a C:\\\\Music\\\\Set1 --folder-b C:\\\\Music\\\\Set2\n"
            "\n"
            "If you omit deck flags, ./uploads is used: top-level audio files first, else a\n"
            "recursive scan. Playlists larger than a few hundred paths are capped for speed.\n"
            "If uploads/ is empty, silent placeholders load; use folder a|b <dir> to load tracks."
        ),
    )
    parser.add_argument("--deck-a", help="Path to deck A track (or use --folder-a)")
    parser.add_argument("--folder-a", help="Folder for deck A playlist (loads all supported audio, mirrors GUI Load Folder)")
    parser.add_argument("--deck-b", help="Path to deck B track (or use --folder-b)")
    parser.add_argument("--folder-b", help="Folder for deck B playlist (loads all supported audio)")
    parser.add_argument("--sr", type=int, default=44100, help="Target sample rate")
    parser.add_argument("--blocksize", type=int, default=2048, help="Audio callback blocksize (larger = smoother, more latency)")
    parser.add_argument("--autoplay-a", action="store_true")
    parser.add_argument("--autoplay-b", action="store_true")
    parser.add_argument("--crossfader", type=float, default=0.5, help="Initial crossfader 0..1")
    args = parser.parse_args()

    auto_files, auto_uploads_mode = collect_auto_uploads_playlist(uploads_dir)

    # Deck A: explicit folder / file, else first file in ./uploads, else placeholder
    if args.folder_a:
        playlist_a = collect_audio_files(args.folder_a, recursive=True)
        if not playlist_a:
            parser.error(f"No supported audio files in folder: {args.folder_a}")
        deck_a_path: Optional[str] = playlist_a[0]
        print(f"Deck A folder: {len(playlist_a)} tracks, using first: {Path(deck_a_path).name}")
    elif args.deck_a:
        if not Path(args.deck_a).is_file():
            parser.error(f"Deck A file not found: {args.deck_a}")
        deck_a_path = args.deck_a
        playlist_a = [deck_a_path]
    elif auto_files:
        deck_a_path = auto_files[0]
        playlist_a = auto_files
        print(
            f"Deck A (auto uploads/, {auto_uploads_mode}): {Path(deck_a_path).name} "
            f"(playlist {len(auto_files)} tracks)"
        )
    else:
        deck_a_path = None
        playlist_a = []
        print("Deck A: ./uploads has no audio — starting silent placeholder. Use: folder a <dir>")

    # Deck B: explicit, else second file in ./uploads (or same file if only one), else placeholder
    if args.folder_b:
        playlist_b = collect_audio_files(args.folder_b, recursive=True)
        if not playlist_b:
            parser.error(f"No supported audio files in folder: {args.folder_b}")
        deck_b_path: Optional[str] = playlist_b[0]
        print(f"Deck B folder: {len(playlist_b)} tracks, using first: {Path(deck_b_path).name}")
    elif args.deck_b:
        if not Path(args.deck_b).is_file():
            parser.error(f"Deck B file not found: {args.deck_b}")
        deck_b_path = args.deck_b
        playlist_b = [deck_b_path]
    elif len(auto_files) >= 2:
        deck_b_path = auto_files[1]
        playlist_b = auto_files
        print(f"Deck B (auto from uploads/): {Path(deck_b_path).name}")
    elif len(auto_files) == 1:
        deck_b_path = auto_files[0]
        playlist_b = auto_files
        print("Deck B (auto): only one file in uploads/ — using same track as deck A")
    else:
        deck_b_path = None
        playlist_b = []
        print("Deck B: ./uploads has no audio — starting silent placeholder. Use: folder b <dir>")

    deck_playlists = {"A": playlist_a, "B": playlist_b}
    deck_playlist_index = {"A": 0, "B": 0}

    print("Loading deck A...")
    if deck_a_path is None:
        deck_a = make_placeholder_deck("A", target_sr=args.sr)
        print(f"Deck A placeholder: bpm={deck_a.bpm:.2f}, duration={deck_a.duration_sec:.2f}s")
    else:
        deck_a = make_deck("A", deck_a_path, target_sr=args.sr)
        print(f"Deck A loaded: bpm={deck_a.bpm:.2f}, duration={deck_a.duration_sec:.2f}s")

    print("Loading deck B...")
    if deck_b_path is None:
        deck_b = make_placeholder_deck("B", target_sr=args.sr)
        print(f"Deck B placeholder: bpm={deck_b.bpm:.2f}, duration={deck_b.duration_sec:.2f}s")
    else:
        deck_b = make_deck("B", deck_b_path, target_sr=args.sr)
        print(f"Deck B loaded: bpm={deck_b.bpm:.2f}, duration={deck_b.duration_sec:.2f}s")

    deck_a.playing = bool(args.autoplay_a)
    deck_b.playing = bool(args.autoplay_b)

    engine = DJEnginePro(deck_a, deck_b, blocksize=args.blocksize)
    engine.set_crossfader(args.crossfader)
    engine.start()

    try:
        print(engine.status())
        command_loop(engine, deck_playlists, deck_playlist_index)
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
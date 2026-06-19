r"""
dj_engine_pro.py

2-deck DJ engine prototype with:
- true BPM beat-sync (no pitch change) via pre-rendered time-stretch
- automatic beat alignment for mixing
- looping and loop roll / slip loops
- hot cues
- drop-sync transitions
- automatic DJ mix mode scaffold
- stereo playback with equal-power crossfader

Dependencies:
    pip install numpy sounddevice librosa soundfile scipy mutagen
    Optional (Queen Mary RNN+DBN beat tracker): pip install madmom

Notes:
- Uses sounddevice OutputStream callback for playback. sounddevice supports
  callback-driven output streams on Windows/macOS/Linux. 
- Uses librosa.effects.time_stretch() for no-pitch-change sync render.
  This is not ideal for frequent live retiming, so sync is pre-rendered when enabled.
- Multi-channel time-stretch is supported by librosa.
- For a future production engine, replace sync rendering with Rubber Band or SoundTouch.

Example:
    python dj_engine_pro.py --deck-a "C:\Users\pc\base\SAMSEL_WEB\Abronoma.mp3" --deck-b "C:\Users\pc\base\SAMSEL_WEB\Adult_Music.mp3"


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
    drop b a
    auto on
    auto off
    status
    quit
"""

from __future__ import annotations

import argparse
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import librosa
import numpy as np
import sounddevice as sd
import soundfile as sf
from scipy import signal as scipy_signal


# ------------------------- helpers -------------------------


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


def detect_bpm_and_beats_queen_mary(path: str, sr: int) -> tuple[float, np.ndarray]:
    """
    Queen Mary–style tempo + beat tracking via madmom (RNN + DBN beat tracker).
    Requires: pip install madmom
    Beat times are mapped to the engine sample rate `sr` (same timeline as librosa-loaded audio).
    """
    from madmom.features.beats import DBNBeatTrackingProcessor, RNNBeatProcessor

    act = RNNBeatProcessor()(path)
    beat_times_sec = DBNBeatTrackingProcessor(fps=100)(act)
    bt = np.asarray(beat_times_sec, dtype=np.float64).ravel()
    if len(bt) < 2:
        return 0.0, np.array([], dtype=np.int64)
    ibi = np.diff(bt)
    ibi = ibi[ibi > 0.02]
    if len(ibi) == 0:
        bpm = 0.0
    else:
        bpm = float(np.clip(60.0 / float(np.median(ibi)), 20.0, 300.0))
    beat_samples = np.asarray(np.round(bt * float(sr)), dtype=np.int64)
    beat_samples = beat_samples[beat_samples >= 0]
    return bpm, beat_samples


def _biquad_peaking_sos(fs: float, f0: float, gain_db: float, q: float = 1.414) -> np.ndarray:
    """Single peaking EQ section as SOS row [b0,b1,b2,a0,a1,a2] normalized."""
    if abs(gain_db) < 0.001:
        return np.array([[1.0, 0.0, 0.0, 1.0, 0.0, 0.0]], dtype=np.float64)
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * f0 / fs
    alpha = np.sin(w0) / (2.0 * q)
    cos_w0 = np.cos(w0)
    b0 = 1.0 + alpha * a
    b1 = -2.0 * cos_w0
    b2 = 1.0 - alpha * a
    a0 = 1.0 + alpha / a
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha / a
    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]], dtype=np.float64)


def build_graphic_eq_sos(fs: float, gains_db: np.ndarray) -> np.ndarray:
    """10-band graphic EQ (approximate ISO centers), serial peaking sections."""
    centers = np.array([32.0, 64.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0], dtype=np.float64)
    g = np.asarray(gains_db, dtype=np.float64).ravel()
    if g.size != 10:
        raise ValueError("Expected 10 EQ bands.")
    rows = []
    for i in range(10):
        f0 = float(min(centers[i], fs * 0.45))
        rows.append(_biquad_peaking_sos(fs, f0, float(g[i]), q=1.414)[0])
    return np.vstack(rows)


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

        raw_rate = target_bpm / self.original_bpm
        rate = clamp(raw_rate, 1.0 - self.max_sync_shift, 1.0 + self.max_sync_shift)

        # Keep position by time before replacing buffer
        current_time_sec = self.playhead / self.sr

        stretched = stretch_multichannel(self.original_audio, rate=rate)
        stretched_beats = stretch_beats(self.original_beats, rate=rate)

        self.audio = ensure_stereo(stretched)
        self.beat_samples = stretched_beats
        self.sr = self.original_sr
        self.bpm = float(self.original_bpm * rate)
        self.playhead = float(clamp(current_time_sec * self.sr, 0, max(0, len(self.audio) - 1)))

        self.sync_enabled = True
        self.sync_target_bpm = float(target_bpm)
        self.sync_rate = float(rate)

        # keep loops/cues roughly in time domain
        self._rescale_loop_and_cues(rate)

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

    def get_block(self, n: int) -> np.ndarray:
        out = np.zeros((n, 2), dtype=np.float32)
        if not self.playing or self.mute or len(self.audio) == 0:
            return out

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
                out[i] = self._sample_linear(audible_pos)
                pos += 1.0
                self.roll.release_playhead = pos
                continue

            # normal loop
            if self.loop.enabled and self.loop.end_sample > self.loop.start_sample:
                if pos < self.loop.start_sample or pos >= self.loop.end_sample:
                    pos = float(self.loop.start_sample)
                out[i] = self._sample_linear(pos)
                pos += 1.0
                if pos >= self.loop.end_sample:
                    pos = float(self.loop.start_sample)
                continue

            out[i] = self._sample_linear(pos)
            pos += 1.0

        self.playhead = float(pos)
        return (out * self.gain).astype(np.float32)

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
    def __init__(self, deck_a: DeckState, deck_b: DeckState, blocksize: int = 1024) -> None:
        if deck_a.sr != deck_b.sr:
            raise ValueError("Deck sample rates must match.")
        self.deck_a = deck_a
        self.deck_b = deck_b
        self.sr = deck_a.sr
        self.blocksize = int(blocksize)
        self.crossfader = 0.0
        self.master_gain = 1.0
        self.limiter = True

        self.stream: Optional[sd.OutputStream] = None
        self.lock = threading.RLock()
        self.running = False

        # Optional stream queue for remote audio (WebSocket). Populated by audio_callback.
        self.stream_queue: Optional[queue.Queue] = None
        self._stream_queue_max = 30

        self.auto_dj_enabled = False
        self.auto_thread: Optional[threading.Thread] = None
        self.auto_stop = threading.Event()

        # 10-band master EQ (applied after crossfade, before limiter)
        self.eq_enabled = True
        self.eq_gains_db = np.zeros(10, dtype=np.float64)
        self._eq_sos = build_graphic_eq_sos(float(self.sr), self.eq_gains_db)
        self._eq_zi: Optional[np.ndarray] = None

        # Optional stereo master recording (float32 WAV)
        self._record_lock = threading.Lock()
        self._record_file: Optional[sf.SoundFile] = None

        # Short jingle / sting mixed on top (e.g. transitions)
        self.overlay_audio: Optional[np.ndarray] = None
        self.overlay_pos: int = 0

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

    def set_master_gain(self, g: float) -> None:
        with self.lock:
            self.master_gain = float(max(0.0, g))

    def set_eq_gains_db(self, gains_db) -> None:
        g = np.asarray(gains_db, dtype=np.float64).ravel()
        if g.size != 10:
            raise ValueError("EQ expects 10 band gains (dB).")
        with self.lock:
            self.eq_gains_db = g.copy()
            self._eq_sos = build_graphic_eq_sos(float(self.sr), self.eq_gains_db)
            self._eq_zi = None

    def set_eq_enabled(self, on: bool) -> None:
        with self.lock:
            self.eq_enabled = bool(on)
            self._eq_zi = None

    def start_recording(self, path: str) -> None:
        with self._record_lock:
            if self._record_file is not None:
                try:
                    self._record_file.close()
                except Exception:
                    pass
                self._record_file = None
            self._record_file = sf.SoundFile(
                path,
                mode="w",
                samplerate=int(self.sr),
                channels=2,
                subtype="PCM_24",
                format="WAV",
            )

    def stop_recording(self) -> None:
        with self._record_lock:
            if self._record_file is not None:
                try:
                    self._record_file.close()
                except Exception:
                    pass
                self._record_file = None

    def is_recording(self) -> bool:
        with self._record_lock:
            return self._record_file is not None

    def queue_jingle_overlay(self, path: str, gain: float = 1.0) -> None:
        jingle, jsr = load_audio_stereo(path, target_sr=int(self.sr))
        jingle = normalize_peak(jingle, peak=0.95) * float(gain)
        with self.lock:
            self.overlay_audio = ensure_stereo(jingle).astype(np.float32)
            self.overlay_pos = 0

    def overlay_seconds_left(self) -> float:
        with self.lock:
            if self.overlay_audio is None or len(self.overlay_audio) == 0:
                return 0.0
            rem = len(self.overlay_audio) - int(self.overlay_pos)
            return max(0.0, rem / float(self.sr))

    def sync(self, slave_name: str, master_name: str) -> None:
        with self.lock:
            slave = self._deck(slave_name)
            master = self._deck(master_name)
            slave.enable_sync_to_bpm(master.bpm)

    def unsync(self, deck_name: str) -> None:
        with self.lock:
            self._deck(deck_name).disable_sync()

    def align_beats(self, slave_name: str, master_name: str, bars_ahead: int = 4) -> None:
        """
        Move slave playhead to a beat-aligned point so that it can enter on time.
        """
        with self.lock:
            slave = self._deck(slave_name)
            master = self._deck(master_name)

            if len(master.beat_samples) < 2 or len(slave.beat_samples) < 2:
                return

            master_now = int(round(master.playhead))
            midx = nearest_idx(master.beat_samples, master_now)
            target_idx = min(len(master.beat_samples) - 1, midx + max(1, int(bars_ahead * 4)))
            target_master_beat = int(master.beat_samples[target_idx])

            # set slave to its nearest cue/drop if available, else nearest beat near current playhead
            entry = int(round(slave.playhead))
            if slave.hot_cues:
                entry = min(slave.hot_cues.values())
            entry = nearest_beat_sample(slave.beat_samples, entry)

            # place slave so its chosen beat becomes aligned now; since both decks run independently,
            # we prep slave at its beat and start it together with a transition.
            slave.set_playhead(entry, quantize=True)

    def drop_sync_transition(self, incoming_name: str, outgoing_name: str, fade_beats: int = 8) -> None:
        """
        Sync incoming BPM, jump it to its detected drop, align to outgoing future beat,
        and start a crossfade thread.
        """
        with self.lock:
            incoming = self._deck(incoming_name)
            outgoing = self._deck(outgoing_name)

            incoming.enable_sync_to_bpm(outgoing.bpm)

            drop_sample = detect_drop_point(incoming.audio, incoming.sr, incoming.beat_samples)
            incoming.set_playhead(drop_sample, quantize=True)

            # align to a beat in the outgoing track a few bars ahead
            self.align_beats(incoming_name, outgoing_name, bars_ahead=max(1, fade_beats // 4))
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
            bpm = max(1e-6, outgoing.bpm if outgoing.bpm > 0 else incoming.bpm if incoming.bpm > 0 else 120.0)
            fade_sec = max(1.0, fade_beats * (60.0 / bpm))
            start_x = self.crossfader

        t0 = time.time()
        while True:
            dt = time.time() - t0
            alpha = clamp(dt / fade_sec, 0.0, 1.0)

            with self.lock:
                if incoming_name.lower().startswith("a"):
                    # fade toward A
                    self.crossfader = float((1.0 - alpha) * start_x)
                else:
                    # fade toward B
                    self.crossfader = float(start_x + alpha * (1.0 - start_x))

            if alpha >= 1.0:
                break
            time.sleep(0.02)

    def mix_block(self, n: int) -> np.ndarray:
        with self.lock:
            a = self.deck_a.get_block(n)
            b = self.deck_b.get_block(n)

            ga, gb = equal_power_gains(self.crossfader)
            out = a * ga + b * gb
            out *= self.master_gain

            if self.eq_enabled and self._eq_sos is not None and self._eq_sos.shape[0] > 0:
                x64 = out.astype(np.float64, copy=False)
                if self._eq_zi is None:
                    z1 = scipy_signal.sosfilt_zi(self._eq_sos)
                    ch = int(x64.shape[1]) if x64.ndim > 1 else 1
                    if ch <= 1:
                        self._eq_zi = z1
                    else:
                        self._eq_zi = np.repeat(z1[:, :, np.newaxis], ch, axis=2)
                x64, self._eq_zi = scipy_signal.sosfilt(self._eq_sos, x64, axis=0, zi=self._eq_zi)
                out = x64.astype(np.float32)

            if self.overlay_audio is not None and len(self.overlay_audio) > 0:
                pos = int(self.overlay_pos)
                olen = len(self.overlay_audio)
                if pos < olen:
                    take = min(n, olen - pos)
                    out[:take] += self.overlay_audio[pos : pos + take]
                    self.overlay_pos = pos + take
                    if self.overlay_pos >= olen:
                        self.overlay_audio = None
                        self.overlay_pos = 0

            if self.limiter:
                peak = float(np.max(np.abs(out)) + 1e-9)
                if peak > 1.0:
                    out = out / peak

            return out.astype(np.float32)

    def audio_callback(self, outdata, frames, _time, status) -> None:
        if status:
            pass  # Suppress output underflow spam; lock contention fix should reduce underflows
        block = self.mix_block(frames)
        outdata[:] = block
        with self._record_lock:
            if self._record_file is not None:
                try:
                    self._record_file.write(block)
                except Exception:
                    pass
        if self.stream_queue is not None and not self.stream_queue.full():
            try:
                self.stream_queue.put_nowait((block.tobytes(), self.sr))
            except Exception:
                pass

    def start(self) -> None:
        if self.running:
            return
        self.stream_queue = queue.Queue(maxsize=self._stream_queue_max)
        self.stream = sd.OutputStream(
            samplerate=self.sr,
            channels=2,
            dtype="float32",
            blocksize=self.blocksize,
            latency="high",
            callback=self.audio_callback,
        )
        self.stream.start()
        self.running = True

    def stop(self) -> None:
        self.disable_auto_dj()
        self.stop_recording()
        self.stream_queue = None
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self.running = False

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

    def _auto_dj_loop(self) -> None:
        """
        Simple AI-DJ scaffold:
        if one deck is dominant and nearing its end, prepare the other deck with sync/drop transition.
        """
        while not self.auto_stop.is_set():
            try:
                with self.lock:
                    a = self.deck_a
                    b = self.deck_b

                    a_time_left = max(0.0, (len(a.audio) - a.playhead) / a.sr)
                    b_time_left = max(0.0, (len(b.audio) - b.playhead) / b.sr)

                    # Decide which deck is currently dominant from crossfader
                    dominant = "a" if self.crossfader < 0.5 else "b"
                    incoming = "b" if dominant == "a" else "a"

                    dominant_deck = self._deck(dominant)
                    incoming_deck = self._deck(incoming)
                    dominant_left = a_time_left if dominant == "a" else b_time_left

                    # If incoming deck is not playing and dominant has < 25 sec left, prepare transition
                    should_transition = (
                        dominant_deck.playing
                        and not incoming_deck.playing
                        and dominant_left < 25.0
                    )

                if should_transition:
                    self.drop_sync_transition(incoming, dominant, fade_beats=8)
                    time.sleep(8.0)

            except Exception as e:
                print(f"[auto-dj] {e}")

            time.sleep(0.5)

    def status(self) -> str:
        with self.lock:
            rec = self.is_recording()
            return (
                f"Engine: running={self.running} crossfader={self.crossfader:.2f} master_gain={self.master_gain:.2f} "
                f"auto_dj={self.auto_dj_enabled} record={rec} eq={'on' if self.eq_enabled else 'bypass'}\n"
                f"  {self.deck_a.status_text()}\n"
                f"  {self.deck_b.status_text()}"
            )


# ------------------------- factory -------------------------


def make_deck(
    name: str,
    path: str,
    target_sr: int = 44100,
    beat_tracker: str = "auto",
) -> DeckState:
    """
    beat_tracker:
      - "auto": try Queen Mary (madmom RNN+DBN) on file, fall back to librosa
      - "queen_mary" / "qm": madmom only, fall back to librosa if unavailable or fails
      - "librosa": librosa beat_track only
    """
    audio, sr = load_audio_stereo(path, target_sr=target_sr)
    bpm, beats = 0.0, np.array([], dtype=np.int64)
    mode = (beat_tracker or "auto").strip().lower()

    if mode in ("auto", "queen_mary", "qm", "madmom"):
        try:
            bpm_q, beats_q = detect_bpm_and_beats_queen_mary(path, sr)
            if bpm_q > 0 and len(beats_q) >= 2:
                bpm, beats = bpm_q, beats_q
        except Exception:
            pass

    if bpm <= 0 or len(beats) < 2:
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
  align a|b a|b
  drop a|b a|b

  quantize a|b on|off
  mute a|b on|off

  auto on
  auto off

  quit
"""


def command_loop(engine: DJEnginePro) -> None:
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

            elif cmd == "align" and len(parts) == 3:
                engine.align_beats(parts[1], parts[2])

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
    parser = argparse.ArgumentParser(description="Upgraded 2-deck DJ engine prototype.")
    parser.add_argument("--deck-a", required=True, help="Path to deck A track")
    parser.add_argument("--deck-b", required=True, help="Path to deck B track")
    parser.add_argument("--sr", type=int, default=44100, help="Target sample rate")
    parser.add_argument("--blocksize", type=int, default=1024, help="Audio callback blocksize")
    parser.add_argument("--autoplay-a", action="store_true")
    parser.add_argument("--autoplay-b", action="store_true")
    parser.add_argument("--crossfader", type=float, default=0.0, help="Initial crossfader 0..1")
    args = parser.parse_args()

    print("Loading deck A...")
    deck_a = make_deck("A", args.deck_a, target_sr=args.sr)
    print(f"Deck A loaded: bpm={deck_a.bpm:.2f}, duration={deck_a.duration_sec:.2f}s")

    print("Loading deck B...")
    deck_b = make_deck("B", args.deck_b, target_sr=args.sr)
    print(f"Deck B loaded: bpm={deck_b.bpm:.2f}, duration={deck_b.duration_sec:.2f}s")

    deck_a.playing = bool(args.autoplay_a)
    deck_b.playing = bool(args.autoplay_b)

    engine = DJEnginePro(deck_a, deck_b, blocksize=args.blocksize)
    engine.set_crossfader(args.crossfader)
    engine.start()

    try:
        print(engine.status())
        command_loop(engine)
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
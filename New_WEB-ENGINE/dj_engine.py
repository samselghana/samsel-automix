"""
dj_engine.py

First working 2-deck DJ engine scaffold with:
- 2 virtual decks
- stereo playback
- hot cues
- sample loops
- auto BPM sync scaffold
- equal-power crossfader
- simple command-line demo

Dependencies:
    pip install numpy sounddevice soundfile librosa

Notes:
- This is a real-time playback scaffold, not a full commercial DJ app.
- BPM sync is implemented as a scaffold using lightweight resampling speed change
  during playback. That means tempo sync will also change pitch.
- For true time-stretch without pitch shift, later replace the block renderer with
  Rubber Band / SoundTouch / phase vocoder streaming.
- Looping and hot cues work now.
- Beat grid + quantized cue/loop helpers are included.

Example:
    python dj_engine.py --deck-a "C:\\music\\track1.mp3" --deck-b "C:\\music\\track2.mp3"

Interactive commands while running:
    help
    play a
    play b
    stop a
    stop b
    x 0.25                # crossfader 0..1
    gain a 0.8
    gain b 1.0
    cue set a 1
    cue jump a 1
    cue set b 2
    cue jump b 2
    loop beats a 8
    loop beats b 16
    loop off a
    sync b a              # sync deck b to deck a
    unsync b
    status
    quit
"""

from __future__ import annotations

import argparse
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import librosa
import numpy as np
import sounddevice as sd


# ------------------------- analysis helpers -------------------------


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def load_audio_stereo(path: str, target_sr: int = 44100) -> tuple[np.ndarray, int]:
    y, sr = librosa.load(path, sr=target_sr, mono=False)
    if y.ndim == 1:
        stereo = np.repeat(y[:, None], 2, axis=1)
    else:
        y = np.asarray(y, dtype=np.float32)
        if y.shape[0] == 1:
            stereo = np.repeat(y.T, 2, axis=1)
        else:
            stereo = y.T[:, :2]
    return stereo.astype(np.float32), sr


def detect_bpm_and_beats(y_stereo: np.ndarray, sr: int, hop_length: int = 512) -> tuple[float, np.ndarray]:
    y_mono = np.mean(y_stereo, axis=1).astype(np.float32)
    tempo_raw, beat_frames = librosa.beat.beat_track(y=y_mono, sr=sr, hop_length=hop_length)
    tempo = float(np.ravel(tempo_raw)[0]) if np.size(tempo_raw) else 0.0
    beat_frames = np.asarray(beat_frames, dtype=int)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop_length)
    beat_samples = np.asarray(np.round(beat_times * sr), dtype=np.int64)
    return tempo, beat_samples


def nearest_beat_sample(beat_samples: np.ndarray, sample_pos: int) -> int:
    if len(beat_samples) == 0:
        return int(sample_pos)
    idx = int(np.argmin(np.abs(beat_samples - sample_pos)))
    return int(beat_samples[idx])


def loop_from_beats(
    beat_samples: np.ndarray,
    playhead: int,
    num_beats: int,
    default_len_samples: int,
) -> tuple[int, int]:
    if len(beat_samples) < 2:
        start = max(0, playhead)
        end = start + max(1, default_len_samples)
        return start, end

    start = nearest_beat_sample(beat_samples, playhead)
    idx = int(np.searchsorted(beat_samples, start, side="left"))
    end_idx = min(len(beat_samples) - 1, idx + max(1, num_beats))
    end = int(beat_samples[end_idx])

    if end <= start:
        end = start + max(1, default_len_samples)

    return start, end


# ------------------------- deck state -------------------------


@dataclass
class LoopState:
    enabled: bool = False
    start_sample: int = 0
    end_sample: int = 0

    def length(self) -> int:
        return max(0, self.end_sample - self.start_sample)


@dataclass
class DeckState:
    name: str
    track_path: str
    audio: np.ndarray
    sr: int
    bpm: float
    beat_samples: np.ndarray
    playhead: float = 0.0
    gain: float = 1.0
    playing: bool = False
    hot_cues: Dict[int, int] = field(default_factory=dict)
    loop: LoopState = field(default_factory=LoopState)

    sync_enabled: bool = False
    sync_target_bpm: Optional[float] = None
    speed_scale: float = 1.0
    max_sync_shift: float = 0.08

    quantize: bool = True
    mute: bool = False

    def __post_init__(self) -> None:
        self.audio = np.asarray(self.audio, dtype=np.float32)
        if self.audio.ndim != 2 or self.audio.shape[1] != 2:
            raise ValueError(f"{self.name}: audio must be stereo shape [samples, 2].")
        self.playhead = float(clamp(self.playhead, 0, max(0, len(self.audio) - 1)))

    @property
    def duration_sec(self) -> float:
        return len(self.audio) / float(self.sr)

    def set_gain(self, gain: float) -> None:
        self.gain = float(max(0.0, gain))

    def set_playing(self, is_playing: bool) -> None:
        self.playing = bool(is_playing)

    def stop(self) -> None:
        self.playing = False

    def play(self) -> None:
        self.playing = True

    def set_playhead(self, sample_pos: int, quantize: Optional[bool] = None) -> None:
        q = self.quantize if quantize is None else quantize
        pos = int(clamp(sample_pos, 0, max(0, len(self.audio) - 1)))
        if q:
            pos = nearest_beat_sample(self.beat_samples, pos)
        self.playhead = float(pos)

    def set_hot_cue(self, idx: int, quantize: Optional[bool] = None) -> None:
        q = self.quantize if quantize is None else quantize
        pos = int(round(self.playhead))
        if q:
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
        if self.bpm > 0:
            beat_len = int(round((60.0 / self.bpm) * self.sr))
        else:
            beat_len = int(0.5 * self.sr)
        start, end = loop_from_beats(
            self.beat_samples,
            int(round(self.playhead)),
            int(num_beats),
            default_len_samples=max(1, beat_len * max(1, int(num_beats))),
        )
        self.enable_loop(start, end, quantize=True)

    def disable_loop(self) -> None:
        self.loop.enabled = False

    def enable_sync(self, target_bpm: float, max_shift: Optional[float] = None) -> None:
        self.sync_enabled = True
        self.sync_target_bpm = float(target_bpm)
        if max_shift is not None:
            self.max_sync_shift = float(max(0.0, max_shift))
        self.update_speed_scale()

    def disable_sync(self) -> None:
        self.sync_enabled = False
        self.sync_target_bpm = None
        self.speed_scale = 1.0

    def update_speed_scale(self) -> None:
        if not self.sync_enabled or self.sync_target_bpm is None or self.bpm <= 0:
            self.speed_scale = 1.0
            return
        raw = float(self.sync_target_bpm / max(1e-9, self.bpm))
        self.speed_scale = clamp(raw, 1.0 - self.max_sync_shift, 1.0 + self.max_sync_shift)

    def _advance_with_loop(self, pos: float, step: float) -> float:
        new_pos = pos + step
        if self.loop.enabled and self.loop.end_sample > self.loop.start_sample:
            loop_len = float(self.loop.end_sample - self.loop.start_sample)
            while new_pos >= self.loop.end_sample:
                new_pos -= loop_len
            while new_pos < self.loop.start_sample:
                new_pos += loop_len
            return new_pos

        if new_pos >= len(self.audio):
            self.playing = False
            return float(len(self.audio))
        return new_pos

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

        self.update_speed_scale()
        step = float(self.speed_scale)

        pos = float(self.playhead)
        if self.loop.enabled and self.loop.end_sample > self.loop.start_sample:
            if pos < self.loop.start_sample or pos >= self.loop.end_sample:
                pos = float(self.loop.start_sample)

        for i in range(n):
            if pos >= len(self.audio):
                self.playing = False
                break
            out[i] = self._sample_linear(pos)
            pos = self._advance_with_loop(pos, step)
            if not self.playing and pos >= len(self.audio):
                break

        self.playhead = float(pos)
        return (out * self.gain).astype(np.float32)

    def status_text(self) -> str:
        loop_txt = "off"
        if self.loop.enabled:
            loop_txt = f"on [{self.loop.start_sample}:{self.loop.end_sample}]"
        sync_txt = "off"
        if self.sync_enabled and self.sync_target_bpm is not None:
            sync_txt = f"on target={self.sync_target_bpm:.2f} scale={self.speed_scale:.4f}"
        return (
            f"{self.name}: playing={self.playing} playhead={self.playhead/self.sr:.2f}s "
            f"bpm={self.bpm:.2f} gain={self.gain:.2f} loop={loop_txt} sync={sync_txt}"
        )


# ------------------------- engine -------------------------


class DJEngine:
    def __init__(
        self,
        deck_a: DeckState,
        deck_b: DeckState,
        blocksize: int = 1024,
        limiter: bool = True,
    ) -> None:
        if deck_a.sr != deck_b.sr:
            raise ValueError("Both decks must use the same sample rate.")
        self.deck_a = deck_a
        self.deck_b = deck_b
        self.sr = deck_a.sr
        self.blocksize = int(blocksize)
        self.crossfader = 0.0  # 0=A only, 1=B only
        self.master_gain = 1.0
        self.limiter = bool(limiter)
        self.stream: Optional[sd.OutputStream] = None
        self.lock = threading.RLock()
        self.running = False

    def set_crossfader(self, x: float) -> None:
        with self.lock:
            self.crossfader = float(clamp(x, 0.0, 1.0))

    def set_master_gain(self, g: float) -> None:
        with self.lock:
            self.master_gain = float(max(0.0, g))

    def sync_deck_to_other(self, deck_name: str, master_name: str) -> None:
        with self.lock:
            deck = self._get_deck(deck_name)
            master = self._get_deck(master_name)
            deck.enable_sync(master.bpm)

    def unsync_deck(self, deck_name: str) -> None:
        with self.lock:
            self._get_deck(deck_name).disable_sync()

    def _get_deck(self, name: str) -> DeckState:
        key = name.strip().lower()
        if key in {"a", "decka", "deck_a"}:
            return self.deck_a
        if key in {"b", "deckb", "deck_b"}:
            return self.deck_b
        raise ValueError(f"Unknown deck: {name}")

    def mix_block(self, n: int) -> np.ndarray:
        with self.lock:
            a = self.deck_a.get_block(n)
            b = self.deck_b.get_block(n)

            # equal-power crossfade
            x = float(clamp(self.crossfader, 0.0, 1.0))
            ga = float(np.cos(x * np.pi / 2.0))
            gb = float(np.sin(x * np.pi / 2.0))

            out = a * ga + b * gb
            out *= self.master_gain

            if self.limiter:
                peak = float(np.max(np.abs(out)) + 1e-9)
                if peak > 1.0:
                    out = out / peak

            return out.astype(np.float32)

    def audio_callback(self, outdata, frames, _time, status) -> None:
        if status:
            print(status)
        outdata[:] = self.mix_block(frames)

    def start(self) -> None:
        if self.running:
            return
        self.stream = sd.OutputStream(
            samplerate=self.sr,
            channels=2,
            dtype="float32",
            callback=self.audio_callback,
            blocksize=self.blocksize,
        )
        self.stream.start()
        self.running = True

    def stop(self) -> None:
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self.running = False

    def status(self) -> str:
        with self.lock:
            return (
                f"Engine: running={self.running} crossfader={self.crossfader:.2f} master_gain={self.master_gain:.2f}\n"
                f"  {self.deck_a.status_text()}\n"
                f"  {self.deck_b.status_text()}"
            )


# ------------------------- deck factory -------------------------


def make_deck(name: str, track_path: str, target_sr: int = 44100) -> DeckState:
    audio, sr = load_audio_stereo(track_path, target_sr=target_sr)
    bpm, beat_samples = detect_bpm_and_beats(audio, sr)
    return DeckState(
        name=name,
        track_path=track_path,
        audio=audio,
        sr=sr,
        bpm=bpm,
        beat_samples=beat_samples,
        playhead=0.0,
        gain=1.0,
        playing=False,
    )


# ------------------------- cli demo -------------------------


HELP_TEXT = """
Commands:
  help
  status
  play a|b
  stop a|b
  x <0..1>                     set crossfader
  mgain <value>                set master gain
  gain a|b <value>             set deck gain
  seek a|b <seconds>           jump playhead
  cue set a|b <1..8>           store hot cue at current playhead
  cue jump a|b <1..8>          jump to hot cue
  loop beats a|b <n>           enable quantized loop of n beats from current playhead
  loop off a|b                 disable loop
  sync a|b a|b                 sync first deck to second deck BPM
  unsync a|b
  quantize a|b on|off
  mute a|b on|off
  quit
"""


def command_loop(engine: DJEngine) -> None:
    print(HELP_TEXT.strip())
    while True:
        try:
            raw = input("dj> ").strip()
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
                    engine._get_deck(parts[1]).play()

            elif cmd == "stop" and len(parts) == 2:
                with engine.lock:
                    engine._get_deck(parts[1]).stop()

            elif cmd == "x" and len(parts) == 2:
                engine.set_crossfader(float(parts[1]))

            elif cmd == "mgain" and len(parts) == 2:
                engine.set_master_gain(float(parts[1]))

            elif cmd == "gain" and len(parts) == 3:
                with engine.lock:
                    engine._get_deck(parts[1]).set_gain(float(parts[2]))

            elif cmd == "seek" and len(parts) == 3:
                with engine.lock:
                    deck = engine._get_deck(parts[1])
                    sec = float(parts[2])
                    deck.set_playhead(int(sec * deck.sr), quantize=False)

            elif cmd == "cue" and len(parts) == 4:
                action, deck_name, cue_idx = parts[1], parts[2], int(parts[3])
                with engine.lock:
                    deck = engine._get_deck(deck_name)
                    if action == "set":
                        deck.set_hot_cue(cue_idx)
                    elif action == "jump":
                        ok = deck.jump_hot_cue(cue_idx)
                        if not ok:
                            print(f"No hot cue {cue_idx} on deck {deck_name}")
                    else:
                        print("Use: cue set a 1  OR  cue jump a 1")

            elif cmd == "loop" and len(parts) >= 3:
                sub = parts[1].lower()
                deck_name = parts[2]
                with engine.lock:
                    deck = engine._get_deck(deck_name)
                    if sub == "off":
                        deck.disable_loop()
                    elif sub == "beats" and len(parts) == 4:
                        deck.enable_loop_beats(int(parts[3]))
                    else:
                        print("Use: loop beats a 8  OR  loop off a")

            elif cmd == "sync" and len(parts) == 3:
                engine.sync_deck_to_other(parts[1], parts[2])

            elif cmd == "unsync" and len(parts) == 2:
                engine.unsync_deck(parts[1])

            elif cmd == "quantize" and len(parts) == 3:
                with engine.lock:
                    deck = engine._get_deck(parts[1])
                    deck.quantize = parts[2].lower() == "on"

            elif cmd == "mute" and len(parts) == 3:
                with engine.lock:
                    deck = engine._get_deck(parts[1])
                    deck.mute = parts[2].lower() == "on"

            elif cmd in {"quit", "exit"}:
                break

            else:
                print("Unknown command. Type 'help'.")
        except Exception as e:
            print(f"Error: {e}")


# ------------------------- main -------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="2-deck DJ engine scaffold with loops, hot cues, and BPM sync.")
    parser.add_argument("--deck-a", required=True, help="Path to deck A track")
    parser.add_argument("--deck-b", required=True, help="Path to deck B track")
    parser.add_argument("--sr", type=int, default=44100, help="Target sample rate")
    parser.add_argument("--blocksize", type=int, default=1024, help="Audio callback blocksize")
    parser.add_argument("--autoplay-a", action="store_true", help="Start deck A immediately")
    parser.add_argument("--autoplay-b", action="store_true", help="Start deck B immediately")
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

    engine = DJEngine(deck_a, deck_b, blocksize=args.blocksize)
    engine.set_crossfader(args.crossfader)
    engine.start()

    try:
        print(engine.status())
        command_loop(engine)
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
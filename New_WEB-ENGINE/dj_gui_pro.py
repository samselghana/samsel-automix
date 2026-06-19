import csv
import json
import os
import random
import re

import numpy as np
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from dj_engine_pro import DJEnginePro, make_deck

try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None


APP_BG = "#0f1220"
PANEL_BG = "#171b2e"
CARD_BG = "#1f2540"
TEXT = "#f5f7ff"
MUTED = "#aab3d9"
ACCENT_A = "#ff4d8d"
ACCENT_B = "#00d4ff"
ACCENT_C = "#8cff66"
ACCENT_D = "#ffb703"
ACCENT_E = "#9b5cff"
DANGER = "#ff5a5f"
WAVE_BG = "#0b1020"
GRID = "#2a3158"

SUPPORTED_AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"}

LIBRARY_JSON = Path.home() / ".samsel_dj_track_library.json"

EQ_BAND_HZ = ("32", "64", "125", "250", "500", "1k", "2k", "4k", "8k", "16k")


class ToolTip:
    def __init__(self, widget, text: str, delay_ms: int = 400):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._win = None
        self._after = None
        widget.bind("<Enter>", self._schedule)
        widget.bind("<Leave>", self._hide)

    def set_text(self, text: str) -> None:
        self.text = text

    def _schedule(self, _event=None):
        self._hide()
        self._after = self.widget.after(self.delay_ms, self._show)

    def _show(self):
        if self._win or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._win = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tw,
            text=self.text,
            justify="left",
            bg="#2a3158",
            fg=TEXT,
            relief=tk.SOLID,
            borderwidth=1,
            font=("Segoe UI", 9),
            padx=8,
            pady=4,
        ).pack()

    def _hide(self, _event=None):
        if self._after:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None
        if self._win:
            try:
                self._win.destroy()
            except Exception:
                pass
            self._win = None


def apply_3d_button(btn: tk.Button) -> None:
    btn.configure(
        relief=tk.RAISED,
        bd=3,
        highlightthickness=1,
        highlightbackground="#5a6aa8",
        highlightcolor="#8899dd",
        activebackground=btn.cget("bg"),
    )


def apply_3d_entry(entry: tk.Entry) -> None:
    entry.configure(
        relief=tk.SUNKEN,
        bd=3,
        highlightthickness=2,
        highlightbackground="#3d4a7a",
        highlightcolor=ACCENT_B,
        bg="#14182e",
        fg=TEXT,
        insertbackground=TEXT,
        font=("Segoe UI", 10),
    )


def parse_lrc_file(path: str) -> list[tuple[float, str]]:
    lines: list[tuple[float, str]] = []
    if not path or not os.path.isfile(path):
        return lines
    pat = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            m = pat.match(line)
            if not m:
                continue
            mm, ss, frac = m.group(1), m.group(2), m.group(3) or "0"
            t = int(mm) * 60 + int(ss) + int(frac.ljust(3, "0")[:3]) / 1000.0
            text = pat.sub("", line, count=1).strip()
            if text:
                lines.append((t, text))
    lines.sort(key=lambda x: x[0])
    return lines


def mutagen_metadata_flat(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if MutagenFile is None or not path or not os.path.isfile(path):
        out["path"] = path or ""
        return out

    def _merge_tags(audio) -> None:
        if audio is None or not hasattr(audio, "tags") or not audio.tags:
            return
        for k, v in audio.tags.items():
            key = str(k)
            if isinstance(v, (list, tuple)):
                out[key] = " / ".join(str(x) for x in v)
            else:
                out[key] = str(v)

    try:
        audio_easy = MutagenFile(path, easy=True)
        _merge_tags(audio_easy)
        audio_full = MutagenFile(path, easy=False)
        if audio_full is not None:
            _merge_tags(audio_full)
            for k in ("bitrate", "length", "mime"):
                if hasattr(audio_full, "info") and getattr(audio_full.info, k, None) is not None:
                    out[f"info.{k}"] = str(getattr(audio_full.info, k))
    except Exception as e:
        out["error"] = str(e)
    out["path"] = path
    out["filename"] = os.path.basename(path)
    return dict(sorted(out.items(), key=lambda kv: kv[0].lower()))


def quick_bpm_for_sort(path: str) -> float:
    if not path or not os.path.isfile(path):
        return 999.0
    meta = mutagen_metadata_flat(path)
    for k in ("tbpm", "bpm", "TEMPO"):
        for mk, mv in meta.items():
            if k.lower() in mk.lower():
                try:
                    return float(str(mv).replace(",", ".").split()[0])
                except Exception:
                    pass
    try:
        import librosa

        y, sr = librosa.load(path, sr=22050, mono=True, duration=45.0)
        if len(y) < 2048:
            return 999.0
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr, hop_length=512)
        t = float(np.ravel(tempo)[0]) if np.size(tempo) else 0.0
        return t if t > 0 else 999.0
    except Exception:
        return 999.0


class ScrollableDeckFrame(tk.Frame):
    def __init__(self, master, bg_color=CARD_BG):
        super().__init__(master, bg=bg_color)

        self.canvas = tk.Canvas(
            self,
            bg=bg_color,
            highlightthickness=0,
            bd=0,
        )
        self.v_scroll = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg_color)

        self.inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        self.window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.v_scroll.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.v_scroll.pack(side="right", fill="y")

        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfig(self.window_id, width=e.width)
        )

        self.canvas.bind("<Enter>", self._bind_mousewheel)
        self.canvas.bind("<Leave>", self._unbind_mousewheel)

    def _bind_mousewheel(self, _event=None):
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event=None):
        self.canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event):
        try:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:
            pass


class WaveformView(tk.Canvas):
    def __init__(self, master, app, deck_name: str, width=520, height=140, accent="#00d4ff"):
        super().__init__(
            master,
            width=width,
            height=height,
            bg=WAVE_BG,
            highlightthickness=1,
            highlightbackground="#303a68",
            relief="flat",
            bd=0,
            cursor="crosshair",
        )
        self.app = app
        self.deck_name = deck_name
        self.accent = accent
        self.deck = None
        self.cached_wave = None
        self.cached_audio_len = None

        self.dragging_loop = False
        self.drag_start_x = None
        self.drag_current_x = None

        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Button-1>", self.on_left_down)
        self.bind("<B1-Motion>", self.on_left_drag)
        self.bind("<ButtonRelease-1>", self.on_left_up)
        self.bind("<Button-3>", self.on_right_click)

    def set_deck(self, deck):
        self.deck = deck
        self.cached_wave = None
        self.cached_audio_len = None
        self.redraw()

    def invalidate(self):
        self.cached_wave = None
        self.redraw()

    def x_to_sample(self, x: float) -> int:
        if self.deck is None or self.deck.audio is None or len(self.deck.audio) == 0:
            return 0
        w = max(1, self.winfo_width())
        x = max(0, min(w, x))
        ratio = x / w
        return int(ratio * len(self.deck.audio))

    def on_left_down(self, event):
        if not self.app.require_engine(silent=True):
            return
        self.dragging_loop = True
        self.drag_start_x = event.x
        self.drag_current_x = event.x
        self.redraw()

    def on_left_drag(self, event):
        if not self.dragging_loop:
            return
        self.drag_current_x = event.x
        self.redraw()

    def on_left_up(self, event):
        if not self.app.require_engine(silent=True):
            self.dragging_loop = False
            return

        if self.deck is None:
            self.dragging_loop = False
            return

        end_x = event.x
        start_x = self.drag_start_x if self.drag_start_x is not None else end_x
        drag_pixels = abs(end_x - start_x)

        if drag_pixels < 8:
            sample = self.x_to_sample(end_x)
            self.app.waveform_seek(self.deck_name, sample)
        else:
            s1 = self.x_to_sample(start_x)
            s2 = self.x_to_sample(end_x)
            self.app.waveform_set_loop(self.deck_name, min(s1, s2), max(s1, s2))

        self.dragging_loop = False
        self.drag_start_x = None
        self.drag_current_x = None
        self.redraw()

    def on_right_click(self, event):
        if not self.app.require_engine(silent=True):
            return
        sample = self.x_to_sample(event.x)
        self.app.waveform_set_hotcue_next(self.deck_name, sample)
        self.redraw()

    def _build_wave_cache(self):
        if self.deck is None or self.deck.audio is None or len(self.deck.audio) == 0:
            self.cached_wave = None
            return

        w = max(10, self.winfo_width())
        audio = self.deck.audio
        mono = audio.mean(axis=1)
        n = len(mono)
        self.cached_audio_len = n

        samples_per_pixel = max(1, n // w)
        mins = []
        maxs = []

        for i in range(w):
            start = i * samples_per_pixel
            end = min(n, start + samples_per_pixel)
            if start >= n:
                mins.append(0.0)
                maxs.append(0.0)
                continue
            chunk = mono[start:end]
            if len(chunk) == 0:
                mins.append(0.0)
                maxs.append(0.0)
            else:
                mins.append(float(chunk.min()))
                maxs.append(float(chunk.max()))

        self.cached_wave = (mins, maxs)

    def redraw(self):
        self.delete("all")

        w = max(10, self.winfo_width())
        h = max(10, self.winfo_height())
        mid = h / 2

        self.create_rectangle(0, 0, w, h, fill=WAVE_BG, outline="")

        for x in range(0, w, 64):
            self.create_line(x, 0, x, h, fill="#16203d")
        self.create_line(0, mid, w, mid, fill=GRID)

        if self.deck is None:
            self.create_text(
                w / 2,
                h / 2,
                text="No track loaded",
                fill=MUTED,
                font=("Segoe UI", 11, "bold"),
            )
            return

        if self.cached_wave is None or self.cached_audio_len != len(self.deck.audio):
            self._build_wave_cache()

        if self.cached_wave is None:
            return

        mins, maxs = self.cached_wave

        for x, (mn, mx) in enumerate(zip(mins, maxs)):
            y1 = mid - (mx * (h * 0.42))
            y2 = mid - (mn * (h * 0.42))
            self.create_line(x, y1, x, y2, fill=self.accent)

        if getattr(self.deck, "beat_samples", None) is not None and len(self.deck.beat_samples) > 0:
            n = max(1, len(self.deck.audio))
            for s in self.deck.beat_samples[::4]:
                x = (float(s) / n) * w
                self.create_line(x, 0, x, h, fill="#ffffff", stipple="gray50")

        if getattr(self.deck, "hot_cues", None):
            n = max(1, len(self.deck.audio))
            for cue_id, s in self.deck.hot_cues.items():
                x = (float(s) / n) * w
                self.create_line(x, 0, x, h, fill=ACCENT_D, width=2)
                self.create_text(
                    x + 8,
                    12,
                    text=str(cue_id),
                    fill=ACCENT_D,
                    anchor="w",
                    font=("Segoe UI", 8, "bold"),
                )

        if getattr(self.deck, "loop", None) and self.deck.loop.enabled:
            n = max(1, len(self.deck.audio))
            x1 = (float(self.deck.loop.start_sample) / n) * w
            x2 = (float(self.deck.loop.end_sample) / n) * w
            self.create_rectangle(x1, 0, x2, h, outline=ACCENT_C, width=2)

        if getattr(self.deck, "roll", None) and self.deck.roll.enabled:
            n = max(1, len(self.deck.audio))
            x1 = (float(self.deck.roll.start_sample) / n) * w
            x2 = (float(self.deck.roll.end_sample) / n) * w
            self.create_rectangle(x1, 0, x2, h, outline=ACCENT_E, width=2)

        if self.dragging_loop and self.drag_start_x is not None and self.drag_current_x is not None:
            x1 = min(self.drag_start_x, self.drag_current_x)
            x2 = max(self.drag_start_x, self.drag_current_x)
            self.create_rectangle(x1, 0, x2, h, outline="#ffffff", width=2, dash=(4, 4))
            self.create_text(
                (x1 + x2) / 2,
                h - 12,
                text="Loop selection",
                fill="#ffffff",
                font=("Segoe UI", 8, "bold"),
            )

        playhead = float(getattr(self.deck, "playhead", 0.0))
        n = max(1, len(self.deck.audio))
        x = (playhead / n) * w
        self.create_line(x, 0, x, h, fill="#ffffff", width=2)
        self.create_oval(x - 4, 4, x + 4, 12, fill="#ffffff", outline="")


class DeckPanel(ttk.Frame):
    def __init__(self, master, app, deck_name: str, accent: str):
        super().__init__(master, style="Card.TFrame", padding=12)
        self.app = app
        self.deck_name = deck_name
        self.accent = accent

        self.path_var = tk.StringVar(value=f"Deck {deck_name}: no track loaded")
        self.status_var = tk.StringVar(value="Not loaded")
        self.playlist_var = tk.StringVar(value="Playlist: none")
        self.gain_var = tk.DoubleVar(value=1.0)
        self.seek_var = tk.DoubleVar(value=0.0)
        self.loop_beats_var = tk.StringVar(value="8")
        self.roll_beats_var = tk.StringVar(value="4")

        title = tk.Label(
            self,
            text=f"DECK {deck_name}",
            bg=CARD_BG,
            fg=accent,
            font=("Segoe UI", 18, "bold"),
        )
        title.pack(anchor="w", pady=(0, 8))

        tk.Label(
            self,
            textvariable=self.path_var,
            bg=CARD_BG,
            fg=TEXT,
            font=("Segoe UI", 10, "bold"),
            wraplength=560,
            justify="left",
        ).pack(anchor="w")

        tk.Label(
            self,
            textvariable=self.status_var,
            bg=CARD_BG,
            fg=MUTED,
            font=("Segoe UI", 10),
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(4, 8))

        playlist_box = tk.Frame(self, bg=CARD_BG)
        playlist_box.pack(fill="x", pady=(0, 8))

        tk.Label(
            playlist_box,
            textvariable=self.playlist_var,
            bg=CARD_BG,
            fg=ACCENT_C,
            font=("Segoe UI", 9, "bold"),
            wraplength=560,
            justify="left",
        ).pack(anchor="w")

        playlist_btns = tk.Frame(playlist_box, bg=CARD_BG)
        playlist_btns.pack(anchor="w", pady=(4, 0))

        self._btn(playlist_btns, "Load Folder", lambda: self.app.load_folder_to_deck(self.deck_name), ACCENT_C).pack(side="left", padx=4)
        self._btn(playlist_btns, "Prev", lambda: self.app.playlist_prev(self.deck_name), ACCENT_D).pack(side="left", padx=4)
        self._btn(playlist_btns, "Next", lambda: self.app.playlist_next(self.deck_name), ACCENT_D).pack(side="left", padx=4)
        self._btn(playlist_btns, "Reload Deck", lambda: self.app.reload_single_deck_from_path(self.deck_name), ACCENT_E).pack(side="left", padx=4)

        listbox_wrap = tk.Frame(self, bg=CARD_BG)
        listbox_wrap.pack(fill="x", pady=(0, 8))

        tk.Label(
            listbox_wrap,
            text="Playlist Browser",
            bg=CARD_BG,
            fg=TEXT,
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 4))

        playlist_list_frame = tk.Frame(listbox_wrap, bg=CARD_BG)
        playlist_list_frame.pack(fill="x")

        self.playlist_listbox = tk.Listbox(
            playlist_list_frame,
            height=7,
            bg="#0c1020",
            fg=TEXT,
            selectbackground=accent,
            selectforeground="black",
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground="#303a68",
            font=("Segoe UI", 9),
            activestyle="none",
        )
        self.playlist_listbox.pack(side="left", fill="x", expand=True)

        list_scroll = tk.Scrollbar(playlist_list_frame, orient="vertical", command=self.playlist_listbox.yview)
        list_scroll.pack(side="right", fill="y")
        self.playlist_listbox.configure(yscrollcommand=list_scroll.set)

        self.playlist_listbox.bind("<Double-Button-1>", lambda e: self.app.playlist_double_click(self.deck_name))
        self.playlist_listbox.bind("<Return>", lambda e: self.app.playlist_double_click(self.deck_name))

        self.waveform = WaveformView(self, app, deck_name, width=560, height=140, accent=accent)
        self.waveform.pack(fill="x", pady=(4, 10))

        hint = tk.Label(
            self,
            text="Mouse: left-click seek • left-drag loop • right-click set next cue",
            bg=CARD_BG,
            fg=MUTED,
            font=("Segoe UI", 9, "italic"),
        )
        hint.pack(anchor="w", pady=(0, 8))

        row1 = tk.Frame(self, bg=CARD_BG)
        row1.pack(fill="x", pady=4)

        self._btn(row1, "Load File", lambda: self.app.load_deck(self.deck_name), accent).pack(side="left", padx=4)
        self._btn(row1, "Play", lambda: self.app.deck_play(self.deck_name), ACCENT_C).pack(side="left", padx=4)
        self._btn(row1, "Stop", lambda: self.app.deck_stop(self.deck_name), DANGER).pack(side="left", padx=4)
        self._btn(row1, "Align to Other", lambda: self.app.align_to_other(self.deck_name), ACCENT_D).pack(side="left", padx=4)
        self._btn(row1, "Drop Sync", lambda: self.app.drop_sync(self.deck_name), ACCENT_E).pack(side="left", padx=4)

        row2 = tk.Frame(self, bg=CARD_BG)
        row2.pack(fill="x", pady=8)

        self._btn(row2, "Sync to Other", lambda: self.app.sync_to_other(self.deck_name), ACCENT_B).pack(side="left", padx=4)
        self._btn(row2, "Unsync", lambda: self.app.unsync(self.deck_name), DANGER).pack(side="left", padx=4)
        self._btn(row2, "Mute On/Off", lambda: self.app.toggle_mute(self.deck_name), ACCENT_D).pack(side="left", padx=4)
        self._btn(row2, "Quantize On/Off", lambda: self.app.toggle_quantize(self.deck_name), ACCENT_E).pack(side="left", padx=4)

        gain_wrap = tk.Frame(self, bg=CARD_BG)
        gain_wrap.pack(fill="x", pady=(8, 6))

        tk.Label(gain_wrap, text="Gain", bg=CARD_BG, fg=TEXT, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Scale(
            gain_wrap,
            from_=0.0,
            to=2.0,
            resolution=0.01,
            orient="horizontal",
            variable=self.gain_var,
            command=lambda _=None: self.app.set_deck_gain(self.deck_name, self.gain_var.get()),
            bg=CARD_BG,
            fg=TEXT,
            highlightthickness=0,
            troughcolor="#2d3560",
            activebackground=accent,
            length=400,
        ).pack(anchor="w")

        seek_wrap = tk.Frame(self, bg=CARD_BG)
        seek_wrap.pack(fill="x", pady=(6, 8))

        tk.Label(seek_wrap, text="Seek (seconds)", bg=CARD_BG, fg=TEXT, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        seek_row = tk.Frame(seek_wrap, bg=CARD_BG)
        seek_row.pack(anchor="w", fill="x")
        seek_e = tk.Entry(seek_row, textvariable=self.seek_var, width=10)
        apply_3d_entry(seek_e)
        seek_e.pack(side="left", padx=(0, 8))
        self._btn(seek_row, "Go", lambda: self.app.seek_deck(self.deck_name, self.seek_var.get()), accent).pack(side="left")

        loop_box = tk.LabelFrame(
            self,
            text="Loop / Roll",
            bg=CARD_BG,
            fg=TEXT,
            font=("Segoe UI", 10, "bold"),
            bd=1,
            relief="solid",
        )
        loop_box.pack(fill="x", pady=8)

        loop_row = tk.Frame(loop_box, bg=CARD_BG)
        loop_row.pack(fill="x", pady=6)
        tk.Label(loop_row, text="Loop beats", bg=CARD_BG, fg=TEXT).pack(side="left", padx=6)
        ttk.Combobox(loop_row, textvariable=self.loop_beats_var, values=["1", "2", "4", "8", "16", "32"], width=6).pack(side="left")
        self._btn(loop_row, "Loop On", lambda: self.app.loop_beats(self.deck_name, int(self.loop_beats_var.get())), ACCENT_C).pack(side="left", padx=6)
        self._btn(loop_row, "Loop Off", lambda: self.app.loop_off(self.deck_name), DANGER).pack(side="left", padx=6)

        roll_row = tk.Frame(loop_box, bg=CARD_BG)
        roll_row.pack(fill="x", pady=6)
        tk.Label(roll_row, text="Roll beats", bg=CARD_BG, fg=TEXT).pack(side="left", padx=6)
        ttk.Combobox(roll_row, textvariable=self.roll_beats_var, values=["1", "2", "4", "8", "16"], width=6).pack(side="left")
        self._btn(roll_row, "Roll On", lambda: self.app.roll_beats(self.deck_name, int(self.roll_beats_var.get())), ACCENT_D).pack(side="left", padx=6)
        self._btn(roll_row, "Roll Off", lambda: self.app.roll_off(self.deck_name), DANGER).pack(side="left", padx=6)

        cue_box = tk.LabelFrame(
            self,
            text="Hot Cues",
            bg=CARD_BG,
            fg=TEXT,
            font=("Segoe UI", 10, "bold"),
            bd=1,
            relief="solid",
        )
        cue_box.pack(fill="x", pady=8)

        for row in range(2):
            cue_row = tk.Frame(cue_box, bg=CARD_BG)
            cue_row.pack(fill="x", pady=4)
            for i in range(4):
                idx = row * 4 + i + 1
                self._btn(cue_row, f"Set {idx}", lambda n=idx: self.app.cue_set(self.deck_name, n), self.accent).pack(side="left", padx=4)
                self._btn(cue_row, f"Go {idx}", lambda n=idx: self.app.cue_jump(self.deck_name, n), ACCENT_B).pack(side="left", padx=4)

    def _btn(self, parent, text, command, color):
        b = tk.Button(
            parent,
            text=text,
            command=command,
            bg=color,
            fg="black",
            activebackground=color,
            activeforeground="black",
            relief=tk.RAISED,
            bd=3,
            padx=8,
            pady=5,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
            highlightthickness=1,
            highlightbackground="#5a6aa8",
            highlightcolor="#8899dd",
        )
        return b


class DJGuiApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("DJ Engine Pro GUI")
        self.root.configure(bg=APP_BG)
        self.root.geometry("1700x980")
        self.root.minsize(1100, 700)
        self.root.resizable(True, True)

        self.engine = None
        self.deck_paths = {"A": None, "B": None}
        self.deck_playlists = {"A": [], "B": []}
        self.deck_playlist_index = {"A": -1, "B": -1}
        self.deck_finished_flags = {"A": False, "B": False}
        self.crossfader_var = tk.DoubleVar(value=0.0)
        self.master_gain_var = tk.DoubleVar(value=1.0)
        self.beat_tracker_var = tk.StringVar(value="auto")
        self.jingles_folder = ""
        self._library_paths: list[str] = []
        self._load_track_library()

        self._style()
        self._build_ui()
        self._start_status_updater()
        self._start_waveform_updater()
        self._start_playlist_auto_advance()

    def _style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Card.TFrame", background=CARD_BG)
        style.configure("Panel.TFrame", background=PANEL_BG)
        style.configure("Main.TFrame", background=APP_BG)

    def _build_ui(self):
        main = ttk.Frame(self.root, style="Main.TFrame", padding=12)
        main.pack(fill="both", expand=True)

        header = tk.Frame(main, bg=APP_BG)
        header.pack(fill="x", pady=(0, 10))

        tk.Label(
            header,
            text="SAMSEL DJ ENGINE PRO",
            bg=APP_BG,
            fg=TEXT,
            font=("Segoe UI", 24, "bold"),
        ).pack(side="left")

        tk.Label(
            header,
            text="Waveform • playhead • beats • cues • loops • rolls • sync • drop transitions • auto DJ • playlists",
            bg=APP_BG,
            fg=MUTED,
            font=("Segoe UI", 11),
        ).pack(side="left", padx=16, pady=8)

        toolbar = tk.Frame(main, bg=PANEL_BG, highlightthickness=1, highlightbackground=GRID)
        toolbar.pack(fill="x", pady=(0, 10))
        tb_inner = tk.Frame(toolbar, bg=PANEL_BG)
        tb_inner.pack(fill="x", padx=10, pady=8)
        for label, cmd, tip, col in [
            ("10-Band EQ", self.open_eq_window, "Master graphic EQ (popup)", ACCENT_B),
            ("Track metadata", self.open_metadata_window, "All tags for decks + playlists (popup)", ACCENT_E),
            ("Synced lyrics", self.open_lyrics_window, "LRC file next to track (popup)", ACCENT_C),
            ("Track library", self.open_library_window, "Saved folders / tracks list", ACCENT_D),
            ("Export playlist", self.export_playlist_dialog, "JSON/CSV with full metadata", ACCENT_A),
            ("Sort BPM ↑", self.sort_bpm_menu, "Sort playlist by tempo ascending", ACCENT_B),
            ("Mix folder", self.mix_folder_menu, "Load folder, sort by BPM, queue on deck", ACCENT_C),
            ("Jingles folder", self.choose_jingles_folder, "Short stings mixed before next track", ACCENT_D),
            ("Record", self.toggle_master_record, "Record post-fader master to WAV", DANGER),
        ]:
            b = self._toolbar_btn(tb_inner, label, cmd, col)
            b.pack(side="left", padx=4, pady=2)
            ToolTip(b, tip)

        self._build_crossfader_strip(main)

        top = tk.Frame(main, bg=APP_BG)
        top.pack(fill="both", expand=True)

        self.deck_a_wrap = ScrollableDeckFrame(top, bg_color=CARD_BG)
        self.deck_a_wrap.pack(side="left", fill="both", expand=True, padx=(0, 8))

        self.deck_a_panel = DeckPanel(self.deck_a_wrap.inner, self, "A", ACCENT_A)
        self.deck_a_panel.pack(fill="both", expand=True)

        center = tk.Frame(top, bg=PANEL_BG, bd=0, highlightthickness=0, width=220)
        center.pack(side="left", fill="y", padx=8)
        center.pack_propagate(False)

        self._build_center_panel(center)

        self.deck_b_wrap = ScrollableDeckFrame(top, bg_color=CARD_BG)
        self.deck_b_wrap.pack(side="left", fill="both", expand=True, padx=(8, 0))

        self.deck_b_panel = DeckPanel(self.deck_b_wrap.inner, self, "B", ACCENT_B)
        self.deck_b_panel.pack(fill="both", expand=True)

        bottom = tk.Frame(main, bg=PANEL_BG)
        bottom.pack(fill="x", pady=(12, 0))

        tk.Label(
            bottom,
            text="ENGINE STATUS",
            bg=PANEL_BG,
            fg=TEXT,
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor="w", padx=12, pady=(10, 4))

        self.status_box = tk.Text(
            bottom,
            height=10,
            bg="#0c1020",
            fg="#9dfc9d",
            insertbackground="#9dfc9d",
            relief="flat",
            bd=0,
            font=("Consolas", 10),
            wrap="word",
        )
        self.status_box.pack(fill="x", padx=12, pady=(0, 12))
        self.status_box.insert("1.0", "Load both decks to begin.")
        self.status_box.configure(state="disabled")

    def _build_crossfader_strip(self, main: tk.Frame) -> None:
        """Full-width crossfader between toolbar and deck columns (always visible)."""
        strip = tk.Frame(main, bg=PANEL_BG, highlightthickness=1, highlightbackground=GRID)
        strip.pack(fill="x", pady=(0, 10))
        title = tk.Label(
            strip,
            text="CROSSFADER  (Deck A ◄———————————————— Deck B)",
            bg=PANEL_BG,
            fg=TEXT,
            font=("Segoe UI", 12, "bold"),
        )
        title.pack(anchor="w", padx=14, pady=(10, 4))
        ToolTip(title, "0 = full Deck A • 1 = full Deck B • Equal-power blend in the middle.")

        row = tk.Frame(strip, bg=PANEL_BG)
        row.pack(fill="x", padx=12, pady=(4, 12))
        tk.Label(row, text="A", bg=PANEL_BG, fg=ACCENT_A, font=("Segoe UI", 14, "bold"), width=3).pack(
            side="left"
        )
        xf = tk.Scale(
            row,
            from_=0.0,
            to=1.0,
            resolution=0.01,
            orient="horizontal",
            variable=self.crossfader_var,
            command=lambda _=None: self.set_crossfader(),
            bg=PANEL_BG,
            fg=TEXT,
            highlightthickness=0,
            troughcolor="#243054",
            activebackground=ACCENT_D,
            length=720,
            showvalue=1,
        )
        xf.pack(side="left", fill="x", expand=True, padx=6)
        tk.Label(row, text="B", bg=PANEL_BG, fg=ACCENT_B, font=("Segoe UI", 14, "bold"), width=3).pack(
            side="left"
        )
        ToolTip(xf, "Drag toward B to bring Deck B up; toward A for Deck A.")

    def _build_center_panel(self, parent):
        tk.Label(
            parent,
            text="MIXER",
            bg=PANEL_BG,
            fg=TEXT,
            font=("Segoe UI", 18, "bold"),
        ).pack(pady=(14, 10))

        load_row = tk.Frame(parent, bg=PANEL_BG)
        load_row.pack(fill="x", padx=12, pady=6)

        self._btn(load_row, "Load A", lambda: self.load_deck("A"), ACCENT_A).pack(fill="x", pady=4)
        self._btn(load_row, "Load B", lambda: self.load_deck("B"), ACCENT_B).pack(fill="x", pady=4)
        self._btn(load_row, "Start Engine", self.init_engine, ACCENT_C).pack(fill="x", pady=4)
        self._btn(load_row, "Stop Engine", self.stop_engine, DANGER).pack(fill="x", pady=4)

        tk.Label(parent, text="Beat detection", bg=PANEL_BG, fg=TEXT, font=("Segoe UI", 10, "bold")).pack(
            pady=(12, 2)
        )
        beat_row = tk.Frame(parent, bg=PANEL_BG)
        beat_row.pack(fill="x", padx=8)
        cb = ttk.Combobox(
            beat_row,
            textvariable=self.beat_tracker_var,
            values=("auto", "librosa"),
            width=11,
            state="readonly",
        )
        cb.pack(fill="x")
        ToolTip(
            cb,
            "auto: Queen Mary RNN+DBN (madmom) when installed, else librosa.\n"
            "librosa: librosa.beat.beat_track only.\n"
            "Install: pip install madmom",
        )

        tk.Label(parent, text="Master Gain", bg=PANEL_BG, fg=TEXT, font=("Segoe UI", 11, "bold")).pack(pady=(18, 4))
        tk.Scale(
            parent,
            from_=0.0,
            to=2.0,
            resolution=0.01,
            orient="vertical",
            variable=self.master_gain_var,
            command=lambda _=None: self.set_master_gain(),
            bg=PANEL_BG,
            fg=TEXT,
            highlightthickness=0,
            troughcolor="#243054",
            activebackground=ACCENT_C,
            length=160,
        ).pack(pady=4)

        ai_box = tk.LabelFrame(
            parent,
            text="Auto DJ / Smart Actions",
            bg=PANEL_BG,
            fg=TEXT,
            font=("Segoe UI", 10, "bold"),
            bd=1,
            relief="solid",
        )
        ai_box.pack(fill="x", padx=12, pady=16)

        self._btn(ai_box, "Auto DJ ON", self.auto_on, ACCENT_C).pack(fill="x", padx=8, pady=5)
        self._btn(ai_box, "Auto DJ OFF", self.auto_off, DANGER).pack(fill="x", padx=8, pady=5)
        self._btn(ai_box, "Drop B into A", lambda: self.manual_drop("B"), ACCENT_E).pack(fill="x", padx=8, pady=5)
        self._btn(ai_box, "Drop A into B", lambda: self.manual_drop("A"), ACCENT_E).pack(fill="x", padx=8, pady=5)
        self._btn(ai_box, "Align B to A", lambda: self.align_specific("B", "A"), ACCENT_D).pack(fill="x", padx=8, pady=5)
        self._btn(ai_box, "Align A to B", lambda: self.align_specific("A", "B"), ACCENT_D).pack(fill="x", padx=8, pady=5)

    def _btn(self, parent, text, command, color):
        b = tk.Button(
            parent,
            text=text,
            command=command,
            bg=color,
            fg="black",
            activebackground=color,
            activeforeground="black",
            relief=tk.RAISED,
            bd=3,
            padx=10,
            pady=8,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
            highlightthickness=1,
            highlightbackground="#5a6aa8",
            highlightcolor="#8899dd",
        )
        return b

    def _toolbar_btn(self, parent, text, command, color):
        return self._btn(parent, text, command, color)

    def _beat_tracker_kw(self) -> str:
        v = (self.beat_tracker_var.get() or "auto").strip().lower()
        return "librosa" if v == "librosa" else "auto"

    def _load_track_library(self) -> None:
        self._library_paths = []
        try:
            if LIBRARY_JSON.is_file():
                data = json.loads(LIBRARY_JSON.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._library_paths = [str(x) for x in data if isinstance(x, str) and os.path.isfile(x)]
        except Exception:
            self._library_paths = []

    def _save_track_library(self) -> None:
        try:
            LIBRARY_JSON.write_text(json.dumps(self._library_paths, indent=2), encoding="utf-8")
        except Exception as e:
            messagebox.showerror("Library save failed", str(e))

    def open_eq_window(self) -> None:
        top = tk.Toplevel(self.root)
        top.title("10-Band Graphic EQ (master)")
        top.configure(bg=CARD_BG)
        top.geometry("420x520")
        gains = [
            tk.DoubleVar(
                value=float(self.engine.eq_gains_db[i]) if self.engine and hasattr(self.engine, "eq_gains_db") else 0.0
            )
            for i in range(10)
        ]

        bypass_var = tk.BooleanVar(value=not (self.engine and self.engine.eq_enabled))

        def apply_eq() -> None:
            if not self.require_engine():
                return
            self.engine.set_eq_enabled(not bypass_var.get())
            self.engine.set_eq_gains_db([g.get() for g in gains])

        hdr = tk.Label(top, text="Master EQ • applied after crossfader", bg=CARD_BG, fg=TEXT, font=("Segoe UI", 12, "bold"))
        hdr.pack(anchor="w", padx=12, pady=(12, 6))

        for i in range(10):
            row = tk.Frame(top, bg=CARD_BG)
            row.pack(fill="x", padx=12, pady=2)
            tk.Label(row, text=f"{EQ_BAND_HZ[i]} Hz", bg=CARD_BG, fg=MUTED, width=6, anchor="w").pack(side="left")
            tk.Scale(
                row,
                from_=-12.0,
                to=12.0,
                resolution=0.5,
                orient="horizontal",
                variable=gains[i],
                length=260,
                command=lambda _v=None: apply_eq() if self.engine else None,
                bg=CARD_BG,
                fg=TEXT,
                highlightthickness=0,
                troughcolor="#243054",
            ).pack(side="left", fill="x", expand=True)

        row2 = tk.Frame(top, bg=CARD_BG)
        row2.pack(fill="x", padx=12, pady=10)
        tk.Checkbutton(
            row2,
            text="Bypass EQ",
            variable=bypass_var,
            command=apply_eq,
            bg=CARD_BG,
            fg=TEXT,
            selectcolor="#243054",
            activebackground=CARD_BG,
            activeforeground=TEXT,
        ).pack(side="left")

        def reset_eq() -> None:
            for g in gains:
                g.set(0.0)
            apply_eq()

        self._btn(row2, "Reset flat", reset_eq, ACCENT_D).pack(side="left", padx=8)
        self._btn(row2, "Close", top.destroy, MUTED).pack(side="right")

    def open_metadata_window(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Track metadata (all tags)")
        win.configure(bg=CARD_BG)
        win.geometry("900x640")
        txt = tk.Text(
            win,
            bg="#0c1020",
            fg=TEXT,
            insertbackground=TEXT,
            relief=tk.SUNKEN,
            bd=3,
            font=("Consolas", 9),
            wrap="word",
        )
        txt.pack(fill="both", expand=True, padx=10, pady=10)

        def refresh() -> None:
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            if MutagenFile is None:
                txt.insert("end", "mutagen not installed. pip install mutagen\n\n")
            for deck in ("A", "B"):
                txt.insert("end", f"========== DECK {deck} (current file) ==========\n")
                p = self.deck_paths.get(deck)
                meta = mutagen_metadata_flat(p or "")
                for k, v in meta.items():
                    txt.insert("end", f"{k}: {v}\n")
                txt.insert("end", "\n")
            for deck in ("A", "B"):
                txt.insert("end", f"========== DECK {deck} PLAYLIST (every track) ==========\n")
                for idx, p in enumerate(self.deck_playlists.get(deck, []), start=1):
                    txt.insert("end", f"\n--- #{idx} {os.path.basename(p)} ---\n")
                    for k, v in mutagen_metadata_flat(p).items():
                        txt.insert("end", f"  {k}: {v}\n")
                txt.insert("end", "\n")
            txt.configure(state="disabled")

        refresh()
        bf = tk.Frame(win, bg=CARD_BG)
        bf.pack(fill="x", padx=10, pady=(0, 10))
        self._btn(bf, "Refresh", refresh, ACCENT_C).pack(side="left")
        self._btn(bf, "Close", win.destroy, MUTED).pack(side="right")

    def open_lyrics_window(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Synced lyrics (LRC)")
        win.configure(bg=CARD_BG)
        win.geometry("560x420")
        deck_var = tk.StringVar(value="A")
        head = tk.Frame(win, bg=CARD_BG)
        head.pack(fill="x", padx=10, pady=8)
        tk.Label(head, text="Deck", bg=CARD_BG, fg=TEXT).pack(side="left")
        ttk.Combobox(head, textvariable=deck_var, values=("A", "B"), width=4, state="readonly").pack(side="left", padx=6)

        line_var = tk.StringVar(value="Load a track and matching .lrc (same name).")
        cur = tk.Label(
            win,
            textvariable=line_var,
            bg="#14182e",
            fg=ACCENT_C,
            font=("Segoe UI", 16, "bold"),
            wraplength=500,
            justify="center",
            relief=tk.SUNKEN,
            bd=4,
            padx=12,
            pady=24,
        )
        cur.pack(fill="both", expand=True, padx=12, pady=8)

        def current_lines():
            d = deck_var.get()
            path = self.deck_paths.get(d)
            if not path:
                return [], 0.0, ""
            lrc_path = str(Path(path).with_suffix(".lrc"))
            return parse_lrc_file(lrc_path), 0.0, lrc_path

        def tick() -> None:
            if not win.winfo_exists():
                return
            d = deck_var.get()
            lines, _, lrc_path = current_lines()
            t_sec = 0.0
            if self.engine:
                try:
                    with self.engine.lock:
                        deck = self.engine._deck(d)
                        t_sec = float(deck.playhead) / float(deck.sr)
                except Exception:
                    t_sec = 0.0
            if not lines:
                line_var.set(f"No lyrics.\nExpected:\n{lrc_path if self.deck_paths.get(d) else 'No track'}")
            else:
                text = ""
                for i, (ts, tx) in enumerate(lines):
                    if ts <= t_sec:
                        text = tx
                    else:
                        break
                line_var.set(text or lines[0][1])
            win.after(220, tick)

        tick()
        bf = tk.Frame(win, bg=CARD_BG)
        bf.pack(fill="x", padx=10, pady=(0, 10))
        self._btn(bf, "Close", win.destroy, MUTED).pack(side="right")

    def open_library_window(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Tracks library")
        win.configure(bg=CARD_BG)
        win.geometry("640x480")
        lb = tk.Listbox(
            win,
            bg="#0c1020",
            fg=TEXT,
            selectbackground=ACCENT_B,
            font=("Segoe UI", 10),
            height=18,
        )
        lb.pack(fill="both", expand=True, padx=10, pady=10)

        def refresh_lb() -> None:
            lb.delete(0, tk.END)
            for p in self._library_paths:
                lb.insert(tk.END, p)

        def add_folder() -> None:
            folder = filedialog.askdirectory(title="Add folder to library")
            if not folder:
                return
            found = self.collect_audio_files(folder, True)
            before = set(self._library_paths)
            for p in found:
                if p not in before:
                    self._library_paths.append(p)
            self._save_track_library()
            refresh_lb()

        def load_to_deck_a() -> None:
            sel = lb.curselection()
            if not sel:
                return
            path = self._library_paths[int(sel[0])]
            if os.path.isfile(path):
                self.deck_paths["A"] = path
                self.deck_playlists["A"] = [path]
                self.deck_playlist_index["A"] = 0
                self.deck_a_panel.path_var.set(path)
                self.deck_a_panel.status_var.set("From library — Start Engine or Reload Deck.")
                self.refresh_playlist_ui("A")

        refresh_lb()
        bf = tk.Frame(win, bg=CARD_BG)
        bf.pack(fill="x", padx=10, pady=(0, 10))
        self._btn(bf, "Add folder", add_folder, ACCENT_C).pack(side="left", padx=4)
        self._btn(bf, "Load selected → Deck A", load_to_deck_a, ACCENT_A).pack(side="left", padx=4)
        self._btn(bf, "Remove selected", lambda: remove_sel(), DANGER).pack(side="left", padx=4)
        self._btn(bf, "Close", win.destroy, MUTED).pack(side="right")

        def remove_sel() -> None:
            sel = lb.curselection()
            if not sel:
                return
            i = int(sel[0])
            self._library_paths.pop(i)
            self._save_track_library()
            refresh_lb()

    def export_playlist_dialog(self) -> None:
        deck = messagebox.askyesnocancel("Export", "Yes = Deck A, No = Deck B, Cancel = abort")
        if deck is None:
            return
        name = "A" if deck else "B"
        files = list(self.deck_playlists.get(name, []))
        if not files:
            messagebox.showwarning("Empty", f"No playlist for Deck {name}")
            return
        path = filedialog.asksaveasfilename(
            title="Export playlist + metadata",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("CSV", "*.csv")],
        )
        if not path:
            return
        rows = []
        for p in files:
            row = mutagen_metadata_flat(p)
            row["path"] = p
            rows.append(row)
        ext = Path(path).suffix.lower()
        try:
            if ext == ".csv":
                keys: set[str] = set()
                for r in rows:
                    keys.update(r.keys())
                fieldnames = sorted(keys)
                with open(path, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                    w.writeheader()
                    w.writerows(rows)
            else:
                payload = {
                    "exported_at": datetime.now().isoformat(timespec="seconds"),
                    "deck": name,
                    "track_count": len(rows),
                    "tracks": rows,
                }
                Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            messagebox.showinfo("Exported", path)
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

    def sort_bpm_menu(self) -> None:
        deck = messagebox.askyesnocancel("Sort by BPM", "Yes = Deck A, No = Deck B, Cancel = abort")
        if deck is None:
            return
        self.sort_playlist_by_bpm("A" if deck else "B")

    def mix_folder_menu(self) -> None:
        deck = messagebox.askyesnocancel("Mix folder", "Yes = Deck A, No = Deck B, Cancel = abort")
        if deck is None:
            return
        self.mix_folder_to_deck("A" if deck else "B")

    def sort_playlist_by_bpm(self, deck_name: str) -> None:
        files = list(self.deck_playlists.get(deck_name, []))
        if not files:
            messagebox.showinfo("Playlist", "No tracks to sort.")
            return
        self.append_status(f"Deck {deck_name}: analyzing BPM for {len(files)} file(s)...")
        self.root.update_idletasks()
        scored = [(quick_bpm_for_sort(p), p) for p in files]
        scored.sort(key=lambda x: (x[0], os.path.basename(x[1]).lower()))
        new_list = [p for _, p in scored]
        self.deck_playlists[deck_name] = new_list
        cur = self.deck_paths.get(deck_name)
        if cur in new_list:
            self.deck_playlist_index[deck_name] = new_list.index(cur)
        else:
            self.deck_playlist_index[deck_name] = 0
        self.refresh_playlist_ui(deck_name)
        self.append_status(f"Deck {deck_name}: playlist sorted by BPM (ascending).")

    def mix_folder_to_deck(self, deck_name: str) -> None:
        folder = filedialog.askdirectory(title="Folder to mix / queue (BPM sort)")
        if not folder:
            return
        files = self.collect_audio_files(folder, True)
        if not files:
            messagebox.showwarning("No audio", "No supported audio files in that folder.")
            return
        self.append_status(f"Deck {deck_name}: scanning BPM for {len(files)} file(s)...")
        self.root.update_idletasks()
        scored = [(quick_bpm_for_sort(p), p) for p in files]
        scored.sort(key=lambda x: (x[0], os.path.basename(x[1]).lower()))
        ordered = [p for _, p in scored]
        self.deck_playlists[deck_name] = ordered
        self.deck_playlist_index[deck_name] = 0
        self.deck_paths[deck_name] = ordered[0]
        self.deck_finished_flags[deck_name] = False
        panel = self._deck_panel(deck_name)
        panel.path_var.set(ordered[0])
        panel.status_var.set(
            f"Mix folder: {len(ordered)} track(s), BPM-sorted.\nStart Engine or Reload Deck to play."
        )
        self.refresh_playlist_ui(deck_name)
        for p in ordered:
            if p not in self._library_paths:
                self._library_paths.append(p)
        self._save_track_library()
        self.append_status(f"Deck {deck_name}: mix folder loaded + library updated.")

    def choose_jingles_folder(self) -> None:
        folder = filedialog.askdirectory(title="Jingles / stings folder")
        if not folder:
            return
        self.jingles_folder = folder
        self.append_status(f"Jingles folder: {folder}")

    def toggle_master_record(self) -> None:
        if not self.require_engine():
            return
        if self.engine.is_recording():
            self.engine.stop_recording()
            self.append_status("Master recording stopped.")
            return
        path = filedialog.asksaveasfilename(
            title="Record master mix",
            defaultextension=".wav",
            filetypes=[("WAV", "*.wav")],
        )
        if not path:
            return
        try:
            self.engine.start_recording(path)
            self.append_status(f"Recording master to: {path}")
        except Exception as e:
            messagebox.showerror("Record", str(e))

    def collect_audio_files(self, folder: str, recursive: bool = True):
        p = Path(folder)
        if not p.exists() or not p.is_dir():
            return []

        files = []
        iterator = p.rglob("*") if recursive else p.glob("*")
        for f in iterator:
            if f.is_file() and f.suffix.lower() in SUPPORTED_AUDIO_EXTS:
                files.append(str(f))

        files.sort(key=lambda s: s.lower())
        return files

    def require_engine(self, silent=False):
        if not self.engine:
            if not silent:
                messagebox.showwarning("Engine not ready", "Start the engine first.")
            return False
        return True

    def load_deck(self, deck_name: str):
        path = filedialog.askopenfilename(
            title=f"Load track for Deck {deck_name}",
            filetypes=[("Audio files", "*.mp3 *.wav *.flac *.ogg *.m4a *.aac"), ("All files", "*.*")],
        )
        if not path:
            return

        self.deck_paths[deck_name] = path
        self.deck_playlists[deck_name] = [path]
        self.deck_playlist_index[deck_name] = 0
        self.deck_finished_flags[deck_name] = False

        panel = self._deck_panel(deck_name)
        panel.path_var.set(path)
        panel.status_var.set("Loaded path. Press Start Engine to analyze and initialize deck.")
        self.refresh_playlist_ui(deck_name)

    def load_folder_to_deck(self, deck_name: str):
        folder = filedialog.askdirectory(title=f"Load folder for Deck {deck_name}")
        if not folder:
            return

        files = self.collect_audio_files(folder, recursive=True)
        if not files:
            messagebox.showwarning("No audio files", f"No supported audio files found in:\n{folder}")
            return

        self.deck_playlists[deck_name] = files
        self.deck_playlist_index[deck_name] = 0
        self.deck_paths[deck_name] = files[0]
        self.deck_finished_flags[deck_name] = False

        panel = self._deck_panel(deck_name)
        panel.path_var.set(files[0])
        panel.status_var.set(
            f"Playlist loaded: {len(files)} track(s)\n"
            f"Track 1 of {len(files)} ready. Press Start Engine to initialize."
        )

        self.append_status(f"Deck {deck_name} folder loaded: {len(files)} tracks")
        self.refresh_playlist_ui(deck_name)

    def refresh_playlist_ui(self, deck_name: str):
        panel = self._deck_panel(deck_name)
        files = self.deck_playlists[deck_name]
        idx = self.deck_playlist_index[deck_name]

        panel.playlist_listbox.delete(0, tk.END)
        for i, path in enumerate(files):
            panel.playlist_listbox.insert(tk.END, f"{i + 1:02d}. {os.path.basename(path)}")

        if not files or idx < 0 or idx >= len(files):
            panel.playlist_var.set("Playlist: none")
            return

        panel.playlist_var.set(
            f"Playlist: {idx + 1}/{len(files)} • {os.path.basename(files[idx])}"
        )
        panel.playlist_listbox.selection_clear(0, tk.END)
        panel.playlist_listbox.selection_set(idx)
        panel.playlist_listbox.activate(idx)
        panel.playlist_listbox.see(idx)

    def select_playlist_track(self, deck_name: str, new_index: int):
        files = self.deck_playlists[deck_name]
        if not files:
            messagebox.showwarning("No playlist", f"No playlist loaded for Deck {deck_name}")
            return

        new_index = max(0, min(len(files) - 1, int(new_index)))
        self.deck_playlist_index[deck_name] = new_index
        self.deck_paths[deck_name] = files[new_index]
        self.deck_finished_flags[deck_name] = False

        panel = self._deck_panel(deck_name)
        panel.path_var.set(files[new_index])
        panel.status_var.set(
            f"Playlist track selected: {new_index + 1}/{len(files)}\n"
            f"Press Start Engine to initialize, or use Reload Deck if engine is already running."
        )

        self.refresh_playlist_ui(deck_name)
        self.append_status(f"Deck {deck_name} selected playlist track {new_index + 1}")

    def playlist_prev(self, deck_name: str):
        files = self.deck_playlists[deck_name]
        if not files:
            return
        idx = self.deck_playlist_index[deck_name]
        if idx > 0:
            self.select_playlist_track(deck_name, idx - 1)

    def playlist_next(self, deck_name: str):
        files = self.deck_playlists[deck_name]
        if not files:
            return
        idx = self.deck_playlist_index[deck_name]
        if idx < len(files) - 1:
            self.select_playlist_track(deck_name, idx + 1)

    def playlist_double_click(self, deck_name: str):
        panel = self._deck_panel(deck_name)
        selection = panel.playlist_listbox.curselection()
        if not selection:
            return
        idx = int(selection[0])
        self.select_playlist_track(deck_name, idx)
        if self.engine:
            self.reload_single_deck_from_path(deck_name)

    def reload_single_deck_from_path(self, deck_name: str):
        if not self.require_engine():
            return

        path = self.deck_paths[deck_name]
        if not path:
            messagebox.showwarning("No track", f"No track selected for Deck {deck_name}")
            return

        try:
            new_deck = make_deck(deck_name, path, beat_tracker=self._beat_tracker_kw())

            with self.engine.lock:
                current = self.engine._deck(deck_name)
                was_playing = current.playing
                gain = current.gain
                mute = current.mute
                quantize = current.quantize

                new_deck.gain = gain
                new_deck.mute = mute
                new_deck.quantize = quantize
                new_deck.playing = was_playing

                if deck_name == "A":
                    self.engine.deck_a = new_deck
                else:
                    self.engine.deck_b = new_deck

            panel = self._deck_panel(deck_name)
            panel.path_var.set(path)
            panel.status_var.set(f"BPM {new_deck.bpm:.2f} • {new_deck.duration_sec:.2f}s")
            panel.waveform.set_deck(new_deck)
            self.deck_finished_flags[deck_name] = False
            self.refresh_playlist_ui(deck_name)

            self.append_status(f"Deck {deck_name} reloaded: {os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Reload error", str(e))
            self.append_status(f"Deck {deck_name} reload error: {e}")

    def auto_advance_deck(self, deck_name: str):
        files = self.deck_playlists[deck_name]
        idx = self.deck_playlist_index[deck_name]

        if not files or idx < 0:
            return
        if idx >= len(files) - 1:
            self.append_status(f"Deck {deck_name} reached end of playlist")
            return

        def advance_to_next(from_idx: int) -> None:
            next_idx = from_idx + 1
            if next_idx >= len(files):
                return
            self.deck_playlist_index[deck_name] = next_idx
            self.deck_paths[deck_name] = files[next_idx]
            self.deck_finished_flags[deck_name] = False
            self.append_status(
                f"Deck {deck_name} auto-loading next playlist track: {os.path.basename(files[next_idx])}"
            )
            self.reload_single_deck_from_path(deck_name)

        jroot = (self.jingles_folder or "").strip()
        if jroot and os.path.isdir(jroot) and self.engine:
            jfiles = self.collect_audio_files(jroot, recursive=True)
            if jfiles:
                jp = random.choice(jfiles)
                try:
                    self.engine.queue_jingle_overlay(jp, gain=0.95)
                    delay_ms = int(max(self.engine.overlay_seconds_left(), 0.05) * 1000) + 120
                    self.root.after(delay_ms, lambda: advance_to_next(idx))
                    return
                except Exception as e:
                    self.append_status(f"Jingle skipped: {e}")

        advance_to_next(idx)

    def init_engine(self):
        if not self.deck_paths["A"] or not self.deck_paths["B"]:
            messagebox.showwarning("Missing decks", "Load both Deck A and Deck B first.")
            return

        try:
            self.append_status("Loading Deck A...")
            deck_a = make_deck("A", self.deck_paths["A"], beat_tracker=self._beat_tracker_kw())
            self.append_status(f"Deck A loaded: bpm={deck_a.bpm:.2f}, duration={deck_a.duration_sec:.2f}s")

            self.append_status("Loading Deck B...")
            deck_b = make_deck("B", self.deck_paths["B"], beat_tracker=self._beat_tracker_kw())
            self.append_status(f"Deck B loaded: bpm={deck_b.bpm:.2f}, duration={deck_b.duration_sec:.2f}s")

            self.engine = DJEnginePro(deck_a, deck_b, blocksize=1024)
            self.engine.set_crossfader(self.crossfader_var.get())
            self.engine.set_master_gain(self.master_gain_var.get())
            self.engine.start()

            self.deck_a_panel.status_var.set(f"BPM {deck_a.bpm:.2f} • {deck_a.duration_sec:.2f}s")
            self.deck_b_panel.status_var.set(f"BPM {deck_b.bpm:.2f} • {deck_b.duration_sec:.2f}s")

            self.deck_a_panel.waveform.set_deck(deck_a)
            self.deck_b_panel.waveform.set_deck(deck_b)

            self.deck_finished_flags["A"] = False
            self.deck_finished_flags["B"] = False
            self.refresh_playlist_ui("A")
            self.refresh_playlist_ui("B")

            self.append_status("Engine started.")
        except Exception as e:
            messagebox.showerror("Engine error", str(e))
            self.append_status(f"ERROR: {e}")

    def stop_engine(self):
        if self.engine:
            try:
                self.engine.stop()
                self.append_status("Engine stopped.")
            except Exception as e:
                self.append_status(f"ERROR stopping engine: {e}")

    def deck_play(self, deck_name):
        if not self.require_engine():
            return
        with self.engine.lock:
            self.engine._deck(deck_name).play()
            self.deck_finished_flags[deck_name] = False
        self.append_status(f"Deck {deck_name} play")

    def deck_stop(self, deck_name):
        if not self.require_engine():
            return
        with self.engine.lock:
            self.engine._deck(deck_name).stop()
        self.append_status(f"Deck {deck_name} stop")

    def set_crossfader(self):
        if self.engine:
            self.engine.set_crossfader(self.crossfader_var.get())

    def set_master_gain(self):
        if self.engine:
            self.engine.set_master_gain(self.master_gain_var.get())

    def set_deck_gain(self, deck_name, gain):
        if not self.require_engine():
            return
        with self.engine.lock:
            self.engine._deck(deck_name).set_gain(float(gain))

    def seek_deck(self, deck_name, seconds):
        if not self.require_engine():
            return
        with self.engine.lock:
            deck = self.engine._deck(deck_name)
            deck.set_playhead(int(float(seconds) * deck.sr), quantize=False)
            self.deck_finished_flags[deck_name] = False
        self._deck_panel(deck_name).waveform.redraw()
        self.append_status(f"Deck {deck_name} seek -> {seconds}s")

    def waveform_seek(self, deck_name, sample):
        if not self.require_engine():
            return
        with self.engine.lock:
            deck = self.engine._deck(deck_name)
            deck.set_playhead(int(sample), quantize=False)
            self.deck_finished_flags[deck_name] = False
        self._deck_panel(deck_name).waveform.redraw()
        self.append_status(f"Deck {deck_name} waveform seek")

    def waveform_set_loop(self, deck_name, start_sample, end_sample):
        if not self.require_engine():
            return
        with self.engine.lock:
            deck = self.engine._deck(deck_name)
            deck.enable_loop(int(start_sample), int(end_sample), quantize=True)
        self._deck_panel(deck_name).waveform.invalidate()
        self.append_status(f"Deck {deck_name} loop set from waveform")

    def waveform_set_hotcue_next(self, deck_name, sample):
        if not self.require_engine():
            return
        with self.engine.lock:
            deck = self.engine._deck(deck_name)
            deck.set_playhead(int(sample), quantize=True)

            used = set(deck.hot_cues.keys())
            next_idx = None
            for i in range(1, 9):
                if i not in used:
                    next_idx = i
                    break
            if next_idx is None:
                next_idx = 8

            deck.set_hot_cue(next_idx, quantize=True)

        self._deck_panel(deck_name).waveform.invalidate()
        self.append_status(f"Deck {deck_name} set waveform cue {next_idx}")

    def cue_set(self, deck_name, idx):
        if not self.require_engine():
            return
        with self.engine.lock:
            self.engine._deck(deck_name).set_hot_cue(idx)
        self._deck_panel(deck_name).waveform.invalidate()
        self.append_status(f"Deck {deck_name} set cue {idx}")

    def cue_jump(self, deck_name, idx):
        if not self.require_engine():
            return
        with self.engine.lock:
            ok = self.engine._deck(deck_name).jump_hot_cue(idx)
            if ok:
                self.deck_finished_flags[deck_name] = False
        self._deck_panel(deck_name).waveform.redraw()
        if ok:
            self.append_status(f"Deck {deck_name} jump cue {idx}")
        else:
            self.append_status(f"Deck {deck_name} cue {idx} not set")

    def loop_beats(self, deck_name, beats):
        if not self.require_engine():
            return
        with self.engine.lock:
            self.engine._deck(deck_name).enable_loop_beats(beats)
        self._deck_panel(deck_name).waveform.invalidate()
        self.append_status(f"Deck {deck_name} loop {beats} beats")

    def loop_off(self, deck_name):
        if not self.require_engine():
            return
        with self.engine.lock:
            self.engine._deck(deck_name).disable_loop()
        self._deck_panel(deck_name).waveform.invalidate()
        self.append_status(f"Deck {deck_name} loop off")

    def roll_beats(self, deck_name, beats):
        if not self.require_engine():
            return
        with self.engine.lock:
            self.engine._deck(deck_name).enable_roll_beats(beats)
        self._deck_panel(deck_name).waveform.invalidate()
        self.append_status(f"Deck {deck_name} roll {beats} beats")

    def roll_off(self, deck_name):
        if not self.require_engine():
            return
        with self.engine.lock:
            self.engine._deck(deck_name).disable_roll()
        self._deck_panel(deck_name).waveform.invalidate()
        self.append_status(f"Deck {deck_name} roll off")

    def sync_to_other(self, deck_name):
        if not self.require_engine():
            return
        other = "B" if deck_name == "A" else "A"
        try:
            self.engine.sync(deck_name, other)
            self._deck_panel(deck_name).waveform.set_deck(self.engine._deck(deck_name))
            self.append_status(f"Deck {deck_name} synced to Deck {other}")
        except Exception as e:
            self.append_status(f"Sync error: {e}")

    def unsync(self, deck_name):
        if not self.require_engine():
            return
        try:
            self.engine.unsync(deck_name)
            self._deck_panel(deck_name).waveform.set_deck(self.engine._deck(deck_name))
            self.append_status(f"Deck {deck_name} unsynced")
        except Exception as e:
            self.append_status(f"Unsync error: {e}")

    def align_to_other(self, deck_name):
        if not self.require_engine():
            return
        other = "B" if deck_name == "A" else "A"
        self.align_specific(deck_name, other)

    def align_specific(self, deck_name, other):
        if not self.require_engine():
            return
        try:
            self.engine.align_beats(deck_name, other)
            self._deck_panel(deck_name).waveform.redraw()
            self.append_status(f"Deck {deck_name} aligned to Deck {other}")
        except Exception as e:
            self.append_status(f"Align error: {e}")

    def drop_sync(self, deck_name):
        if not self.require_engine():
            return
        other = "B" if deck_name == "A" else "A"
        self.manual_drop(deck_name, other)

    def manual_drop(self, incoming, outgoing=None):
        if not self.require_engine():
            return
        if outgoing is None:
            outgoing = "B" if incoming == "A" else "A"
        try:
            self.engine.drop_sync_transition(incoming, outgoing, fade_beats=8)
            self._deck_panel(incoming).waveform.redraw()
            self.append_status(f"Drop-sync: Deck {incoming} into Deck {outgoing}")
        except Exception as e:
            self.append_status(f"Drop-sync error: {e}")

    def toggle_mute(self, deck_name):
        if not self.require_engine():
            return
        with self.engine.lock:
            deck = self.engine._deck(deck_name)
            deck.mute = not deck.mute
            state = deck.mute
        self.append_status(f"Deck {deck_name} mute -> {state}")

    def toggle_quantize(self, deck_name):
        if not self.require_engine():
            return
        with self.engine.lock:
            deck = self.engine._deck(deck_name)
            deck.quantize = not deck.quantize
            state = deck.quantize
        self.append_status(f"Deck {deck_name} quantize -> {state}")

    def auto_on(self):
        if not self.require_engine():
            return
        try:
            self.engine.enable_auto_dj()
            self.append_status("Auto DJ ON")
        except Exception as e:
            self.append_status(f"Auto DJ error: {e}")

    def auto_off(self):
        if not self.require_engine():
            return
        try:
            self.engine.disable_auto_dj()
            self.append_status("Auto DJ OFF")
        except Exception as e:
            self.append_status(f"Auto DJ off error: {e}")

    def _deck_panel(self, deck_name):
        return self.deck_a_panel if deck_name == "A" else self.deck_b_panel

    def append_status(self, text: str):
        self.status_box.configure(state="normal")
        self.status_box.insert("end", text + "\n")
        self.status_box.see("end")
        self.status_box.configure(state="disabled")

    def _start_status_updater(self):
        def tick():
            try:
                if self.engine:
                    status = self.engine.status()
                    self.status_box.configure(state="normal")
                    current = self.status_box.get("1.0", "end-1c").splitlines()
                    tail = current[-20:] if len(current) > 20 else current
                    display = "\n".join(tail) + ("\n\n" if tail else "") + status
                    self.status_box.delete("1.0", "end")
                    self.status_box.insert("1.0", display)
                    self.status_box.configure(state="disabled")
            except Exception:
                pass
            self.root.after(700, tick)

        self.root.after(700, tick)

    def _start_waveform_updater(self):
        def tick():
            try:
                if self.engine:
                    self.deck_a_panel.waveform.redraw()
                    self.deck_b_panel.waveform.redraw()
            except Exception:
                pass
            self.root.after(120, tick)

        self.root.after(120, tick)

    def _start_playlist_auto_advance(self):
        def tick():
            try:
                if self.engine:
                    for deck_name in ("A", "B"):
                        with self.engine.lock:
                            deck = self.engine._deck(deck_name)
                            finished = (not deck.playing) and (deck.playhead >= len(deck.audio) - 1)

                        if finished and not self.deck_finished_flags[deck_name]:
                            self.deck_finished_flags[deck_name] = True
                            self.append_status(f"Deck {deck_name} finished track")
                            self.auto_advance_deck(deck_name)
                        elif not finished:
                            self.deck_finished_flags[deck_name] = False
            except Exception:
                pass
            self.root.after(500, tick)

        self.root.after(500, tick)


def main():
    root = tk.Tk()
    app = DJGuiApp(root)

    def on_close():
        try:
            app.stop_engine()
        finally:
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
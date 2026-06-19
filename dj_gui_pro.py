import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from dj_engine_pro import DJEnginePro, make_deck


# Default folder for saving recordings (created on first use)
RECORDINGS_DIR = Path(__file__).resolve().parent / "recordings"


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


class HoverTooltip:
    """Yellow hover tooltip with multi-line / step-by-step help (tkinter has no native tooltips)."""

    def __init__(self, widget, text: str, wraplength: int = 440, delay_ms: int = 400):
        self.widget = widget
        self.text = text.strip()
        self.wraplength = wraplength
        self.delay_ms = delay_ms
        self._tip: Optional[tk.Toplevel] = None
        self._after_id: Optional[str] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except (tk.TclError, ValueError):
                pass
            self._after_id = None

    def _show(self):
        self._after_id = None
        if self._tip is not None:
            return
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        try:
            tw.attributes("-topmost", True)
        except tk.TclError:
            pass
        x = self.widget.winfo_rootx() + min(24, max(0, self.widget.winfo_width() // 4))
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tw,
            text=self.text,
            justify="left",
            background="#fffef0",
            foreground="#1a1a2e",
            relief="solid",
            borderwidth=1,
            font=("Segoe UI", 9),
            wraplength=self.wraplength,
            padx=12,
            pady=10,
        ).pack()

    def _hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


def attach_tooltip(widget, text: str, wraplength: int = 440) -> None:
    HoverTooltip(widget, text, wraplength=wraplength)


# Step-by-step instruction strings (hover tooltips)
TT = {
    "load_folder": (
        "Load folder (playlist)\n\n"
        "Step 1: Click Load Folder and choose a directory.\n"
        "Step 2: Supported files (.mp3, .wav, .flac, .ogg, .m4a, .aac) are listed in the playlist.\n"
        "Step 3: Select a row (or use Prev/Next), then Start Engine (mixer) if not running.\n"
        "Step 4: Double-click a track or use Reload Deck to load it into this deck."
    ),
    "playlist_prev_next": (
        "Playlist Prev / Next\n\n"
        "Step 1: Load a folder or file so the playlist is not empty.\n"
        "Step 2: Click Prev or Next to change the highlighted track index.\n"
        "Step 3: If the engine is running, double-click the list or Reload Deck to load the selected file."
    ),
    "reload_deck": (
        "Reload Deck\n\n"
        "Step 1: Start Engine (mixer) after loading paths for A and B.\n"
        "Step 2: Ensure this deck’s current path is set (file or playlist selection).\n"
        "Step 3: Click Reload Deck to re-analyze BPM/beats and refresh the waveform."
    ),
    "open_mixer": (
        "Open Mixer\n\n"
        "Step 1: Click to open the Mixer / Auto DJ window.\n"
        "Step 2: Use Start Engine to build the audio engine from loaded deck paths.\n"
        "Step 3: Adjust crossfader, master gain, Auto DJ, and recording from there."
    ),
    "playlist_list": (
        "Playlist browser\n\n"
        "Step 1: Load Folder or Load File to fill the list.\n"
        "Step 2: Click a row to highlight; double-click or press Enter to select that track.\n"
        "Step 3: If the engine is already started, double-click reloads this deck from that file."
    ),
    "waveform": (
        "Waveform\n\n"
        "Step 1: Start Engine and Reload Deck so the waveform shows the current track.\n"
        "Step 2: Left-click (short drag) → seek playhead to that time.\n"
        "Step 3: Left-drag across a wider distance → set a loop region (quantized if Quantize is on).\n"
        "Step 4: Right-click → assign the next free hot cue at that position (quantized if on).\n"
        "Beat markers: vertical lines; playhead: white line."
    ),
    "load_file": (
        "Load File\n\n"
        "Step 1: Choose one audio file for this deck.\n"
        "Step 2: Path appears above; playlist becomes a single track.\n"
        "Step 3: Open Mixer → Start Engine, then Reload Deck on this deck to analyze and play."
    ),
    "play_stop": (
        "Play / Stop\n\n"
        "Play — Step 1: Engine must be started. Step 2: Deck must be loaded (Reload Deck). Step 3: Starts playback.\n"
        "Stop — Stops this deck’s playback."
    ),
    "align_other": (
        "Align to Other\n\n"
        "Step 1: Both decks loaded and engine running.\n"
        "Step 2: Click to align this deck’s beat grid / timing toward the other deck (see status log).\n"
        "Step 3: Use for manual sync before mixing."
    ),
    "drop_sync": (
        "Drop Sync\n\n"
        "Step 1: Both decks playing or ready; engine running.\n"
        "Step 2: Performs a tempo-sync style transition toward the other deck.\n"
        "Step 3: Watch ENGINE STATUS for confirmation or errors."
    ),
    "sync_unsync": (
        "Sync to Other / Unsync\n\n"
        "Sync — Step 1: Engine running. Step 2: Stretches this deck toward the other deck’s BPM (quality depends on material). Step 3: Check status for result.\n"
        "Unsync — Restores this deck’s original unstretched audio (reset)."
    ),
    "mute_quantize": (
        "Mute / Quantize\n\n"
        "Mute On/Off — Toggles this deck output mute in the engine.\n"
        "Quantize On/Off — When On: seeks, loops, cues, and hot cues snap to detected beat positions."
    ),
    "gain": (
        "Gain\n\n"
        "Step 1: Engine must be running.\n"
        "Step 2: Drag the slider (0–2×) to change this deck’s level before the crossfader.\n"
        "Step 3: 1.0 is unity gain."
    ),
    "seek": (
        "Seek (seconds)\n\n"
        "Step 1: Type a time in seconds in the box (e.g. 45 or 90.5).\n"
        "Step 2: Click Go — engine seeks this deck (quantized if Quantize is on).\n"
        "Step 3: Requires engine running and deck loaded."
    ),
    "loop_beats_combo": (
        "Loop beats (length)\n\n"
        "Step 1: Choose how many beats the loop will span (1, 2, 4, 8, 16, or 32).\n"
        "Step 2: With the deck playing, click Loop On — loop starts at nearest beat under the playhead.\n"
        "Step 3: Loop Off disables looping."
    ),
    "loop_on": (
        "Loop On\n\n"
        "Step 1: Engine running; track playing or positioned.\n"
        "Step 2: Pick Loop beats count.\n"
        "Step 3: Click Loop On — enables a beat-quantized loop from the current playhead.\n"
        "Step 4: Loop Off to exit."
    ),
    "loop_off": (
        "Loop Off\n\n"
        "Step 1: Click to disable the active loop on this deck.\n"
        "Step 2: Playback continues from the current playhead without looping."
    ),
    "roll_beats_combo": (
        "Roll beats (length)\n\n"
        "Step 1: Choose loop length in beats (1, 2, 4, 8, or 16).\n"
        "Step 2: Roll On — you hear a loop, but the internal timeline keeps moving (slip roll).\n"
        "Step 3: Roll Off — playhead jumps to where the timeline would be without the roll."
    ),
    "roll_on": (
        "Roll On\n\n"
        "Step 1: Engine running; deck playing.\n"
        "Step 2: Select roll length in beats.\n"
        "Step 3: Click Roll On — slip-style roll from nearest beat.\n"
        "Step 4: Roll Off releases to the correct position on the full track timeline."
    ),
    "roll_off": (
        "Roll Off\n\n"
        "Step 1: Ends slip roll and restores playhead to the advanced timeline position.\n"
        "Step 2: If roll was not active, this still clears roll state safely."
    ),
    "hotcue_set": (
        "Set (hot cue)\n\n"
        "Step 1: Engine running; play or pause at the desired moment.\n"
        "Step 2: Click Set N — stores cue N at the current playhead (snapped to beat if Quantize On).\n"
        "Step 3: Repeat for other slots (1–8)."
    ),
    "hotcue_go": (
        "Go (hot cue)\n\n"
        "Step 1: Set N must have been stored first.\n"
        "Step 2: Click Go N — jumps playhead to that cue (quantized if Quantize On).\n"
        "Step 3: If nothing stored, jump does nothing (see status)."
    ),
    "mixer_load_ab": (
        "Load A / Load B\n\n"
        "Step 1: Opens file picker for that deck (same as deck panel Load File).\n"
        "Step 2: After both paths are chosen, click Start Engine.\n"
        "Step 3: Use Reload Deck on each deck panel to refresh waveforms."
    ),
    "start_engine": (
        "Start Engine\n\n"
        "Step 1: Load File (or folder) for Deck A and Deck B so paths are set.\n"
        "Step 2: Click Start Engine — analyzes tracks, builds decks, starts audio output.\n"
        "Step 3: Use Play on each deck and Reload Deck when you change the selected file."
    ),
    "stop_engine": (
        "Stop Engine\n\n"
        "Step 1: Stops audio engine and playback.\n"
        "Step 2: You may change files and Start Engine again.\n"
        "Step 3: Unsaved engine state (cues/loops) is reset on restart."
    ),
    "auto_dj_on_off": (
        "Auto DJ ON / OFF\n\n"
        "ON — Step 1: Engine running. Step 2: Enables automatic mixing behavior (see docs / status).\n"
        "OFF — Disables Auto DJ."
    ),
    "drop_ab": (
        "Drop B into A / Drop A into B\n\n"
        "Step 1: Engine running; both decks loaded.\n"
        "Step 2: Triggers a timed drop-style transition from the source deck into the target.\n"
        "Step 3: Monitor ENGINE STATUS for progress."
    ),
    "align_ab": (
        "Align B to A / Align A to B\n\n"
        "Step 1: Engine running.\n"
        "Step 2: Aligns the first-named deck’s timing to the second (beat alignment).\n"
        "Step 3: Use before manual blends."
    ),
    "mix_next": (
        "Mix Next A→B / B→A\n\n"
        "Step 1: Engine running; playlists with a “next” track on the source deck.\n"
        "Step 2: Loads next playlist track on the outgoing deck and runs a mix into the other.\n"
        "Step 3: See status log for details."
    ),
    "rec_start_stop": (
        "Start / Stop Recording\n\n"
        "Step 1: Start Recording — begins capturing the mixed output (path in status).\n"
        "Step 2: Stop Recording — finalizes the WAV file.\n"
        "Step 3: Use Open Recordings Folder to find files."
    ),
    "rec_quick_open": (
        "Quick Record / Open Recordings Folder\n\n"
        "Quick Record — shortcut to start a timed or one-shot capture (see implementation).\n"
        "Open Recordings Folder — opens the recordings directory in your file manager."
    ),
    "crossfader": (
        "Crossfader\n\n"
        "Step 1: Engine running.\n"
        "Step 2: Drag vertically: bottom = Deck A loud, top = Deck B loud (typical DJ layout).\n"
        "Step 3: Middle = blend between A and B."
    ),
    "master_gain": (
        "Master Gain\n\n"
        "Step 1: Engine running.\n"
        "Step 2: Scales the final mixed output level (0–2×).\n"
        "Step 3: 1.0 is unity; reduce if clipping."
    ),
    "engine_status": (
        "Engine status log\n\n"
        "Step 1: Read messages here after each action (load, sync, mix, errors).\n"
        "Step 2: If something fails, scroll up for the last error line.\n"
        "Step 3: This area is read-only; it mirrors major GUI operations."
    ),
}


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

        self._btn(
            playlist_btns, "Load Folder", lambda: self.app.load_folder_to_deck(self.deck_name), ACCENT_C, TT["load_folder"]
        ).pack(side="left", padx=4)
        self._btn(
            playlist_btns, "Prev", lambda: self.app.playlist_prev(self.deck_name), ACCENT_D, TT["playlist_prev_next"]
        ).pack(side="left", padx=4)
        self._btn(
            playlist_btns, "Next", lambda: self.app.playlist_next(self.deck_name), ACCENT_D, TT["playlist_prev_next"]
        ).pack(side="left", padx=4)
        self._btn(
            playlist_btns, "Reload Deck", lambda: self.app.reload_single_deck_from_path(self.deck_name), ACCENT_E, TT["reload_deck"]
        ).pack(side="left", padx=4)
        self._btn(
            playlist_btns, "Open Mixer", lambda: self.app.open_mixer_popup(), ACCENT_B, TT["open_mixer"]
        ).pack(side="left", padx=4)

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
        attach_tooltip(self.playlist_listbox, TT["playlist_list"])

        self.playlist_listbox.bind("<Double-Button-1>", lambda e: self.app.playlist_double_click(self.deck_name))
        self.playlist_listbox.bind("<Return>", lambda e: self.app.playlist_double_click(self.deck_name))

        self.waveform = WaveformView(self, app, deck_name, width=560, height=140, accent=accent)
        self.waveform.pack(fill="x", pady=(4, 10))
        attach_tooltip(self.waveform, TT["waveform"])

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

        self._btn(row1, "Load File", lambda: self.app.load_deck(self.deck_name), accent, TT["load_file"]).pack(
            side="left", padx=4
        )
        self._btn(row1, "Play", lambda: self.app.deck_play(self.deck_name), ACCENT_C, TT["play_stop"]).pack(
            side="left", padx=4
        )
        self._btn(row1, "Stop", lambda: self.app.deck_stop(self.deck_name), DANGER, TT["play_stop"]).pack(
            side="left", padx=4
        )
        self._btn(
            row1, "Align to Other", lambda: self.app.align_to_other(self.deck_name), ACCENT_D, TT["align_other"]
        ).pack(side="left", padx=4)
        self._btn(row1, "Drop Sync", lambda: self.app.drop_sync(self.deck_name), ACCENT_E, TT["drop_sync"]).pack(
            side="left", padx=4
        )

        row2 = tk.Frame(self, bg=CARD_BG)
        row2.pack(fill="x", pady=8)

        self._btn(
            row2, "Sync to Other", lambda: self.app.sync_to_other(self.deck_name), ACCENT_B, TT["sync_unsync"]
        ).pack(side="left", padx=4)
        self._btn(row2, "Unsync", lambda: self.app.unsync(self.deck_name), DANGER, TT["sync_unsync"]).pack(
            side="left", padx=4
        )
        self._btn(row2, "Mute On/Off", lambda: self.app.toggle_mute(self.deck_name), ACCENT_D, TT["mute_quantize"]).pack(
            side="left", padx=4
        )
        self._btn(
            row2, "Quantize On/Off", lambda: self.app.toggle_quantize(self.deck_name), ACCENT_E, TT["mute_quantize"]
        ).pack(side="left", padx=4)

        gain_wrap = tk.Frame(self, bg=CARD_BG)
        gain_wrap.pack(fill="x", pady=(8, 6))

        tk.Label(gain_wrap, text="Gain", bg=CARD_BG, fg=TEXT, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        _gain_sc = tk.Scale(
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
        )
        _gain_sc.pack(anchor="w")
        attach_tooltip(_gain_sc, TT["gain"])

        seek_wrap = tk.Frame(self, bg=CARD_BG)
        seek_wrap.pack(fill="x", pady=(6, 8))

        tk.Label(seek_wrap, text="Seek (seconds)", bg=CARD_BG, fg=TEXT, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        seek_row = tk.Frame(seek_wrap, bg=CARD_BG)
        seek_row.pack(anchor="w", fill="x")
        _seek_ent = tk.Entry(seek_row, textvariable=self.seek_var, width=10)
        _seek_ent.pack(side="left", padx=(0, 8))
        attach_tooltip(_seek_ent, TT["seek"])
        self._btn(seek_row, "Go", lambda: self.app.seek_deck(self.deck_name, self.seek_var.get()), accent, TT["seek"]).pack(
            side="left"
        )

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
        _loop_cb = ttk.Combobox(loop_row, textvariable=self.loop_beats_var, values=["1", "2", "4", "8", "16", "32"], width=6)
        _loop_cb.pack(side="left")
        attach_tooltip(_loop_cb, TT["loop_beats_combo"])
        self._btn(
            loop_row,
            "Loop On",
            lambda: self.app.loop_beats(self.deck_name, int(self.loop_beats_var.get())),
            ACCENT_C,
            TT["loop_on"],
        ).pack(side="left", padx=6)
        self._btn(loop_row, "Loop Off", lambda: self.app.loop_off(self.deck_name), DANGER, TT["loop_off"]).pack(
            side="left", padx=6
        )

        roll_row = tk.Frame(loop_box, bg=CARD_BG)
        roll_row.pack(fill="x", pady=6)
        tk.Label(roll_row, text="Roll beats", bg=CARD_BG, fg=TEXT).pack(side="left", padx=6)
        _roll_cb = ttk.Combobox(roll_row, textvariable=self.roll_beats_var, values=["1", "2", "4", "8", "16"], width=6)
        _roll_cb.pack(side="left")
        attach_tooltip(_roll_cb, TT["roll_beats_combo"])
        self._btn(
            roll_row,
            "Roll On",
            lambda: self.app.roll_beats(self.deck_name, int(self.roll_beats_var.get())),
            ACCENT_D,
            TT["roll_on"],
        ).pack(side="left", padx=6)
        self._btn(roll_row, "Roll Off", lambda: self.app.roll_off(self.deck_name), DANGER, TT["roll_off"]).pack(
            side="left", padx=6
        )

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
                self._btn(
                    cue_row,
                    f"Set {idx}",
                    lambda n=idx: self.app.cue_set(self.deck_name, n),
                    self.accent,
                    TT["hotcue_set"],
                ).pack(side="left", padx=4)
                self._btn(
                    cue_row,
                    f"Go {idx}",
                    lambda n=idx: self.app.cue_jump(self.deck_name, n),
                    ACCENT_B,
                    TT["hotcue_go"],
                ).pack(side="left", padx=4)

    def _btn(self, parent, text, command, color, tooltip=None):
        b = tk.Button(
            parent,
            text=text,
            command=command,
            bg=color,
            fg="black",
            activebackground=color,
            activeforeground="black",
            relief="flat",
            bd=0,
            padx=8,
            pady=5,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )
        if tooltip:
            attach_tooltip(b, tooltip)
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
        self.crossfader_var = tk.DoubleVar(value=0.5)
        self.master_gain_var = tk.DoubleVar(value=1.0)
        self.record_status_var = tk.StringVar(value="Not recording")
        self.mixer_popup = None
        self._a_to_b_mix_triggered = False
        self._b_to_a_mix_triggered = False

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

        top = tk.Frame(main, bg=APP_BG)
        top.pack(fill="both", expand=True)

        self.deck_a_wrap = ScrollableDeckFrame(top, bg_color=CARD_BG)
        self.deck_a_wrap.pack(side="left", fill="both", expand=True, padx=(0, 8))

        self.deck_a_panel = DeckPanel(self.deck_a_wrap.inner, self, "A", ACCENT_A)
        self.deck_a_panel.pack(fill="both", expand=True)

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
        attach_tooltip(self.status_box, TT["engine_status"])

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

        self._btn(load_row, "Load A", lambda: self.load_deck("A"), ACCENT_A, TT["mixer_load_ab"]).pack(
            fill="x", pady=4
        )
        self._btn(load_row, "Load B", lambda: self.load_deck("B"), ACCENT_B, TT["mixer_load_ab"]).pack(
            fill="x", pady=4
        )
        self._btn(load_row, "Start Engine", self.init_engine, ACCENT_C, TT["start_engine"]).pack(fill="x", pady=4)
        self._btn(load_row, "Stop Engine", self.stop_engine, DANGER, TT["stop_engine"]).pack(fill="x", pady=4)

        # Auto DJ / Smart Actions — placed high so always visible
        ai_box = tk.LabelFrame(
            parent,
            text="Auto DJ / Smart Actions",
            bg=PANEL_BG,
            fg=TEXT,
            font=("Segoe UI", 10, "bold"),
            bd=1,
            relief="solid",
        )
        ai_box.pack(fill="x", padx=12, pady=12)

        # Compact 3-row layout so all 6 actions are always visible
        row1 = tk.Frame(ai_box, bg=PANEL_BG)
        row1.pack(fill="x", padx=8, pady=3)
        self._btn(row1, "Auto DJ ON", self.auto_on, ACCENT_C, TT["auto_dj_on_off"]).pack(
            side="left", expand=True, fill="x", padx=(0, 4)
        )
        self._btn(row1, "Auto DJ OFF", self.auto_off, DANGER, TT["auto_dj_on_off"]).pack(
            side="left", expand=True, fill="x", padx=(4, 0)
        )

        row2 = tk.Frame(ai_box, bg=PANEL_BG)
        row2.pack(fill="x", padx=8, pady=3)
        self._btn(row2, "Drop B into A", lambda: self.manual_drop("B", "A"), ACCENT_E, TT["drop_ab"]).pack(
            side="left", expand=True, fill="x", padx=(0, 4)
        )
        self._btn(row2, "Drop A into B", lambda: self.manual_drop("A", "B"), ACCENT_E, TT["drop_ab"]).pack(
            side="left", expand=True, fill="x", padx=(4, 0)
        )

        row3 = tk.Frame(ai_box, bg=PANEL_BG)
        row3.pack(fill="x", padx=8, pady=3)
        self._btn(row3, "Align B to A", lambda: self.align_specific("B", "A"), ACCENT_D, TT["align_ab"]).pack(
            side="left", expand=True, fill="x", padx=(0, 4)
        )
        self._btn(row3, "Align A to B", lambda: self.align_specific("A", "B"), ACCENT_D, TT["align_ab"]).pack(
            side="left", expand=True, fill="x", padx=(4, 0)
        )

        row4 = tk.Frame(ai_box, bg=PANEL_BG)
        row4.pack(fill="x", padx=8, pady=3)
        self._btn(row4, "Mix Next A→B", self.auto_play_and_mix_next_a_to_b, ACCENT_C, TT["mix_next"]).pack(
            side="left", expand=True, fill="x", padx=(0, 4)
        )
        self._btn(row4, "Mix Next B→A", self.auto_play_and_mix_next_b_to_a, ACCENT_C, TT["mix_next"]).pack(
            side="left", expand=True, fill="x", padx=(4, 0)
        )

        # Record mix to WAV
        rec_box = tk.LabelFrame(
            parent,
            text="Record Mix",
            bg=PANEL_BG,
            fg=TEXT,
            font=("Segoe UI", 10, "bold"),
            bd=1,
            relief="solid",
        )
        rec_box.pack(fill="x", padx=12, pady=12)
        rec_row = tk.Frame(rec_box, bg=PANEL_BG)
        rec_row.pack(fill="x", padx=8, pady=6)
        self._btn(rec_row, "Start Recording", self.start_recording_mix, DANGER, TT["rec_start_stop"]).pack(
            side="left", padx=(0, 4)
        )
        self._btn(rec_row, "Stop Recording", self.stop_recording_mix, ACCENT_C, TT["rec_start_stop"]).pack(
            side="left", padx=(4, 0)
        )
        rec_row2 = tk.Frame(rec_box, bg=PANEL_BG)
        rec_row2.pack(fill="x", padx=8, pady=4)
        self._btn(rec_row2, "Quick Record", self.quick_record_mix, ACCENT_E, TT["rec_quick_open"]).pack(
            side="left", padx=(0, 4)
        )
        self._btn(rec_row2, "Open Recordings Folder", self.open_recordings_folder, MUTED, TT["rec_quick_open"]).pack(
            side="left", padx=(4, 0)
        )
        tk.Label(
            rec_box,
            textvariable=self.record_status_var,
            bg=PANEL_BG,
            fg=MUTED,
            font=("Segoe UI", 9),
            wraplength=280,
        ).pack(anchor="w", padx=8, pady=(0, 6))

        tk.Label(parent, text="Crossfader", bg=PANEL_BG, fg=TEXT, font=("Segoe UI", 11, "bold")).pack(pady=(18, 4))
        _xf = tk.Scale(
            parent,
            from_=0.0,
            to=1.0,
            resolution=0.01,
            orient="vertical",
            variable=self.crossfader_var,
            command=lambda _=None: self.set_crossfader(),
            bg=PANEL_BG,
            fg=TEXT,
            highlightthickness=0,
            troughcolor="#243054",
            activebackground=ACCENT_D,
            length=240,
        )
        _xf.pack(pady=4)
        attach_tooltip(_xf, TT["crossfader"])

        tk.Label(parent, text="A ←    → B", bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 10)).pack()

        tk.Label(parent, text="Master Gain", bg=PANEL_BG, fg=TEXT, font=("Segoe UI", 11, "bold")).pack(pady=(18, 4))
        _mg = tk.Scale(
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
        )
        _mg.pack(pady=4)
        attach_tooltip(_mg, TT["master_gain"])

    def open_mixer_popup(self):
        # Reuse the same mixer UI in a separate popup window.
        if self.mixer_popup is not None and self.mixer_popup.winfo_exists():
            self.mixer_popup.lift()
            self.mixer_popup.focus_set()
            return

        popup = tk.Toplevel(self.root)
        popup.title("Mixer / Auto DJ")
        popup.configure(bg=PANEL_BG)
        popup.geometry("340x640")

        canvas = tk.Canvas(popup, bg=PANEL_BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(popup, orient="vertical", command=canvas.yview)
        container = tk.Frame(canvas, bg=PANEL_BG)

        container.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win_id = canvas.create_window((0, 0), window=container, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_canvas_configure(e):
            canvas.itemconfig(win_id, width=e.width)

        canvas.bind("<Configure>", _on_canvas_configure)
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
        canvas.bind("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
        canvas.bind("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

        # Build mixer UI into the popup; it shares the same DoubleVars and engine
        self._build_center_panel(container)

        def _on_close():
            self.mixer_popup = None
            popup.destroy()

        popup.protocol("WM_DELETE_WINDOW", _on_close)
        self.mixer_popup = popup

    def _btn(self, parent, text, command, color, tooltip=None):
        b = tk.Button(
            parent,
            text=text,
            command=command,
            bg=color,
            fg="black",
            activebackground=color,
            activeforeground="black",
            relief="flat",
            bd=0,
            padx=10,
            pady=8,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )
        if tooltip:
            attach_tooltip(b, tooltip)
        return b

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
            new_deck = make_deck(deck_name, path)

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

        next_idx = idx + 1
        self.deck_playlist_index[deck_name] = next_idx
        self.deck_paths[deck_name] = files[next_idx]
        self.deck_finished_flags[deck_name] = False

        self.append_status(f"Deck {deck_name} auto-loading next playlist track: {os.path.basename(files[next_idx])}")
        self.reload_single_deck_from_path(deck_name)

    def auto_play_and_mix_next_a_to_b(self):
        """Load next track from Deck A's playlist onto Deck B and mix (Drop B into A)."""
        if not self.require_engine():
            return
        pl_a = self.deck_playlists.get("A", [])
        idx_a = self.deck_playlist_index.get("A", -1)
        if not pl_a or idx_a < 0 or idx_a >= len(pl_a) - 1:
            self.append_status("No next track in Deck A playlist for A→B mix.")
            self._a_to_b_mix_triggered = False
            return
        next_path = pl_a[idx_a + 1]
        self.deck_paths["B"] = next_path
        self.deck_playlist_index["B"] = idx_a + 1
        self.deck_finished_flags["B"] = False
        try:
            self.reload_single_deck_from_path("B")
            self.engine.drop_sync_transition("B", "A")
            self.deck_playlist_index["A"] = idx_a + 1
            self.deck_paths["A"] = next_path
            self.reload_single_deck_from_path("A")
            self.refresh_playlist_ui("A")
            self.refresh_playlist_ui("B")
            self.append_status(f"Auto mix: next from A → B ({os.path.basename(next_path)})")
        except Exception as e:
            self.append_status(f"A→B mix error: {e}")

    def auto_play_and_mix_next_b_to_a(self):
        """Load next track from Deck B's playlist onto Deck A and mix (Drop A into B)."""
        if not self.require_engine():
            return
        pl_b = self.deck_playlists.get("B", [])
        idx_b = self.deck_playlist_index.get("B", -1)
        if not pl_b or idx_b < 0 or idx_b >= len(pl_b) - 1:
            self.append_status("No next track in Deck B playlist for B→A mix.")
            self._b_to_a_mix_triggered = False
            return
        next_path = pl_b[idx_b + 1]
        self.deck_paths["A"] = next_path
        self.deck_playlist_index["A"] = idx_b + 1
        self.deck_finished_flags["A"] = False
        try:
            self.reload_single_deck_from_path("A")
            self.engine.drop_sync_transition("A", "B")
            self.deck_playlist_index["B"] = idx_b + 1
            self.deck_paths["B"] = next_path
            self.reload_single_deck_from_path("B")
            self.refresh_playlist_ui("A")
            self.refresh_playlist_ui("B")
            self.append_status(f"Auto mix: next from B → A ({os.path.basename(next_path)})")
        except Exception as e:
            self.append_status(f"B→A mix error: {e}")

    def init_engine(self):
        if not self.deck_paths["A"] or not self.deck_paths["B"]:
            messagebox.showwarning("Missing decks", "Load both Deck A and Deck B first.")
            return

        try:
            self.append_status("Loading Deck A...")
            deck_a = make_deck("A", self.deck_paths["A"])
            self.append_status(f"Deck A loaded: bpm={deck_a.bpm:.2f}, duration={deck_a.duration_sec:.2f}s")

            self.append_status("Loading Deck B...")
            deck_b = make_deck("B", self.deck_paths["B"])
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

    def start_recording_mix(self):
        if not self.require_engine():
            return
        if self.engine.is_recording():
            self.append_status("Already recording.")
            return
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        default_name = f"mix_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
        path = filedialog.asksaveasfilename(
            title="Save mix recording",
            initialdir=str(RECORDINGS_DIR),
            initialfile=default_name,
            defaultextension=".wav",
            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            self.engine.start_recording(path)
            self.record_status_var.set(f"Recording to: {os.path.basename(path)}")
            self.append_status(f"Recording mix to {path}")
        except Exception as e:
            messagebox.showerror("Record error", str(e))
            self.append_status(f"Record start error: {e}")

    def quick_record_mix(self):
        """Start recording immediately to the default recordings folder (no dialog)."""
        if not self.require_engine():
            return
        if self.engine.is_recording():
            self.append_status("Already recording.")
            return
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        default_name = f"mix_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
        path = str(RECORDINGS_DIR / default_name)
        try:
            self.engine.start_recording(path)
            self.record_status_var.set(f"Recording to: {os.path.basename(path)}")
            self.append_status(f"Recording mix to {path}")
        except Exception as e:
            messagebox.showerror("Record error", str(e))
            self.append_status(f"Record start error: {e}")

    def open_recordings_folder(self):
        """Open the folder where recordings are saved in the system file manager."""
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        path = str(RECORDINGS_DIR.resolve())
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.run(["open", path], check=False)
            else:
                subprocess.run(["xdg-open", path], check=False)
            self.append_status(f"Opened recordings folder: {path}")
        except Exception as e:
            messagebox.showerror("Open folder", f"Could not open folder: {e}")
            self.append_status(f"Open folder error: {e}")

    def stop_recording_mix(self):
        if not self.require_engine():
            return
        if not self.engine.is_recording():
            self.record_status_var.set("Not recording")
            return
        try:
            path = self.engine.stop_recording()
            self.record_status_var.set("Not recording")
            if path:
                self.append_status(f"Mix saved: {path}")
        except Exception as e:
            messagebox.showerror("Record error", str(e))
            self.append_status(f"Record stop error: {e}")

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
                    if self.engine.auto_dj_enabled:
                        with self.engine.lock:
                            deck_a = self.engine.deck_a
                            deck_b = self.engine.deck_b
                            a_playing = deck_a.playing
                            b_playing = deck_b.playing
                            a_time_left = max(0.0, (len(deck_a.audio) - deck_a.playhead) / deck_a.sr) if len(deck_a.audio) > 0 else 0.0
                            b_time_left = max(0.0, (len(deck_b.audio) - deck_b.playhead) / deck_b.sr) if len(deck_b.audio) > 0 else 0.0
                        if a_playing and a_time_left < 25.0 and not self._a_to_b_mix_triggered:
                            self._a_to_b_mix_triggered = True
                            self._b_to_a_mix_triggered = False
                            self.auto_play_and_mix_next_a_to_b()
                        elif a_time_left >= 30.0 or not a_playing:
                            self._a_to_b_mix_triggered = False
                        if b_playing and b_time_left < 25.0 and not self._b_to_a_mix_triggered:
                            self._b_to_a_mix_triggered = True
                            self._a_to_b_mix_triggered = False
                            self.auto_play_and_mix_next_b_to_a()
                        elif b_time_left >= 30.0 or not b_playing:
                            self._b_to_a_mix_triggered = False
                        for deck_name in ("A", "B"):
                            with self.engine.lock:
                                deck = self.engine._deck(deck_name)
                                finished = (not deck.playing) and (deck.playhead >= len(deck.audio) - 1)

                            if finished and not self.deck_finished_flags[deck_name]:
                                self.deck_finished_flags[deck_name] = True
                                self.append_status(f"Deck {deck_name} finished track (Auto DJ: loading next)")
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
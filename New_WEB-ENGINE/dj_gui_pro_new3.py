import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from dj_engine_pro import DJEnginePro, make_deck


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


class WaveformView(tk.Canvas):
    def __init__(self, master, app, deck_name: str, width=520, height=150, accent="#00d4ff"):
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

    def sample_to_x(self, sample: int) -> float:
        if self.deck is None or self.deck.audio is None or len(self.deck.audio) == 0:
            return 0.0
        w = max(1, self.winfo_width())
        return (float(sample) / max(1, len(self.deck.audio))) * w

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

        # small movement = seek
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

        self.waveform = WaveformView(self, app, deck_name, width=560, height=170, accent=accent)
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

        self._btn(row1, "Load", lambda: self.app.load_deck(self.deck_name), accent).pack(side="left", padx=4)
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
        tk.Entry(seek_row, textvariable=self.seek_var, width=10).pack(side="left", padx=(0, 8))
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
        return tk.Button(
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
            pady=6,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )


class DJGuiApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("DJ Engine Pro GUI")
        self.root.configure(bg=APP_BG)
        self.root.geometry("1600x950")
        self.root.minsize(1320, 780)

        self.engine = None
        self.deck_paths = {"A": None, "B": None}
        self.crossfader_var = tk.DoubleVar(value=0.0)
        self.master_gain_var = tk.DoubleVar(value=1.0)

        self._style()
        self._build_ui()
        self._start_status_updater()
        self._start_waveform_updater()

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
            text="DJ ENGINE PRO",
            bg=APP_BG,
            fg=TEXT,
            font=("Segoe UI", 24, "bold"),
        ).pack(side="left")

        tk.Label(
            header,
            text="Waveform • playhead • beats • cues • loops • rolls • sync • drop transitions • auto DJ",
            bg=APP_BG,
            fg=MUTED,
            font=("Segoe UI", 11),
        ).pack(side="left", padx=16, pady=8)

        top = tk.Frame(main, bg=APP_BG)
        top.pack(fill="both", expand=True)

        self.deck_a_panel = DeckPanel(top, self, "A", ACCENT_A)
        self.deck_a_panel.pack(side="left", fill="both", expand=True, padx=(0, 8))

        center = tk.Frame(top, bg=PANEL_BG, bd=0, highlightthickness=0)
        center.pack(side="left", fill="y", padx=8)

        self._build_center_panel(center)

        self.deck_b_panel = DeckPanel(top, self, "B", ACCENT_B)
        self.deck_b_panel.pack(side="left", fill="both", expand=True, padx=(8, 0))

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

        tk.Label(parent, text="Crossfader", bg=PANEL_BG, fg=TEXT, font=("Segoe UI", 11, "bold")).pack(pady=(18, 4))
        tk.Scale(
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
            length=260,
        ).pack(pady=4)

        tk.Label(parent, text="A ←    → B", bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 10)).pack()

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
            length=180,
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
        return tk.Button(
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
        panel = self.deck_a_panel if deck_name == "A" else self.deck_b_panel
        panel.path_var.set(path)
        panel.status_var.set("Loaded path. Press Start Engine to analyze and initialize deck.")

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
        self._deck_panel(deck_name).waveform.redraw()
        self.append_status(f"Deck {deck_name} seek -> {seconds}s")

    def waveform_seek(self, deck_name, sample):
        if not self.require_engine():
            return
        with self.engine.lock:
            deck = self.engine._deck(deck_name)
            deck.set_playhead(int(sample), quantize=False)
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
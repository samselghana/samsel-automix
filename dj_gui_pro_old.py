import threading
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
            wraplength=420,
            justify="left",
        ).pack(anchor="w")

        tk.Label(
            self,
            textvariable=self.status_var,
            bg=CARD_BG,
            fg=MUTED,
            font=("Segoe UI", 10),
            wraplength=420,
            justify="left",
        ).pack(anchor="w", pady=(4, 10))

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
            length=360,
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
        self.root.geometry("1500x900")
        self.root.minsize(1280, 760)

        self.engine = None
        self.deck_paths = {"A": None, "B": None}
        self.auto_dj_var = tk.BooleanVar(value=False)
        self.crossfader_var = tk.DoubleVar(value=0.0)
        self.master_gain_var = tk.DoubleVar(value=1.0)
        self.status_text_var = tk.StringVar(value="Load both decks to begin.")

        self._style()
        self._build_ui()
        self._start_status_updater()

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
            text="Vivid mixing console • sync • hot cues • loops • drop transitions • auto DJ",
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
        self.status_box.insert("1.0", self.status_text_var.get())
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

        tk.Label(
            parent,
            text="Crossfader",
            bg=PANEL_BG,
            fg=TEXT,
            font=("Segoe UI", 11, "bold"),
        ).pack(pady=(18, 4))

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

        tk.Label(
            parent,
            text="Master Gain",
            bg=PANEL_BG,
            fg=TEXT,
            font=("Segoe UI", 11, "bold"),
        ).pack(pady=(18, 4))

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

    def require_engine(self):
        if not self.engine:
            messagebox.showwarning("Engine not ready", "Start the engine first.")
            return False
        return True

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
        self.append_status(f"Deck {deck_name} seek -> {seconds}s")

    def cue_set(self, deck_name, idx):
        if not self.require_engine():
            return
        with self.engine.lock:
            self.engine._deck(deck_name).set_hot_cue(idx)
        self.append_status(f"Deck {deck_name} set cue {idx}")

    def cue_jump(self, deck_name, idx):
        if not self.require_engine():
            return
        with self.engine.lock:
            ok = self.engine._deck(deck_name).jump_hot_cue(idx)
        if ok:
            self.append_status(f"Deck {deck_name} jump cue {idx}")
        else:
            self.append_status(f"Deck {deck_name} cue {idx} not set")

    def loop_beats(self, deck_name, beats):
        if not self.require_engine():
            return
        with self.engine.lock:
            self.engine._deck(deck_name).enable_loop_beats(beats)
        self.append_status(f"Deck {deck_name} loop {beats} beats")

    def loop_off(self, deck_name):
        if not self.require_engine():
            return
        with self.engine.lock:
            self.engine._deck(deck_name).disable_loop()
        self.append_status(f"Deck {deck_name} loop off")

    def roll_beats(self, deck_name, beats):
        if not self.require_engine():
            return
        with self.engine.lock:
            self.engine._deck(deck_name).enable_roll_beats(beats)
        self.append_status(f"Deck {deck_name} roll {beats} beats")

    def roll_off(self, deck_name):
        if not self.require_engine():
            return
        with self.engine.lock:
            self.engine._deck(deck_name).disable_roll()
        self.append_status(f"Deck {deck_name} roll off")

    def sync_to_other(self, deck_name):
        if not self.require_engine():
            return
        other = "B" if deck_name == "A" else "A"
        try:
            self.engine.sync(deck_name, other)
            self.append_status(f"Deck {deck_name} synced to Deck {other}")
        except Exception as e:
            self.append_status(f"Sync error: {e}")

    def unsync(self, deck_name):
        if not self.require_engine():
            return
        try:
            self.engine.unsync(deck_name)
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
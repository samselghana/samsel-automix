"""SAMSEL AutoMix Downloader v2 — desktop Tk UI. Logic: automix_core.

Standalone web (same features, browser GUI):  py AutoMix_DownLoader_v2.py --web
  Optional: --port 8766   --host 0.0.0.0
"""
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

_script = Path(__file__).resolve()
if _script.parent.name == "automix" and _script.parent.parent.name == "tools":
    _web_root = _script.parent.parent.parent
else:
    _web_root = _script.parent
_sr = str(_web_root)
if _sr not in sys.path:
    sys.path.insert(0, _sr)

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image, ImageTk
except Exception:
    Image = None
    ImageTk = None

from automix_core import (
    APP_NAME,
    DEFAULT_OUTPUT,
    AppConfig,
    DownloaderEngine,
    Job,
    ensure_dir,
    ts,
)

SCRIPT_DIR = Path(__file__).resolve().parent
LOGO_ICO = SCRIPT_DIR / "logo.ico"
LOGO_PNG = SCRIPT_DIR / "logo.png"

UI_BG = "#e4eaf3"
UI_PANEL = "#d8e3f2"
UI_ENTRY_BG = "#ffffff"
UI_ENTRY_BD = "#5c7eb8"
UI_BTN_PRIMARY = "#2b6cb0"
UI_BTN_PRIMARY_A = "#1e5490"
UI_BTN_ACCENT = "#2f855a"
UI_BTN_ACCENT_A = "#276749"
UI_BTN_MUTED = "#5a6578"
UI_BTN_MUTED_A = "#4a5568"
UI_TEXT = "#1a2634"
UI_SUBTEXT = "#4a5568"


class AutoMixDownloaderUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1024x660")
        self.root.minsize(720, 480)
        self.root.configure(bg=UI_BG)
        self._logo_spin_after: Optional[str] = None
        self._tk_logo_ref = None
        self._logo_pil_base = None
        self._logo_angle = 0

        self._apply_window_icon()
        self._apply_ttk_styles()

        self.config = AppConfig.load()
        ensure_dir(self.config.output_dir)

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self._build_vars()
        self.engine = DownloaderEngine(
            config=self.config,
            logger=self.log,
            progress_callback=self.on_progress,
            table_update_callback=self.refresh_jobs_table,
        )
        self._build_ui()
        self.engine.start()
        self._load_config_into_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._drain_log_queue)
        self.log(
            "[START] Ready — type a track or paste a URL/CSV path, then click **Add to Queue** "
            "(downloads only run after you queue a job)."
        )
        self.root.after(400, self._probe_ytdlp_status)

    def _build_vars(self) -> None:
        self.var_source = tk.StringVar()
        self.var_output_dir = tk.StringVar(value=self.config.output_dir)
        self.var_audio_format = tk.StringVar(value=self.config.audio_format)
        self.var_audio_quality = tk.StringVar(value=self.config.audio_quality)
        self.var_embed_thumbnail = tk.BooleanVar(value=self.config.embed_thumbnail)
        self.var_add_metadata = tk.BooleanVar(value=self.config.add_metadata)
        self.var_fetch_lyrics = tk.BooleanVar(value=self.config.fetch_lyrics)
        self.var_embed_lyrics_in_mp3 = tk.BooleanVar(value=self.config.embed_lyrics_in_mp3)
        self.var_uslt_embed_full_lrc = tk.BooleanVar(value=self.config.uslt_embed_full_lrc)
        self.var_detect_bpm = tk.BooleanVar(value=self.config.detect_bpm)
        self.var_detect_genre = tk.BooleanVar(value=self.config.detect_genre)
        self.var_auto_import_library = tk.BooleanVar(value=self.config.auto_import_library)
        self.var_playlist_subfolders = tk.BooleanVar(value=self.config.playlist_subfolders)
        self.var_overwrite_files = tk.BooleanVar(value=self.config.overwrite_files)
        self.var_ffmpeg_path = tk.StringVar(value=self.config.ffmpeg_path)
        self.var_status = tk.StringVar(value="Ready")
        self.var_eta = tk.StringVar(value="ETA: --")
        self.var_source_type = tk.StringVar(value="single")

    def _apply_window_icon(self) -> None:
        if LOGO_ICO.is_file():
            try:
                self.root.iconbitmap(str(LOGO_ICO))
            except tk.TclError:
                pass
        elif LOGO_PNG.is_file():
            try:
                self._wm_icon = tk.PhotoImage(file=str(LOGO_PNG))
                self.root.iconphoto(True, self._wm_icon)
            except tk.TclError:
                pass

    def _apply_ttk_styles(self) -> None:
        try:
            st = ttk.Style()
            if "vista" in st.theme_names():
                st.theme_use("vista")
            st.configure("TNotebook", background=UI_BG)
            st.configure("TFrame", background=UI_BG)
            st.configure("TPanedwindow", background=UI_BG)
            st.configure(
                "Treeview",
                background=UI_ENTRY_BG,
                foreground=UI_TEXT,
                fieldbackground=UI_ENTRY_BG,
                rowheight=22,
            )
            st.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
            st.configure("TProgressbar", thickness=10)
        except Exception:
            pass

    def _tk_entry(self, parent: tk.Widget, **kw) -> tk.Entry:
        return tk.Entry(
            parent,
            relief=tk.SUNKEN,
            bd=2,
            bg=UI_ENTRY_BG,
            fg=UI_TEXT,
            insertbackground=UI_TEXT,
            highlightthickness=1,
            highlightbackground=UI_ENTRY_BD,
            highlightcolor=UI_BTN_PRIMARY,
            font=("Segoe UI", 9),
            **kw,
        )

    def _tk_button(
        self,
        parent: tk.Widget,
        text: str,
        command,
        style: str = "primary",
        **kw,
    ) -> tk.Button:
        if style == "accent":
            bg, abg = UI_BTN_ACCENT, UI_BTN_ACCENT_A
        elif style == "muted":
            bg, abg = UI_BTN_MUTED, UI_BTN_MUTED_A
        else:
            bg, abg = UI_BTN_PRIMARY, UI_BTN_PRIMARY_A
        return tk.Button(
            parent,
            text=text,
            command=command,
            relief=tk.RAISED,
            bd=3,
            bg=bg,
            fg="white",
            activebackground=abg,
            activeforeground="white",
            font=("Segoe UI", 9, "bold"),
            padx=8,
            pady=2,
            cursor="hand2",
            **kw,
        )

    def _build_header(self, parent: tk.Widget) -> None:
        row = tk.Frame(parent, bg=UI_BG)
        row.pack(fill="x")

        logo_box = tk.Frame(row, bg=UI_BG)
        logo_box.pack(side=tk.LEFT, padx=(0, 10))
        self._logo_label = tk.Label(logo_box, bg=UI_BG)
        self._logo_label.pack()
        self._init_logo_widget()

        titles = tk.Frame(row, bg=UI_BG)
        titles.pack(side=tk.LEFT, fill="x", expand=True)
        tk.Label(
            titles,
            text=APP_NAME,
            font=("Segoe UI", 15, "bold"),
            bg=UI_BG,
            fg=UI_TEXT,
        ).pack(anchor="w")
        tk.Label(
            titles,
            text="Playlist + lyrics + BPM + genre + queue-based downloader",
            font=("Segoe UI", 9),
            bg=UI_BG,
            fg=UI_SUBTEXT,
        ).pack(anchor="w", pady=(1, 0))

    def _init_logo_widget(self) -> None:
        if not LOGO_PNG.is_file():
            self._logo_label.configure(text="♪", font=("Segoe UI", 26), fg=UI_BTN_PRIMARY)
            return
        if Image is not None and ImageTk is not None:
            try:
                im = Image.open(LOGO_PNG).convert("RGBA")
                try:
                    res = Image.Resampling.LANCZOS  # type: ignore[attr-defined]
                except AttributeError:
                    res = Image.LANCZOS  # type: ignore[attr-defined]
                im = im.resize((48, 48), res)
                self._logo_pil_base = im
                self._spin_logo_tick()
                return
            except Exception:
                pass
        self._load_static_logo_png()

    def _pil_resample(self):
        R = getattr(Image, "Resampling", None)
        if R is not None:
            return R.BICUBIC
        return Image.BICUBIC  # type: ignore

    def _spin_logo_tick(self) -> None:
        if self._logo_pil_base is None or ImageTk is None:
            return
        try:
            self._logo_angle = (self._logo_angle + 6) % 360
            rot = self._logo_pil_base.rotate(
                -self._logo_angle,
                resample=self._pil_resample(),
                expand=False,
            )
            self._tk_logo_ref = ImageTk.PhotoImage(rot)
            self._logo_label.configure(image=self._tk_logo_ref)
        except Exception:
            pass
        self._logo_spin_after = self.root.after(75, self._spin_logo_tick)

    def _load_static_logo_png(self) -> None:
        if not LOGO_PNG.is_file():
            return
        try:
            self._tk_logo_ref = tk.PhotoImage(file=str(LOGO_PNG))
            self._logo_label.configure(image=self._tk_logo_ref)
        except tk.TclError:
            self._logo_label.configure(text="♪", font=("Segoe UI", 26), fg=UI_BTN_PRIMARY)

    def _scroll_wrap(
        self, parent: tk.Widget
    ) -> Tuple[tk.Canvas, ttk.Scrollbar, tk.Frame]:
        """Vertical scroll area: returns (canvas, scrollbar, inner_frame)."""
        canvas = tk.Canvas(parent, bg=UI_BG, highlightthickness=0)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        inner = tk.Frame(canvas, bg=UI_BG)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _inner_cfg(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _canvas_cfg(event):
            canvas.itemconfigure(win, width=event.width)

        inner.bind("<Configure>", _inner_cfg)
        canvas.bind("<Configure>", _canvas_cfg)

        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        return canvas, vsb, inner

    def _bind_mousewheel_recursive(self, canvas: tk.Canvas, root: tk.Widget) -> None:
        """Scroll the left canvas; skip Treeview (it has its own scrollbar + handler)."""

        def _wheel(event) -> None:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _bind(w: tk.Widget) -> None:
            if isinstance(w, ttk.Treeview):
                return
            w.bind("<MouseWheel>", _wheel)
            for c in w.winfo_children():
                _bind(c)

        _bind(root)

    def _build_ui(self) -> None:
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        top = tk.Frame(self.root, bg=UI_BG)
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=4)
        self._build_header(top)

        main = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        main.grid(row=1, column=0, sticky="nsew", padx=6, pady=2)

        left_outer = tk.Frame(main, bg=UI_BG)
        right_outer = tk.Frame(main, bg=UI_BG)
        main.add(left_outer, weight=3)
        main.add(right_outer, weight=2)

        self._left_scroll_canvas, _lsb, left_inner = self._scroll_wrap(left_outer)
        self._build_left_panel(left_inner)
        self._bind_mousewheel_recursive(self._left_scroll_canvas, left_inner)

        self._build_right_panel(right_outer)

        bottom = tk.Frame(self.root, bg=UI_BG)
        bottom.grid(row=2, column=0, sticky="ew", padx=8, pady=(2, 6))
        self.root.grid_rowconfigure(1, weight=1)
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.pack(fill="x")
        footer = tk.Frame(bottom, bg=UI_BG)
        footer.pack(fill="x", pady=(4, 0))
        tk.Label(footer, textvariable=self.var_status, bg=UI_BG, fg=UI_TEXT, font=("Segoe UI", 9)).pack(
            side="left"
        )
        tk.Label(footer, textvariable=self.var_eta, bg=UI_BG, fg=UI_SUBTEXT, font=("Segoe UI", 9)).pack(
            side="right"
        )

    def _build_left_panel(self, parent: tk.Frame) -> None:
        rb_kw = dict(
            bg=UI_PANEL,
            fg=UI_TEXT,
            activebackground=UI_PANEL,
            activeforeground=UI_TEXT,
            selectcolor=UI_ENTRY_BG,
            font=("Segoe UI", 8),
            highlightthickness=0,
        )
        cb_kw = dict(
            bg=UI_PANEL,
            fg=UI_TEXT,
            activebackground=UI_PANEL,
            activeforeground=UI_TEXT,
            selectcolor=UI_ENTRY_BG,
            font=("Segoe UI", 8),
            highlightthickness=0,
        )

        source_card = tk.LabelFrame(
            parent,
            text=" Source ",
            bg=UI_PANEL,
            fg=UI_TEXT,
            font=("Segoe UI", 9, "bold"),
            padx=6,
            pady=4,
        )
        source_card.pack(fill="x")

        type_row = tk.Frame(source_card, bg=UI_PANEL)
        type_row.pack(fill="x", pady=(0, 4))
        tk.Radiobutton(
            type_row, text="Single / Search", variable=self.var_source_type, value="single", **rb_kw
        ).pack(side="left")
        tk.Radiobutton(
            type_row, text="Playlist", variable=self.var_source_type, value="playlist", **rb_kw
        ).pack(side="left", padx=(6, 0))
        tk.Radiobutton(type_row, text="CSV", variable=self.var_source_type, value="csv", **rb_kw).pack(
            side="left", padx=(6, 0)
        )
        tk.Radiobutton(
            type_row, text="Scan Folder", variable=self.var_source_type, value="folder_scan", **rb_kw
        ).pack(side="left", padx=(6, 0))

        entry_row = tk.Frame(source_card, bg=UI_PANEL)
        entry_row.pack(fill="x")
        e_src = self._tk_entry(entry_row, textvariable=self.var_source)
        e_src.pack(side="left", fill="x", expand=True)
        self._tk_button(entry_row, "Browse", self.browse_source, style="muted").pack(
            side="left", padx=(6, 0)
        )
        self._tk_button(entry_row, "Add to Queue", self.add_job, style="accent").pack(
            side="left", padx=(6, 0)
        )

        output_card = tk.LabelFrame(
            parent,
            text=" Output & Processing ",
            bg=UI_PANEL,
            fg=UI_TEXT,
            font=("Segoe UI", 9, "bold"),
            padx=6,
            pady=4,
        )
        output_card.pack(fill="x", pady=(6, 0))

        out_row = tk.Frame(output_card, bg=UI_PANEL)
        out_row.pack(fill="x")
        tk.Label(out_row, text="Output:", bg=UI_PANEL, fg=UI_TEXT, font=("Segoe UI", 9)).pack(side="left")
        e_out = self._tk_entry(out_row, textvariable=self.var_output_dir)
        e_out.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self._tk_button(out_row, "Choose", self.choose_output_dir).pack(side="left")

        grid = tk.Frame(output_card, bg=UI_PANEL)
        grid.pack(fill="x", pady=(6, 0))

        tk.Label(grid, text="Format", bg=UI_PANEL, fg=UI_TEXT, font=("Segoe UI", 8)).grid(
            row=0, column=0, sticky="w", pady=1
        )
        ttk.Combobox(
            grid,
            textvariable=self.var_audio_format,
            values=["mp3", "wav", "m4a", "flac"],
            width=9,
            state="readonly",
        ).grid(row=0, column=1, sticky="w", padx=(4, 12))
        tk.Label(grid, text="Quality", bg=UI_PANEL, fg=UI_TEXT, font=("Segoe UI", 8)).grid(
            row=0, column=2, sticky="w", pady=1
        )
        ttk.Combobox(
            grid,
            textvariable=self.var_audio_quality,
            values=["0", "2", "5", "7", "9"],
            width=9,
            state="readonly",
        ).grid(row=0, column=3, sticky="w", padx=(4, 0))

        tk.Checkbutton(grid, text="Embed Thumbnail", variable=self.var_embed_thumbnail, **cb_kw).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=1
        )
        tk.Checkbutton(grid, text="Add Metadata", variable=self.var_add_metadata, **cb_kw).grid(
            row=1, column=2, sticky="w", pady=1
        )
        tk.Checkbutton(grid, text="Fetch .lrc", variable=self.var_fetch_lyrics, **cb_kw).grid(
            row=1, column=3, sticky="w", pady=1
        )
        tk.Checkbutton(grid, text="Auto Library", variable=self.var_auto_import_library, **cb_kw).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=1
        )
        tk.Checkbutton(
            grid, text="Embed USLT+SYLT", variable=self.var_embed_lyrics_in_mp3, **cb_kw
        ).grid(row=2, column=2, columnspan=2, sticky="w", pady=1)
        tk.Checkbutton(
            grid, text="USLT: full LRC (Mp3tag)", variable=self.var_uslt_embed_full_lrc, **cb_kw
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=1)
        tk.Checkbutton(grid, text="Detect BPM", variable=self.var_detect_bpm, **cb_kw).grid(
            row=3, column=2, sticky="w", pady=1
        )
        tk.Checkbutton(grid, text="Detect Genre", variable=self.var_detect_genre, **cb_kw).grid(
            row=3, column=3, sticky="w", pady=1
        )
        tk.Checkbutton(grid, text="Playlist subfolders", variable=self.var_playlist_subfolders, **cb_kw).grid(
            row=4, column=0, sticky="w", pady=1
        )
        tk.Checkbutton(grid, text="Overwrite", variable=self.var_overwrite_files, **cb_kw).grid(
            row=4, column=1, sticky="w", pady=1
        )

        ff_row = tk.Frame(output_card, bg=UI_PANEL)
        ff_row.pack(fill="x", pady=(4, 0))
        tk.Label(ff_row, text="FFmpeg:", bg=UI_PANEL, fg=UI_TEXT, font=("Segoe UI", 9)).pack(side="left")
        e_ff = self._tk_entry(ff_row, textvariable=self.var_ffmpeg_path)
        e_ff.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self._tk_button(ff_row, "Browse", self.choose_ffmpeg_dir, style="muted").pack(side="left")

        action_card = tk.LabelFrame(
            parent,
            text=" Actions & Queue ",
            bg=UI_PANEL,
            fg=UI_TEXT,
            font=("Segoe UI", 9, "bold"),
            padx=6,
            pady=4,
        )
        action_card.pack(fill="both", expand=True, pady=(6, 0))

        btns = tk.Frame(action_card, bg=UI_PANEL)
        btns.pack(fill="x")
        self._tk_button(btns, "Start Worker", self.engine.start).pack(side="left")
        self._tk_button(btns, "Stop Worker", self.engine.stop, style="muted").pack(side="left", padx=(6, 0))
        self._tk_button(btns, "Save Settings", self.save_settings, style="muted").pack(side="left", padx=(6, 0))
        self._tk_button(btns, "Open Output", self.open_output_folder, style="muted").pack(side="left", padx=(6, 0))

        tw_outer = tk.Frame(action_card, bg=UI_PANEL)
        tw_outer.pack(fill="both", expand=True, pady=(6, 0))
        tw_outer.grid_rowconfigure(0, weight=1)
        tw_outer.grid_columnconfigure(0, weight=1)

        cols = ("job_id", "type", "source", "status", "progress", "item", "error")
        self.jobs_table = ttk.Treeview(
            tw_outer,
            columns=cols,
            show="headings",
            height=7,
        )
        headings = {
            "job_id": "Job ID",
            "type": "Type",
            "source": "Source",
            "status": "Status",
            "progress": "%",
            "item": "Current Item",
            "error": "Error",
        }
        widths = {"job_id": 120, "type": 70, "source": 180, "status": 72, "progress": 44, "item": 160, "error": 140}
        for c in cols:
            self.jobs_table.heading(c, text=headings[c])
            self.jobs_table.column(c, width=widths[c], anchor="w", stretch=True)
        vsb = ttk.Scrollbar(tw_outer, orient="vertical", command=self.jobs_table.yview)
        hsb = ttk.Scrollbar(tw_outer, orient="horizontal", command=self.jobs_table.xview)
        self.jobs_table.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.jobs_table.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        def _tw_wheel(event) -> None:
            self.jobs_table.yview_scroll(int(-1 * (event.delta / 120)), "units")

        self.jobs_table.bind("<MouseWheel>", _tw_wheel)

    def _build_right_panel(self, parent: tk.Frame) -> None:
        parent.grid_rowconfigure(1, weight=1)
        parent.grid_columnconfigure(0, weight=1)

        help_card = tk.LabelFrame(
            parent,
            text=" Quick Tips ",
            bg=UI_PANEL,
            fg=UI_TEXT,
            font=("Segoe UI", 9, "bold"),
            padx=4,
            pady=2,
        )
        help_card.grid(row=0, column=0, sticky="ew", padx=0, pady=0)
        tips = (
            "• Single / Search: artist - title\n"
            "• Playlist: YouTube playlist URL\n"
            "• CSV: artist/title columns or one query per line\n"
            "• Scan Folder: lyrics + BPM + genre on audio files\n"
            "• FFmpeg: folder with ffmpeg.exe + ffprobe.exe\n"
            "• Mp3tag: USLT only — use “USLT: full LRC” for [mm:ss]\n"
            "• pip: mutagen (USLT+SYLT), yt-dlp, syncedlyrics, librosa, numpy"
        )
        tip_frame = tk.Frame(help_card, bg=UI_PANEL)
        tip_frame.pack(fill="both")
        tip_sb = ttk.Scrollbar(tip_frame)
        tip_txt = tk.Text(
            tip_frame,
            wrap="word",
            height=8,
            font=("Segoe UI", 8),
            bg=UI_ENTRY_BG,
            fg=UI_TEXT,
            relief=tk.SUNKEN,
            bd=2,
            highlightthickness=1,
            highlightbackground=UI_ENTRY_BD,
            yscrollcommand=tip_sb.set,
        )
        tip_sb.config(command=tip_txt.yview)
        tip_sb.pack(side=tk.RIGHT, fill=tk.Y)
        tip_txt.pack(side=tk.LEFT, fill="both", expand=True)
        tip_txt.insert("1.0", tips)
        tip_txt.configure(state="disabled")

        log_card = tk.LabelFrame(
            parent,
            text=" Live Log ",
            bg=UI_PANEL,
            fg=UI_TEXT,
            font=("Segoe UI", 9, "bold"),
            padx=4,
            pady=2,
        )
        log_card.grid(row=1, column=0, sticky="nsew", padx=0, pady=(6, 0))
        log_card.grid_rowconfigure(0, weight=1)
        log_card.grid_columnconfigure(0, weight=1)

        log_wrap = tk.Frame(log_card, bg=UI_PANEL)
        log_wrap.grid(row=0, column=0, sticky="nsew")
        log_wrap.grid_rowconfigure(0, weight=1)
        log_wrap.grid_columnconfigure(0, weight=1)

        log_sb = ttk.Scrollbar(log_wrap, orient="vertical")
        self.txt_log = tk.Text(
            log_wrap,
            wrap="word",
            height=12,
            font=("Consolas", 9),
            bg="#f8fafc",
            fg=UI_TEXT,
            relief=tk.SUNKEN,
            bd=2,
            highlightthickness=1,
            highlightbackground=UI_ENTRY_BD,
            yscrollcommand=log_sb.set,
        )
        log_sb.config(command=self.txt_log.yview)
        log_sb.grid(row=0, column=1, sticky="ns")
        self.txt_log.grid(row=0, column=0, sticky="nsew")

    def browse_source(self) -> None:
        source_type = self.var_source_type.get()
        if source_type == "csv":
            path = filedialog.askopenfilename(filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
            if path:
                self.var_source.set(path)
        elif source_type == "folder_scan":
            path = filedialog.askdirectory()
            if path:
                self.var_source.set(path)
        else:
            messagebox.showinfo("Browse", "Paste a track name/search query or playlist URL into the source box.")

    def choose_output_dir(self) -> None:
        path = filedialog.askdirectory(initialdir=self.var_output_dir.get() or DEFAULT_OUTPUT)
        if path:
            self.var_output_dir.set(path)

    def choose_ffmpeg_dir(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self.var_ffmpeg_path.set(path)

    def add_job(self) -> None:
        source = self.var_source.get().strip()
        source_type = self.var_source_type.get().strip()
        if not source:
            messagebox.showwarning("Missing source", "Enter a source first.")
            return
        self.save_settings(silent=True)
        self.engine.enqueue_source(source, source_type)
        self.var_status.set(f"Queued: {source}")

    def save_settings(self, silent: bool = False) -> None:
        self.config.output_dir = self.var_output_dir.get().strip() or DEFAULT_OUTPUT
        self.config.audio_format = self.var_audio_format.get().strip() or "mp3"
        self.config.audio_quality = self.var_audio_quality.get().strip() or "0"
        self.config.embed_thumbnail = self.var_embed_thumbnail.get()
        self.config.add_metadata = self.var_add_metadata.get()
        self.config.fetch_lyrics = self.var_fetch_lyrics.get()
        self.config.embed_lyrics_in_mp3 = self.var_embed_lyrics_in_mp3.get()
        self.config.uslt_embed_full_lrc = self.var_uslt_embed_full_lrc.get()
        self.config.detect_bpm = self.var_detect_bpm.get()
        self.config.detect_genre = self.var_detect_genre.get()
        self.config.auto_import_library = self.var_auto_import_library.get()
        self.config.playlist_subfolders = self.var_playlist_subfolders.get()
        self.config.overwrite_files = self.var_overwrite_files.get()
        self.config.ffmpeg_path = self.var_ffmpeg_path.get().strip()
        ensure_dir(self.config.output_dir)
        self.config.save()
        self.engine.config = self.config
        if not silent:
            self.var_status.set("Settings saved")
            self.log("[CONFIG] Settings saved.")

    def open_output_folder(self) -> None:
        path = self.var_output_dir.get().strip() or DEFAULT_OUTPUT
        ensure_dir(path)
        os.startfile(path)

    def refresh_jobs_table(self) -> None:
        def _update():
            for item in self.jobs_table.get_children():
                self.jobs_table.delete(item)
            for job in self.engine.jobs.values():
                self.jobs_table.insert(
                    "", "end",
                    values=(
                        job.job_id,
                        job.source_type,
                        job.source,
                        job.status,
                        f"{job.progress:.1f}",
                        job.current_item,
                        job.error,
                    )
                )
        self.root.after(0, _update)

    def on_progress(self, pct: float, eta: str) -> None:
        def _update():
            self.progress["value"] = pct
            self.var_eta.set(f"ETA: {eta or '--'}")
            self.var_status.set(f"Running... {pct:.1f}%")
        self.root.after(0, _update)

    def log(self, message: str) -> None:
        self.log_queue.put(f"[{ts()}] {message}")

    def _drain_log_queue(self) -> None:
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.txt_log.insert("end", msg + "\n")
                self.txt_log.see("end")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log_queue)

    def _probe_ytdlp_status(self) -> None:
        """Avoid blocking startup: resolve yt-dlp after the window is visible."""

        def work() -> None:
            cmd = self.engine.get_ytdlp_cmd()

            def done() -> None:
                if cmd:
                    self.log(f"[START] yt-dlp OK ({' '.join(cmd)})")
                else:
                    self.log(
                        "[START] [WARN] yt-dlp not found. Install: py -3.10 -m pip install yt-dlp"
                    )

            self.root.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def _load_config_into_ui(self) -> None:
        self.var_output_dir.set(self.config.output_dir)
        self.var_audio_format.set(self.config.audio_format)
        self.var_audio_quality.set(self.config.audio_quality)
        self.var_embed_thumbnail.set(self.config.embed_thumbnail)
        self.var_add_metadata.set(self.config.add_metadata)
        self.var_fetch_lyrics.set(self.config.fetch_lyrics)
        self.var_embed_lyrics_in_mp3.set(self.config.embed_lyrics_in_mp3)
        self.var_uslt_embed_full_lrc.set(self.config.uslt_embed_full_lrc)
        self.var_detect_bpm.set(self.config.detect_bpm)
        self.var_detect_genre.set(self.config.detect_genre)
        self.var_auto_import_library.set(self.config.auto_import_library)
        self.var_playlist_subfolders.set(self.config.playlist_subfolders)
        self.var_overwrite_files.set(self.config.overwrite_files)
        self.var_ffmpeg_path.set(self.config.ffmpeg_path)

    def on_close(self) -> None:
        if self._logo_spin_after is not None:
            try:
                self.root.after_cancel(self._logo_spin_after)
            except Exception:
                pass
            self._logo_spin_after = None
        self.save_settings(silent=True)
        self.engine.stop()
        self.root.after(150, self.root.destroy)


def main() -> None:
    print(
        "SAMSEL AutoMix Downloader v2: opening window… (add a job from the UI; this line is normal.)",
        flush=True,
    )
    root = tk.Tk()
    app = AutoMixDownloaderUI(root)
    root.mainloop()


def _parse_web_port(argv: list[str]) -> str:
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            return argv[i + 1].strip() or "8765"
        if a.startswith("--port="):
            return a.split("=", 1)[1].strip() or "8765"
    return (os.environ.get("SAMSEL_PORT") or os.environ.get("PORT") or "8765").strip() or "8765"


def _parse_web_host(argv: list[str]) -> str:
    for i, a in enumerate(argv):
        if a == "--host" and i + 1 < len(argv):
            return argv[i + 1].strip() or "0.0.0.0"
        if a.startswith("--host="):
            return a.split("=", 1)[1].strip() or "0.0.0.0"
    return "0.0.0.0"


def run_standalone_web() -> None:
    """Start the browser UI only (FastAPI + same DownloaderEngine via automix_standalone_app)."""
    import subprocess

    script_dir = Path(__file__).resolve().parent
    rest = [a for a in sys.argv[1:] if a not in ("--web", "--http", "-w")]
    port = _parse_web_port(rest)
    host = _parse_web_host(rest)
    env = os.environ.copy()
    env["SAMSEL_PORT"] = port
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "automix_standalone_app:app",
        "--host",
        host,
        "--port",
        port,
    ]
    print(
        f"AutoMix Downloader v2 — standalone web\n  → http://127.0.0.1:{port}/\n"
        f"  (LAN phones: use this PC's IP and port {port}; same env as SAMSEL Web: SAMSEL_AUTOMIX_LAN, etc.)\n",
        flush=True,
    )
    r = subprocess.run(cmd, cwd=str(script_dir), env=env)
    raise SystemExit(r.returncode)


if __name__ == "__main__":
    if any(a in sys.argv[1:] for a in ("--web", "--http", "-w")):
        run_standalone_web()
    else:
        main()

"""
SAMSEL DJ Engine Pro — desktop app: same web UI in a native window.

Starts uvicorn (default bind 127.0.0.1; picks a free port from 8000 upward unless
``--port`` is set), then opens a WebView2 window (Windows) / WebKit (macOS) /
GTK WebKit (Linux).

Remote use outside the LAN: (1) ``--bind-host 0.0.0.0 --port 8000`` on the PC,
forward that port on your router to this machine, and open ``http://YOUR_PUBLIC_IP:8000``
(HTTPS recommended via a reverse proxy). (2) Simpler: keep the default bind and run a
tunnel (e.g. ``cloudflared tunnel --url http://127.0.0.1:PORT``) after noting the printed
local URL, or pass ``--port`` so the tunnel target is fixed. Exposing the mixer on the
public internet has no built-in auth — prefer Tailscale/ZeroTier or a tunnel with access
controls.

Mixer UI includes optional transition jingle (Drop Sync / Auto DJ crossfades);
see desktop_app/README.md.

Camouflage-colored buttons (same idea as SAMSEL Web AutoMix standalone): the UI loads
/static/css/camo-btn-stacks.css and /static/js/camo-buttons.js. Optional PNG textures
come from a Camouflage_png/ folder next to app.py (stack_1.png … stack_7.png), served
at /camo/stack_N.png when that directory exists.

Desktop window: on each pywebview loaded event, inject CSS so button labels
(btn-* classes) use near-white text with a black outline shadow. Browser-only
sessions are unchanged. load_css must run after the page has loaded (here via
window.events.loaded), not before webview.start().

The window opens maximized so it uses the current display’s work area; if the user
restores it, size follows the detected screen (tkinter probe) with sane minimums.

Jingle folder (same idea as SAMSEL V3 “Folder…” + random-from-folder): before the
server starts, you can point at a directory of audio files; up to four files are
chosen at random and copied into transition_jingle_uploads/ so the web UI’s
transition jingle slots load on startup. Use env SAMSEL_JINGLE_FOLDER, or
--jingle-folder DIR, or --pick-jingle-folder for a dialog. Enable “Play jingle at
transitions” and set order to Random in the mixer if you want varied picks across
the loaded slots.

SAMSEL Web (``samsel_web\\server.py``): started in the background when the tree is
found. One **popup-style** webview (lower memory than multiple windows) opens from
**SAMSEL Web →** Play / Cues / Lyrics / …; each entry loads ``/index.html#<tab>`` plus a
``loaded``-time ``evaluate_js`` fallback so the correct panel opens even when query
strings are dropped or ``samsel.js`` is an older build. Use **Hide** to dismiss without
closing the DJ window. If this repo is on your machine
with ``C:\\Users\\pc\\Downloads\\SAMSEL_V1_PRO\\samsel_web`` present, it is used
automatically. Override with ``SAMSEL_WEB_ROOT``, ``--samsel-web [DIR]``, or
``--samsel-web`` (probe); ``--no-samsel-web`` disables. Child server sets
``SAMSEL_AUTOMIX_LAN=1`` like the batch file. If ``SAMSEL_AutoMix_Jingle_3.mp3`` exists in that tree
and ``SAMSEL_JINGLE_PATH`` is not already set, the child server gets that file as the default transition
jingle (see ``samsel_web/server.py``).

Requires: pip install pywebview
Windows: WebView2 Runtime (usually already installed with Edge).
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
LOGO_PNG_PATH = BASE_DIR / "logo.png"
LOGO_ICON_EXT_PATH = BASE_DIR / "logo.Icon"
ICON_PATH = BASE_DIR / "logo.ico"
# Persistent WebView2 user-data dir (avoids WinError 5 when pywebview deletes a temp folder
# while Edge still holds CrashpadMetrics / .pma files).
WEBVIEW_PROFILE_DIR = BASE_DIR / ".webview_profile"
# Same directory and naming convention as app.py (transition_jingle_uploads / jingle_slot_N.*).
JINGLE_SLOT_DIR = BASE_DIR / "transition_jingle_uploads"
# Match app.py SUPPORTED_AUDIO for files the engine will accept.
_JINGLE_AUDIO_EXTS = frozenset({".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"})
# argparse sentinel: ``--samsel-web`` with no path → probe common directories
_SAMSEL_WEB_CLI_PROBE = ":samsel-web-probe:"
# When env/CLI do not set a path, use this tree if ``server.py`` exists (integrated popup).
_INTEGRATED_SAMSEL_WEB_ROOT = Path(r"C:\Users\pc\Downloads\SAMSEL_V1_PRO\samsel_web")


class _SamselPopupHolder:
    __slots__ = ("window", "base_url", "pending_tab")

    def __init__(self) -> None:
        self.window: object | None = None
        self.base_url: str = ""
        self.pending_tab: str | None = None


# Injected from a window.events.loaded handler (load_css waits on that event).
# Strong stroke + shadow keeps labels readable on camo textures and bright gradients.
_DESKTOP_HIGH_CONTRAST_BTN_CSS = """
button[class*='btn-'] {
  color: #fafafa !important;
  font-weight: 800 !important;
  letter-spacing: 0.04em !important;
  text-shadow:
    -1px -1px 0 #000,
    1px -1px 0 #000,
    -1px 1px 0 #000,
    1px 1px 0 #000,
    0 0 6px #000,
    0 2px 10px rgba(0,0,0,0.95) !important;
  -webkit-font-smoothing: antialiased;
}
button[class*='btn-']:disabled {
  color: #e8e8e8 !important;
  text-shadow:
    -1px -1px 0 #222,
    1px -1px 0 #222,
    -1px 1px 0 #222,
    1px 1px 0 #222 !important;
}
.camo-ui-root .btn, .camo-ui-root .tab, .automix-standalone-wrap .btn {
  color: #fafafa !important;
  font-weight: 800 !important;
  letter-spacing: 0.03em !important;
  text-shadow:
    -1px -1px 0 #000,
    1px -1px 0 #000,
    -1px 1px 0 #000,
    1px 1px 0 #000,
    0 0 5px #000,
    0 2px 8px rgba(0,0,0,0.92) !important;
}
"""


def _webview_window_icon() -> str | None:
    """Title-bar / taskbar icon for native windows: prefer ``logo.ico`` (best on Windows), then ``logo.Icon``, then ``logo.png``."""
    if ICON_PATH.is_file():
        return str(ICON_PATH.resolve())
    if LOGO_ICON_EXT_PATH.is_file():
        return str(LOGO_ICON_EXT_PATH.resolve())
    if LOGO_PNG_PATH.is_file():
        return str(LOGO_PNG_PATH.resolve())
    return None


_winforms_title_icon_patched = False


def _patch_pywebview_winforms_title_icon(icon_abs: str) -> None:
    """
    pywebview's Windows (WinForms) backend ignores ``create_window(..., icon=…)`` and always
    uses the Python executable icon. Patch BrowserForm so ``logo.ico`` appears on every native
    window (DJ + SAMSEL Web). Re-apply on ``Shown`` so hidden-then-shown popups still get it.
    """
    global _winforms_title_icon_patched
    if _winforms_title_icon_patched or sys.platform != "win32":
        return
    p = Path(icon_abs)
    if not p.is_file() or p.suffix.lower() != ".ico":
        return
    resolved = str(p.resolve())
    try:
        from webview.platforms import winforms as wf

        browser_form = wf.BrowserView.BrowserForm
    except Exception:
        return

    original_init = browser_form.__init__

    def patched_init(self, window, cache_dir):  # type: ignore[no-untyped-def]
        original_init(self, window, cache_dir)

        def apply_title_icon() -> None:
            try:
                from System.Drawing import Icon

                self.Icon = Icon(resolved)
            except Exception:
                pass

        apply_title_icon()

        def on_shown_reapply_title_icon(_sender=None, _e=None) -> None:
            apply_title_icon()
            try:
                self.Shown -= on_shown_reapply_title_icon
            except Exception:
                pass

        try:
            self.Shown += on_shown_reapply_title_icon
        except Exception:
            pass

    browser_form.__init__ = patched_init  # type: ignore[method-assign]
    _winforms_title_icon_patched = True


def _python_for_subprocess() -> str:
    """Use a real interpreter path when sys.executable points at a removed install (e.g. stale conda)."""
    exe = sys.executable
    if exe and Path(exe).is_file():
        return exe
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found and Path(found).is_file():
            return found
    return exe or "python"


def _find_free_port(host: str = "127.0.0.1", start: int = 8000, attempts: int = 40) -> int:
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No free TCP port on {host} in range {start}–{start + attempts - 1}")


def _assert_port_free(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError as e:
            raise RuntimeError(f"Port {port} is not available on {host}: {e}") from e


def _parse_desktop_launch_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SAMSEL DJ Engine Pro — desktop shell")
    p.add_argument(
        "--jingle-folder",
        metavar="DIR",
        default=None,
        help="Folder of jingle audio files: seed up to 4 random files into transition jingle slots before startup.",
    )
    p.add_argument(
        "--pick-jingle-folder",
        action="store_true",
        help="Open a folder dialog to choose jingle audio (runs before the server starts).",
    )
    p.add_argument(
        "--samsel-web",
        nargs="?",
        const=_SAMSEL_WEB_CLI_PROBE,
        default=None,
        metavar="DIR",
        help="Also open SAMSEL Web (Player/Lyrics/Trim/EQ/Downloader): path to folder containing server.py, or omit DIR to auto-probe.",
    )
    p.add_argument(
        "--no-samsel-web",
        action="store_true",
        help="Do not launch SAMSEL Web (overrides SAMSEL_WEB_ROOT and --samsel-web).",
    )
    p.add_argument(
        "--bind-host",
        default=(os.environ.get("SAMSEL_DESKTOP_BIND_HOST") or "127.0.0.1").strip(),
        metavar="ADDR",
        help="Uvicorn listen address. 127.0.0.1 = this PC only (default). 0.0.0.0 = all "
        "interfaces (LAN + port-forward). Env: SAMSEL_DESKTOP_BIND_HOST.",
    )
    p.add_argument(
        "--port",
        "-p",
        type=int,
        default=None,
        metavar="N",
        help="Fixed TCP port for the DJ engine (default: first free from 8000). Use with tunnels.",
    )
    return p.parse_known_args()[0]


def _samsel_web_probe_dir() -> Path | None:
    candidates: list[Path] = [
        _INTEGRATED_SAMSEL_WEB_ROOT,
        BASE_DIR / "samsel_web",
        BASE_DIR.parent / "samsel_web",
    ]
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    if home:
        candidates.append(Path(home) / "Downloads" / "SAMSEL_V1_PRO" / "samsel_web")
    for c in candidates:
        try:
            if (c / "server.py").is_file():
                return c.resolve()
        except OSError:
            continue
    return None


def _resolve_samsel_web_root(args: argparse.Namespace) -> Path | None:
    if getattr(args, "no_samsel_web", False):
        return None
    cli = getattr(args, "samsel_web", None)
    if cli == _SAMSEL_WEB_CLI_PROBE:
        found = _samsel_web_probe_dir()
        if found:
            print(f"Samsel Web: using {found}", file=sys.stderr)
            return found
        print(
            "Samsel Web: auto-probe found no server.py. Set SAMSEL_WEB_ROOT or use --samsel-web PATH.",
            file=sys.stderr,
        )
        return None
    if cli and cli != _SAMSEL_WEB_CLI_PROBE:
        p = Path(cli).expanduser().resolve()
        if (p / "server.py").is_file():
            return p
        print(f"Samsel Web: missing server.py under {p}", file=sys.stderr)
        return None
    env_raw = (os.environ.get("SAMSEL_WEB_ROOT") or "").strip()
    if env_raw:
        p = Path(env_raw).expanduser().resolve()
        if (p / "server.py").is_file():
            return p
        print(f"SAMSEL_WEB_ROOT is invalid (need server.py): {env_raw}", file=sys.stderr)
        return None
    try:
        if (_INTEGRATED_SAMSEL_WEB_ROOT / "server.py").is_file():
            return _INTEGRATED_SAMSEL_WEB_ROOT.resolve()
    except OSError:
        pass
    return None


def _samsel_popup_geometry() -> tuple[int, int, tuple[int, int]]:
    """Width, height, min_size for the SAMSEL Web popup window."""
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            sw = int(root.winfo_screenwidth())
            sh = int(root.winfo_screenheight())
        finally:
            root.destroy()
        if sw > 0 and sh > 0:
            # Single shared webview: slightly tighter default than a maximized browser.
            w = max(680, min(int(sw * 0.78), 1200))
            h = max(520, min(int(sh * 0.72), 800))
            return w, h, (600, 400)
    except Exception:
        pass
    return 1020, 700, (600, 400)


_SAMSEL_WEB_TAB_IDS = frozenset({"play", "cues", "lyrics", "eq", "trim", "automix", "info"})


def _samsel_js_fallback_activate_tab(tab_id: str) -> str:
    """Minimal DOM tab switch (no window.samselActivateTab) for older samsel_web builds."""
    return (
        "(function(){var id="
        + json.dumps(tab_id)
        + ",ok={play:1,cues:1,lyrics:1,eq:1,trim:1,automix:1,info:1};if(!ok[id])return;"
        "var t=document.querySelector('.tab[data-tab=\"'+id+'\"]');if(!t)return;"
        "document.querySelectorAll('.tab').forEach(function(x){x.classList.toggle('active',x===t);});"
        "document.querySelectorAll('.panel').forEach(function(p){var on=p.id==='panel-'+id;"
        "p.classList.toggle('active',on);p.hidden=!on;});})();"
    )


def _attach_samsel_menu_tab_bridge(holder: _SamselPopupHolder, window_sw: object) -> None:
    """After each navigation, activate tab from holder.pending_tab (WinForms menu thread)."""
    if window_sw is None:
        return

    def on_loaded() -> None:
        tid = None
        raw = ""
        try:
            raw = str(window_sw.get_current_url() or "")
        except Exception:
            pass
        try:
            frag = urlsplit(raw).fragment.strip().lower()
            if frag == "downloader":
                frag = "automix"
            if frag in _SAMSEL_WEB_TAB_IDS:
                tid = frag
        except Exception:
            pass
        if holder.pending_tab is not None:
            if not tid:
                tid = holder.pending_tab
            holder.pending_tab = None
        if not tid or tid not in _SAMSEL_WEB_TAB_IDS:
            return
        inner = (
            "if(window.samselActivateTab){window.samselActivateTab("
            + json.dumps(tid)
            + ");}else{"
            + _samsel_js_fallback_activate_tab(tid)
            + "}"
        )
        js = "setTimeout(function(){try{" + inner + "}catch(e){}},500);"
        try:
            window_sw.evaluate_js(js)
        except Exception:
            pass

    try:
        window_sw.events.loaded += on_loaded
    except Exception:
        pass


def _samsel_show_web_tab(holder: _SamselPopupHolder, tab_id: str) -> None:
    """Show the single SAMSEL Web window and activate one SPA tab (no extra webviews)."""
    if tab_id not in _SAMSEL_WEB_TAB_IDS:
        return
    w = holder.window
    if w is None:
        return
    try:
        w.show()
    except Exception:
        pass
    # Menu runs on a WinForms worker thread. load_url() marshals with Invoke.
    # Tab in #fragment (not sent to server; survives StaticFiles). pending_tab + loaded
    # bridge runs evaluate_js on the GUI thread after DOM exists (works with old JS too).
    holder.pending_tab = tab_id
    base = (holder.base_url or "").rstrip("/")
    if not base:
        return
    target = f"{base}/index.html?_={time.time_ns()}#{tab_id}"
    try:
        w.load_url(target)
    except Exception:
        pass


def _samsel_web_menu(holder: _SamselPopupHolder):
    from webview.menu import Menu, MenuAction, MenuSeparator

    def hide_popup() -> None:
        win = holder.window
        if win is None:
            return
        try:
            win.hide()
        except Exception:
            pass

    def open_tab(tab_id: str):
        def _fn() -> None:
            _samsel_show_web_tab(holder, tab_id)

        return _fn

    return [
        Menu(
            "SAMSEL Web",
            [
                MenuAction("Play", open_tab("play")),
                MenuAction("Cues", open_tab("cues")),
                MenuAction("Lyrics", open_tab("lyrics")),
                MenuAction("EQ", open_tab("eq")),
                MenuAction("Trim", open_tab("trim")),
                MenuAction("Downloader", open_tab("automix")),
                MenuAction("Info", open_tab("info")),
                MenuSeparator(),
                MenuAction("Hide SAMSEL Web window", hide_popup),
            ],
        )
    ]


def _resolve_jingle_folder_path(args: argparse.Namespace) -> Path | None:
    raw = (os.environ.get("SAMSEL_JINGLE_FOLDER") or "").strip()
    if raw:
        p = Path(raw).expanduser().resolve()
        if p.is_dir():
            return p
        print(f"SAMSEL_JINGLE_FOLDER is not a directory: {raw}", file=sys.stderr)
    if getattr(args, "jingle_folder", None):
        p = Path(args.jingle_folder).expanduser().resolve()
        if p.is_dir():
            return p
        print(f"--jingle-folder is not a directory: {args.jingle_folder}", file=sys.stderr)
    if args.pick_jingle_folder:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass
            picked = filedialog.askdirectory(
                title="Folder with jingle audio files (up to 4 copied into transition slots)",
            )
            root.destroy()
            if picked:
                return Path(picked).resolve()
        except Exception as exc:
            print(f"Jingle folder dialog failed: {exc}", file=sys.stderr)
    return None


def _seed_transition_jingles_from_folder(folder: Path) -> int:
    """
    Copy up to four randomly chosen audio files from ``folder`` into JINGLE_SLOT_DIR
    as jingle_slot_0..3 so app.py's startup restore loads them (like V3 random-from-folder
    but using the engine's four-slot model).
    """
    JINGLE_SLOT_DIR.mkdir(parents=True, exist_ok=True)
    for p in list(JINGLE_SLOT_DIR.glob("jingle_slot_*.*")) + list(JINGLE_SLOT_DIR.glob("transition_jingle.*")):
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass
    candidates = [
        x
        for x in folder.iterdir()
        if x.is_file() and x.suffix.lower() in _JINGLE_AUDIO_EXTS
    ]
    if not candidates:
        print(f"No supported jingle audio files in {folder}", file=sys.stderr)
        return 0
    k = min(4, len(candidates))
    chosen = random.sample(candidates, k)
    for i, src in enumerate(chosen):
        suf = src.suffix.lower() or ".mp3"
        dest = JINGLE_SLOT_DIR / f"jingle_slot_{i}{suf}"
        shutil.copy2(src, dest)
    print(
        f"Transition jingles: copied {k} file(s) from {folder} into {JINGLE_SLOT_DIR.name}/ "
        f"(enable in UI + try Random order for varied transitions).",
        file=sys.stderr,
    )
    return k


def _fit_screen_window_geometry() -> tuple[int, int, tuple[int, int]]:
    """
    Width/height for create_window (used when the user restores from maximized) and
    a reasonable min_size. Avoids webview.screens before webview.start() (extra GUI init).

    Uses tkinter when available (stdlib on most desktop Python installs); otherwise
    defaults that work on typical laptops.
    """
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            sw = int(root.winfo_screenwidth())
            sh = int(root.winfo_screenheight())
        finally:
            root.destroy()
        if sw > 0 and sh > 0:
            # Near-full screen when un-maximized; margins approximate taskbar / frame
            w = max(800, sw - 72)
            h = max(600, sh - 120)
            min_w = max(400, min(1024, sw // 2))
            min_h = max(300, min(720, sh // 2))
            return w, h, (min_w, min_h)
    except Exception:
        pass
    return 1280, 800, (640, 480)


def _wait_for_http(url: str, timeout: float = 90.0, interval: float = 0.12) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return
        except (URLError, OSError):
            time.sleep(interval)
    raise TimeoutError(f"Server did not become ready: {url}")


def _post_stop_engine(base_url: str) -> None:
    """Stop DJ engine before tearing down WebView / killing uvicorn so audio does not keep running."""
    url = f"{base_url.rstrip('/')}/stop_engine"
    try:
        req = Request(url, data=b"", method="POST", headers={"Content-Length": "0"})
        with urlopen(req, timeout=12) as resp:
            if resp.status not in (200, 204):
                pass
    except (URLError, OSError, TimeoutError):
        pass


# --- Windows: keep uvicorn from outliving this process when CMD / console is closed ---
if sys.platform == "win32":
    import ctypes
    import signal
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", wintypes.ULARGE_INTEGER),
            ("WriteOperationCount", wintypes.ULARGE_INTEGER),
            ("OtherOperationCount", wintypes.ULARGE_INTEGER),
            ("ReadTransferCount", wintypes.ULARGE_INTEGER),
            ("WriteTransferCount", wintypes.ULARGE_INTEGER),
            ("OtherTransferCount", wintypes.ULARGE_INTEGER),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _win_uvicorn_job: wintypes.HANDLE | None = None

    class _WinConsoleShutdown:
        __slots__ = ("base_url", "proc", "proc_sw")

        def __init__(self) -> None:
            self.base_url: str | None = None
            self.proc: subprocess.Popen | None = None
            self.proc_sw: subprocess.Popen | None = None

    _win_console_shutdown = _WinConsoleShutdown()

    def _win_process_handle_int(proc: subprocess.Popen) -> int:
        ph = getattr(proc, "_handle", None)
        if ph is None:
            return 0
        try:
            return int(ph)
        except (TypeError, ValueError):
            h = getattr(ph, "handle", None)
            return int(h) if h is not None else 0

    def _win_force_kill_uvicorn_tree(proc: subprocess.Popen | None) -> None:
        """End uvicorn and any child processes (Windows Popen.terminate often leaves audio playing)."""
        if proc is None or proc.poll() is not None:
            return
        pid = proc.pid
        if pid <= 0:
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
                creationflags=flags,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _win_console_ctrl_handler(signum: int, frame: object | None) -> None:
        """Console close / logoff: stop audio and end uvicorn before the shell tears down the parent."""
        st = _win_console_shutdown
        if st.base_url:
            _post_stop_engine(st.base_url)
        _win_force_kill_uvicorn_tree(st.proc_sw)
        _win_force_kill_uvicorn_tree(st.proc)
        os._exit(0)

    def _win_install_console_shutdown_handlers() -> None:
        for name in ("CTRL_CLOSE_EVENT", "CTRL_LOGOFF_EVENT", "CTRL_SHUTDOWN_EVENT"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, _win_console_ctrl_handler)
            except (ValueError, OSError):
                pass

    def _win_uvicorn_kill_on_parent_exit(proc: subprocess.Popen | None) -> None:
        """
        Put the uvicorn child in a job object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE so
        when this desktop_app process exits (including abrupt console teardown), Windows
        terminates the child instead of leaving a headless server still playing audio.
        """
        global _win_uvicorn_job
        if proc is None or proc.poll() is not None:
            return
        ph = getattr(proc, "_handle", None)
        if not ph:
            return
        if _win_uvicorn_job is None:
            h_job = _kernel32.CreateJobObjectW(None, None)
            if not h_job:
                return
            ext = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            ext.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _kernel32.SetInformationJobObject(
                h_job,
                _JobObjectExtendedLimitInformation,
                ctypes.byref(ext),
                ctypes.sizeof(ext),
            ):
                _kernel32.CloseHandle(h_job)
                return
            _win_uvicorn_job = h_job
        raw = _win_process_handle_int(proc)
        h_process = wintypes.HANDLE(raw)
        if not _kernel32.AssignProcessToJobObject(_win_uvicorn_job, h_process):
            # Popen handle can lack job rights; open with SET_QUOTA | TERMINATE for AssignProcessToJobObject.
            access = 0x0100 | 0x0001  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
            dup = _kernel32.OpenProcess(access, False, wintypes.DWORD(proc.pid))
            if dup:
                _kernel32.AssignProcessToJobObject(_win_uvicorn_job, dup)
                _kernel32.CloseHandle(dup)

    _win_install_console_shutdown_handlers()

else:

    def _win_uvicorn_kill_on_parent_exit(proc: subprocess.Popen | None) -> None:
        return


def _attach_desktop_contrast(window: object) -> None:
    if window is None:
        return

    def _inject() -> None:
        try:
            window.load_css(_DESKTOP_HIGH_CONTRAST_BTN_CSS)
        except Exception:
            pass

    window.events.loaded += _inject


def main() -> None:
    launch_args = _parse_desktop_launch_args()
    jingle_dir = _resolve_jingle_folder_path(launch_args)
    if jingle_dir is not None:
        _seed_transition_jingles_from_folder(jingle_dir)

    try:
        import webview
    except ImportError:
        print("Missing dependency. Run: pip install pywebview", file=sys.stderr)
        sys.exit(1)

    # WinForms path ignores create_window(icon=…); patch every BrowserForm (DJ + SAMSEL Web).
    if ICON_PATH.is_file():
        _patch_pywebview_winforms_title_icon(str(ICON_PATH.resolve()))
    else:
        _ico_for_patch = _webview_window_icon()
        if _ico_for_patch:
            _patch_pywebview_winforms_title_icon(_ico_for_patch)

    bind_host = (getattr(launch_args, "bind_host", None) or "127.0.0.1").strip()
    if not bind_host:
        bind_host = "127.0.0.1"
    # WebView and local health checks must use a real client destination (not 0.0.0.0).
    client_host = "127.0.0.1" if bind_host == "0.0.0.0" else bind_host
    samsel_root = _resolve_samsel_web_root(launch_args)
    fixed_port = getattr(launch_args, "port", None)
    if fixed_port is not None:
        _assert_port_free(bind_host, fixed_port)
        port = fixed_port
    else:
        port = _find_free_port(bind_host)
    port_sw = _find_free_port(bind_host, start=port + 1) if samsel_root else None
    base_url = f"http://{client_host}:{port}"
    health_url = f"{base_url}/api/health"

    cmd = [
        _python_for_subprocess(),
        "-m",
        "uvicorn",
        "app:app",
        "--host",
        bind_host,
        f"--port={port}",
        "--timeout-keep-alive",
        "300",
    ]
    popen_kw: dict = {"cwd": str(BASE_DIR)}
    if sys.platform == "win32":
        popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    proc: subprocess.Popen | None = None
    proc_sw: subprocess.Popen | None = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **popen_kw,
        )
        if proc.poll() is not None:
            print("Uvicorn exited immediately. Check: pip install -r requirements.txt", file=sys.stderr)
            sys.exit(1)
        _win_uvicorn_kill_on_parent_exit(proc)
        if sys.platform == "win32":
            _win_console_shutdown.base_url = base_url
            _win_console_shutdown.proc = proc
        _wait_for_http(health_url)
        print(f"SAMSEL DJ engine — local UI: http://{client_host}:{port}/", file=sys.stderr)
        if bind_host == "0.0.0.0":
            print(
                f"  Also bound on {bind_host}:{port} (LAN / port-forward). Allow this TCP port in the "
                "firewall if remote machines cannot connect.",
                file=sys.stderr,
            )

        if samsel_root and port_sw is not None:
            env_sw = os.environ.copy()
            env_sw["SAMSEL_AUTOMIX_LAN"] = "1"
            env_sw["SAMSEL_PORT"] = str(port_sw)
            if not (env_sw.get("SAMSEL_JINGLE_PATH") or "").strip():
                _sw_jingle = (samsel_root / "SAMSEL_AutoMix_Jingle_3.mp3").resolve()
                if _sw_jingle.is_file():
                    env_sw["SAMSEL_JINGLE_PATH"] = str(_sw_jingle)
                else:
                    _eng_jingle = (BASE_DIR / "SAMSEL_AutoMix_Jingle_3.mp3").resolve()
                    if _eng_jingle.is_file():
                        env_sw["SAMSEL_JINGLE_PATH"] = str(_eng_jingle)
            cmd_sw = [
                _python_for_subprocess(),
                "-m",
                "uvicorn",
                "server:app",
                "--host",
                bind_host,
                f"--port={port_sw}",
                "--timeout-keep-alive",
                "300",
            ]
            popen_sw_kw: dict = {"cwd": str(samsel_root), "env": env_sw}
            if sys.platform == "win32":
                popen_sw_kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            proc_sw = subprocess.Popen(
                cmd_sw,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **popen_sw_kw,
            )
            if proc_sw.poll() is not None:
                print(
                    "Samsel Web (server.py) exited immediately — install that tree’s requirements.txt "
                    f"and ensure {samsel_root / 'server.py'} runs with uvicorn.",
                    file=sys.stderr,
                )
                proc_sw = None
            else:
                try:
                    _wait_for_http(f"http://{client_host}:{port_sw}/api/health")
                except TimeoutError:
                    print("Samsel Web did not respond in time; stopping that process.", file=sys.stderr)
                    proc_sw.terminate()
                    try:
                        proc_sw.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc_sw.kill()
                    proc_sw = None
            if proc_sw is not None and proc_sw.poll() is None:
                _win_uvicorn_kill_on_parent_exit(proc_sw)
                if sys.platform == "win32":
                    _win_console_shutdown.proc_sw = proc_sw

        win_w, win_h, min_wh = _fit_screen_window_geometry()
        pop_w, pop_h, pop_min = _samsel_popup_geometry()
        supported = inspect.signature(webview.create_window).parameters
        samsel_holder = _SamselPopupHolder()

        desired: dict = {
            "title": "SAMSEL DJ Engine Pro",
            "url": f"{base_url}/",
            "width": win_w,
            "height": win_h,
            "min_size": min_wh,
            "maximized": True,
            "resizable": True,
        }
        _ico = str(ICON_PATH.resolve()) if ICON_PATH.is_file() else _webview_window_icon()
        if _ico:
            desired["icon"] = _ico
        samsel_hidden = True
        if proc_sw is not None and port_sw is not None:
            if "menu" in supported:
                desired["menu"] = _samsel_web_menu(samsel_holder)
            else:
                samsel_hidden = False
                print(
                    "SAMSEL Web: this pywebview build has no window menu; showing the suite in a visible second window.",
                    file=sys.stderr,
                )
        window_kw = {k: v for k, v in desired.items() if k in supported}
        window = webview.create_window(**window_kw)
        _attach_desktop_contrast(window)

        window_sw = None

        def _on_main_window_closing() -> None:
            _post_stop_engine(base_url)
            # If a second window exists (e.g. SAMSEL Web), it keeps webview.start() alive — destroy it so the process exits.
            try:
                wins = getattr(webview, "windows", None)
                if wins:
                    for w in list(wins):
                        if w is not window:
                            try:
                                w.destroy()
                            except Exception:
                                pass
            except Exception:
                pass

        try:
            window.events.closing += _on_main_window_closing
        except Exception:
            pass

        if proc_sw is not None and port_sw is not None:
            base_sw = f"http://{client_host}:{port_sw}"
            samsel_holder.base_url = base_sw
            desired_sw: dict = {
                "title": "SAMSEL Web — Player · Lyrics · Trim · EQ · Downloader",
                "url": f"{base_sw}/",
                "width": pop_w,
                "height": pop_h,
                "min_size": pop_min,
                "maximized": False,
                "resizable": True,
                "hidden": samsel_hidden,
            }
            _ico_sw = str(ICON_PATH.resolve()) if ICON_PATH.is_file() else _ico
            if _ico_sw:
                desired_sw["icon"] = _ico_sw
            window_kw_sw = {k: v for k, v in desired_sw.items() if k in supported}
            window_sw = webview.create_window(**window_kw_sw)
            samsel_holder.window = window_sw
            _attach_desktop_contrast(window_sw)
            _attach_samsel_menu_tab_bridge(samsel_holder, window_sw)
            if window_sw is not None:

                def open_samsel_web_popup() -> None:
                    w = samsel_holder.window
                    if w is None:
                        return
                    try:
                        w.show()
                    except Exception:
                        pass

                def hide_samsel_web_popup() -> None:
                    w = samsel_holder.window
                    if w is None:
                        return
                    try:
                        w.hide()
                    except Exception:
                        pass

                try:
                    window.expose(open_samsel_web_popup, hide_samsel_web_popup)
                except Exception:
                    pass
            if samsel_hidden:
                print(
                    "SAMSEL Web is running in the background — use menu: SAMSEL Web → Play / Lyrics / … (one window)",
                    file=sys.stderr,
                )

        WEBVIEW_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        start_kw: dict = {
            "storage_path": str(WEBVIEW_PROFILE_DIR),
            "private_mode": False,
        }
        start_params = inspect.signature(webview.start).parameters
        start_kw = {k: v for k, v in start_kw.items() if k in start_params}
        try:
            webview.start(**start_kw)
        except KeyboardInterrupt:
            # Ctrl+C during GUI init or run — avoid a long traceback; finally still stops uvicorn/audio.
            print("\nDJ desktop interrupted (Ctrl+C). Stopping servers…", file=sys.stderr)
    finally:
        if proc is not None and proc.poll() is None:
            _post_stop_engine(base_url)
        if sys.platform == "win32":
            # Kill entire process tree so uvicorn cannot keep playing after the window closes.
            _win_force_kill_uvicorn_tree(proc_sw)
            _win_force_kill_uvicorn_tree(proc)
        else:
            for child in (proc_sw, proc):
                if child is not None and child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        child.kill()


if __name__ == "__main__":
    main()

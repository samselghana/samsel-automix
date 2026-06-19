"""
SAMSEL Web entrypoint when this file lives in SAMSEL-WEB-ENGINE (parent folder).

The full app (``automix_routes``, ``static/``, etc.) ships under ``SAMSEL_ULTIMATE/``
or ``samsel_web/``. Running ``uvicorn server:app`` here loads that implementation.

Prefer: ``cd SAMSEL_ULTIMATE`` then ``py -3.10 -m uvicorn server:app ...`` or use
``SAMSEL_ULTIMATE\\run_web.bat``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent


def _pick_web_dir() -> Path:
    for name in ("SAMSEL_ULTIMATE", "samsel_web"):
        d = _root / name
        if (d / "server.py").is_file() and (d / "automix_routes.py").is_file():
            return d
    raise ImportError(
        "SAMSEL Web: need a subfolder SAMSEL_ULTIMATE/ or samsel_web/ next to this file, "
        "each containing server.py and automix_routes.py. "
        "Open SAMSEL_ULTIMATE\\run_web.bat instead of running from the parent folder."
    )


_web = _pick_web_dir()
_web_s = str(_web)
if sys.path[0] != _web_s:
    sys.path.insert(0, _web_s)

_real = _web / "server.py"
_spec = importlib.util.spec_from_file_location("_samsel_web_impl", _real)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load {_real}")

_mod = importlib.util.module_from_spec(_spec)
# Child module name avoids clobbering this package name ``server`` in sys.modules.
sys.modules.setdefault("_samsel_web_impl", _mod)
_spec.loader.exec_module(_mod)

app = _mod.app

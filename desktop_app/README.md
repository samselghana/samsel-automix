# SAMSEL DJ Engine — desktop shell

The native **desktop** experience is launched from the repo root, not from this folder:

- **Windows:** `run_desktop.bat` or `python desktop_app.py`
- **Other OS:** `python desktop_app.py` (requires `pip install pywebview`)

That opens a window around the same **FastAPI** UI as the browser (`app:app` / `uvicorn`).

## Transition jingles (programmed crossfades)

In the **MIXER** panel, use **Transition jingles** (up to **four** slots):

1. **Load…** per slot — short MP3/WAV/etc. (saved as `jingle_slot_0.*` … `jingle_slot_3.*`).
2. **Play order** — **In slot order** cycles 1→4 (skips empty slots), or **Random** picks among loaded slots.
3. Enable **Play jingle at transitions** and set **Jingle level** if needed.

One jingle plays **once** per **automated** crossfade (Drop Sync, Manual Drop, Auto DJ). Manual crossfader moves do **not** trigger it.

Files live under `transition_jingle_uploads/` next to `app.py`.

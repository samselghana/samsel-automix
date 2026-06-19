"""
Master-bus 10-band EQ for DJEnginePro — log-spaced 30 Hz … 18 kHz (UI labels),
peaking Q = 1.2, gain −12 … +20 dB.

The top slider targets “air” / extreme treble: peaking at ~18 kHz is unreliable
near Nyquist, so band 10 uses the Web Audio highshelf biquad (same math as the
spec) with a shelf pivot around 10 kHz.

Uses SciPy second-order sections + sosfilt (stateful across callbacks).
"""

from __future__ import annotations

import numpy as np
from scipy import signal

N_BANDS = 10
ENGINE_EQ_MIN_DB = -12.0
ENGINE_EQ_MAX_DB = 20.0
ENGINE_EQ_Q = 1.2
# Highshelf pivot (Hz): must sit safely below Nyquist on 44.1 kHz devices.
ENGINE_EQ_HIGHSHELF_HZ = 10000.0
ENGINE_EQ_HIGHSHELF_FMAX_FRAC_SR = 0.22


def engine_eq_center_frequencies_hz() -> np.ndarray:
    lo = np.log10(30.0)
    hi = np.log10(18000.0)
    t = np.arange(N_BANDS, dtype=np.float64) / 9.0
    return np.round(np.power(10.0, lo + t * (hi - lo)) * 100.0) / 100.0


def _rbj_peaking_ba(
    fc: float, gain_db: float, sr: float, q: float = ENGINE_EQ_Q
) -> tuple[np.ndarray, np.ndarray]:
    """Robert Bristow-Johnson peaking EQ; returns (b, a) with a[0] == 1."""
    gain_db = float(np.clip(gain_db, ENGINE_EQ_MIN_DB, ENGINE_EQ_MAX_DB))
    w0 = 2.0 * np.pi * float(fc) / float(sr)
    c_w = float(np.cos(w0))
    s_w = float(np.sin(w0))
    alpha = s_w / (2.0 * q)
    A = float(np.power(10.0, gain_db / 40.0))
    b0 = 1.0 + alpha * A
    b1 = -2.0 * c_w
    b2 = 1.0 - alpha * A
    a0 = 1.0 + alpha / A
    a1 = -2.0 * c_w
    a2 = 1.0 - alpha / A
    b0 /= a0
    b1 /= a0
    b2 /= a0
    a1 /= a0
    a2 /= a0
    b = np.array([b0, b1, b2], dtype=np.float64)
    a = np.array([1.0, a1, a2], dtype=np.float64)
    return b, a


def _web_audio_highshelf_ba(
    f0_hz: float, gain_db: float, sr: float, S: float = 1.0
) -> tuple[np.ndarray, np.ndarray]:
    """Web Audio BiquadFilterNode highshelf coefficients (W3C / webaudio spec), a0 normalized to 1."""
    gain_db = float(np.clip(gain_db, ENGINE_EQ_MIN_DB, ENGINE_EQ_MAX_DB))
    A = float(np.power(10.0, gain_db / 40.0))
    w0 = 2.0 * np.pi * float(f0_hz) / float(sr)
    cos_w0 = float(np.cos(w0))
    sin_w0 = float(np.sin(w0))
    alp_s = (sin_w0 / 2.0) * float(
        np.sqrt((A + 1.0 / A) * (1.0 / float(S) - 1.0) + 2.0)
    )
    sqrt_a = float(np.sqrt(A))
    b0 = A * ((A + 1.0) + (A - 1.0) * cos_w0 + 2.0 * alp_s * sqrt_a)
    b1 = -2.0 * A * ((A - 1.0) + (A + 1.0) * cos_w0)
    b2 = A * ((A + 1.0) + (A - 1.0) * cos_w0 - 2.0 * alp_s * sqrt_a)
    a0 = (A + 1.0) - (A - 1.0) * cos_w0 + 2.0 * alp_s * sqrt_a
    a1 = 2.0 * ((A - 1.0) - (A + 1.0) * cos_w0)
    a2 = (A + 1.0) - (A - 1.0) * cos_w0 - 2.0 * alp_s * sqrt_a
    b0 /= a0
    b1 /= a0
    b2 /= a0
    a1 /= a0
    a2 /= a0
    b = np.array([b0, b1, b2], dtype=np.float64)
    a = np.array([1.0, a1, a2], dtype=np.float64)
    return b, a


def design_engine_eq_sos(gains_db: np.ndarray, sr: float) -> np.ndarray:
    """Stacked SOS shape (10, 6) for scipy.signal.sosfilt."""
    g = np.asarray(gains_db, dtype=np.float64).reshape(N_BANDS)
    g = np.clip(g, ENGINE_EQ_MIN_DB, ENGINE_EQ_MAX_DB)
    fcs = engine_eq_center_frequencies_hz()
    rows: list[np.ndarray] = []
    f_hs = min(ENGINE_EQ_HIGHSHELF_HZ, float(sr) * ENGINE_EQ_HIGHSHELF_FMAX_FRAC_SR)
    for bi, (fc, gdb) in enumerate(zip(fcs, g)):
        if abs(float(gdb)) < 1e-9:
            rows.append(np.array([1.0, 0.0, 0.0, 1.0, 0.0, 0.0], dtype=np.float64))
            continue
        if bi == N_BANDS - 1:
            b, a = _web_audio_highshelf_ba(f_hs, float(gdb), float(sr))
        else:
            b, a = _rbj_peaking_ba(float(fc), float(gdb), float(sr), q=float(ENGINE_EQ_Q))
        sos = signal.tf2sos(b, a)
        rows.append(np.asarray(sos[0], dtype=np.float64))
    return np.vstack(rows)

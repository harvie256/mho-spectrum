#!/usr/bin/env python3
"""Power-spectrum computation for streamed scope records.

A 1 Mpt real FFT is ~15-25 ms with scipy's multithreaded pocketfft, which fits
inside a ~60 ms frame budget with room to spare.  The expensive part for display
is not the FFT but pushing 500k bins into a plot, so reduce_for_display()
collapses them to screen width with a min/max envelope -- the same trick
capture/waveform_gui.py uses for time-domain traces, and the only one that
preserves narrow peaks under heavy decimation.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import fft as sfft

WINDOWS = ("hann", "blackmanharris", "flattop", "rectangular")


def make_window(name: str, n: int) -> tuple[np.ndarray, float]:
    """Return (window, coherent gain).

    Coherent gain normalises amplitude so a full-scale sine reads 0 dBFS
    regardless of window choice.
    """
    name = name.lower()
    if name == "rectangular":
        w = np.ones(n, dtype=np.float32)
    elif name == "hann":
        w = np.hanning(n).astype(np.float32)
    elif name == "blackmanharris":
        k = np.arange(n)
        a = (0.35875, 0.48829, 0.14128, 0.01168)
        w = (a[0] - a[1] * np.cos(2 * np.pi * k / (n - 1))
             + a[2] * np.cos(4 * np.pi * k / (n - 1))
             - a[3] * np.cos(6 * np.pi * k / (n - 1))).astype(np.float32)
    elif name == "flattop":
        k = np.arange(n)
        a = (0.21557895, 0.41663158, 0.277263158, 0.083578947, 0.006947368)
        w = (a[0] - a[1] * np.cos(2 * np.pi * k / (n - 1))
             + a[2] * np.cos(4 * np.pi * k / (n - 1))
             - a[3] * np.cos(6 * np.pi * k / (n - 1))
             + a[4] * np.cos(8 * np.pi * k / (n - 1))).astype(np.float32)
    else:
        raise ValueError(f"unknown window {name!r}; choose from {WINDOWS}")
    return w, float(w.mean())


@dataclass
class Spectrum:
    freqs: np.ndarray            # Hz
    power_db: np.ndarray         # dBFS
    resolution: float            # Hz per bin


class SpectrumEngine:
    """Turns raw ADC codes into a dBFS power spectrum.

    Window and scratch buffers are cached per (n, window) so a steady stream of
    same-sized frames does no per-frame allocation beyond the FFT output.
    """

    def __init__(self, window: str = "hann", averaging: int = 1,
                 peak_hold: bool = False, workers: int = -1):
        # Measured DC offset in ADC codes, subtracted before windowing.  Zero
        # means "assume mid-scale"; capture_dc() fills it from a real record.
        self.dc_offset = 0.0
        self.window_name = window
        self.averaging = max(1, averaging)
        self.peak_hold = peak_hold
        self.workers = workers
        self._win: np.ndarray | None = None
        self._cg = 1.0
        self._win_n = -1
        self._avg: np.ndarray | None = None
        self._peak: np.ndarray | None = None
        self._freqs: np.ndarray | None = None
        self._freq_n = -1
        self._freq_sr = -1.0

    def reset(self):
        self._avg = None
        self._peak = None

    def capture_dc(self, codes: np.ndarray, full_scale: float | None = None):
        """Calibrate out the input's DC offset using the current record.

        A real DC offset is signal, so process() deliberately centres on the
        code midpoint rather than the record mean -- otherwise DC could never be
        seen.  This measures the offset once, on demand, so it can be removed
        without hiding genuine DC changes afterwards.
        """
        if full_scale is None:
            full_scale = float(np.iinfo(codes.dtype).max) + 1.0
        self.dc_offset = float(codes.mean()) - full_scale / 2.0
        self.reset()
        return self.dc_offset

    def clear_dc(self):
        self.dc_offset = 0.0
        self.reset()

    def set_window(self, name: str):
        if name != self.window_name:
            self.window_name = name
            self._win_n = -1
            self.reset()

    def _window_for(self, n: int) -> tuple[np.ndarray, float]:
        if self._win_n != n or self._win is None:
            self._win, self._cg = make_window(self.window_name, n)
            self._win_n = n
        return self._win, self._cg

    def _freqs_for(self, n: int, sample_rate: float) -> np.ndarray:
        if self._freq_n != n or self._freq_sr != sample_rate or self._freqs is None:
            self._freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate).astype(np.float64)
            self._freq_n, self._freq_sr = n, sample_rate
        return self._freqs

    def process(self, codes: np.ndarray, sample_rate: float,
                full_scale: float | None = None) -> Spectrum:
        n = codes.size
        if n < 2:
            raise ValueError("need at least 2 samples")
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")

        # Centre on the code midpoint rather than the record mean: a real DC
        # offset is signal, and subtracting it would hide it from the spectrum.
        if full_scale is None:
            full_scale = float(np.iinfo(codes.dtype).max) + 1.0
        x = codes.astype(np.float32) - np.float32(full_scale / 2.0 + self.dc_offset)
        x /= np.float32(full_scale / 2.0)

        win, cg = self._window_for(n)
        x *= win

        spec = sfft.rfft(x, workers=self.workers)
        mag = np.abs(spec)
        # Single-sided amplitude, normalised so a full-scale sine reads 0 dBFS.
        mag *= 2.0 / (n * cg)
        if mag.size:
            mag[0] *= 0.5
            if n % 2 == 0:
                mag[-1] *= 0.5

        power = mag.astype(np.float64) ** 2

        if self.averaging > 1:
            if self._avg is None or self._avg.shape != power.shape:
                self._avg = power.copy()
            else:
                a = 1.0 / self.averaging
                self._avg += a * (power - self._avg)
            power = self._avg

        if self.peak_hold:
            if self._peak is None or self._peak.shape != power.shape:
                self._peak = power.copy()
            else:
                np.maximum(self._peak, power, out=self._peak)
            power = self._peak

        with np.errstate(divide="ignore"):
            db = 10.0 * np.log10(np.maximum(power, 1e-30))

        freqs = self._freqs_for(n, sample_rate)
        return Spectrum(freqs=freqs, power_db=db, resolution=sample_rate / n)


def reduce_for_display(freqs: np.ndarray, db: np.ndarray, cols: int = 2000,
                       fmin: float | None = None, fmax: float | None = None
                       ) -> tuple[np.ndarray, np.ndarray]:
    """Collapse a spectrum to ~cols points for plotting, keeping peaks *in place*.

    Two things matter here and both are easy to get wrong:

    1. Take the per-bucket **maximum**, not every Nth bin.  Plain slicing aliases
       narrow tones away entirely, and a spectrum is mostly narrow tones.
    2. Plot each maximum at the frequency it actually occurred at, **not** at the
       start of its bucket.  With a 1 GHz Nyquist and 2000 columns a bucket spans
       500 kHz, so bucket-start x-positions drag a 200 kHz tone onto 0 Hz and its
       600 kHz harmonic onto 500 kHz -- peaks that do not line up with the axis.

    Passing fmin/fmax restricts the reduction to the visible span, so zooming in
    re-reduces from full resolution instead of magnifying coarse buckets.
    """
    lo, hi = 0, db.size
    if fmin is not None:
        lo = int(np.searchsorted(freqs, fmin, side="left"))
    if fmax is not None:
        hi = int(np.searchsorted(freqs, fmax, side="right"))
    lo = max(0, min(lo, db.size - 1))
    hi = max(lo + 1, min(hi, db.size))

    f = freqs[lo:hi]
    d = db[lo:hi]
    n = d.size
    if n <= cols:
        return f, d

    step = n // cols
    trimmed = step * cols
    dv = d[:trimmed].reshape(cols, step)
    fv = f[:trimmed].reshape(cols, step)
    idx = dv.argmax(axis=1)
    out_d = dv[np.arange(cols), idx]
    out_f = fv[np.arange(cols), idx]

    # Don't silently drop the tail -- at 500001 bins that is up to 249 bins, and
    # it is where the highest frequencies live.
    if trimmed < n:
        tail_d = d[trimmed:]
        j = int(np.argmax(tail_d))
        out_f = np.append(out_f, f[trimmed + j])
        out_d = np.append(out_d, tail_d[j])

    return out_f, out_d

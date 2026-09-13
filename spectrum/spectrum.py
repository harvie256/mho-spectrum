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

# Sidelobe suppression and worst-case scallop (picket-fence) loss are
# literature values for each window shape -- they depend only on the
# coefficients, not on n, so there is nothing to measure at runtime.  ENBW is
# deliberately *not* tabulated here: it is computed from the actual window
# array in window_enbw(), so a change to the coefficients above cannot leave a
# stale constant behind.  For reference the computed values land at rectangular
# 1.00, hann 1.50, Blackman-Harris 2.00, flat-top 3.77.
WINDOW_NOTES = {
    #                  sidelobe dB   scallop loss dB
    "rectangular":     (-13.3,        3.92),
    "hann":            (-31.5,        1.42),
    "blackmanharris":  (-92.0,        0.83),
    "flattop":         (-93.6,        0.01),
}

# The bin-to-pixel detectors, in the order a UI should offer them.  See
# docs/SPECTRUM_ANALYSER_FEATURES.md; the dB errors quoted in the comments in
# reduce_for_display are for Gaussian noise and are the reason the choice
# matters for any number that gets quoted rather than merely looked at.
DETECTORS = ("+peak", "-peak", "sample", "rms", "avg-voltage", "avg-log",
             "envelope")


def window_enbw(w: np.ndarray) -> float:
    """Equivalent noise bandwidth of a window, in bins.

    ENBW = N x sum(w^2) / (sum w)^2.  This is the factor that turns bin spacing
    into a real RBW: `RBW = ENBW x fs/N`.  Conflating the two is the classic
    error both Keysight and Siglent document, so it is computed rather than
    assumed anywhere a bandwidth is quoted.
    """
    s = float(w.sum())
    if s == 0.0:
        return 1.0
    return float(w.size * float((w.astype(np.float64) ** 2).sum()) / (s * s))


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
    resolution: float            # Hz per bin -- bin spacing, fs/N, NOT the RBW
    enbw_bins: float = 1.0       # window's equivalent noise bandwidth, in bins
    sample_rate: float = 0.0
    n: int = 0
    window: str = ""
    clipped: int = 0             # ADC codes sitting at 0 or full scale

    @property
    def rbw(self) -> float:
        """The real resolution bandwidth: `ENBW x fs/N`.

        `resolution` is the spacing between FFT points; this is the width of
        the filter each point represents.  For hann they differ by 1.5x, which
        is 1.76 dB on any noise-density number derived from them.
        """
        return self.enbw_bins * self.resolution

    @property
    def noise_bandwidth(self) -> float:
        """Alias for rbw, for the readers who reach for this name."""
        return self.rbw


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
        self._enbw = 1.0
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
            self._enbw = window_enbw(self._win)
            self._win_n = n
        return self._win, self._cg

    @property
    def enbw_bins(self) -> float:
        """ENBW of the current window, in bins (1.0 until one is built)."""
        return self._enbw

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

        # Clipping check, before any scaling: a record that hit the rails is
        # not a spectrum, it is a spectrum of a square wave, and every harmonic
        # in it is manufactured by the ADC.  Counting codes pinned at either
        # extreme is the cheap version of the `UNCAL`/over-range annunciator
        # every analyser shows -- baudline counts them outright.  One sample at
        # the rail is not clipping (it is a code that happens to be extreme),
        # so the annunciator applies a threshold, not this count.
        clipped = int(np.count_nonzero(codes <= 0) +
                      np.count_nonzero(codes >= int(full_scale) - 1))

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
        return Spectrum(freqs=freqs, power_db=db, resolution=sample_rate / n,
                        enbw_bins=self._enbw, sample_rate=sample_rate, n=n,
                        window=self.window_name, clipped=clipped)


def reduce_for_display(freqs: np.ndarray, db: np.ndarray, cols: int = 2000,
                       fmin: float | None = None, fmax: float | None = None,
                       detector: str = "+peak"
                       ) -> tuple[np.ndarray, np.ndarray]:
    """Collapse a spectrum to ~cols points for plotting, via a chosen detector.

    At 500k bins across ~2000 columns each column covers ~250 bins, so the
    detector choice here is exactly as meaningful as it is on a bench analyser
    -- it is the same operation, and the same errors follow from it.  The
    default is `+peak`, and two things about it matter:

    1. Take the per-bucket **maximum**, not every Nth bin.  Plain slicing aliases
       narrow tones away entirely, and a spectrum is mostly narrow tones.
    2. Plot each maximum at the frequency it actually occurred at, **not** at the
       start of its bucket.  With a 1 GHz Nyquist and 2000 columns a bucket spans
       500 kHz, so bucket-start x-positions drag a 200 kHz tone onto 0 Hz and its
       600 kHz harmonic onto 500 kHz -- peaks that do not line up with the axis.

    The detectors, and what each one costs you (errors are for Gaussian noise):

    * `+peak`    max of the bucket.  Overstates noise; never use for a level.
    * `-peak`    min of the bucket.  Separates CW from impulsive interference.
    * `sample`   one bin per bucket.  Misses narrow tones outright -- the
                 classic way to lose a comb -- and reads **2.5 dB low** on
                 noise, not the 1.05 dB the feature survey quotes: a single
                 bin's *dB value* carries the same log-of-exponential bias as
                 `avg-log`, and 1.05 dB is the voltage-averaging figure.
                 Measured here at -2.65 dB against a known floor.
    * `rms`      RMS over the bucket's *power*.  The only correct detector for
                 a noise or channel-power number: it needs no crest-factor
                 assumption.
    * `avg-voltage`  mean of linear amplitude.  1.05 dB low; right for AM and
                 pulse envelope shape.
    * `avg-log`  mean of the dB values.  **2.50 dB low** (1.05 + 1.45).  Good
                 for *seeing* a tone near the floor, wrong for any power number.
    * `envelope` both extremes of each bucket, joined by a vertical line
                 (R&S's `Auto Peak`).  Shows the noise band's thickness without
                 lying about either edge.

    The averaging detectors return each point at its bucket's **centre**
    frequency, since an average did not happen at any one bin; the peak and
    sample detectors return the true frequency of the bin they picked.

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
    rows = np.arange(cols)

    def _pick(idx):
        return fv[rows, idx], dv[rows, idx]

    if detector == "-peak":
        out_f, out_d = _pick(dv.argmin(axis=1))
    elif detector == "sample":
        # Middle of the bucket rather than its first bin: taking the first
        # makes the reduction a plain decimation with a systematic bias
        # towards bucket edges.
        out_f, out_d = _pick(np.full(cols, step // 2))
    elif detector in ("rms", "avg-voltage", "avg-log"):
        out_f = fv.mean(axis=1)
        if detector == "avg-log":
            out_d = dv.mean(axis=1)
        else:
            # dB -> linear -> mean -> dB.  The exponent and the factor differ
            # between the power (10) and voltage (20) conventions and mixing
            # them up is a silent 2x error in dB.
            k = 10.0 if detector == "rms" else 20.0
            lin = np.power(10.0, dv.astype(np.float64) / k)
            out_d = k * np.log10(np.maximum(lin.mean(axis=1), 1e-30))
    elif detector == "envelope":
        # Emit max then min at the same x, so the polyline draws a vertical
        # stroke spanning the bucket.  Two points per column, hence the
        # interleave rather than a second curve.
        out_f = np.repeat(fv.mean(axis=1), 2)
        out_d = np.empty(cols * 2, dtype=float)
        out_d[0::2] = dv.max(axis=1)
        out_d[1::2] = dv.min(axis=1)
    else:                                            # "+peak" and anything else
        out_f, out_d = _pick(dv.argmax(axis=1))

    # Don't silently drop the tail -- at 500001 bins that is up to 249 bins, and
    # it is where the highest frequencies live.  The tail is one short bucket,
    # so it gets the same treatment as a full one.
    if trimmed < n:
        tail_f, tail_d = f[trimmed:], d[trimmed:]
        if detector == "-peak":
            j = int(np.argmin(tail_d))
            out_f, out_d = np.append(out_f, tail_f[j]), np.append(out_d, tail_d[j])
        elif detector == "sample":
            j = tail_d.size // 2
            out_f, out_d = np.append(out_f, tail_f[j]), np.append(out_d, tail_d[j])
        elif detector == "avg-log":
            out_f = np.append(out_f, tail_f.mean())
            out_d = np.append(out_d, tail_d.mean())
        elif detector in ("rms", "avg-voltage"):
            k = 10.0 if detector == "rms" else 20.0
            lin = np.power(10.0, tail_d.astype(np.float64) / k).mean()
            out_f = np.append(out_f, tail_f.mean())
            out_d = np.append(out_d, k * np.log10(max(lin, 1e-30)))
        elif detector == "envelope":
            out_f = np.append(out_f, [tail_f.mean()] * 2)
            out_d = np.append(out_d, [tail_d.max(), tail_d.min()])
        else:
            j = int(np.argmax(tail_d))
            out_f, out_d = np.append(out_f, tail_f[j]), np.append(out_d, tail_d[j])

    return out_f, out_d

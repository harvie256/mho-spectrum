#!/usr/bin/env python3
"""Numbers derived from a spectrum: peaks, spurs, exports.

Kept free of Qt so every one of these can be checked headlessly against a known
input, which is the only way any of them can be trusted enough to quote.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass

import math

import numpy as np
from scipy.signal import find_peaks

# Rigol's defaults, which are as good a starting point as any: a peak must
# clear an absolute threshold, and the trace must fall this far between two
# maxima for them to count as separate peaks.  Keysight's phrasing for the
# second one is the clearest: "the change in level that must occur -- in other
# words, hysteresis".
DEFAULT_THRESHOLD_DB = -90.0
DEFAULT_EXCURSION_DB = 10.0


def auto_threshold(db: np.ndarray, margin: float = 5.0) -> float:
    """Where the noise stops, so a peak search is not a noise search.

    A fixed threshold (Rigol defaults to -90 dBm) is a dBm-era convention; with
    no absolute units and a floor that moves with sample rate and record
    length, the useful line is a stated distance above the noise itself.

    For exponentially-distributed noise power the largest of N bins sits about
    `10*log10(ln N)` above the *mean*, and the median is 1.6 dB below the mean,
    so the highest noise bin lands roughly `10*log10(ln N) + 1.6` dB above the
    median: 12.8 dB at N = 500k.  Measured on a synthetic floor: median
    -94.8 dBFS, highest noise peak -82.0, a gap of 12.8 dB.  The margin puts
    the threshold clear of that.

    This is also what keeps the search cheap.  scipy applies the height filter
    before computing prominences, so a threshold below the floor makes every
    noise bin a candidate: measured 12-17 ms per frame at -90 dBFS versus
    2.5 ms above the floor, against an ~8 ms FFT.  A threshold in the noise is
    both wrong and the single most expensive thing this display can do.
    """
    d = db[np.isfinite(db)]
    if d.size < 2:
        return DEFAULT_THRESHOLD_DB
    median = float(np.median(d))
    return median + 10.0 * float(np.log10(np.log(max(d.size, 3)))) + 1.6 + margin


@dataclass
class Peak:
    freq: float
    level: float
    index: int

    def delta(self, other: "Peak") -> tuple[float, float]:
        """(Δfrequency, Δlevel) from `other` to this peak."""
        return self.freq - other.freq, self.level - other.level


def find_spectrum_peaks(freqs: np.ndarray, db: np.ndarray,
                        threshold: float = DEFAULT_THRESHOLD_DB,
                        excursion: float = DEFAULT_EXCURSION_DB,
                        limit: int = 20,
                        exclude_dc_hz: float = 0.0,
                        sort_by: str = "amplitude") -> list[Peak]:
    """Qualifying peaks, strongest first (or by frequency).

    Excursion is implemented as scipy's `prominence`, which is the same
    definition an analyser uses: how far the trace descends from a maximum
    before it climbs to something higher.  Rolling our own hysteresis scan
    would be slower and no more correct.

    `exclude_dc_hz` drops everything below that frequency, which is R&S's
    `Exclude LO` and what Rigol does unconditionally -- on a scope input the DC
    bin and its window skirt are otherwise the loudest thing on screen and win
    every peak search forever.
    """
    lo = int(np.searchsorted(freqs, exclude_dc_hz, side="left")) if exclude_dc_hz else 0
    lo = max(0, min(lo, db.size - 1))
    view = db[lo:]
    if view.size < 3:
        return []

    idx, props = find_peaks(view, height=threshold, prominence=excursion)
    if idx.size == 0:
        return []

    heights = props["peak_heights"]
    # Rank by amplitude to apply the count limit -- a "top 10 peaks" table that
    # truncated by frequency would drop the strongest signal off the bottom.
    order = np.argsort(heights)[::-1][:limit]
    peaks = [Peak(freq=float(freqs[lo + int(idx[i])]),
                  level=float(heights[i]),
                  index=lo + int(idx[i])) for i in order]
    if sort_by == "frequency":
        peaks.sort(key=lambda p: p.freq)
    return peaks


def next_peak(peaks: list[Peak], current: float, direction: str) -> Peak | None:
    """Step between peaks, the `Next Pk Right/Left` / `Next Peak` idiom.

    `direction` is "left"/"right" to move in frequency, or "next" to move to
    the next-strongest peak below the current level.
    """
    if not peaks:
        return None
    if direction == "right":
        later = sorted((p for p in peaks if p.freq > current), key=lambda p: p.freq)
        return later[0] if later else None
    if direction == "left":
        earlier = sorted((p for p in peaks if p.freq < current), key=lambda p: p.freq)
        return earlier[-1] if earlier else None
    # "next": next-highest amplitude strictly below the marker's current level.
    lower = sorted((p for p in peaks if p.level < current), key=lambda p: p.level)
    return lower[-1] if lower else None


def adc_spur_freqs(sample_rate: float, k_max: int = 8) -> list[tuple[int, float]]:
    """The ADC time-interleave spurs, at k x fs/16 below Nyquist.

    These are an artifact of the converter's interleaving, not harmonics of the
    signal: they sit at fixed fractions of fs and they *move when fs moves*,
    which is the test that tells them apart from anything real.  Measured on
    this instrument at about -60 dBFS at fs/8 and fs/4 (docs/STREAMING.md), and
    they are what sets the ~60 dB SFDR floor.  Annotate, never "fix".
    """
    if sample_rate <= 0:
        return []
    step = sample_rate / 16.0
    return [(k, k * step) for k in range(1, k_max + 1) if k * step < sample_rate / 2.0]


# Amplitude units the display can offer, and the reference each is against.
AMP_UNITS = ("dBFS", "dBV", "dBm")
DBM_OHMS = 50.0


def unit_offset_db(unit: str, yinc: float, full_scale: float,
                   ohms: float = DBM_OHMS) -> float:
    """dB to add to a dBFS figure to express it in `unit`.

    The engine normalises so a full-scale sine reads 0 dBFS, which is a sine of
    (full_scale/2) codes peak, i.e. (full_scale/2) * yinc volts peak.  dBV is
    referenced to 1 V *rms*, hence the sqrt(2); dBm to 1 mW in `ohms`.  Both are
    therefore a constant offset, not a per-bin transformation.

    Returns 0.0 for dBFS, and for anything else when yinc is unknown (0) -- the
    tap only learned to send the vertical scale recently, and a source that
    cannot supply it has to stay in dBFS rather than invent absolute numbers.
    """
    if unit == "dBFS" or not yinc or full_scale <= 0:
        return 0.0
    v_rms_at_0dbfs = (full_scale / 2.0) * yinc / math.sqrt(2.0)
    if v_rms_at_0dbfs <= 0:
        return 0.0
    dbv = 20.0 * math.log10(v_rms_at_0dbfs)
    if unit == "dBV":
        return dbv
    if unit == "dBm":
        # P = Vrms^2 / R, referenced to 1 mW: +10*log10(1/(R*1e-3)) dB.
        return dbv + 10.0 * math.log10(1.0 / (ohms * 1e-3))
    return 0.0


def write_trace_csv(path: str, spec, reduced: tuple[np.ndarray, np.ndarray] | None = None,
                    full: bool = True, unit: str = "dBFS") -> int:
    """Write the trace to CSV.  Full resolution by default, and on purpose.

    The display reduction is lossy by design -- that is its whole job -- so an
    export that quietly wrote the reduced curve would be a measurement you
    could not reproduce.  `full=False` writes exactly what is on screen, for
    when the picture is the point.
    """
    if full or reduced is None:
        f, d = spec.freqs, spec.power_db
    else:
        f, d = reduced
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["# window", spec.window, "points", spec.n,
                    "sample_rate_Hz", f"{spec.sample_rate:.6g}"])
        w.writerow(["# bin_spacing_Hz", f"{spec.resolution:.6g}",
                    "rbw_Hz", f"{spec.rbw:.6g}",
                    "enbw_bins", f"{spec.enbw_bins:.6g}"])
        w.writerow(["frequency_Hz", f"power_{unit}"])
        for fi, di in zip(f, d):
            w.writerow([f"{fi:.6f}", f"{di:.4f}"])
    return int(len(f))


def write_traces_csv(path: str, spectra: dict, yinc: dict | None = None,
                     unit: str = "dBFS") -> int:
    """Several channels' traces in one file: one frequency column, then a
    power column per channel, at full resolution.

    A shared frequency column is only honest because the channels come from one
    acquisition -- same record length and sample rate, so the same bins.  That
    is checked rather than assumed: a file whose columns quietly belonged to
    different bins would be worse than no file.  Each channel's volts-per-code
    goes in the header, aligned with its column, because each column was
    converted to `unit` with its own.
    """
    chans = sorted(spectra)
    first = spectra[chans[0]]
    for ch in chans[1:]:
        s = spectra[ch]
        if s.n != first.n or s.sample_rate != first.sample_rate:
            raise ValueError(
                f"CH{ch} is {s.n} pts at {s.sample_rate:g} Sa/s but CH{chans[0]} "
                f"is {first.n} at {first.sample_rate:g}: not one acquisition")
    yinc = yinc or {}
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["# window", first.window, "points", first.n,
                    "sample_rate_Hz", f"{first.sample_rate:.6g}"])
        w.writerow(["# bin_spacing_Hz", f"{first.resolution:.6g}",
                    "rbw_Hz", f"{first.rbw:.6g}",
                    "enbw_bins", f"{first.enbw_bins:.6g}"])
        w.writerow(["# yinc_V_per_code"] + [f"{yinc.get(ch, 0.0):.6g}" for ch in chans])
        w.writerow(["frequency_Hz"] + [f"power_CH{ch}_{unit}" for ch in chans])
        # savetxt rather than a csv row loop: 500k rows x 5 columns is several
        # seconds through the csv module.  \r\n to match the rows above, which
        # is what csv.writer ends lines with.
        cols = np.column_stack([first.freqs] + [spectra[ch].power_db for ch in chans])
        np.savetxt(fh, cols, delimiter=",", newline="\r\n",
                   fmt=["%.6f"] + ["%.4f"] * len(chans))
    return int(first.freqs.size)


def write_peaks_csv(path: str, peaks: list[Peak], spec, offset: float = 0.0,
                    unit: str = "dBFS", channel: int | None = None) -> int:
    """Write the peak table, with the delta columns an analyser shows."""
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["# rbw_Hz", f"{spec.rbw:.6g}", "window", spec.window,
                    "sample_rate_Hz", f"{spec.sample_rate:.6g}"]
                   + (["channel", f"CH{channel}"] if channel is not None else []))
        w.writerow(["n", "frequency_Hz", f"level_{unit}",
                    "delta_freq_Hz", "delta_level_dB"])
        ref = peaks[0] if peaks else None
        for i, p in enumerate(peaks, 1):
            df, dl = p.delta(ref) if ref else (0.0, 0.0)
            w.writerow([i, f"{p.freq:.4f}", f"{p.level + offset:.3f}",
                        f"{df:.4f}", f"{dl:.3f}"])
    return len(peaks)


def format_hz(hz: float, places: int = 4) -> str:
    """Engineering-notation frequency, the way an analyser prints one."""
    a = abs(hz)
    for scale, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if a >= scale:
            return f"{hz / scale:.{places}f} {unit}"
    return f"{hz:.{places}f} Hz"

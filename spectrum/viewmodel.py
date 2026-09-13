#!/usr/bin/env python3
"""The view model: what part of the spectrum is on screen, and how it is scaled.

Deliberately free of Qt.  Every conversion here -- centre/span to start/stop,
reference level and dB/div to a y range, the autoscale heuristic -- is the kind
of arithmetic that is easy to get subtly wrong and impossible to test through a
widget.  The GUI owns widgets; this owns the numbers they display.

Two axes, two models.  `FreqView` is a *view over baseband*, not a retune:
there is no mixer on a scope input, so centre/span here can only ever select a
window of DC..fs/2 (see docs/SPECTRUM_ANALYSER_FEATURES.md).  `AmpScale` is the
instrument-style reference-level/dB-per-division pair that every analyser
presents, replacing what used to be a hardcoded -160..+5 dBFS window.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# A vendor graticule is 10 divisions tall (Rigol, Keysight, Siglent, R&S all
# agree), and dB/div only means anything against a stated division count.
DIVISIONS = 10


@dataclass
class FreqView:
    """The visible frequency window, in Hz, plus the log-x flag.

    Held as (lo, hi) because that is what the plot wants, and exposed as
    centre/span as well because that is what users of an analyser think in.
    Siglent publishes the identity we honour both ways:
        centre = (start + stop) / 2      span = stop - start
    """

    lo: float = 0.0
    hi: float = 1.0
    log_x: bool = False
    nyquist: float = 0.0          # 0 until the first frame sets it

    # -- the two equivalent parameterisations ----------------------------
    @property
    def centre(self) -> float:
        return 0.5 * (self.lo + self.hi)

    @property
    def span(self) -> float:
        return self.hi - self.lo

    def set_start_stop(self, lo: float, hi: float):
        self.lo, self.hi = self._clamp(lo, hi)

    def set_centre_span(self, centre: float, span: float):
        span = max(span, self.min_span)
        self.set_start_stop(centre - span / 2.0, centre + span / 2.0)

    def set_centre(self, centre: float):
        self.set_centre_span(centre, self.span)

    def set_span(self, span: float):
        self.set_centre_span(self.centre, span)

    @property
    def min_span(self) -> float:
        """Never let the span collapse to zero -- 1 Hz is below any real RBW."""
        return 1.0

    def _clamp(self, lo: float, hi: float) -> tuple[float, float]:
        """Keep the window inside DC..Nyquist without changing its width.

        Slide rather than crop: dragging the centre past the edge should stop
        at the edge with the span intact, not silently narrow the view.
        """
        top = self.nyquist or hi
        span = min(max(hi - lo, self.min_span), top if top > 0 else hi - lo)
        if lo < 0.0:
            lo, hi = 0.0, span
        elif top and hi > top:
            lo, hi = top - span, top
        else:
            hi = lo + span
        return max(0.0, lo), hi

    def full_span(self):
        if self.nyquist:
            self.set_start_stop(0.0, self.nyquist)

    def zoom(self, factor: float):
        """Halve or double the span about the centre (Rigol's x1/2 / x2)."""
        self.set_span(self.span * factor)

    # -- log-x plumbing ---------------------------------------------------
    # pyqtgraph's log mode puts the *view* in log10(Hz), so every range set and
    # every range read has to be converted.  Keeping that in one place here is
    # what stops the two directions drifting apart.
    def floor_hz(self, resolution: float) -> float:
        """Lowest frequency a log axis can show: one bin, never zero."""
        return max(resolution, 1.0)

    def to_axis(self, resolution: float) -> tuple[float, float]:
        """(lo, hi) in the units the plot's x axis is currently in."""
        if not self.log_x:
            return self.lo, self.hi
        lo = max(self.lo, self.floor_hz(resolution))
        hi = max(self.hi, lo * 10.0)
        return float(np.log10(lo)), float(np.log10(hi))

    def from_axis(self, x0: float, x1: float) -> tuple[float, float]:
        """Inverse of to_axis: axis units back to Hz."""
        if self.log_x:
            x0, x1 = 10.0 ** x0, 10.0 ** x1
        return max(0.0, x0), x1


@dataclass
class AmpScale:
    """Reference level and dB/div -- the amplitude axis as an analyser states it.

    `ref_level` is the value at the *top* of the graticule, which is the vendor
    convention everywhere (Rigol `Ref Level`, R&S `Reference Level`); the
    bottom follows from dB/div x DIVISIONS.  R&S instead exposes the total
    `Range` with a 100 dB default, which is the same two numbers rearranged.

    `offset` is Rigol's `Ref Offset` / R&S's `Reference Level Offset`: a
    constant added to every amplitude *readout* to account for external gain or
    loss.  It deliberately does not move the trace on screen -- the trace and
    the graticule shift together, so the signal stays where it was.
    """

    ref_level: float = 0.0        # dBFS at the top of the graticule
    db_per_div: float = 10.0
    offset: float = 0.0           # dB added to readouts, not to the picture

    @property
    def bottom(self) -> float:
        return self.ref_level - self.db_per_div * DIVISIONS

    @property
    def range_db(self) -> float:
        return self.db_per_div * DIVISIONS

    def y_range(self) -> tuple[float, float]:
        return self.bottom, self.ref_level

    def set_bottom_top(self, bottom: float, top: float):
        self.ref_level = top
        self.db_per_div = max((top - bottom) / DIVISIONS, 0.1)

    def autoscale(self, db: np.ndarray, headroom: float = 10.0,
                  floor_margin: float = 10.0):
        """Frame the data the way the published SDR heuristic describes it:
        noise floor near the bottom, strongest signal a margin below the top.

        The floor is the 10th percentile rather than the minimum, because a
        single empty bin (or the -300 dB of a zeroed DC bin) would otherwise
        drag the bottom of the graticule into nothing.  The top is the true
        maximum, since that is the one value the user is asking not to clip.
        """
        d = db[np.isfinite(db)]
        if d.size == 0:
            return
        floor = float(np.percentile(d, 10.0))
        peak = float(d.max())
        top = peak + headroom
        bottom = floor - floor_margin
        if top - bottom < 10.0:            # degenerate: a flat or empty trace
            top, bottom = top + 5.0, bottom - 5.0
        # Round dB/div up to 0.5 and the reference to 1 dB, rather than to a
        # 1-2-5 ladder.  A physical graticule with printed per-division values
        # is why bench analysers snap to 1-2-5; here the axis labels are
        # computed, so snapping only wastes screen.  It is not academic: a
        # dBFS floor near -120 with a peak near 0 needs ~13.6 dB/div, and the
        # next 1-2-5 step up is 20 -- a 200 dB graticule for 135 dB of signal,
        # with a third of the plot empty.  Rounding still keeps the readout
        # steady frame to frame, which is the half of it worth having.
        self.db_per_div = max(0.1, np.ceil((top - bottom) / DIVISIONS * 2) / 2)
        self.ref_level = float(np.ceil(top))

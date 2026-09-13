#!/usr/bin/env python3
"""The plotting panes: the spectrum trace, and the frame-interval strip.

Split out of fft_gui.run_gui so that the widgets, the numbers they draw and the
frame loop that drives them stop sharing one 475-line closure.  Each pane owns
its own pyqtgraph items and exposes a small method surface; nothing in here
polls a source or touches the SpectrumEngine.
"""
from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore

from spectrum import reduce_for_display
from viewmodel import FreqView

# The scope's own channel colours, so a trace here reads as the same channel
# on the instrument's screen.  One channel keeps the original blue.
CHANNEL_COLOURS = {1: "#e8e020", 2: "#20d0e8", 3: "#e040e0", 4: "#4a80ff"}
SINGLE_COLOUR = "#1f9bd1"


class TimedPlotWidget(pg.PlotWidget):
    """A PlotWidget that adds up the time Qt spends repainting it.

    The repaint happens *after* the timer callback returns, so it is invisible
    to any stopwatch inside tick() -- yet it runs on the same thread and is
    therefore inside the frame interval.  Left unmeasured it would show up as
    unexplained idle time, which is exactly the bucket we are trying to keep
    meaningful.
    """

    paint_s = 0.0                      # class-wide: totals every plot

    def paintEvent(self, ev):
        t0 = time.perf_counter()
        try:
            super().paintEvent(ev)
        finally:
            TimedPlotWidget.paint_s += time.perf_counter() - t0


class SpectrumPlot(QtCore.QObject):
    """The main trace pane: grid, curve, and the reduction that feeds it.

    Owns the mapping between the FreqView (Hz) and pyqtgraph's x axis (which is
    log10(Hz) in log mode), because keeping the two conversion directions in
    one place is what stops them drifting apart.
    """

    # Emitted when the user pans or zooms the plot itself, so the frequency
    # entry boxes can follow the mouse rather than fight it.
    view_changed = QtCore.Signal()

    def __init__(self, view: FreqView, display_bins: int = 2000):
        super().__init__()
        self.view = view
        self.display_bins = display_bins
        self.detector = "+peak"
        self._spec = None
        self._specs: dict = {}         # channel -> Spectrum, all drawn
        self._curves: dict = {}        # channel -> curve, once there are several
        self._legend = None
        self._syncing = False          # guards the view <-> viewbox round trip
        # Reduction time, accumulated across every call in a frame and drained
        # by the frame loop.  It is counted here rather than at the call site
        # because a user zoom re-enters redraw() through sigXRangeChanged, and
        # that cost belongs to whichever frame interval it lands in just as
        # much as the per-frame redraw does.
        self.draw_s = 0.0

        self.plot = TimedPlotWidget()
        self.plot.setLabel("bottom", "Frequency", units="Hz")
        self.plot.setLabel("left", "Power", units="dBFS")
        self._unit = "dBFS"
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.curve = self.plot.plot(pen=pg.mkPen(SINGLE_COLOUR, width=1))

        self.plot.getViewBox().sigXRangeChanged.connect(self._on_x_range)

    # -- axis plumbing ----------------------------------------------------
    def resolution(self) -> float:
        return self._spec.resolution if self._spec is not None else 1.0

    def apply_view(self):
        """Push the FreqView onto the plot's x axis."""
        lo, hi = self.view.to_axis(self.resolution())
        self._syncing = True
        try:
            self.plot.setXRange(lo, hi, padding=0.0)
        finally:
            self._syncing = False
        self.redraw()

    def _on_x_range(self, *_):
        """The user panned or zoomed: adopt it as the view and re-reduce.

        Re-reducing here rather than only on a new frame is what makes zooming
        show full bin resolution instead of magnifying coarse buckets.
        """
        (x0, x1), _ = self.plot.getViewBox().viewRange()
        self.view.lo, self.view.hi = self.view.from_axis(x0, x1)
        self.redraw()
        if not self._syncing:
            self.view_changed.emit()

    def set_log_x(self, on: bool):
        self.view.log_x = bool(on)
        self.plot.setLogMode(x=bool(on), y=False)
        self.apply_view()

    def set_y_range(self, bottom: float, top: float):
        self.plot.setYRange(bottom, top, padding=0.0)

    # -- data -------------------------------------------------------------
    def set_spectrum(self, spec):
        self.set_spectra({1: spec} if spec is not None else {})

    def set_spectra(self, spectra: dict):
        """Draw these spectra, keyed by channel.  One channel uses the single
        curve; several get a curve each in the channel colours, and a legend."""
        self._specs = dict(spectra)
        self._spec = next(iter(self._specs.values()), None)
        multi = len(self._specs) > 1
        self.curve.setVisible(not multi)
        if multi and self._legend is None:
            self._legend = self.plot.addLegend(offset=(-10, 10))
        for ch in list(self._curves):
            if not multi or ch not in self._specs:
                if self._legend is not None:
                    self._legend.removeItem(self._curves[ch])
                self.plot.removeItem(self._curves.pop(ch))
        if multi:
            for ch in self._specs:
                if ch not in self._curves:
                    self._curves[ch] = self.plot.plot(
                        pen=pg.mkPen(CHANNEL_COLOURS.get(ch, "#c0c0c0"), width=1),
                        name=f"CH{ch}")
        if self._legend is not None:
            self._legend.setVisible(multi)

    def redraw(self):
        t0 = time.perf_counter()
        try:
            self._redraw()
        finally:
            self.draw_s += time.perf_counter() - t0

    def take_draw_ms(self) -> float:
        """Read and reset the accumulated reduction time, in ms."""
        ms = self.draw_s * 1e3
        self.draw_s = 0.0
        return ms

    def set_unit_label(self, unit: str):
        """Relabel the amplitude axis.  The data is shifted by the caller."""
        self._unit = unit
        self.plot.setLabel("left", "Power", units=unit)

    def _redraw(self):
        if not self._specs:
            return
        for ch, spec in self._specs.items():
            f, d = reduce_for_display(spec.freqs, spec.power_db, self.display_bins,
                                      fmin=self.view.lo, fmax=self.view.hi,
                                      detector=self.detector)
            if self.view.log_x:
                keep = f > 0          # log-x cannot show DC
                f, d = f[keep], d[keep]
            (self._curves.get(ch) or self.curve).setData(f, d)


class TimingStrip:
    """The frame-interval strip under the spectrum.

    A stall is a tail event: it never shows in the fps number, but it is
    unmistakable as a spike here.  Wall interval and the GUI thread's CPU time
    over the same interval are drawn together on purpose -- a spike with CPU
    following it is work, a spike with CPU flat along the bottom is the thread
    not running at all.
    """

    def __init__(self, span: int, visible: bool = True):
        from collections import deque

        self.iv = deque(maxlen=span)
        self.cpu = deque(maxlen=span)
        self.src = deque(maxlen=span)

        w = TimedPlotWidget()
        w.setMaximumHeight(150)
        w.setLabel("left", "frame time, ms")
        w.setLabel("bottom", "frames ago")
        w.showGrid(x=False, y=True, alpha=0.3)
        w.setMouseEnabled(x=False, y=False)
        w.hideButtons()
        # Log y, because that is the shape of the problem: a 2 s stall next to
        # a 90 ms frame flattens a linear axis into a baseline and a spike, and
        # the baseline is where the ordinary jitter lives.
        w.setLogMode(x=False, y=True)
        w.addLegend(offset=(70, 4), labelTextSize="8pt",
                    horSpacing=12, verSpacing=-4)
        self.iv_curve = w.plot(pen=pg.mkPen("#e0b040", width=1), name="interval")
        self.cpu_curve = w.plot(pen=pg.mkPen("#48a860", width=1), name="GUI cpu")
        self.src_curve = w.plot(
            pen=pg.mkPen("#8060c0", width=1, style=QtCore.Qt.PenStyle.DashLine),
            name="source gap")
        self.limit_line = pg.InfiniteLine(
            angle=0, pen=pg.mkPen("#c04040", width=1,
                                  style=QtCore.Qt.PenStyle.DashLine))
        w.addItem(self.limit_line)
        w.setVisible(visible)
        self.widget = w

    def is_visible(self) -> bool:
        return self.widget.isVisible()

    def set_visible(self, on: bool):
        self.widget.setVisible(bool(on))

    def append(self, interval_ms: float, cpu_ms: float, src_gap_ms: float):
        self.iv.append(interval_ms)
        self.cpu.append(cpu_ms)
        self.src.append(src_gap_ms)

    def redraw(self, stall_limit_ms: float):
        n = len(self.iv)
        if not n:
            return
        x = np.arange(-n + 1, 1)

        # Log mode cannot plot a zero, and both cpu_ms and the first frame's
        # source gap legitimately reach it.  Clamp at 1 ms rather than at
        # epsilon: a single 0 would otherwise stretch the axis over three
        # decades of nothing and squash the band that matters.
        def _pos(seq):
            return np.maximum(np.fromiter(seq, float, n), 1.0)

        self.iv_curve.setData(x, _pos(self.iv))
        self.cpu_curve.setData(x, _pos(self.cpu))
        self.src_curve.setData(x, _pos(self.src))
        self.limit_line.setValue(np.log10(max(stall_limit_ms, 1.0)))

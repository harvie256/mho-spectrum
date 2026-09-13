#!/usr/bin/env python3
"""Markers: placeable readouts on the trace, and the deltas between them.

The taxonomy is Rigol's, because it is the most completely specified one:
a *normal* marker reads absolute frequency and amplitude, and a *delta* marker
reads the difference from a reference whose own readout stays absolute
(Tektronix states that rule explicitly, and it is the one people get wrong).

Placement snaps to the nearest FFT bin and then reports that bin's true
frequency, not the mouse's -- a marker that reported where you clicked rather
than what you clicked on would be a worse readout than the status bar it
replaces.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore

from analysis import format_hz

MAX_MARKERS = 8            # Rigol's marker table is 8 rows; so is ours

# Distinct enough to tell apart on a dark background at 1 px, and stable per
# marker number so a marker keeps its colour as others come and go.
MARKER_COLOURS = ("#f0c040", "#40d0a0", "#e06060", "#a080e0",
                  "#60b0e0", "#d0d060", "#e090c0", "#80d060")


@dataclass
class Marker:
    number: int
    freq: float = 0.0
    level: float = 0.0
    index: int = 0
    delta: bool = False            # read against the reference marker
    active: bool = True
    _items: list = field(default_factory=list, repr=False)

    @property
    def colour(self) -> str:
        return MARKER_COLOURS[(self.number - 1) % len(MARKER_COLOURS)]

    def delta_from(self, other: "Marker") -> tuple[float, float]:
        return self.freq - other.freq, self.level - other.level


class MarkerSet(QtCore.QObject):
    """Every marker on the plot, plus the items that draw them.

    Owns its pyqtgraph items so the plot pane does not have to know markers
    exist beyond handing over a ViewBox to draw into.
    """

    changed = QtCore.Signal()

    def __init__(self, plot: pg.PlotWidget):
        super().__init__()
        self.plot = plot
        self.markers: list[Marker] = []
        self.reference: Marker | None = None      # what deltas are measured from
        self.selected: Marker | None = None
        self._log_x = False
        # Only the label.  Levels arrive already in these units (the window
        # shifts power_db before markers read it), so nothing is converted here.
        self.unit = "dBFS"

    # -- lifecycle --------------------------------------------------------
    def add(self, freq: float, level: float, index: int,
            delta: bool = False) -> Marker | None:
        if len(self.markers) >= MAX_MARKERS:
            return None
        n = next(i for i in range(1, MAX_MARKERS + 1)
                 if all(m.number != i for m in self.markers))
        m = Marker(number=n, freq=freq, level=level, index=index, delta=delta)
        self.markers.append(m)
        self.markers.sort(key=lambda m: m.number)
        if self.reference is None:
            self.reference = m
        self.selected = m
        self._build(m)
        self.changed.emit()
        return m

    def remove(self, m: Marker):
        for it in m._items:
            self.plot.removeItem(it)
        m._items.clear()
        self.markers.remove(m)
        if self.reference is m:
            self.reference = self.markers[0] if self.markers else None
        if self.selected is m:
            self.selected = self.markers[-1] if self.markers else None
        self.changed.emit()

    def clear(self):
        for m in list(self.markers):
            self.remove(m)

    # -- movement ---------------------------------------------------------
    def move_to(self, m: Marker, freqs: np.ndarray, db: np.ndarray, freq: float):
        """Snap a marker to the bin nearest `freq` and read its true values."""
        i = int(np.clip(np.searchsorted(freqs, freq), 0, db.size - 1))
        # searchsorted lands on the bin above; take whichever neighbour is
        # actually closer, or a click just left of a peak reads the wrong bin.
        if i > 0 and abs(freqs[i - 1] - freq) < abs(freqs[i] - freq):
            i -= 1
        m.index, m.freq, m.level = i, float(freqs[i]), float(db[i])
        self._place(m)
        self.changed.emit()

    def refresh(self, freqs: np.ndarray, db: np.ndarray):
        """Re-read every marker's level from a new frame, holding frequency.

        A marker is pinned in frequency, not in amplitude: the whole point is
        to watch one frequency's level change frame to frame.
        """
        if db.size == 0:
            return
        for m in self.markers:
            if m.index < db.size:
                m.level = float(db[m.index])
                self._place(m)
        if self.markers:
            self.changed.emit()

    def set_unit(self, unit: str):
        self.unit = unit
        for m in self.markers:
            self._place(m)
        self.changed.emit()

    def set_log_x(self, on: bool):
        self._log_x = bool(on)
        for m in self.markers:
            self._place(m)

    # -- drawing ----------------------------------------------------------
    def _x(self, freq: float) -> float:
        """Marker x in the axis's own units (log10 Hz when log-x is on)."""
        if self._log_x:
            return float(np.log10(max(freq, 1e-9)))
        return freq

    def _build(self, m: Marker):
        pen = pg.mkPen(m.colour, width=1)
        dot = pg.ScatterPlotItem(size=9, symbol="d", pen=pen,
                                 brush=pg.mkBrush(m.colour))
        dot.setZValue(20)
        label = pg.TextItem(color=m.colour, anchor=(0.5, 1.2))
        label.setZValue(21)
        self.plot.addItem(dot)
        self.plot.addItem(label)
        m._items = [dot, label]
        self._place(m)

    def _place(self, m: Marker):
        if not m._items:
            return
        dot, label = m._items
        x = self._x(m.freq)
        dot.setData([x], [m.level])
        label.setText(self.text_for(m))
        label.setPos(x, m.level)

    def text_for(self, m: Marker) -> str:
        """The marker's own on-plot label."""
        if m.delta and self.reference is not None and self.reference is not m:
            df, dl = m.delta_from(self.reference)
            return f"Δ{m.number}  {format_hz(df, 3)}  {dl:+.2f} dB"
        return f"{m.number}  {format_hz(m.freq, 4)}  {m.level:.2f} {self.unit}"

    # -- table ------------------------------------------------------------
    def rows(self, offset: float = 0.0) -> list[tuple]:
        """(number, type, frequency, level) per marker, for the marker table.

        Delta rows carry the difference; the reference marker's own row stays
        absolute regardless, which is Tektronix's stated rule.
        """
        out = []
        for m in self.markers:
            if m.delta and self.reference is not None and self.reference is not m:
                df, dl = m.delta_from(self.reference)
                out.append((m.number, f"Δ{self.reference.number}",
                            format_hz(df, 4), f"{dl:+.2f} dB"))
            else:
                tag = "ref" if m is self.reference else "normal"
                out.append((m.number, tag, format_hz(m.freq, 4),
                            f"{m.level + offset:.2f} {self.unit}"))
        return out

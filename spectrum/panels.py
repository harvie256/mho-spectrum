#!/usr/bin/env python3
"""The control panels, grouped the way an analyser's soft keys are.

FREQ / AMPT / BW / TRACE / MARKER is the mental model every user of a spectrum
analyser already has, and the survey recommends adopting it once there are more
than a dozen controls -- Phase 1 alone takes the count past thirty.  A tab bar
is the Qt analogue: it costs one row of chrome instead of a growing toolbar,
and it keeps the plot the biggest thing on screen.

Each panel owns its widgets and emits plain signals.  None of them holds a
reference to the engine, the source or the plot, so what a control *does* stays
in one place (SpectrumWindow) rather than being spread across the closures the
old single-function GUI used.
"""
from __future__ import annotations

import numpy as np
from pyqtgraph.Qt import QtCore, QtWidgets

from analysis import AMP_UNITS, DBM_OHMS
from spectrum import DETECTORS, WINDOWS

_SUFFIXES = {"": 1.0, "h": 1.0, "k": 1e3, "khz": 1e3, "m": 1e6, "mhz": 1e6,
             "g": 1e9, "ghz": 1e9, "hz": 1.0}


def parse_hz(text: str) -> float | None:
    """Parse '12.5M', '250 kHz', '1e6' into Hz.

    Typing the suffix is how every analyser's numeric entry works, and it beats
    a separate units dropdown: at a 1 GHz Nyquist the useful entries span nine
    decades and picking the unit first is one interaction too many.
    """
    s = text.strip().lower().replace(",", "").replace(" ", "")
    if not s:
        return None
    for n in range(len(s), 0, -1):
        head, tail = s[:n], s[n:]
        if tail in _SUFFIXES:
            try:
                return float(head) * _SUFFIXES[tail]
            except ValueError:
                continue
    return None


class FreqEdit(QtWidgets.QLineEdit):
    """A frequency box that accepts engineering suffixes and prints them back."""

    committed = QtCore.Signal(float)

    def __init__(self, width: int = 108):
        super().__init__()
        self.setFixedWidth(width)
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self._value = 0.0
        self.editingFinished.connect(self._commit)

    def value(self) -> float:
        return self._value

    def set_value(self, hz: float):
        """Set without emitting -- for following the plot, not driving it."""
        self._value = float(hz)
        self.setText(self._format(self._value))

    def _commit(self):
        hz = parse_hz(self.text())
        if hz is None:
            self.setText(self._format(self._value))     # reject, don't guess
            return
        self._value = hz
        self.setText(self._format(hz))
        self.committed.emit(hz)

    @staticmethod
    def _format(hz: float) -> str:
        a = abs(hz)
        for scale, unit in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
            if a >= scale:
                return f"{hz / scale:.6g} {unit}"
        return f"{hz:.6g}"


def _row(*widgets) -> QtWidgets.QWidget:
    """Pack widgets into a left-aligned row; bare strings become labels."""
    w = QtWidgets.QWidget()
    lay = QtWidgets.QHBoxLayout(w)
    lay.setContentsMargins(6, 3, 6, 3)
    lay.setSpacing(6)
    for item in widgets:
        lay.addWidget(QtWidgets.QLabel(item) if isinstance(item, str) else item)
    lay.addStretch(1)
    return w


class FreqPanel(QtWidgets.QWidget):
    """Centre/span and start/stop, kept in sync, plus the span gestures.

    Both parameterisations are shown at once rather than behind a toggle: they
    are the same two numbers, users think in whichever suits the task, and
    Keysight exposes them interconvertibly for exactly that reason.
    """

    centre_span_set = QtCore.Signal(float, float)
    start_stop_set = QtCore.Signal(float, float)
    full_span = QtCore.Signal()
    zoom = QtCore.Signal(float)
    zoom_signal = QtCore.Signal()
    log_x = QtCore.Signal(bool)

    def __init__(self, log_default: bool = False):
        super().__init__()
        self.centre = FreqEdit()
        self.span = FreqEdit()
        self.start = FreqEdit()
        self.stop = FreqEdit()
        self.full_btn = QtWidgets.QPushButton("full span")
        self.in_btn = QtWidgets.QPushButton("zoom in")
        self.out_btn = QtWidgets.QPushButton("zoom out")
        self.sig_btn = QtWidgets.QPushButton("zoom to signal")
        self.logx_chk = QtWidgets.QCheckBox("log freq")
        self.logx_chk.setChecked(log_default)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(_row("centre", self.centre, "span", self.span,
                           "start", self.start, "stop", self.stop))
        lay.addWidget(_row(self.full_btn, self.in_btn, self.out_btn,
                           self.sig_btn, self.logx_chk))

        self.centre.committed.connect(
            lambda hz: self.centre_span_set.emit(hz, self.span.value()))
        self.span.committed.connect(
            lambda hz: self.centre_span_set.emit(self.centre.value(), hz))
        self.start.committed.connect(
            lambda hz: self.start_stop_set.emit(hz, self.stop.value()))
        self.stop.committed.connect(
            lambda hz: self.start_stop_set.emit(self.start.value(), hz))
        self.full_btn.clicked.connect(self.full_span.emit)
        # Rigol's x1/2 and x2 -- halve or double the span about the centre.
        self.in_btn.clicked.connect(lambda: self.zoom.emit(0.5))
        self.out_btn.clicked.connect(lambda: self.zoom.emit(2.0))
        self.sig_btn.clicked.connect(self.zoom_signal.emit)
        self.logx_chk.toggled.connect(self.log_x.emit)

    def show_view(self, view):
        """Follow the view without emitting -- the plot is driving here."""
        self.centre.set_value(view.centre)
        self.span.set_value(view.span)
        self.start.set_value(view.lo)
        self.stop.set_value(view.hi)


class AmpPanel(QtWidgets.QWidget):
    """Reference level, dB/div and auto-scale -- replacing a hardcoded window.

    The old display pinned the y axis to -160..+5 dBFS regardless of what was
    in the record, which wasted most of the graticule on empty space below the
    noise floor.
    """

    scale_changed = QtCore.Signal()
    autoscale = QtCore.Signal()
    autoscale_mode = QtCore.Signal(bool)

    def __init__(self, scale, auto_default: bool = True):
        super().__init__()
        self.scale = scale
        self.ref = QtWidgets.QDoubleSpinBox()
        self.ref.setRange(-200.0, 200.0)
        self.ref.setDecimals(1)
        self.ref.setSingleStep(5.0)
        self.ref.setSuffix(" dBFS")
        self.ref.setValue(scale.ref_level)

        # A spinbox rather than the vendor 0.1/0.2/0.5/1/2/5/10/20 preset list:
        # autoscale lands on values that list cannot express (a -120 dBFS floor
        # under a 0 dBFS peak needs ~13.6), and a control that silently refused
        # to show the scale actually in use would be worse than no preset.
        self.div = QtWidgets.QDoubleSpinBox()
        self.div.setRange(0.1, 50.0)
        self.div.setDecimals(1)
        self.div.setSingleStep(0.5)
        self.div.setSuffix(" dB/div")
        self.div.setValue(scale.db_per_div)

        self.offset = QtWidgets.QDoubleSpinBox()
        self.offset.setRange(-300.0, 300.0)
        self.offset.setDecimals(1)
        self.offset.setSuffix(" dB")
        self.offset.setToolTip(
            "Reference level offset: added to every amplitude readout to "
            "account for external gain or loss. The trace does not move.")

        self.auto_btn = QtWidgets.QPushButton("auto scale")
        self.auto_chk = QtWidgets.QCheckBox("keep auto")
        self.auto_chk.setChecked(auto_default)
        self.auto_chk.setToolTip("Re-scale on every frame rather than once.")

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(_row("ref level", self.ref, "scale", self.div,
                           "ref offset", self.offset))
        lay.addWidget(_row(self.auto_btn, self.auto_chk))

        self.ref.valueChanged.connect(self._pull)
        self.div.valueChanged.connect(self._pull)
        self.offset.valueChanged.connect(self._pull)
        self.auto_btn.clicked.connect(self.autoscale.emit)
        self.auto_chk.toggled.connect(self.autoscale_mode.emit)

    def _pull(self, *_):
        self.scale.ref_level = self.ref.value()
        self.scale.db_per_div = float(self.div.value())
        self.scale.offset = self.offset.value()
        self.scale_changed.emit()

    def set_unit(self, unit: str):
        self.ref.setSuffix(f" {unit}")

    def show_scale(self):
        """Follow the model after an autoscale, without re-emitting."""
        for w in (self.ref, self.div, self.offset):
            w.blockSignals(True)
        self.ref.setValue(self.scale.ref_level)
        self.div.setValue(self.scale.db_per_div)
        self.offset.setValue(self.scale.offset)
        for w in (self.ref, self.div, self.offset):
            w.blockSignals(False)


class BwPanel(QtWidgets.QWidget):
    """Window, detector and averaging -- everything that sets what a bin means."""

    window_changed = QtCore.Signal(str)
    detector_changed = QtCore.Signal(str)
    average_changed = QtCore.Signal(int)
    peak_hold_changed = QtCore.Signal(bool)
    reset = QtCore.Signal()

    def __init__(self, window: str, average: int, detector: str = "+peak"):
        super().__init__()
        self.win_box = QtWidgets.QComboBox()
        self.win_box.addItems(WINDOWS)
        self.win_box.setCurrentText(window)

        self.det_box = QtWidgets.QComboBox()
        self.det_box.addItems(DETECTORS)
        self.det_box.setCurrentText(detector)
        self.det_box.setToolTip(
            "Bin-to-pixel reduction. RMS is the only correct choice for a "
            "noise or channel-power number; +peak overstates noise, and "
            "sample and avg-log read ~2.5 dB low on it.")

        self.avg_box = QtWidgets.QSpinBox()
        self.avg_box.setRange(1, 64)
        self.avg_box.setValue(average)
        self.peak_chk = QtWidgets.QCheckBox("peak hold")
        self.reset_btn = QtWidgets.QPushButton("reset avg/peak")

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(_row("window", self.win_box, "detector", self.det_box))
        lay.addWidget(_row("average", self.avg_box, self.peak_chk,
                           self.reset_btn))

        self.win_box.currentTextChanged.connect(self.window_changed.emit)
        self.det_box.currentTextChanged.connect(self.detector_changed.emit)
        self.avg_box.valueChanged.connect(self.average_changed.emit)
        self.peak_chk.toggled.connect(self.peak_hold_changed.emit)
        self.reset_btn.clicked.connect(self.reset.emit)


class MarkerPanel(QtWidgets.QWidget):
    """Marker placement and the peak-search family.

    Threshold and excursion live here rather than with the peak table because
    they govern both: `Next Peak` and the table are the same qualifying-peak
    list presented two ways, and having two sets of criteria would be a bug
    waiting to be filed.
    """

    peak_search = QtCore.Signal()
    step_peak = QtCore.Signal(str)
    add_delta = QtCore.Signal()
    marker_to_centre = QtCore.Signal()
    clear = QtCore.Signal()
    criteria_changed = QtCore.Signal()

    def __init__(self, threshold: float, excursion: float):
        super().__init__()
        self.peak_btn = QtWidgets.QPushButton("peak search")
        self.left_btn = QtWidgets.QPushButton("◀ peak")
        self.right_btn = QtWidgets.QPushButton("peak ▶")
        self.next_btn = QtWidgets.QPushButton("next peak")
        self.delta_btn = QtWidgets.QPushButton("delta")
        self.to_cf_btn = QtWidgets.QPushButton("mkr → centre")
        self.clear_btn = QtWidgets.QPushButton("clear markers")

        self.thresh = QtWidgets.QDoubleSpinBox()
        self.thresh.setRange(-200.0, 50.0)
        self.thresh.setDecimals(1)
        self.thresh.setSuffix(" dBFS")
        self.thresh.setValue(threshold)
        self.thresh.setToolTip("A maximum below this does not count as a peak.")
        self.auto_thresh = QtWidgets.QCheckBox("auto")
        self.auto_thresh.setChecked(True)
        self.auto_thresh.setToolTip(
            "Track the noise floor instead of pinning an absolute level. "
            "A threshold below the floor makes every noise bin a candidate "
            "peak, which costs 12-17 ms a frame against an ~8 ms FFT.")
        self.auto_thresh.toggled.connect(self.thresh.setDisabled)
        self.thresh.setDisabled(True)

        self.excursion = QtWidgets.QDoubleSpinBox()
        self.excursion.setRange(0.0, 80.0)
        self.excursion.setDecimals(1)
        self.excursion.setSuffix(" dB")
        self.excursion.setValue(excursion)
        self.excursion.setToolTip(
            "Peak excursion: how far the trace must fall between two maxima "
            "for them to count as separate peaks (hysteresis).")

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(_row(self.peak_btn, self.left_btn, self.right_btn,
                           self.next_btn, self.delta_btn, self.to_cf_btn,
                           self.clear_btn))
        lay.addWidget(_row("threshold", self.thresh, self.auto_thresh,
                           "excursion", self.excursion,
                           QtWidgets.QLabel("   shift+click the plot to place "
                                            "a marker, right-click to remove")))

        self.peak_btn.clicked.connect(self.peak_search.emit)
        self.left_btn.clicked.connect(lambda: self.step_peak.emit("left"))
        self.right_btn.clicked.connect(lambda: self.step_peak.emit("right"))
        self.next_btn.clicked.connect(lambda: self.step_peak.emit("next"))
        self.delta_btn.clicked.connect(self.add_delta.emit)
        self.to_cf_btn.clicked.connect(self.marker_to_centre.emit)
        self.clear_btn.clicked.connect(self.clear.emit)
        self.thresh.valueChanged.connect(lambda *_: self.criteria_changed.emit())

    def set_unit(self, unit: str, delta_db: float):
        """Relabel the threshold, and move a manual one with the trace.

        Peaks are searched on the already-shifted trace, so a pinned threshold
        left at its old number would suddenly sit delta_db higher or lower
        against the signal.  An auto threshold is recomputed anyway."""
        self.thresh.setSuffix(f" {unit}")
        if not self.auto_thresh.isChecked():
            self.thresh.blockSignals(True)
            self.thresh.setValue(self.thresh.value() + delta_db)
            self.thresh.blockSignals(False)
        self.excursion.valueChanged.connect(lambda *_: self.criteria_changed.emit())
        self.auto_thresh.toggled.connect(lambda *_: self.criteria_changed.emit())


class ViewPanel(QtWidgets.QWidget):
    """Everything that is about the instrument rather than the measurement."""

    capture_dc = QtCore.Signal()
    clear_dc = QtCore.Signal()
    timing_strip = QtCore.Signal(bool)
    spurs = QtCore.Signal(bool)
    tables = QtCore.Signal(bool)
    freeze = QtCore.Signal(bool)
    screenshot = QtCore.Signal()
    export_trace = QtCore.Signal()
    export_peaks = QtCore.Signal()
    amp_unit = QtCore.Signal(str)

    def show_unit(self, unit: str):
        """Follow the model without re-emitting -- the unit can change without
        the user touching the box, when a source cannot supply a vertical
        scale and the display falls back to dBFS."""
        i = self.unit_box.findData(unit)
        if i < 0:
            return
        self.unit_box.blockSignals(True)
        self.unit_box.setCurrentIndex(i)
        self.unit_box.blockSignals(False)

    def __init__(self, timing_default: bool = True):
        super().__init__()
        self.dc_btn = QtWidgets.QPushButton("capture DC")
        self.dc_clear_btn = QtWidgets.QPushButton("clear DC")
        self.timing_chk = QtWidgets.QCheckBox("frame timing")
        self.timing_chk.setChecked(timing_default)
        self.spur_chk = QtWidgets.QCheckBox("ADC spur marks")
        self.spur_chk.setChecked(True)
        self.spur_chk.setToolTip(
            "Mark k·fs/16, where this ADC's time-interleave spurs land. They "
            "are converter artifacts, not harmonics: they move when the "
            "sample rate moves.")
        self.tables_chk = QtWidgets.QCheckBox("tables")
        self.tables_chk.setChecked(True)
        self.freeze_chk = QtWidgets.QCheckBox("freeze")
        self.freeze_chk.setToolTip(
            "Stop updating but keep the display interactive, so a frame can "
            "be zoomed and measured at full bin resolution.")
        self.unit_box = QtWidgets.QComboBox()
        for u in AMP_UNITS:
            self.unit_box.addItem(u if u != "dBm" else f"dBm ({int(DBM_OHMS)}\u03a9)", u)
        self.unit_box.setToolTip(
            "Amplitude units.  dBFS is always available; dBV and dBm need the "
            "scope's volts-per-code, which the tap sends once at startup. "
            "dBm assumes the signal is developed across "
            f"{int(DBM_OHMS)} ohms -- it is a conversion, not a measurement of "
            "delivered power, and is wrong if the input is not so terminated.")
        self.shot_btn = QtWidgets.QPushButton("screenshot")
        self.trace_btn = QtWidgets.QPushButton("export trace CSV")
        self.peaks_btn = QtWidgets.QPushButton("export peaks CSV")

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(_row(self.dc_btn, self.dc_clear_btn, self.freeze_chk,
                           self.timing_chk, self.spur_chk, self.tables_chk))
        lay.addWidget(_row(QtWidgets.QLabel("units"), self.unit_box,
                           self.shot_btn, self.trace_btn, self.peaks_btn))

        self.dc_btn.clicked.connect(self.capture_dc.emit)
        self.dc_clear_btn.clicked.connect(self.clear_dc.emit)
        self.timing_chk.toggled.connect(self.timing_strip.emit)
        self.spur_chk.toggled.connect(self.spurs.emit)
        self.tables_chk.toggled.connect(self.tables.emit)
        self.freeze_chk.toggled.connect(self.freeze.emit)
        self.unit_box.currentIndexChanged.connect(
            lambda _i: self.amp_unit.emit(self.unit_box.currentData()))
        self.shot_btn.clicked.connect(self.screenshot.emit)
        self.trace_btn.clicked.connect(self.export_trace.emit)
        self.peaks_btn.clicked.connect(self.export_peaks.emit)


class TablePane(QtWidgets.QTabWidget):
    """Marker table and peak table, side by side with the trace.

    R&S auto-shows the marker table above two active markers, which is a good
    default and the one adopted here.
    """

    peak_clicked = QtCore.Signal(float)

    def __init__(self):
        super().__init__()
        self.markers = self._table(("#", "type", "frequency", "level"))
        self.peaks = self._table(("#", "frequency", "level", "Δf", "Δ level"))
        self.set_unit("dBFS")
        self.addTab(self.markers, "markers")
        self.addTab(self.peaks, "peaks")
        self.setMinimumWidth(330)
        self._peak_freqs: list[float] = []
        self.peaks.cellClicked.connect(self._on_peak_click)

    @staticmethod
    def _table(headers) -> QtWidgets.QTableWidget:
        t = QtWidgets.QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(list(headers))
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        t.horizontalHeader().setStretchLastSection(True)
        t.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.Stretch)
        # The number column never needs more than three digits, and letting it
        # take an equal share of a 330 px pane squeezes the frequency out.
        t.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        t.setAlternatingRowColors(True)
        t.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return t

    @staticmethod
    def _fill(table: QtWidgets.QTableWidget, rows):
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                item = QtWidgets.QTableWidgetItem(str(val))
                if c:
                    item.setTextAlignment(
                        QtCore.Qt.AlignmentFlag.AlignRight
                        | QtCore.Qt.AlignmentFlag.AlignVCenter)
                table.setItem(r, c, item)

    def set_unit(self, unit: str):
        # Peak rows are bare numbers, so the unit lives in the header.  The
        # deltas say what they are relative to: a bare "Δf" beside "frequency"
        # read as a negative frequency for every peak below the strongest.
        self.peaks.setHorizontalHeaderLabels(
            ["#", "frequency", f"level ({unit})", "Δf vs #1", "Δ level vs #1"])

    def show_markers(self, rows):
        self._fill(self.markers, rows)
        self.setTabText(0, f"markers ({len(rows)})" if rows else "markers")

    def show_peaks(self, peaks, offset: float = 0.0):
        from analysis import AMP_UNITS, DBM_OHMS, format_hz
        self._peak_freqs = [p.freq for p in peaks]
        ref = peaks[0] if peaks else None
        rows = []
        for i, p in enumerate(peaks, 1):
            df, dl = p.delta(ref) if ref else (0.0, 0.0)
            rows.append((i, format_hz(p.freq, 4), f"{p.level + offset:.2f}",
                         (("+" if df > 0 else "") + format_hz(df, 3))
                         if i > 1 else "—",
                         f"{dl:+.2f}" if i > 1 else "—"))
        self._fill(self.peaks, rows)
        self.setTabText(1, f"peaks ({len(rows)})" if rows else "peaks")

    def _on_peak_click(self, row: int, _col: int):
        if 0 <= row < len(self._peak_freqs):
            self.peak_clicked.emit(self._peak_freqs[row])


class Annunciators(QtWidgets.QWidget):
    """The warning strip: currently just ADC clipping.

    It is here because a clipped record produces harmonics that are not in the
    signal, which the display would otherwise imply are real.
    """

    def __init__(self):
        super().__init__()
        self.clip = QtWidgets.QLabel("")
        self.clip.setFrameStyle(QtWidgets.QFrame.Shape.StyledPanel
                                | QtWidgets.QFrame.Shadow.Sunken)
        self.clip.setVisible(False)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.clip)
        lay.addStretch(1)

    def show_clipping(self, clipped: int, npoints: int):
        # One sample at the rail is a code that happened to be extreme; a run
        # of them is the front end running out of range.  0.01% of a 1 Mpt
        # record is 100 samples, which is well clear of noise touching the top
        # code and well below anything that would distort a spectrum unseen.
        bad = npoints > 0 and clipped > max(8, npoints // 10000)
        self.clip.setVisible(bad)
        if bad:
            self.clip.setText(
                f"  ADC CLIPPING — {clipped:,} of {npoints:,} samples "
                f"({100.0 * clipped / npoints:.2f}%) at the rails; harmonics "
                f"below are manufactured. Reduce the scope's V/div.  ")
            self.clip.setStyleSheet(
                "background:#7a1f1f; color:#ffe0e0; font-weight:bold;")



def spur_lines(plot, sample_rate: float, log_x: bool):
    """Vertical marks at the ADC interleave spurs, k x fs/16.

    Returns the items so the caller can remove them when fs changes -- they are
    a property of the sample rate, so they must move with it or they become a
    lie about where the artifacts are.

    The labels ride on the lines rather than being placed at a fixed y: the
    reference level moves with autoscale, so an absolute y would drift off the
    graticule the moment the trace was rescaled.
    """
    import pyqtgraph as pg

    from analysis import adc_spur_freqs

    items = []
    pen = pg.mkPen("#605040", width=1, style=QtCore.Qt.PenStyle.DotLine)
    for k, f in adc_spur_freqs(sample_rate):
        x = float(np.log10(max(f, 1.0))) if log_x else f
        # Only the two measured on this instrument get a label (-60 dBFS at
        # fs/8 and fs/4, docs/STREAMING.md); labelling all seven turns the plot
        # into a picket fence of text.
        label = f"fs/{16 // k}" if k in (2, 4) else None
        line = pg.InfiniteLine(
            pos=x, angle=90, movable=False, pen=pen, label=label,
            labelOpts={"position": 0.96, "color": "#806040",
                       "fill": (0, 0, 0, 120), "movable": False})
        line.setZValue(-10)          # behind the trace: an annotation, not data
        plot.addItem(line)
        items.append(line)
    return items

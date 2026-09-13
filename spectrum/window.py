#!/usr/bin/env python3
"""The main window: panels, panes, and the frame loop that drives them.

This is what used to be fft_gui.run_gui -- a single 475-line function whose
every handler was a closure over the same scope.  The split is by
responsibility rather than by line count: viewmodel.py owns the numbers,
panels.py owns the widgets, plots.py owns the drawing, and what is left here is
the wiring plus the per-frame instrumentation, which genuinely does need to see
everything at once.

The instrumentation is unchanged and deliberately so: it decomposes each frame
interval into work / paint / idle with the GUI thread's own CPU time over the
same window, which is the only way to tell "we are doing too much" from "the
thread was not scheduled".  Any new per-frame cost added by a feature shows up
in it immediately, which is how the peak search's cost was caught.
"""
from __future__ import annotations

import time
from bisect import bisect_left
from collections import deque

import numpy as np
from pyqtgraph.Qt import QtCore, QtWidgets

import analysis
from analysis import (DEFAULT_EXCURSION_DB, DEFAULT_THRESHOLD_DB, unit_offset_db,
                      find_spectrum_peaks, format_hz, next_peak)
from frametime import (DISPLAY_FIELDS, FrameLog, GCWatch, StallDetector, nivcsw,
                       stall_line, thread_cpu)
from markers import MarkerSet
from stream_client import StreamServer
from panels import (AmpPanel, Annunciators, BwPanel, FreqPanel, MarkerPanel,
                    TablePane, TracePanel, ViewPanel, spur_lines)
from plots import SpectrumPlot, TimedPlotWidget, TimingStrip
from viewmodel import AmpScale, FreqView

# Peaks are recomputed at most this often.  Even above the noise floor the
# search is ~2.5 ms against an ~8 ms FFT, and a peak table that updates three
# times a second is no less readable than one that updates fourteen.
PEAK_INTERVAL_S = 0.3


class SpectrumWindow(QtWidgets.QMainWindow):
    def __init__(self, args, src, eng, app):
        super().__init__()
        self.args, self.src, self.eng, self.app = args, src, eng, app
        self.setWindowTitle("MHO934 live spectrum")

        # -- model --------------------------------------------------------
        self.view = FreqView(log_x=False)
        self.scale = AmpScale(ref_level=0.0, db_per_div=10.0)
        # The active channel's frame and spectrum.  Peaks, markers, the
        # annotation block and zoom-to-signal all work on it; every channel in
        # self.spectra is drawn.
        self.spec = None
        self.frame = None
        self.frames: list = []           # every channel of the last acquisition
        self.spectra: dict = {}          # channel -> Spectrum, in display units
        self.active_ch: int | None = None
        self._channel_list: list[int] = []    # the source's, as last seen
        self.peaks: list = []
        self.frozen = False
        # Autoscale off at startup.  A bench analyser opens on a fixed
        # graticule, and a scale that moves by itself makes two consecutive
        # looks at the same signal hard to compare -- the AMPT panel's "auto"
        # box turns it on when wanted.  The default is then AmpScale's own
        # 0 dBFS top at 10 dB/div, which spans 0..-100 dBFS.
        # Amplitude units.  power_db comes out of the engine in dBFS; a unit
        # change is a constant dB offset applied once, here, so the plot,
        # markers, peak table, readouts and CSV all follow without each
        # needing to know about units.
        self.amp_unit = "dBFS"
        self.unit_offset = 0.0
        self.auto_scale = False
        if args.ref_level or args.db_per_div:
            self.scale.ref_level = args.ref_level
            self.scale.db_per_div = args.db_per_div or 10.0
        self._spur_items: list = []
        self._spur_fs = 0.0
        self._last_peak_t = 0.0
        self._ranged = False

        # -- panes ---------------------------------------------------------
        self.plot = SpectrumPlot(self.view, display_bins=args.display_bins)
        self.plot.detector = args.detector
        self.markers = MarkerSet(self.plot.plot)
        self.strip = TimingStrip(args.timing_span, visible=bool(args.timing_strip))
        self.tables = TablePane()
        self.annun = Annunciators()

        # -- panels --------------------------------------------------------
        self.freq_panel = FreqPanel()
        self.amp_panel = AmpPanel(self.scale, auto_default=self.auto_scale)
        self.bw_panel = BwPanel(args.window, args.average, args.detector)
        self.trace_panel = TracePanel()
        self.marker_panel = MarkerPanel(DEFAULT_THRESHOLD_DB, DEFAULT_EXCURSION_DB)
        self.view_panel = ViewPanel(timing_default=bool(args.timing_strip))

        tabs = QtWidgets.QTabWidget()
        tabs.setDocumentMode(True)
        for panel, name in ((self.freq_panel, "FREQ"), (self.amp_panel, "AMPT"),
                            (self.bw_panel, "BW / DET"),
                            (self.trace_panel, "TRACE"),
                            (self.marker_panel, "MARKER"),
                            (self.view_panel, "VIEW")):
            tabs.addTab(panel, name)
        tabs.setMaximumHeight(tabs.sizeHint().height())
        self.tabs = tabs

        # -- layout --------------------------------------------------------
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        split.addWidget(self.plot.plot)
        split.addWidget(self.tables)
        split.setStretchFactor(0, 1)
        split.setSizes([980, 340])
        self.split = split

        self.status = QtWidgets.QLabel("waiting for first frame...")
        self.status.setFrameStyle(QtWidgets.QFrame.Shape.StyledPanel
                                  | QtWidgets.QFrame.Shadow.Sunken)
        # The instrument-style annotation block: what the trace *means*, kept
        # separate from the throughput telemetry below it.  R&S makes the same
        # split between a channel bar and a diagram footer.
        self.annot = QtWidgets.QLabel("")
        self.annot.setFrameStyle(QtWidgets.QFrame.Shape.StyledPanel
                                 | QtWidgets.QFrame.Shadow.Sunken)
        self.annot.setStyleSheet("font-family: monospace;")

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        lay = QtWidgets.QVBoxLayout(central)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(tabs)
        lay.addWidget(self.annot)
        lay.addWidget(split, 1)
        lay.addWidget(self.strip.widget)
        lay.addWidget(self.annun)
        lay.addWidget(self.status)

        # -- instrumentation -----------------------------------------------
        self.ftlog = FrameLog("display", DISPLAY_FIELDS, capacity=args.timing_keep)
        self.gcw = GCWatch().install()
        self.stall = StallDetector(threshold_ms=args.stall_ms)
        self.t0 = time.perf_counter()
        self.draws = 0
        # Draw timestamps for the trailing-window rate.  The source reports its
        # rate over the same window (StreamServer.RATE_WINDOW_S), and the two
        # numbers sit side by side in the status bar -- measuring them over
        # different spans made the draw figure read permanently low, because a
        # cumulative mean never recovers from the startup ramp.
        self._draw_hist: deque = deque(maxlen=4096)
        self.fft_ms = 0.0
        self.last_msg = ""
        self.tmr = {"last_end": time.perf_counter(), "last_cpu": thread_cpu(),
                    "last_paint": 0.0, "last_ivcsw": nivcsw(), "last_recv": None,
                    "last_dropped": 0, "work_s": 0.0, "empty": 0,
                    "status_ms": 0.0, "intervals": deque(maxlen=32)}

        self._connect()
        self.plot.set_y_range(*self.scale.y_range())
        self.resize(1400, 820)

    # -- wiring ------------------------------------------------------------
    def _connect(self):
        f, a, b, m, v = (self.freq_panel, self.amp_panel, self.bw_panel,
                         self.marker_panel, self.view_panel)

        f.centre_span_set.connect(self._set_centre_span)
        f.start_stop_set.connect(self._set_start_stop)
        f.full_span.connect(self._full_span)
        f.zoom.connect(self._zoom)
        f.zoom_signal.connect(self.zoom_to_signal)
        f.log_x.connect(self._set_log_x)
        self.plot.view_changed.connect(lambda: f.show_view(self.view))

        a.scale_changed.connect(self._apply_scale)
        a.autoscale.connect(lambda: self._autoscale(force=True))
        a.autoscale_mode.connect(self._set_autoscale)

        b.window_changed.connect(self.eng.set_window)
        b.detector_changed.connect(self._set_detector)
        b.average_changed.connect(lambda n: setattr(self.eng, "averaging", n))
        b.peak_hold_changed.connect(lambda on: setattr(self.eng, "peak_hold", on))
        b.reset.connect(self.eng.reset)

        t = self.trace_panel
        t.active_changed.connect(self._set_active_channel)
        t.visibility_changed.connect(self._set_channel_visible)

        m.peak_search.connect(self.peak_search)
        m.step_peak.connect(self.step_peak)
        m.add_delta.connect(self.add_delta_marker)
        m.marker_to_centre.connect(self.marker_to_centre)
        m.clear.connect(self.markers.clear)
        m.criteria_changed.connect(self._recompute_peaks)

        v.capture_dc.connect(self.capture_dc)
        v.clear_dc.connect(self.eng.clear_dc)
        v.timing_strip.connect(self.strip.set_visible)
        v.spurs.connect(self._set_spurs)
        v.tables.connect(self.tables.setVisible)
        v.freeze.connect(self._set_frozen)
        v.amp_unit.connect(self._set_amp_unit)
        v.screenshot.connect(self.save_screenshot)
        v.export_trace.connect(self.export_trace)
        v.export_peaks.connect(self.export_peaks)

        self.markers.changed.connect(self._show_marker_table)
        # Peaks are only computed while their tab is showing, so switching to
        # it must fill it now rather than leaving it blank until a frame lands.
        self.tables.currentChanged.connect(
            lambda i: self._recompute_peaks() if i == 1 else None)
        self.tables.peak_clicked.connect(self._marker_at)
        self.plot.plot.scene().sigMouseClicked.connect(self._on_click)

    # -- frequency ---------------------------------------------------------
    def _set_centre_span(self, centre: float, span: float):
        self.view.set_centre_span(centre, span)
        self._sync_view()

    def _set_start_stop(self, lo: float, hi: float):
        self.view.set_start_stop(lo, hi)
        self._sync_view()

    def _reprocess(self):
        """Recompute the stored frames under the current settings."""
        if not self.frames:
            return
        self._process_group(self.frames)
        self.plot.set_spectra(self.spectra)
        self.plot.redraw()
        self.markers.refresh(self.spectra)

    def _process_group(self, frames):
        """Spectra for one acquisition, in display units, and the active one.

        Does not touch the plot: while frozen the numbers still follow the
        source but the trace under the cursor must not change.
        """
        spectra = self.eng.process(frames)
        self.frames, self.spectra = frames, spectra
        if self.active_ch not in spectra:
            shown = [f.channel for f in frames if f.channel not in self.plot.hidden]
            self.active_ch = shown[0] if shown else frames[0].channel
        # Each channel shifts by its own offset.  Channels have their own V/div,
        # so a single offset -- which this was, the last channel's winning --
        # reads right for one channel and wrong by the V/div ratio for the rest.
        for f in frames:
            off = self._unit_offset_for(f)
            if f.channel == self.active_ch:
                self.unit_offset = off
            if off:
                spectra[f.channel].power_db = spectra[f.channel].power_db + off
        self.frame = next(f for f in frames if f.channel == self.active_ch)
        self.spec = spectra[self.active_ch]
        self._sync_channels(frames)

    def _unit_offset_for(self, frame, unit: str | None = None) -> float:
        """dB from dBFS to `unit` (default: the current one) on this frame's
        own vertical scale; 0 when the source sent none."""
        if frame is None or getattr(frame, "samples", None) is None:
            return 0.0
        full_scale = float(2 ** (8 * frame.samples.itemsize))
        return unit_offset_db(unit or self.amp_unit,
                              getattr(frame, "yinc", 0.0), full_scale)

    # -- channels ----------------------------------------------------------
    def _ch_label(self) -> int | None:
        """The active channel, for labels; None with one channel, where naming
        it would only be noise."""
        return self.active_ch if len(self._channel_list) > 1 else None

    def _sync_channels(self, frames):
        """Carry the source's channel set into the TRACE tab and the markers.

        Only when it changes -- with the tap, once per session, since it
        streams whatever was switched on when it started.
        """
        chans = [f.channel for f in frames]
        if chans == self._channel_list:
            return
        self._channel_list = chans
        hidden = self.plot.hidden & set(chans)
        self.plot.set_hidden(hidden)
        self.plot.set_active(self.active_ch)
        self.markers.set_multi(len(chans) > 1)
        self.markers.set_hidden(hidden)
        self.tables.set_multi(len(chans) > 1)
        self.trace_panel.set_channels(chans, self.active_ch, hidden)

    def _set_active_channel(self, ch: int):
        """Point the readouts at another channel.

        Nothing is recomputed but what reads one spectrum: the peak list (its
        indices belong to the old channel's trace), the annotation block and
        the units offset the graticule is shifted by on a unit change.
        Markers stay where they are, on their own channels.
        """
        if ch == self.active_ch or ch not in self.spectra:
            return
        self.active_ch = ch
        self.frame = next(f for f in self.frames if f.channel == ch)
        self.spec = self.spectra[ch]
        self.unit_offset = self._unit_offset_for(self.frame)
        self.plot.set_active(ch)
        self.trace_panel.show_active(ch)
        self._recompute_peaks()
        self._show_annotation()

    def _set_channel_visible(self, ch: int, on: bool):
        """Show or hide one channel's trace.  It keeps being processed."""
        hidden = set(self.plot.hidden)
        if on:
            hidden.discard(ch)
        else:
            shown = [c for c in self._channel_list if c != ch and c not in hidden]
            if not shown:
                # An empty plot with every readout still quoting a hidden trace
                # is a worse state than refusing the click.
                self.trace_panel.show_visible(ch, True)
                self.status.setText("at least one channel has to stay visible")
                return
            hidden.add(ch)
            if ch == self.active_ch:
                self._set_active_channel(shown[0])
        self.plot.set_hidden(hidden)
        self.markers.set_hidden(hidden)
        self.plot.redraw()
        self._autoscale()

    def _set_amp_unit(self, unit: str):
        """Switch units, keeping the trace where it is on screen.

        The graticule moves with the trace, so the picture is unchanged and
        only the numbers differ -- switching units should not look like the
        signal jumped.
        """
        prev = self.unit_offset
        # Every channel needs a scale, or one of them would be labelled in
        # absolute units while still reading dBFS.
        if unit != "dBFS" and not all(self._unit_offset_for(f, unit)
                                      for f in (self.frames or [self.frame])):
            self.status.setText(
                f"{unit} needs the scope's volts-per-code for every channel, "
                f"which this source did not send -- staying in {self.amp_unit}")
            self.view_panel.show_unit(self.amp_unit)
            return
        self.amp_unit = unit
        self.unit_offset = self._unit_offset_for(self.frame)
        delta = self.unit_offset - prev
        self.scale.ref_level += delta
        self.amp_panel.show_scale()
        self.view_panel.show_unit(self.amp_unit)
        self.plot.set_unit_label(self.amp_unit)
        # Every other place that prints a level carries its own unit label;
        # missing any of these left the numbers converted but still "dBFS".
        self.amp_panel.set_unit(self.amp_unit)
        self.marker_panel.set_unit(self.amp_unit, delta)
        self.tables.set_unit(self.amp_unit)
        self.markers.set_unit(self.amp_unit)
        self._apply_scale()
        # Re-derive from the stored frame so the change is visible at once
        # rather than at the next arrival -- and while frozen, at all.
        self._reprocess()
        self._recompute_peaks()

    def _full_span(self):
        self.view.full_span()
        self._sync_view()

    def _zoom(self, factor: float):
        self.view.zoom(factor)
        self._sync_view()

    def _set_log_x(self, on: bool):
        self.plot.set_log_x(on)
        self.markers.set_log_x(on)
        self._rebuild_spurs()
        self.freq_panel.show_view(self.view)

    def _sync_view(self):
        self.plot.apply_view()
        self.freq_panel.show_view(self.view)
        # The annotation block quotes centre and span, so it has to follow a
        # view change now rather than at the next frame -- between frames is
        # exactly when someone is reading it to check what they just typed.
        self._show_annotation()

    def zoom_to_signal(self):
        """Frame the strongest tone with room for its harmonics.

        Sets the view to DC..8x the strongest bin, ignoring everything below
        ~1 kHz so the DC bin cannot win.  At 50 MSa/s the Nyquist is 25 MHz, so
        a 0.77 MHz tone occupies the leftmost 3% of a full-span view -- legible,
        but its harmonics are not, which is what the 8x is for.
        """
        spec = self.spec
        if spec is None:
            return
        skip = max(1, int(1e3 / spec.resolution))      # ignore DC and near-DC
        pk = int(np.argmax(spec.power_db[skip:])) + skip
        f_pk = float(spec.freqs[pk])
        hi = min(max(f_pk * 8.0, 10 * spec.resolution), float(spec.freqs[-1]))
        self.view.set_start_stop(spec.resolution, hi)
        self._sync_view()

    # -- amplitude ---------------------------------------------------------
    def _apply_scale(self):
        self.plot.set_y_range(*self.scale.y_range())
        self._show_annotation()

    def _set_autoscale(self, on: bool):
        self.auto_scale = bool(on)
        if on:
            self._autoscale(force=True)

    def _autoscale(self, force: bool = False):
        if self.spec is None or not (force or self.auto_scale):
            return
        # Scale from what is *visible*, not the whole record: after zooming
        # into a quiet corner, a full-span autoscale would leave the trace a
        # flat line at the bottom of a graticule sized for a distant carrier.
        # With several channels, fit every visible one -- they share one scale,
        # and a hidden channel's level must not squash the ones on screen.
        bands = []
        shown = [s for ch, s in self.spectra.items() if ch not in self.plot.hidden]
        for s in (shown or [self.spec]):
            lo = int(np.searchsorted(s.freqs, self.view.lo, "left"))
            hi = int(np.searchsorted(s.freqs, self.view.hi, "right"))
            band = s.power_db[max(0, lo):max(lo + 1, hi)]
            bands.append(band if band.size else s.power_db)
        self.scale.autoscale(np.concatenate(bands))
        self.amp_panel.show_scale()
        self._apply_scale()

    # -- detectors and traces ---------------------------------------------
    def _set_detector(self, name: str):
        self.plot.detector = name
        self.plot.redraw()
        self._show_annotation()

    def capture_dc(self):
        if not self.frames:
            return
        self.eng.capture_dc(self.frames)
        self._reprocess()

    def _set_frozen(self, on: bool):
        """Freeze keeps the display interactive at full bin resolution.

        Not a source pause: frames keep arriving and the source's own counters
        keep advancing, so unfreezing does not look like a stall.  What stops
        is the trace being replaced under the user's cursor mid-measurement.
        """
        self.frozen = bool(on)

    def _set_spurs(self, on: bool):
        if on:
            self._rebuild_spurs()
        else:
            self._clear_spurs()

    def _clear_spurs(self):
        for it in self._spur_items:
            self.plot.plot.removeItem(it)
        self._spur_items = []

    def _rebuild_spurs(self):
        self._clear_spurs()
        if not self.view_panel.spur_chk.isChecked() or not self._spur_fs:
            return
        self._spur_items = spur_lines(self.plot.plot, self._spur_fs,
                                      self.view.log_x)

    # -- markers and peaks -------------------------------------------------
    def _threshold(self) -> float:
        if self.marker_panel.auto_thresh.isChecked() and self.spec is not None:
            th = analysis.auto_threshold(self.spec.power_db)
            self.marker_panel.thresh.blockSignals(True)
            self.marker_panel.thresh.setValue(th)
            self.marker_panel.thresh.blockSignals(False)
            return th
        return self.marker_panel.thresh.value()

    def _recompute_peaks(self):
        if self.spec is None:
            return
        self.peaks = find_spectrum_peaks(
            self.spec.freqs, self.spec.power_db,
            threshold=self._threshold(),
            excursion=self.marker_panel.excursion.value(),
            limit=20,
            # One RBW's worth of DC skirt, not a fixed frequency: at 2 GSa/s
            # and 1 Mpt that is a few kHz, and at a lower rate it must shrink
            # with the bins or it hides real low-frequency signal.
            exclude_dc_hz=max(10.0 * self.spec.rbw, 100.0))
        self.tables.show_peaks(self.peaks, self.scale.offset, self._ch_label())
        self._last_peak_t = time.perf_counter()

    def _move_marker(self, m, freq: float):
        """Put a marker on the active channel's trace, at the bin nearest freq.

        Every marker gesture acts on the active channel, so a marker sitting on
        another channel moves across with it -- that is how "search CH2" is
        asked for without a second set of buttons.
        """
        m.channel = self.active_ch
        self.markers.move_to(m, self.spec.freqs, self.spec.power_db, freq)

    def _new_marker(self, freq: float, level: float = 0.0, index: int = 0,
                    delta: bool = False):
        return self.markers.add(freq, level, index, delta=delta,
                                channel=self.active_ch)

    def _marker_at(self, freq: float):
        if self.spec is None:
            return
        m = self.markers.selected or self._new_marker(freq)
        if m is not None:
            self._move_marker(m, freq)

    def _on_click(self, ev):
        """Shift+click places a marker; right-click removes the nearest one."""
        if self.spec is None:
            return
        vb = self.plot.plot.getViewBox()
        if not vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        pt = vb.mapSceneToView(ev.scenePos())
        freq, _ = self.view.from_axis(pt.x(), pt.x())

        if ev.button() == QtCore.Qt.MouseButton.RightButton:
            if self.markers.markers:
                near = min(self.markers.markers, key=lambda m: abs(m.freq - freq))
                self.markers.remove(near)
                ev.accept()
            return
        if ev.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier:
            m = self._new_marker(freq)
            if m is not None:
                self._move_marker(m, freq)
            ev.accept()

    def peak_search(self):
        if self.spec is None:
            return
        if not self.peaks:
            self._recompute_peaks()
        if not self.peaks:
            return
        top = self.peaks[0]
        m = self.markers.selected or self._new_marker(top.freq, top.level, top.index)
        if m is not None:
            self._move_marker(m, top.freq)

    def step_peak(self, direction: str):
        m = self.markers.selected
        if m is None or self.spec is None:
            self.peak_search()
            return
        if not self.peaks:
            self._recompute_peaks()
        cur = m.level if direction == "next" else m.freq
        nxt = next_peak(self.peaks, cur, direction)
        if nxt is not None:
            self._move_marker(m, nxt.freq)

    def add_delta_marker(self):
        """Add a marker that reads against the current one.

        The current marker becomes the reference and keeps its absolute
        readout; the new one reads the difference.  That asymmetry is
        Tektronix's stated rule and the thing people expect.

        The new marker goes on the active channel, which need not be the
        reference's: a reference on CH1 and a delta added with CH2 active read
        CH2 - CH1 at one frequency, the comparison two channels are for.
        """
        if self.spec is None or self.markers.selected is None:
            self.peak_search()
        cur = self.markers.selected
        if cur is None:
            return
        self.markers.reference = cur
        m = self._new_marker(cur.freq, cur.level, cur.index, delta=True)
        if m is not None:
            self._move_marker(m, cur.freq)

    def marker_to_centre(self):
        m = self.markers.selected
        if m is None:
            return
        self.view.set_centre(m.freq)
        self._sync_view()

    def _show_marker_table(self):
        rows = self.markers.rows(self.scale.offset)
        self.tables.show_markers(rows)
        # R&S auto-shows the marker table once there is more than one marker;
        # below that the on-plot label already says everything it would.
        if len(rows) > 1 and self.tables.currentIndex() != 0:
            self.tables.setCurrentIndex(0)

    # -- export ------------------------------------------------------------
    def _ask_path(self, title: str, default: str) -> str:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, title, default)
        return path

    def export_trace(self):
        if self.spec is None:
            return
        path = self._ask_path("Export trace (full resolution)", "trace.csv")
        if not path:
            return
        if len(self.spectra) > 1:
            # Every channel, hidden ones included: an export is the data, and
            # hiding is only about what fits on screen.
            n = analysis.write_traces_csv(
                path, self.spectra, {f.channel: f.yinc for f in self.frames},
                unit=self.amp_unit)
            self.status.setText(f"wrote {n:,} bins x {len(self.spectra)} "
                                f"channels to {path}")
        else:
            n = analysis.write_trace_csv(path, self.spec, full=True,
                                         unit=self.amp_unit)
            self.status.setText(f"wrote {n:,} bins to {path}")

    def export_peaks(self):
        if self.spec is None:
            return
        if not self.peaks:
            self._recompute_peaks()
        path = self._ask_path("Export peak table", "peaks.csv")
        if path:
            n = analysis.write_peaks_csv(path, self.peaks, self.spec,
                                         self.scale.offset, unit=self.amp_unit,
                                         channel=self._ch_label())
            self.status.setText(f"wrote {n} peaks to {path}")

    def save_screenshot(self, path: str = ""):
        path = path or self._ask_path("Save screenshot", "spectrum.png")
        if path:
            # grab() renders the widget directly rather than going through a
            # compositor, so it works under Wayland with no helper tool.
            self.grab().save(path)
            self.status.setText(f"saved {path}")

    # -- the frame loop ----------------------------------------------------
    def start(self):
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(self.args.poll_ms)
        if self.args.run_seconds:
            QtCore.QTimer.singleShot(int(self.args.run_seconds * 1000),
                                     self._finish)

    def _finish(self):
        print(self.status.text())
        if self.args.screenshot:
            # Grab before quitting: the window has to still exist.
            self.grab().save(self.args.screenshot)
            print(f"saved {self.args.screenshot}")
        self.app.quit()

    def tick(self):
        src = self.src
        t_tick = time.perf_counter()

        t_get = time.perf_counter()
        if hasattr(src, "get_group"):
            frames = src.get_group(timeout=0.0)
        else:
            one = src.get(timeout=0.0)
            frames = [one] if one is not None else None
        get_ms = (time.perf_counter() - t_get) * 1e3
        if not frames:
            st = src.stats()
            if not getattr(src, "tap_alive", lambda: True)():
                tail = src.tap_log_tail(3).strip().replace("\n", " | ")
                self.status.setText(f"on-scope tap exited: {tail[:180]}")
                return
            msg = st.get("error") or st.get("warning")
            if msg and msg != self.last_msg:
                self.last_msg = msg
                kind = "error" if st.get("error") else "waiting"
                self.status.setText(f"source {kind}: {msg}")
            # An empty poll is the timer running while the source has nothing.
            # Counting them separates "waiting for the scope" from "blocked",
            # which is the whole question a stall raises.
            self.tmr["empty"] += 1
            self.tmr["work_s"] += time.perf_counter() - t_tick
            return

        t0 = time.perf_counter()
        self._process_group(frames)
        # All channels: the frame interval pays for every FFT, not one.
        self.fft_ms = (time.perf_counter() - t0) * 1e3
        frame, spec = self.frame, self.spec

        nyq = float(spec.freqs[-1])
        if not self._ranged or abs(nyq - self.view.nyquist) > 1.0:
            self._on_sample_rate_change(spec, nyq)

        if not self.frozen:
            self.plot.set_spectra(self.spectra)
            self.plot.redraw()
            self.markers.refresh(self.spectra)
            self._autoscale()
            if (self.tables.isVisible() and self.tables.currentIndex() == 1
                    and time.perf_counter() - self._last_peak_t > PEAK_INTERVAL_S):
                self._recompute_peaks()

        self._close_frame(frame, t_tick, get_ms)

    def _on_sample_rate_change(self, spec, nyq: float):
        """Fix the x range once, and move everything pinned to the sample rate.

        Setting the range here rather than letting autorange do it stops
        setData feeding back into sigXRangeChanged and looping.
        """
        self.view.nyquist = nyq
        self._ranged = True
        self.plot.plot.enableAutoRange(x=False)
        # Full span unless --fmax says otherwise: the whole record is what the
        # instrument measured, and picking a narrower view on the user's behalf
        # hides signal they did not ask to hide.  `zoom to signal` is one click
        # away when they want it.
        hi = min(self.args.fmax, nyq) if self.args.fmax else nyq
        self.view.set_start_stop(spec.resolution if self.view.log_x else 0.0, hi)
        self._sync_view()
        # The spurs are at k x fs/16, so they belong to the sample rate and
        # have to be rebuilt when it changes -- otherwise they mark the wrong
        # frequencies, which is worse than not marking them at all.
        self._spur_fs = spec.sample_rate
        self._rebuild_spurs()
        # Only fit the graticule to the first record if autoscale is on.  This
        # used to force it regardless, which quietly overrode the fixed scale
        # the analyser now starts with.
        if self.auto_scale:
            self._autoscale(force=True)

    def _close_frame(self, frame, t_tick: float, get_ms: float):
        """Close out the frame interval and update every readout.

        The boundary is the end of the redraw, so every piece of the window
        belongs to exactly one interval: this tick's own work, the empty polls
        and repaint that preceded it, and whatever is left over.
        """
        tmr, st = self.tmr, self.src.stats()
        end = time.perf_counter()
        cpu_end = thread_cpu()
        ivcsw_end = nivcsw()
        paint_now = TimedPlotWidget.paint_s

        draw_ms = self.plot.take_draw_ms()
        interval_ms = (end - tmr["last_end"]) * 1e3
        cpu_ms = (cpu_end - tmr["last_cpu"]) * 1e3
        paint_ms = (paint_now - tmr["last_paint"]) * 1e3
        work_ms = tmr["work_s"] * 1e3 + (end - t_tick) * 1e3
        _gc_n, gc_ms = self.gcw.between(tmr["last_end"], end)
        row = self.ftlog.record(
            t=end,
            interval_ms=interval_ms,
            cpu_ms=cpu_ms,
            work_ms=work_ms,
            paint_ms=paint_ms,
            idle_ms=max(0.0, interval_ms - work_ms - paint_ms),
            get_ms=get_ms,
            fft_ms=self.fft_ms,
            draw_ms=draw_ms,
            status_ms=tmr.get("status_ms", 0.0),
            gc_ms=gc_ms,
            age_ms=(end - frame.recv_time) * 1e3,
            src_gap_ms=((frame.recv_time - tmr["last_recv"]) * 1e3
                        if tmr["last_recv"] else 0.0),
            empty_ticks=tmr["empty"],
            nivcsw=ivcsw_end - tmr["last_ivcsw"],
            dropped=st.get("dropped", 0) - tmr["last_dropped"],
            base_ms=self.stall.median,      # what "normal" was, for explain()
            seq=frame.seq)
        if self.stall.check(interval_ms):
            print(stall_line(row, self.t0), flush=True)
        tmr.update(last_end=end, last_cpu=cpu_end, last_paint=paint_now,
                   last_ivcsw=ivcsw_end, last_recv=frame.recv_time,
                   last_dropped=st.get("dropped", 0), work_s=0.0, empty=0)

        t_status = time.perf_counter()
        self.draws += 1
        self._draw_hist.append(t_status)
        tmr["intervals"].append(interval_ms)
        self.strip.append(interval_ms, cpu_ms, row["src_gap_ms"])
        if self.strip.is_visible():
            self.strip.redraw(self.stall.limit())
        self._show_annotation()
        self._show_status(frame, st, interval_ms)
        self._show_annunciators(frame, interval_ms)
        # Building the readouts lands after the interval boundary, so carry
        # them into the next interval rather than losing them.
        tmr["status_ms"] = (time.perf_counter() - t_status) * 1e3
        tmr["work_s"] = tmr["status_ms"] / 1e3

    # -- readouts ----------------------------------------------------------
    def _show_annotation(self):
        """The measurement context block: what the trace on screen means.

        Bin spacing and RBW are shown together and labelled differently on
        purpose.  They are not the same number -- RBW is ENBW x fs/N, 1.5x the
        spacing for hann -- and conflating them is the error both Keysight and
        Siglent document, so the display states both rather than picking one
        and hoping.
        """
        spec = self.spec
        if spec is None:
            return
        off = f"  offset {self.scale.offset:+.1f} dB" if self.scale.offset else ""
        ch = self._ch_label()
        self.annot.setText(
            (f"CH{ch} active   |   " if ch is not None else "") +
            f"ref {self.scale.ref_level:+.1f} {self.amp_unit}   "
            f"{self.scale.db_per_div:g} dB/div{off}   |   "
            f"centre {format_hz(self.view.centre)}   "
            f"span {format_hz(self.view.span)}   |   "
            f"RBW {format_hz(spec.rbw, 2)} ({spec.window}, "
            f"ENBW {spec.enbw_bins:.2f} bins)   "
            f"Δf {format_hz(spec.resolution, 2)}/bin   |   "
            f"det {self.plot.detector}   "
            f"avg {self.eng.averaging}"
            f"{' + max hold' if self.eng.peak_hold else ''}   "
            f"acq {spec.n / spec.sample_rate * 1e6:.0f} µs")

    def _draw_fps(self) -> float:
        """Draw rate over the same trailing window the source reports on.

        Falls back to the whole-run mean when the window holds fewer than two
        draws -- while it is still filling, or after a freeze -- which is what
        StreamServer.stats() does with the source rate, so the two numbers in
        the status bar always mean the same thing.
        """
        h = self._draw_hist
        el = time.perf_counter() - self.t0
        whole = self.draws / el if el > 0 else 0.0
        if len(h) < 2:
            return whole
        i = bisect_left(h, time.perf_counter() - StreamServer.RATE_WINDOW_S)
        if len(h) - i < 2:
            return whole
        span = h[-1] - h[i]
        return (len(h) - 1 - i) / span if span > 0 else whole

    def _show_status(self, frame, st, interval_ms: float):
        spec = self.spec
        pk = int(np.argmax(spec.power_db))
        multi = len(self.frames) > 1
        self.status.setText(
            (f"CH{'+'.join(str(f.channel) for f in self.frames)}  ·  " if multi else "")
            + f"{frame.npoints:,} pts @ {frame.sample_rate / 1e6:.1f} MSa/s  ·  "
            f"src {st['fps']:.2f} fps, {st['mbps']:.1f} MB/s, "
            f"{st['dropped']} dropped"
            + (f", {st['repeats']} STALE" if st.get("repeats") else "") + "  ·  "
            f"draw {self._draw_fps():.1f} fps  ·  fft {self.fft_ms:.0f} ms  ·  "
            f"frame {interval_ms:.0f} ms (med {self.stall.median:.0f}, "
            f"worst {self.stall.worst:.0f}, {self.stall.count} stalls)  ·  "
            f"peak{f' CH{self.active_ch}' if multi else ''} "
            f"{spec.freqs[pk] / 1e6:.4f} MHz @ "
            f"{spec.power_db[pk] + self.scale.offset:.1f} {self.amp_unit}"
            + (f"  ·  DC cal {self.eng.dc_offset:+.0f} codes"
               if self.eng.dc_offset else "")
            + ("  ·  FROZEN" if self.frozen else ""))

    def _show_annunciators(self, frame, interval_ms: float):
        # Clipping on any channel matters: its harmonics land on the shared plot.
        clipped = max((s.clipped for s in self.spectra.values()), default=self.spec.clipped)
        self.annun.show_clipping(clipped, frame.npoints)

    # -- shutdown ----------------------------------------------------------
    def teardown(self):
        self.gcw.remove()

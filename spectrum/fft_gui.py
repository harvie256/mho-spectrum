#!/usr/bin/env python3
"""Live FFT power spectrum from a Rigol MHO934.

    # no scope needed -- known synthetic signal, for checking the display
    python3 fftdemo/fft_gui.py --source synthetic

    # real scope over SCPI (run patch/patch_scope.py alongside for ~10x)
    python3 fftdemo/fft_gui.py --source scpi --host 192.168.23.20 --channel 1

    # fast path: on-scope tap pushes frames to us
    python3 fftdemo/fft_gui.py --source stream --port 5560

pyqtgraph rather than matplotlib: a 500k-bin spectrum has to be redrawn every
frame, and matplotlib cannot do that at frame rate.  The bins are collapsed to
screen width with a peak-preserving envelope before plotting (see
spectrum.reduce_for_display) -- that, not the FFT, is what makes it keep up.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import deque

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from frametime import DISPLAY_FIELDS, SOURCE_FIELDS  # noqa: E402
from spectrum import SpectrumEngine, WINDOWS, reduce_for_display  # noqa: E402


def report_timing(args, src, ftlog, gcw=None, stall=None):
    """Print what the frame timing showed, and optionally dump the raw rows.

    Both halves are printed together on purpose: the display log alone cannot
    say whether a stall was ours, and the source log alone cannot say whether a
    clean arrival ever reached the screen.
    """
    if ftlog is None or not ftlog.rows:
        return
    print()
    print(ftlog.report(DISPLAY_FIELDS))
    if stall is not None:
        print(f"  stalls: {stall.count} over {ftlog.n} frames "
              f"(threshold {stall.limit():.0f} ms), worst {stall.worst:.0f} ms")
    print(ftlog.worst_report("interval_ms",
                             ("interval_ms", "cpu_ms", "work_ms", "paint_ms",
                              "idle_ms", "fft_ms", "draw_ms", "gc_ms",
                              "src_gap_ms", "empty_ticks", "nivcsw"),
                             n=args.timing_worst))
    stl = getattr(src, "timing", None)
    if stl is not None and stl.rows:
        print()
        print(stl.report(SOURCE_FIELDS))
        print(stl.worst_report("loop_ms", SOURCE_FIELDS, n=args.timing_worst))
    if gcw is not None:
        print("\n" + gcw.summary())
    if args.timing_csv:
        base, ext = os.path.splitext(args.timing_csv)
        ext = ext or ".csv"
        n = ftlog.write_csv(base + ext)
        print(f"\nwrote {n} display rows to {base + ext}")
        if stl is not None and stl.rows:
            n = stl.write_csv(f"{base}-source{ext}")
            print(f"wrote {n} source rows to {base}-source{ext}")


def build(args):
    from sources import ScpiSource, StreamSource, SyntheticSource
    if args.source == "synthetic":
        return SyntheticSource(npoints=args.points or 1_000_000,
                               sample_rate=args.sample_rate,
                               fps_limit=args.synthetic_fps)
    if args.source == "scpi":
        if not args.host:
            raise SystemExit("--source scpi needs --host")
        return ScpiSource(args.host, channel=args.channel, fmt=args.format,
                          points=args.points, rearm=not args.no_rearm,
                          native_device=args.native_acq or None,
                          fill_ms=args.fill_ms)
    return StreamSource(host=args.bind, port=args.port,
                        scope_ip=args.tap or None, pc_host=args.pc_host or None,
                        channel=args.channel, fmt=args.format,
                        scpi_ip=args.scpi_ip or None,
                        drive_poll_ms=args.tap_poll_ms,
                        quiet_ui=args.tap_quiet_ui,
                        drive_csv=args.tap_drive_csv,
                        no_scpi_sleep=args.tap_no_scpi_sleep,
                        scpi_sleep_us=args.tap_scpi_sleep_us)


def run_headless(args, src, eng):
    """Throughput/latency check with no display -- also a correctness harness.

    Instrumented the same way as the GUI, deliberately: run the same source
    headless and any stall that survives is not the GUI's fault.  That is the
    cheapest way to test the "the display is being starved" theory.
    """
    from frametime import FrameLog, GCWatch, StallDetector, nivcsw, stall_line, thread_cpu

    print(f"headless: source={args.source}")
    t_end = time.time() + (args.seconds or 10.0)
    n = 0
    fft_ms = []
    state_warn = {}
    last_report = time.time()
    ftlog = FrameLog("display(headless)", DISPLAY_FIELDS, capacity=args.timing_keep)
    gcw = GCWatch().install()
    stall = StallDetector(threshold_ms=args.stall_ms)
    t_run = time.perf_counter()
    tmr = {"last_end": t_run, "last_cpu": thread_cpu(), "last_ivcsw": nivcsw(),
           "last_recv": None, "work_s": 0.0, "empty": 0}
    while time.time() < t_end:
        t_tick = time.perf_counter()
        frame = src.get(timeout=2.0)
        t_got = time.perf_counter()
        if frame is None:
            # Only the part after the (blocking) get is our own work; the wait
            # itself is idle time and must not be charged to compute.
            tmr["empty"] += 1
            tmr["work_s"] += time.perf_counter() - t_got
            st = src.stats()
            if st.get("error"):
                print("source error:", st["error"])
                break
            if not getattr(src, "tap_alive", lambda: True)():
                print("on-scope tap exited:\n" + src.tap_log_tail())
                break
            if st.get("warning") and st["warning"] != state_warn.get("last"):
                state_warn["last"] = st["warning"]
                print("  warning:", st["warning"])
            continue
        t0 = time.perf_counter()
        spec = eng.process(frame.samples, frame.sample_rate)
        fft_ms.append((time.perf_counter() - t0) * 1e3)
        n += 1

        end = time.perf_counter()
        cpu_end, ivcsw_end = thread_cpu(), nivcsw()
        interval_ms = (end - tmr["last_end"]) * 1e3
        # Headless polls with a blocking get(), so unlike the GUI its wait is
        # not idle-in-an-event-loop but idle-inside-the-call: subtract it, or
        # every stall reads as "compute" when it is really "nothing arrived".
        get_ms = (t_got - t_tick) * 1e3
        work_ms = tmr["work_s"] * 1e3 + (end - t_tick) * 1e3 - get_ms
        _gc_n, gc_ms = gcw.between(tmr["last_end"], end)
        row = ftlog.record(
            t=end, interval_ms=interval_ms,
            cpu_ms=(cpu_end - tmr["last_cpu"]) * 1e3,
            work_ms=work_ms, idle_ms=max(0.0, interval_ms - work_ms),
            get_ms=get_ms, fft_ms=fft_ms[-1], gc_ms=gc_ms,
            age_ms=(end - frame.recv_time) * 1e3,
            src_gap_ms=((frame.recv_time - tmr["last_recv"]) * 1e3
                        if tmr["last_recv"] else 0.0),
            empty_ticks=tmr["empty"], nivcsw=ivcsw_end - tmr["last_ivcsw"],
            base_ms=stall.median, seq=frame.seq)
        if stall.check(interval_ms):
            print(stall_line(row, t_run), flush=True)
        tmr.update(last_end=end, last_cpu=cpu_end, last_ivcsw=ivcsw_end,
                   last_recv=frame.recv_time, work_s=0.0, empty=0)

        if time.time() - last_report >= 1.0:
            st = src.stats()
            pk = int(np.argmax(spec.power_db))
            extra = ""
            for k in ("resyncs", "timeouts", "short", "stale", "repeats"):
                if st.get(k):
                    extra += f"  {k} {st[k]}"
            print(f"  {n:5d} frames  src {st['fps']:5.2f} fps  "
                  f"{st['mbps']:6.2f} MB/s  dropped {st['dropped']}  "
                  f"fft {np.median(fft_ms):5.1f} ms  "
                  f"peak {spec.freqs[pk]/1e6:8.4f} MHz @ {spec.power_db[pk]:6.1f} dBFS"
                  + extra)
            if st.get("warning") and st["warning"] != state_warn.get("last"):
                state_warn["last"] = st["warning"]
                print("    last warning:", st["warning"])
            last_report = time.time()
    if fft_ms:
        print(f"\n{n} frames, fft median {np.median(fft_ms):.1f} ms "
              f"(p95 {np.percentile(fft_ms, 95):.1f} ms)")
    st = src.stats()
    extra = "".join(f", {st[k]} {k}" for k in
                    ("dropped", "repeats", "short", "resyncs", "timeouts")
                    if st.get(k))
    print(f"source: {st['frames']} frames, {st['fps']:.2f} fps, "
          f"{st['mbps']:.2f} MB/s{extra or ', clean'}")
    if st.get("repeats"):
        print(f"  {st['repeats']} stale reads were discarded before reaching the "
              f"display; dwell settled at {st.get('fill_ms', 0):.0f} ms "
              f"(--native-acq avoids this entirely)")
    gcw.remove()
    report_timing(args, src, ftlog, gcw, stall)


def run_gui(args, src, eng):
    import pyqtgraph as pg
    from pyqtgraph.Qt import QtCore, QtWidgets

    from frametime import (DISPLAY_FIELDS, FrameLog, GCWatch, StallDetector,
                           nivcsw, stall_line, thread_cpu)

    class TimedPlotWidget(pg.PlotWidget):
        """A PlotWidget that adds up the time Qt spends repainting it.

        The repaint happens *after* the timer callback returns, so it is
        invisible to any stopwatch inside tick() -- yet it runs on the same
        thread and is therefore inside the frame interval.  Left unmeasured it
        would show up as unexplained idle time, which is exactly the bucket we
        are trying to keep meaningful.
        """

        paint_s = 0.0                      # class-wide: totals every plot

        def paintEvent(self, ev):
            t0 = time.perf_counter()
            try:
                super().paintEvent(ev)
            finally:
                TimedPlotWidget.paint_s += time.perf_counter() - t0

    pg.setConfigOptions(antialias=False, useOpenGL=False)
    app = QtWidgets.QApplication([])
    win = QtWidgets.QMainWindow()
    win.setWindowTitle("MHO934 live spectrum")
    central = QtWidgets.QWidget()
    win.setCentralWidget(central)
    layout = QtWidgets.QVBoxLayout(central)
    layout.setContentsMargins(6, 6, 6, 6)

    # -- controls ---------------------------------------------------------
    bar = QtWidgets.QHBoxLayout()
    win_box = QtWidgets.QComboBox(); win_box.addItems(WINDOWS)
    win_box.setCurrentText(args.window)
    avg_box = QtWidgets.QSpinBox(); avg_box.setRange(1, 64); avg_box.setValue(args.average)
    peak_chk = QtWidgets.QCheckBox("peak hold")
    logx_chk = QtWidgets.QCheckBox("log freq")
    reset_btn = QtWidgets.QPushButton("reset avg/peak")
    dc_btn = QtWidgets.QPushButton("capture DC")
    dc_clear_btn = QtWidgets.QPushButton("clear DC")
    span_btn = QtWidgets.QPushButton("full span")
    signal_btn = QtWidgets.QPushButton("zoom to signal")
    timing_chk = QtWidgets.QCheckBox("frame timing")
    timing_chk.setChecked(bool(args.timing_strip))
    for label, w in (("window", win_box), ("average", avg_box),
                     (None, peak_chk), (None, logx_chk), (None, reset_btn),
                     (None, dc_btn), (None, dc_clear_btn), (None, span_btn),
                     (None, signal_btn), (None, timing_chk)):
        if label:
            bar.addWidget(QtWidgets.QLabel(label))
        bar.addWidget(w)
    bar.addStretch(1)
    layout.addLayout(bar)

    plot = TimedPlotWidget()
    plot.setLabel("bottom", "Frequency", units="Hz")
    plot.setLabel("left", "Power", units="dBFS")
    plot.showGrid(x=True, y=True, alpha=0.3)
    plot.setYRange(-160, 5)
    curve = plot.plot(pen=pg.mkPen("#1f9bd1", width=1))
    layout.addWidget(plot, 1)

    # -- frame-interval strip ---------------------------------------------
    # A stall is a tail event: it never shows in the fps number, but it is
    # unmistakable as a spike here.  Wall interval and the GUI thread's CPU time
    # over the same interval are drawn together on purpose -- a spike with CPU
    # following it is work, a spike with CPU flat along the bottom is the thread
    # not running at all.
    strip = TimedPlotWidget()
    strip.setMaximumHeight(150)
    strip.setLabel("left", "frame time, ms")
    strip.setLabel("bottom", "frames ago")
    strip.showGrid(x=False, y=True, alpha=0.3)
    strip.setMouseEnabled(x=False, y=False)
    strip.hideButtons()
    # Log y, because that is the shape of the problem: a 2 s stall next to a
    # 90 ms frame flattens a linear axis into a baseline and a spike, and the
    # baseline is where the ordinary jitter lives.
    strip.setLogMode(x=False, y=True)
    strip.addLegend(offset=(70, 4), labelTextSize="8pt",
                    horSpacing=12, verSpacing=-4)
    iv_curve = strip.plot(pen=pg.mkPen("#e0b040", width=1), name="interval")
    cpu_curve = strip.plot(pen=pg.mkPen("#48a860", width=1), name="GUI cpu")
    src_curve = strip.plot(pen=pg.mkPen("#8060c0", width=1, style=QtCore.Qt.PenStyle.DashLine),
                           name="source gap")
    limit_line = pg.InfiniteLine(angle=0, pen=pg.mkPen("#c04040", width=1,
                                 style=QtCore.Qt.PenStyle.DashLine))
    strip.addItem(limit_line)
    strip.setVisible(bool(args.timing_strip))
    layout.addWidget(strip)

    status = QtWidgets.QLabel("waiting for first frame...")
    status.setFrameStyle(QtWidgets.QFrame.Shape.StyledPanel | QtWidgets.QFrame.Shadow.Sunken)
    layout.addWidget(status)

    state = {"draws": 0, "t0": time.perf_counter(), "fft_ms": 0.0, "last": "",
             "spec": None, "frame": None, "nyquist": 0.0, "ranged": False}

    # -- frame-time instrumentation ---------------------------------------
    # One record per displayed frame, decomposing the interval since the last
    # one into work / paint / idle, with the GUI thread's own CPU time over the
    # same window so starvation is distinguishable from doing too much.
    ftlog = FrameLog("display", DISPLAY_FIELDS, capacity=args.timing_keep)
    gcw = GCWatch().install()
    stall = StallDetector(threshold_ms=args.stall_ms)
    tmr = {"last_end": time.perf_counter(), "last_cpu": thread_cpu(),
           "last_paint": 0.0, "last_ivcsw": nivcsw(), "last_recv": None,
           "last_dropped": 0, "work_s": 0.0, "draw_s": 0.0, "empty": 0,
           "iv": deque(maxlen=args.timing_span),
           "cpu": deque(maxlen=args.timing_span),
           "src": deque(maxlen=args.timing_span)}

    def on_reset():
        eng.reset()
    reset_btn.clicked.connect(on_reset)

    def on_capture_dc():
        frame = state["frame"]
        if frame is None:
            return
        eng.capture_dc(frame.samples)
        if state["spec"] is not None:
            state["spec"] = eng.process(frame.samples, frame.sample_rate)
            redraw()
    dc_btn.clicked.connect(on_capture_dc)

    def on_clear_dc():
        eng.clear_dc()
    dc_clear_btn.clicked.connect(on_clear_dc)

    def on_full_span():
        if state["nyquist"]:
            lo = state["spec"].resolution if logx_chk.isChecked() else 0.0
            plot.setXRange(np.log10(max(lo, 1.0)) if logx_chk.isChecked() else 0.0,
                           np.log10(state["nyquist"]) if logx_chk.isChecked()
                           else state["nyquist"], padding=0.0)
    span_btn.clicked.connect(on_full_span)

    def set_span(lo, hi):
        if logx_chk.isChecked():
            lo = max(lo, 1.0)
            plot.setXRange(np.log10(lo), np.log10(max(hi, lo * 10)), padding=0.0)
        else:
            plot.setXRange(lo, hi, padding=0.0)

    def on_zoom_signal():
        """Frame the actual signal.

        At 2 GSa/s the Nyquist is 1 GHz, so a 200 kHz tone sits in the leftmost
        0.02% of a full-span view and is effectively invisible.  Pick the span
        from where the energy actually is, ignoring DC.
        """
        spec = state["spec"]
        if spec is None:
            return
        skip = max(1, int(1e3 / spec.resolution))      # ignore DC and near-DC
        pk = int(np.argmax(spec.power_db[skip:])) + skip
        f_pk = float(spec.freqs[pk])
        hi = min(max(f_pk * 8.0, 10 * spec.resolution), float(spec.freqs[-1]))
        set_span(spec.resolution, hi)
    signal_btn.clicked.connect(on_zoom_signal)

    def on_logx(v):
        plot.setLogMode(x=bool(v), y=False)
        on_full_span()
    logx_chk.stateChanged.connect(on_logx)

    def visible_span():
        """Current x view in Hz (the view is log10(Hz) when log mode is on)."""
        (x0, x1), _ = plot.getViewBox().viewRange()
        if logx_chk.isChecked():
            x0, x1 = 10.0 ** x0, 10.0 ** x1
        return max(0.0, x0), x1

    def redraw():
        # Timed here rather than in tick() because a user zoom or pan re-enters
        # this through sigXRangeChanged, and that cost belongs to the frame
        # interval it lands in just as much as the per-frame redraw does.
        t_draw = time.perf_counter()
        try:
            _redraw()
        finally:
            tmr["draw_s"] += time.perf_counter() - t_draw

    def _redraw():
        spec = state["spec"]
        if spec is None:
            return
        lo, hi = visible_span()
        # Reduce over the *visible* span, so zooming re-reduces from full
        # resolution instead of magnifying coarse buckets.
        f, d = reduce_for_display(spec.freqs, spec.power_db, args.display_bins,
                                  fmin=lo, fmax=hi)
        if logx_chk.isChecked():
            keep = f > 0          # log-x cannot show DC
            f, d = f[keep], d[keep]
        curve.setData(f, d)

    # Re-reduce when the user zooms or pans, not only when a frame arrives.
    plot.getViewBox().sigXRangeChanged.connect(lambda *_: redraw())

    def tick():
        t_tick = time.perf_counter()
        eng.set_window(win_box.currentText())
        eng.averaging = avg_box.value()
        eng.peak_hold = peak_chk.isChecked()

        t_get = time.perf_counter()
        frame = src.get(timeout=0.0)
        get_ms = (time.perf_counter() - t_get) * 1e3
        if frame is None:
            st = src.stats()
            if not getattr(src, "tap_alive", lambda: True)():
                tail = src.tap_log_tail(3).strip().replace("\n", " | ")
                status.setText(f"on-scope tap exited: {tail[:180]}")
                return
            msg = st.get("error") or st.get("warning")
            if msg and msg != state["last"]:
                state["last"] = msg
                kind = "error" if st.get("error") else "waiting"
                status.setText(f"source {kind}: {msg}")
            # An empty poll is the timer running while the source has nothing.
            # Counting them separates "waiting for the scope" from "blocked",
            # which is the whole question a stall raises.
            tmr["empty"] += 1
            tmr["work_s"] += time.perf_counter() - t_tick
            return
        t0 = time.perf_counter()
        spec = eng.process(frame.samples, frame.sample_rate)
        state["fft_ms"] = (time.perf_counter() - t0) * 1e3
        state["spec"] = spec
        state["frame"] = frame

        nyq = float(spec.freqs[-1])
        if not state["ranged"] or abs(nyq - state["nyquist"]) > 1.0:
            # Fix the x range once so setData cannot feed autorange back into
            # sigXRangeChanged and cause a redraw loop.
            state["nyquist"] = nyq
            state["ranged"] = True
            plot.enableAutoRange(x=False)
            if args.fmax:
                set_span(spec.resolution if logx_chk.isChecked() else 0.0,
                         min(args.fmax, nyq))
            else:
                # Default to framing the signal rather than the full Nyquist:
                # full span buries a low-frequency tone in the first pixel.
                on_zoom_signal()
        redraw()

        # -- close out the frame interval ---------------------------------
        # The boundary is the end of the redraw, so every piece of the window
        # belongs to exactly one interval: this tick's own work, the empty polls
        # and repaint that preceded it, and whatever is left over.
        end = time.perf_counter()
        cpu_end = thread_cpu()
        ivcsw_end = nivcsw()
        paint_now = TimedPlotWidget.paint_s

        draw_ms = tmr["draw_s"] * 1e3
        tmr["draw_s"] = 0.0
        interval_ms = (end - tmr["last_end"]) * 1e3
        cpu_ms = (cpu_end - tmr["last_cpu"]) * 1e3
        paint_ms = (paint_now - tmr["last_paint"]) * 1e3
        work_ms = tmr["work_s"] * 1e3 + (end - t_tick) * 1e3
        _gc_n, gc_ms = gcw.between(tmr["last_end"], end)
        st = src.stats()
        row = ftlog.record(
            t=end,
            interval_ms=interval_ms,
            cpu_ms=cpu_ms,
            work_ms=work_ms,
            paint_ms=paint_ms,
            idle_ms=max(0.0, interval_ms - work_ms - paint_ms),
            get_ms=get_ms,
            fft_ms=state["fft_ms"],
            draw_ms=draw_ms,
            status_ms=tmr.get("status_ms", 0.0),
            gc_ms=gc_ms,
            age_ms=(end - frame.recv_time) * 1e3,
            src_gap_ms=((frame.recv_time - tmr["last_recv"]) * 1e3
                        if tmr["last_recv"] else 0.0),
            empty_ticks=tmr["empty"],
            nivcsw=ivcsw_end - tmr["last_ivcsw"],
            dropped=st.get("dropped", 0) - tmr["last_dropped"],
            base_ms=stall.median,          # what "normal" was, for explain()
            seq=frame.seq)
        if stall.check(interval_ms):
            print(stall_line(row, state["t0"]), flush=True)
        tmr.update(last_end=end, last_cpu=cpu_end, last_paint=paint_now,
                   last_ivcsw=ivcsw_end, last_recv=frame.recv_time,
                   last_dropped=st.get("dropped", 0), work_s=0.0, empty=0)

        t_status = time.perf_counter()
        state["draws"] += 1
        el = end - state["t0"]
        pk = int(np.argmax(spec.power_db))
        tmr["iv"].append(interval_ms)
        tmr["cpu"].append(cpu_ms)
        tmr["src"].append(row["src_gap_ms"])
        if strip.isVisible():
            n = len(tmr["iv"])
            x = np.arange(-n + 1, 1)
            # Log mode cannot plot a zero, and both cpu_ms and the first
            # frame's source gap legitimately reach it.  Clamp at 1 ms rather
            # than at epsilon: a single 0 would otherwise stretch the axis over
            # three decades of nothing and squash the band that matters.
            def _pos(seq):
                return np.maximum(np.fromiter(seq, float, n), 1.0)
            iv_curve.setData(x, _pos(tmr["iv"]))
            cpu_curve.setData(x, _pos(tmr["cpu"]))
            src_curve.setData(x, _pos(tmr["src"]))
            limit_line.setValue(np.log10(max(stall.limit(), 1.0)))
        status.setText(
            f"{frame.npoints:,} pts @ {frame.sample_rate/1e6:.1f} MSa/s  ·  "
            f"{spec.resolution:.0f} Hz/bin  ·  "
            f"src {st['fps']:.2f} fps, {st['mbps']:.1f} MB/s, {st['dropped']} dropped"
            + (f", {st['repeats']} STALE" if st.get("repeats") else "") + "  ·  "
            f"draw {state['draws']/el:.1f} fps  ·  fft {state['fft_ms']:.0f} ms  ·  "
            f"frame {interval_ms:.0f} ms (med {stall.median:.0f}, "
            f"worst {stall.worst:.0f}, {stall.count} stalls)  ·  "
            f"peak {spec.freqs[pk]/1e6:.4f} MHz @ {spec.power_db[pk]:.1f} dBFS"
            + (f"  ·  DC cal {eng.dc_offset:+.0f} codes" if eng.dc_offset else ""))
        # Building the status line lands after the interval boundary, so carry
        # it into the next interval rather than losing it.
        tmr["status_ms"] = (time.perf_counter() - t_status) * 1e3
        tmr["work_s"] = tmr["status_ms"] / 1e3

    timing_chk.stateChanged.connect(lambda v: strip.setVisible(bool(v)))

    timer = QtCore.QTimer()
    timer.timeout.connect(tick)
    timer.start(args.poll_ms)

    if args.run_seconds:
        def _finish():
            print(status.text())
            if args.screenshot:
                # Grab before quitting: the window has to still exist, and
                # widget.grab() renders it directly rather than going through
                # a compositor, so it works under Wayland with no helper tool.
                win.grab().save(args.screenshot)
                print(f"saved {args.screenshot}")
            app.quit()
        QtCore.QTimer.singleShot(int(args.run_seconds * 1000), _finish)

    win.resize(1200, 760)
    win.show()
    try:
        app.exec()
    finally:
        gcw.remove()
        report_timing(args, src, ftlog, gcw, stall)
        src.stop()
        # let the source thread detach cleanly (frida) before we exit
        t = getattr(src, "_thread", None)
        if t is not None:
            t.join(timeout=3.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=("synthetic", "scpi", "stream"),
                    default="synthetic")
    ap.add_argument("--host", default=None, help="scope IP for --source scpi")
    ap.add_argument("--bind", default="0.0.0.0", help="listen address for --source stream")
    ap.add_argument("--tap", default="", metavar="SCOPE_IP",
                    help="launch the on-scope tap too (implies --source stream): "
                         "injects libmhotap.so and streams records straight out "
                         "of the app, bypassing the SCPI reply path")
    ap.add_argument("--pc-host", default="",
                    help="this PC as the scope sees it (default: auto-detect)")
    ap.add_argument("--scpi-ip", default="",
                    help="scope IP for SCPI if different from --tap")
    ap.add_argument("--port", type=int, default=5560)
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--format", default="WORD", choices=("WORD", "BYTE"))
    ap.add_argument("--points", type=int, default=0, help="0 = full record")
    ap.add_argument("--native-acq", default="", metavar="IP:PORT",
                    help="drive acquisition via DrvAcquire_* on the scope "
                         "(adb device id, e.g. 172.30.188.217:55555). Faster "
                         "than :RUN/:STOP and guarantees each record is new; "
                         "needs frida-server (patch_scope.py provisions it)")
    # On by default: the scope's own plot thread is ~60% of a core on a box
    # whose big cores are saturated, and while the spectrum is on the PC its
    # screen is not earning that.  Worth ~2 fps and it removes the stalls, so
    # the demo should not need to be told.  Restored when the tap exits.
    ap.add_argument("--no-tap-quiet-ui", dest="tap_quiet_ui",
                    action="store_false",
                    help="leave the scope redrawing its own waveform while "
                         "the tap streams (costs ~2 fps and reintroduces "
                         "stalls; the default is to pause it and restore it "
                         "on exit)")
    ap.set_defaults(tap_quiet_ui=True)
    ap.add_argument("--fill-ms", type=float, default=0.0,
                    help="override the :RUN dwell before :STOP, in ms "
                         "(SCPI arming only; too short silently re-reads)")
    ap.add_argument("--no-rearm", action="store_true",
                    help="re-read the stored record without re-acquiring (faster, static)")
    ap.add_argument("--window", default="hann", choices=WINDOWS)
    ap.add_argument("--average", type=int, default=1)
    ap.add_argument("--display-bins", type=int, default=2000)
    ap.add_argument("--fmax", type=float, default=0.0,
                    help="initial upper frequency of the view, in Hz "
                         "(default: auto-frame the strongest signal)")
    ap.add_argument("--sample-rate", type=float, default=50e6, help="synthetic only")
    ap.add_argument("--synthetic-fps", type=float, default=20.0)
    ap.add_argument("--poll-ms", type=int, default=10,
                    help="display timer period in ms (how often the source is "
                         "polled; also the floor on frame-to-frame jitter)")
    tg = ap.add_argument_group("frame timing")
    tg.add_argument("--stall-ms", type=float, default=0.0,
                    help="report a frame as a stall above this interval in ms "
                         "(0 = adapt: 2.5x the running median, floor +20 ms)")
    tg.add_argument("--timing-csv", default="",
                    help="on exit, dump per-frame rows here (a -source.csv "
                         "companion gets the source-thread rows)")
    tg.add_argument("--timing-keep", type=int, default=20000,
                    help="how many per-frame records to retain")
    tg.add_argument("--timing-worst", type=int, default=5,
                    help="how many slowest frames to detail on exit")
    tg.add_argument("--timing-span", type=int, default=300,
                    help="frames shown in the live frame-interval strip")
    tg.add_argument("--tap-poll-ms", type=float, default=0.0,
                    help="with --tap: sample the on-scope capture loop this "
                         "often and log slow cycles to the tap log (100 is a "
                         "good value).  Diagnostic only -- the extra Frida RPC "
                         "costs the scope ~2 fps, so leave it off when the "
                         "frame rate itself is what is being measured")
    # On by default, like --tap-quiet-ui: the app sleeps a hardcoded 20 ms
    # before answering any SCPI command and the tap pays it once per frame,
    # which is ~23% of the cycle spent asleep.  Worth 11.3 -> 13.9 fps, and
    # restored when the tap exits, so the demo should not need to be told.
    tg.add_argument("--tap-keep-scpi-sleep", dest="tap_no_scpi_sleep",
                    action="store_false",
                    help="leave the app's hardcoded 20 ms per-SCPI-command "
                         "sleep alone (costs ~2.5 fps; the default is to "
                         "shorten it to --tap-scpi-sleep-us and restore it "
                         "on exit)")
    tg.set_defaults(tap_no_scpi_sleep=True)
    tg.add_argument("--tap-scpi-sleep-us", type=int, default=1000,
                    help="what --tap-no-scpi-sleep shortens the app's 20 ms "
                         "per-command wait to (default 1000; 0 removes it)")
    tg.add_argument("--tap-drive-csv", default="",
                    help="with --tap-poll-ms: per-cycle on-scope phase times")
    tg.add_argument("--no-timing-strip", dest="timing_strip",
                    action="store_false", help="hide the frame-interval strip")
    tg.set_defaults(timing_strip=True)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--run-seconds", type=float, default=0.0,
                    help="close the window after N seconds (demos, smoke tests)")
    ap.add_argument("--screenshot", default="", metavar="PATH",
                    help="save a PNG of the window just before --run-seconds "
                         "closes it")
    ap.add_argument("--seconds", type=float, default=0.0)
    args = ap.parse_args()
    if args.tap:
        args.source = "stream"

    eng = SpectrumEngine(window=args.window, averaging=args.average)
    src = build(args).start()
    try:
        if args.headless:
            run_headless(args, src, eng)
        else:
            run_gui(args, src, eng)
    finally:
        src.stop()


if __name__ == "__main__":
    main()

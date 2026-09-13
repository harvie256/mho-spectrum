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
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from frametime import DISPLAY_FIELDS, SOURCE_FIELDS  # noqa: E402
from spectrum import DETECTORS, SpectrumEngine, WINDOWS  # noqa: E402


def report_timing(args, src, ftlog, gcw=None, stall=None):
    """Print what the frame timing showed, and optionally dump the raw rows.

    Both halves are printed together on purpose: the display log alone cannot
    say whether a stall was ours, and the source log alone cannot say whether a
    clean arrival ever reached the screen.
    """
    if ftlog is None or not ftlog.rows:
        return
    # Off unless asked for.  This is a development dump -- two tables, the five
    # worst frames of each and the GC log -- and it is noise for anyone who
    # just wants to look at a spectrum.  --timing-csv implies it, since asking
    # for the raw rows means you want the analysis too.
    if not (getattr(args, "timing_report", False) or args.timing_csv):
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


# The scope-side changes the tap makes by default.  Everything here is measured
# and restored on exit; the experimental flags below default to off.
#
# ADC_SETTLE is the one that had to be found the hard way.  CDrvScope::SetState
# is 31.6 ms of a 76 ms acquisition cycle, and 20 ms of that is a fixed settling
# wait inside CCalibration_ADC::DrvCalibration_SetAdcStary.  Halving it is worth
# roughly 12.9 -> 14.8 fps.  Halving it *again* is not: at 5 ms the scope starts
# handing back stale records (the receiver CRCs every frame and catches them),
# so 10 ms is the floor, not a starting point for further trimming.
#
# The offset is a return-address-relative movz in the build shipped in
# /data/app/com.rigol.scope-2/base.apk -- NOT the copy in the firmware image,
# which is a different build.  A firmware update will move it; the patch checks
# the instruction really is a movz holding 20000 before writing, so a stale
# offset is refused and the run simply continues unpatched.
# These were command-line flags during development.  They are fixed now: the
# tuning they exposed was only ever useful while chasing the frame loop, and a
# released analyser should not ask anyone to think about them.  The machinery
# behind them is still live -- the on-screen timing strip and --timing-report
# both use it.
TIMING_KEEP = 20000        # frames of per-frame history to retain
TIMING_WORST = 5           # worst frames listed by --timing-report
TIMING_SPAN = 300          # frames shown in the on-screen interval strip
STALL_MS = 0.0             # 0 = adaptive threshold from the running median

ADC_SETTLE_SPEC = "0x3417fc:20000:10000"

SETTINGS_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "mho-spectrum", "settings.json")


def load_settings() -> dict:
    try:
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(**kw) -> None:
    """Best effort -- losing the remembered IP is not worth failing a run."""
    try:
        cur = load_settings()
        cur.update(kw)
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w") as f:
            json.dump(cur, f, indent=2)
    except Exception:
        pass


# Module-level so the QApplication outlives whoever created it.  Without this
# the object is collected as soon as the caller drops its reference, taking the
# C++ instance with it, and the next widget dies with "Must construct a
# QApplication before a QWidget".  PyQt5 tolerated it; PyQt6 does not.
_APP = None


def qt_app(themed: bool = True):
    """The one QApplication, themed once.

    Both the startup dialog and the window need it and either may run first,
    so it is created here rather than at either call site -- Qt allows only
    one instance.  qt-material is optional: without it the native look is
    used, which is why the import is guarded rather than a hard requirement.

    density_scale shrinks Material's default padding.  At the stock size the
    analyser's soft-key rows and the FREQ/AMPT entry boxes no longer fit the
    window without scrolling.
    """
    global _APP
    from pyqtgraph.Qt import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _APP = app
    if themed and not getattr(app, "_mho_themed", False):
        try:
            from qt_material import apply_stylesheet
            apply_stylesheet(app, theme="dark_teal.xml",
                             extra={"density_scale": "-2"})
        except Exception as e:                       # not installed, or broken
            print(f"(theme unavailable, using the native look: {e})")
        # The window frame stays in the desktop's style on purpose.  It is not
        # reachable from a stylesheet, and on GNOME Wayland Qt draws it itself
        # from the palette/colour scheme -- setting both was tried and did not
        # darken it, so the remaining options were forcing the whole desktop to
        # prefer-dark or reimplementing the title bar frameless.  Neither is
        # worth it for a frame.
        app._mho_themed = True
    return app


SOURCE_CHOICES = [
    ("tap",       "Live from the scope (fastest, ~15 fps)"),
    ("scpi",      "Live over SCPI (slower, no injection)"),
    ("synthetic", "Synthetic signal (no scope needed)"),
    ("stream",    "Listen for a tap started elsewhere"),
]


def prompt_startup(source: str, ip: str, themed: bool = True):
    """Ask which source to use and, where it needs one, the scope IP.

    Returns (source, ip), or (None, None) if cancelled.  Defaults come from the
    previous run so the common case is one Return press.

    This creates the QApplication if there is not one yet: the source -- and
    with it the tap injection -- is built before the window, so this dialog
    runs first and run_gui reuses the instance rather than making a second.
    """
    from pyqtgraph.Qt import QtWidgets

    qt_app(themed)
    dlg = QtWidgets.QDialog()
    dlg.setWindowTitle("MHO934 spectrum analyser")
    form = QtWidgets.QFormLayout(dlg)

    combo = QtWidgets.QComboBox()
    for key, label in SOURCE_CHOICES:
        combo.addItem(label, key)
    keys = [k for k, _ in SOURCE_CHOICES]
    combo.setCurrentIndex(keys.index(source) if source in keys else 0)
    form.addRow("Source:", combo)

    edit = QtWidgets.QLineEdit(ip)
    edit.setPlaceholderText("192.168.0.10")
    form.addRow("Scope IP:", edit)

    SB = QtWidgets.QDialogButtonBox.StandardButton

    def sync():
        needs_ip = combo.currentData() in ("tap", "scpi")
        edit.setEnabled(needs_ip)
        buttons.button(SB.Ok).setEnabled(
            bool(edit.text().strip()) or not needs_ip)

    buttons = QtWidgets.QDialogButtonBox(SB.Ok | SB.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    form.addRow(buttons)
    combo.currentIndexChanged.connect(sync)
    edit.textChanged.connect(sync)
    sync()
    edit.setFocus()

    if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return None, None
    return combo.currentData(), edit.text().strip()


def build(args):
    from sources import ScpiSource, StreamSource, SyntheticSource
    if args.source == "synthetic":
        return SyntheticSource(npoints=args.points or 1_000_000,
                               sample_rate=args.sample_rate,
                               fps_limit=args.synthetic_fps,
                               channels=range(1, max(1, min(4, args.synthetic_channels)) + 1))
    if args.source == "scpi":
        if not args.host:
            raise SystemExit("--source scpi needs --host")
        return ScpiSource(args.host, channel=args.channel, fmt=args.format,
                          points=args.points)
    return StreamSource(host=args.bind, port=args.port,
                        scope_ip=args.tap or None, pc_host=args.pc_host or None,
                        channel=args.channel,
                        scpi_ip=args.scpi_ip or None,
                        quiet_ui=args.tap_quiet_ui,
                        quiet_logd=args.tap_quiet_logd,
                        sleep_consts=[ADC_SETTLE_SPEC] if args.tap_adc_sleep else [])


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
        if hasattr(src, "get_group"):
            frames = src.get_group(timeout=2.0)
        else:
            one = src.get(timeout=2.0)
            frames = [one] if one is not None else None
        t_got = time.perf_counter()
        if not frames:
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
        spectra = eng.process(frames)
        frame = frames[0]
        spec = spectra[frame.channel]
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
            extra = ""
            for k in ("resyncs", "timeouts", "short", "stale", "repeats"):
                if st.get(k):
                    extra += f"  {k} {st[k]}"
            # Headless only, and deliberately terse: one line every couple of
            # seconds is a progress indicator, not a measurement.  The detail
            # lives behind --timing-report.
            peaks = []
            for ch, s in spectra.items():
                pk = int(np.argmax(s.power_db))
                peaks.append(f"{'CH%d ' % ch if len(spectra) > 1 else ''}"
                             f"{s.freqs[pk]/1e6:8.4f} MHz @ {s.power_db[pk]:6.1f} dBFS")
            print(f"  {n:5d} frames  {st['fps']:5.1f} fps  peak "
                  + "  |  ".join(peaks) + extra)
            if st.get("warning") and st["warning"] != state_warn.get("last"):
                state_warn["last"] = st["warning"]
                print("    last warning:", st["warning"])
            last_report = time.time()
    if fft_ms and (getattr(args, "timing_report", False) or args.timing_csv):
        print(f"\n{n} frames, fft median {np.median(fft_ms):.1f} ms "
              f"(p95 {np.percentile(fft_ms, 95):.1f} ms)")
    st = src.stats()
    extra = "".join(f", {st[k]} {k}" for k in
                    ("dropped", "repeats", "short", "resyncs", "timeouts")
                    if st.get(k))
    # The trailing-window rate, not the whole-run mean: a run takes a few
    # seconds to reach steady state and averaging over the ramp understates it
    # (a 45 s run once read 12.70 fps cumulative against ~15 steady).  The
    # cumulative figures are still in stats() as fps_avg/mbps_avg.
    print(f"source: {st['frames']} frames, {st['fps']:.1f} fps, "
          f"{st['mbps']:.1f} MB/s{extra or ', clean'}")
    if st.get("repeats"):
        print(f"  {st['repeats']} stale reads were discarded before reaching "
              f"the display")
    gcw.remove()
    report_timing(args, src, ftlog, gcw, stall)


def run_gui(args, src, eng):
    """Build the window and hand control to Qt.

    Everything that used to live in this function is now in window.py and the
    modules under it; what is left is the process-level bracket -- Qt setup,
    and the teardown that has to happen after the event loop returns whether it
    exited cleanly or not.
    """
    import pyqtgraph as pg
    from pyqtgraph.Qt import QtWidgets

    from window import SpectrumWindow

    pg.setConfigOptions(antialias=False, useOpenGL=False)
    app = qt_app(not args.no_theme)
    win = SpectrumWindow(args, src, eng, app)
    win.start()
    win.show()
    try:
        app.exec()
    finally:
        win.teardown()
        report_timing(args, src, win.ftlog, win.gcw, win.stall)
        src.stop()
        # let the source thread detach cleanly (frida) before we exit
        t = getattr(src, "_thread", None)
        if t is not None:
            t.join(timeout=3.0)


def build_parser() -> argparse.ArgumentParser:
    """The CLI.  Split out of main() so tests/acq_sweep.py builds its sources
    from exactly the defaults a normal run gets, rather than a copy of them
    that drifts the next time one is flipped."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=("synthetic", "scpi", "stream"),
                    default="synthetic")
    ap.add_argument("--host", default=None, help="scope IP for --source scpi")
    ap.add_argument("--bind", default="0.0.0.0", help="listen address for --source stream")
    ap.add_argument("--tap", default="", metavar="SCOPE_IP",
                    help="launch the on-scope tap too (implies --source stream): "
                         "injects libmhotap.so and streams records straight out "
                         "of the app, bypassing the SCPI reply path")
    ap.add_argument("--choose", action="store_true",
                    help="pick the source and scope IP in a dialog, defaulting "
                         "to whatever was used last time.  This is what "
                         "`run_fft.sh` with no arguments does; with --headless "
                         "it silently reuses the remembered settings")
    ap.add_argument("--pc-host", default="",
                    help="this PC as the scope sees it (default: auto-detect)")
    ap.add_argument("--scpi-ip", default="",
                    help="scope IP for SCPI if different from --tap")
    ap.add_argument("--port", type=int, default=5560)
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--format", default="WORD", choices=("WORD", "BYTE"),
                    help="scpi source only; the tap always reads 16-bit")
    ap.add_argument("--points", type=int, default=0, help="0 = full record")
    ap.add_argument("--no-tap-quiet-ui", dest="tap_quiet_ui",
                    action="store_false",
                    help="leave the scope redrawing its own waveform while "
                         "the tap streams (costs ~2 fps and reintroduces "
                         "stalls; the default is to pause it and restore it "
                         "on exit)")
    ap.set_defaults(tap_quiet_ui=True)
    ap.add_argument("--window", default="hann", choices=WINDOWS)
    ap.add_argument("--average", type=int, default=1)
    ap.add_argument("--display-bins", type=int, default=2000)
    ap.add_argument("--detector", default="+peak", choices=DETECTORS,
                    help="bin-to-pixel reduction (default +peak). Use rms for "
                         "any noise or channel-power number: +peak overstates "
                         "noise, and sample and avg-log read ~2.5 dB low on it")
    ap.add_argument("--ref-level", type=float, default=0.0, metavar="DBFS",
                    help="top of the graticule in dBFS; giving this (or "
                         "--db-per-div) pins the amplitude axis instead of "
                         "auto-scaling it from each frame")
    ap.add_argument("--db-per-div", type=float, default=0.0,
                    help="vertical scale, over a 10-division graticule")
    ap.add_argument("--fmax", type=float, default=0.0,
                    help="initial upper frequency of the view, in Hz "
                         "(default: auto-frame the strongest signal)")
    ap.add_argument("--sample-rate", type=float, default=50e6, help="synthetic only")
    ap.add_argument("--synthetic-fps", type=float, default=20.0)
    ap.add_argument("--synthetic-channels", type=int, default=1, metavar="N",
                    help="synthetic only: generate CH1..CHN (1-4), each with "
                         "its own tones, to exercise the multi-channel display")
    ap.add_argument("--poll-ms", type=int, default=10,
                    help="display timer period in ms (how often the source is "
                         "polled; also the floor on frame-to-frame jitter)")
    tg = ap.add_argument_group("frame timing")
    tg.add_argument("--timing-report", action="store_true",
                    help="print the per-frame timing breakdown on exit "
                         "(display and source tables, worst frames, GC). "
                         "Implied by --timing-csv")
    tg.add_argument("--timing-csv", default="",
                    help="on exit, dump per-frame rows here (a -source.csv "
                         "companion gets the source-thread rows)")
    # On by default for the same reason as the redraw pause: the scope's logging
    # is the largest non-app CPU consumer while streaming, and none of it is
    # wanted during a measurement.  Measured 2026-09-09: 0.62 of a core back,
    # 88.8% -> 81.7% busy, 12.5 -> 13.4 fps.  logd is restarted on exit.
    tg.add_argument("--tap-keep-logd", dest="tap_quiet_logd",
                    action="store_false",
                    help="leave the scope's logd running.  By default the tap "
                         "stops it for the session (worth ~0.6 of a core) and "
                         "starts it again on exit; use this if you need the "
                         "scope's logs while streaming")
    tg.set_defaults(tap_quiet_logd=True)
    # The third and last scope-side change, alongside the redraw pause and
    # logd; all three are restored on exit.
    tg.add_argument("--tap-keep-adc-sleep", dest="tap_adc_sleep",
                    action="store_false",
                    help="leave the ADC settling wait at its stock 20 ms. By "
                         f"default it is patched to 10 ms ({ADC_SETTLE_SPEC}), "
                         "worth ~2 fps; 5 ms was tried and returns stale records")
    # TEMPORARILY OFF (2026-09-13) while checking whether shortening the ADC
    # settling wait is behind the occasional stalls.  Flip back to True to
    # restore it -- it is worth ~2 fps.
    tg.set_defaults(tap_adc_sleep=False)
    tg.add_argument("--no-timing-strip", dest="timing_strip",
                    action="store_false", help="hide the frame-interval strip")
    tg.set_defaults(timing_strip=True)
    ap.add_argument("--no-theme", action="store_true",
                    help="use the native Qt look instead of the dark theme")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--run-seconds", type=float, default=0.0,
                    help="close the window after N seconds (demos, smoke tests)")
    ap.add_argument("--screenshot", default="", metavar="PATH",
                    help="save a PNG of the window just before --run-seconds "
                         "closes it")
    ap.add_argument("--seconds", type=float, default=0.0)
    return ap


def main():
    args = build_parser().parse_args()
    # Former flags, see the constants at the top of this file.
    args.stall_ms = STALL_MS
    args.timing_keep = TIMING_KEEP
    args.timing_worst = TIMING_WORST
    args.timing_span = TIMING_SPAN

    # With no source on the command line, ask.  The point of the dialog is
    # that the common case needs no arguments at all: it opens on whatever was
    # used last (tap the first time) and one Return starts it.
    cfg = load_settings()
    if args.choose:
        chosen_ip = cfg.get("scope_ip", "")
        if args.headless:
            # No dialog without a display; reuse what was remembered so
            # unattended runs still work.
            args.source = cfg.get("source", "tap")
        else:
            args.source, chosen_ip = prompt_startup(cfg.get("source", "tap"),
                                                    chosen_ip,
                                                    not args.no_theme)
            if args.source is None:
                return                                  # cancelled
            save_settings(source=args.source)
            if chosen_ip:
                save_settings(scope_ip=chosen_ip)
        if args.source in ("tap", "scpi") and not chosen_ip:
            raise SystemExit("no scope IP: give one, e.g. --tap 192.168.0.10")
        if args.source == "tap":
            args.tap = chosen_ip
        elif args.source == "scpi":
            args.host = chosen_ip

    if args.tap:
        args.source = "stream"
        if args.tap != cfg.get("scope_ip", ""):
            save_settings(scope_ip=args.tap, source="tap")

    # One engine per channel, driven as one; a single-channel source simply
    # only ever creates CH1's.
    from channels import ChannelEngines
    eng = ChannelEngines(window=args.window, averaging=args.average)
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

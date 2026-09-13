#!/usr/bin/env python3
"""Sweep the capture and GUI across timebases and memory depths.

Everything so far has been measured at 2 ms/div and 1 Mpt.  This walks a
timebase x depth matrix and, for each point, checks that the tap still delivers
what the scope says it captured and that the window still keeps up.

    # what would run, and roughly how long it takes
    .venv/bin/python tests/acq_sweep.py --dry-run

    # live: headless capture check + GUI smoke test at every point, with a
    # known 1 MHz tone on the input
    .venv/bin/python tests/acq_sweep.py 192.168.23.20 --tone 1M

    # a narrower sweep, capture only
    .venv/bin/python tests/acq_sweep.py --timebases 1ms,2ms,5ms \\
        --depths 100k,1M --no-gui

    # no scope: the PC half only (FFT and draw cost at each record size)
    .venv/bin/python tests/acq_sweep.py --synthetic

Per point, live:

  1. Over SCPI, set :TIMebase:MAIN:SCALe and :ACQuire:MDEPth and read back what
     the scope actually took -- the scope silently clamps both, and a point it
     refused is reported as `rejected` rather than tested at the wrong setting.
  2. Headless: start a tap session built by fft_gui.build() (so the same
     scope-side defaults as a normal run), let it settle, then check every
     frame: point count constant and equal to what the tap primed, sample rate
     equal to the SCPI readback, spectrum finite with Nyquist in the right
     place, and no byte-identical repeats.
  3. GUI: `run_fft.sh tap IP --run-seconds N --screenshot --timing-csv`, then
     parse the status line it prints on exit and the display CSV (draw_ms /
     work_ms are the columns that catch a feature's cost -- see CLAUDE.md).

Each point needs its own tap session: tap_stream.py reads the point count and
sample rate once while priming and sizes the scope-side buffers from them, and
nothing talks SCPI while it streams.  Frida startup makes that ~10-15 s of
overhead a session.

The scope's original timebase and depth are restored at the end, including on
Ctrl+C.  Results land in --out as results.csv / results.json, with each
session's tap log, GUI log, screenshot and timing CSV beside them.

Known limits this is expected to hit, rather than bugs it has found:

  * The on-scope driver abandons an acquisition after a fixed 2 s
    (libmhotap.c, driver_main).  A record longer than that -- 10 x timebase,
    so beyond ~200 ms/div -- will produce arm timeouts and no frames.
  * The tap mallocs NSLOTS (3) x depth x 2 B on the scope: 60 MB at 10 M,
    300 MB at 50 M, and a 50 M record is 100 MB a frame over a link measured
    at ~35 MB/s.  Depths above 10 M need --allow-large.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "spectrum"))
sys.path.insert(0, os.path.join(ROOT, "device"))

import fft_gui  # noqa: E402
from rigol_mho import Scope, ScopeError  # noqa: E402
from spectrum import SpectrumEngine  # noqa: E402

RUN_FFT = os.path.join(ROOT, "run_fft.sh")

DEFAULT_TIMEBASES = "500us,2ms,10ms,50ms"
DEFAULT_DEPTHS = "10k,100k,1M,10M"
LARGE_DEPTH = 10_000_000

# Rough wall-clock per session for the --dry-run estimate: frida attach, tap
# priming and teardown, on top of the measured seconds.  Not a measurement --
# refine it from a real run's results.json (t_first_frame_s) if it matters.
SESSION_OVERHEAD_S = 20.0

# The synthetic source has no scope to clamp the sample rate, so the emulated
# rate is depth / (10 x timebase) capped here.  This is an assumption, not the
# MHO934's documented maximum: the scope was seen at 500 MSa/s (10 M, 2 ms/div),
# so the true cap is at least that.
SYNTH_MAX_SRATE = 2.5e9

_SI = {"n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "": 1.0,
       "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}


# -- units ------------------------------------------------------------------

def parse_si(text: str, unit: str = "") -> float:
    """'500us' -> 5e-4, '2ms' -> 2e-3, '10k' -> 1e4, '1M' -> 1e6, '1e-3' -> 1e-3."""
    t = text.strip()
    if unit and t.endswith(unit):
        t = t[:-len(unit)]
    m = re.fullmatch(r"([0-9.eE+-]+)\s*([nuµmkKMG]?)", t)
    if not m:
        raise argparse.ArgumentTypeError(f"cannot parse {text!r}")
    # 'm' is milli for a time and never a valid depth; 'M' is mega for both.
    return float(m.group(1)) * _SI[m.group(2)]


def fmt_time(s: float) -> str:
    for scale, suffix in ((1.0, "s"), (1e-3, "ms"), (1e-6, "us"), (1e-9, "ns")):
        if s >= scale:
            return f"{s / scale:g}{suffix}"
    return f"{s:g}s"


def depth_token(n: float) -> str:
    """The form :ACQuire:MDEPth takes: 10k, 1M, 25M."""
    n = int(round(n))
    if n >= 1_000_000 and n % 1_000_000 == 0:
        return f"{n // 1_000_000}M"
    if n >= 1_000 and n % 1_000 == 0:
        return f"{n // 1_000}k"
    return str(n)


def _close(a: float, b: float, rel: float) -> bool:
    return abs(a - b) <= rel * max(abs(a), abs(b), 1e-30)


# -- scope state ------------------------------------------------------------

def read_state(sc: Scope) -> dict:
    md_raw = sc.query(":ACQuire:MDEPth?")
    try:
        md = float(md_raw)
    except ValueError:
        md = None                                     # AUTO
    return {"timebase": float(sc.query(":TIMebase:MAIN:SCALe?")),
            "mdepth_raw": md_raw, "mdepth": md,
            "srate": float(sc.query(":ACQuire:SRATe?"))}


def apply_state(ip: str, timebase: float, depth: str) -> dict:
    """Set timebase and depth, then read back what the scope really took.

    Done in RUN: on Rigol's DHO/MHO line the memory depth is only accepted while
    the scope is running.  The socket is closed before returning so it is not
    held open under the tap's own SCPI connection.
    """
    with Scope(host=ip, timeout=10.0) as sc:
        sc.write(":RUN")
        sc.write(f":TIMebase:MAIN:SCALe {timebase:g}")
        sc.write(f":ACQuire:MDEPth {depth}")
        sc.opc()
        # The SCPI tick is ~25 ms and a depth change reallocates acquisition
        # memory; give it a moment rather than reading back a half-applied state.
        time.sleep(0.5)
        return read_state(sc)


def restore_state(ip: str, orig: dict) -> None:
    token = "AUTO" if orig["mdepth"] is None else depth_token(orig["mdepth"])
    try:
        got = apply_state(ip, orig["timebase"], token)
        print(f"restored scope to {fmt_time(got['timebase'])}/div, "
              f"depth {got['mdepth_raw']}, {got['srate'] / 1e6:g} MSa/s")
    except (ScopeError, OSError) as e:
        print(f"WARNING: could not restore the scope ({e}); it was "
              f"{fmt_time(orig['timebase'])}/div at depth {token}",
              file=sys.stderr)


# -- tap log ----------------------------------------------------------------

# tap_stream.py's own lines, via adbutil.log.  Formats as of 2026-09-13; if a
# field goes missing here, check the f-strings in device/tap_stream.py.
RE_PRIMED = re.compile(r"CH\d+ (?:WORD|BYTE): (\d+) pts")
# The export capture loop's stats line.  Logs from the old SCPI-driven loop
# (empty=, decl=, chunks=...) do not match and parse as no tap stats.
RE_TAPSTAT = re.compile(
    r"tap\s+([\d.]+) fps\s+([\d.]+) MB/s\s+in=(\d+) sent=(\d+) dropped=(\d+)"
    r"\s+cycles=(\d+) arm=(\d+)ms rnt=(\d+)ms lock=(\d+)ms export=(\d+)ms "
    r"cycle=(\d+)ms armTO=(\d+) expErr=(\d+)")


def parse_tap_log(path: str) -> dict:
    out: dict = {}
    try:
        text = open(path, errors="replace").read()
    except OSError:
        return out
    m = RE_PRIMED.search(text)
    if m:
        out["tap_npts"] = int(m.group(1))
    stats = RE_TAPSTAT.findall(text)
    if stats:
        # Counters are cumulative, so the last report is the session total
        # (bar the final <2 s, which is after measurement ended anyway).
        s = stats[-1]
        out.update(tap_fps=float(s[0]), tap_dropped=int(s[4]),
                   cycles=int(s[5]), arm_ms=int(s[6]), rnt_ms=int(s[7]),
                   lock_ms=int(s[8]), export_ms=int(s[9]), cycle_ms=int(s[10]),
                   arm_timeouts_total=int(s[11]),
                   export_errors_total=int(s[12]))
        # The warning counters are taken relative to the first report, which
        # covers the tap's startup -- counting a transient there would WARN
        # every point on the session start, not on the capture.  Needs two
        # reports, i.e. a session longer than ~4 s.
        base = stats[0] if len(stats) > 1 else ("0",) * len(s)
        out.update(arm_timeouts=int(s[11]) - int(base[11]),
                   export_errors=int(s[12]) - int(base[12]))
    out["tap_warnings"] = len(re.findall(r"WARNING", text))
    return out


# -- headless capture check -------------------------------------------------

def gui_args(a, cfg: dict, extra=()) -> list[str]:
    """fft_gui arguments for this point, shared by both phases."""
    if a.synthetic:
        return ["--source", "synthetic", "--points", str(cfg["expect_npts"]),
                "--sample-rate", repr(cfg["expect_srate"]), *extra]
    return ["--tap", a.ip, "--channel", str(a.channel), *extra]


def run_headless(a, cfg: dict, outdir: str, tag: str) -> dict:
    args = fft_gui.build_parser().parse_args(gui_args(a, cfg))
    args.stall_ms, args.timing_keep = fft_gui.STALL_MS, fft_gui.TIMING_KEEP
    # main() is what turns --tap into --source stream.  Without this the
    # parser's default source (synthetic) is built instead, which quietly
    # passes every check at 1 Mpt / 50 MSa/s -- found on the first live run.
    if args.tap:
        args.source = "stream"
    eng = SpectrumEngine(window=args.window, averaging=1)
    r: dict = {}
    src = fft_gui.build(args).start()
    try:
        _headless_loop(a, src, eng, cfg, r)
    finally:
        src.stop()
        t = getattr(src, "_thread", None)
        if t is not None:
            t.join(timeout=3.0)
        log = getattr(getattr(src, "_tap_log", None), "name", None)
        if log and os.path.exists(log):
            dst = os.path.join(outdir, f"{tag}-tap.log")
            shutil.copy(log, dst)
            r.update(parse_tap_log(dst))
    return r


def _headless_loop(a, src, eng, cfg: dict, r: dict) -> None:
    alive = getattr(src, "tap_alive", lambda: True)
    t0 = time.perf_counter()

    # Wait for the first frame.  This is where frida attach, priming and a
    # long record's first acquisition all land, so it is timed separately and
    # never mixed into the rate.
    while True:
        f = src.get(timeout=1.0)
        if f is not None:
            break
        st = src.stats()
        if st.get("error"):
            r["error"] = f"source error: {st['error']}"
            return
        if not alive():
            r["error"] = "tap exited: " + src.tap_log_tail(3).strip()
            return
        if time.perf_counter() - t0 > a.warmup:
            r["error"] = f"no frame within {a.warmup:.0f} s"
            return
    r["t_first_frame_s"] = round(time.perf_counter() - t0, 2)

    # The first few seconds of a session are a ramp (stream_client.py measured
    # 12.7 fps cumulative against ~15 steady), so settle before counting.
    t_settle = time.perf_counter() + a.settle
    while time.perf_counter() < t_settle:
        src.get(timeout=0.5)

    st0 = src.stats()
    b0 = getattr(src, "bytes", None)
    t_start = time.perf_counter()
    npts, srates, fft_ms = set(), set(), []
    nonfinite = nyq_bad = tone_bad = 0
    peak_hz: list[float] = []
    while time.perf_counter() - t_start < a.seconds:
        f = src.get(timeout=1.0)
        if f is None:
            if not alive():
                r["error"] = "tap exited mid-run: " + src.tap_log_tail(3).strip()
                break
            continue
        t = time.perf_counter()
        spec = eng.process(f.samples, f.sample_rate)
        fft_ms.append((time.perf_counter() - t) * 1e3)
        npts.add(f.npoints)
        srates.add(f.sample_rate)
        # Outside the FFT timing on purpose: this is the harness's cost, not
        # the analyser's.
        if not np.isfinite(spec.power_db).all():
            nonfinite += 1
        if abs(spec.freqs[-1] - f.sample_rate / 2) > 2 * spec.resolution:
            nyq_bad += 1
        if a.tone:
            # The only check that knows what the samples should contain.  A
            # record whose sample spacing does not match its header passes
            # everything above -- 10 M first failed exactly that way, with a
            # 1 MHz input peaking at 10 MHz.  Bin 0 (DC) is skipped.
            pk = 1 + int(np.argmax(spec.power_db[1:]))
            peak_hz.append(float(spec.freqs[pk]))
            if abs(spec.freqs[pk] - a.tone) > max(3 * spec.resolution, 1e-3 * a.tone):
                tone_bad += 1
    el = time.perf_counter() - t_start
    st1 = src.stats()

    frames = st1["frames"] - st0["frames"]
    r.update(
        frames=frames,
        fps=round(frames / el, 2) if el > 0 else 0.0,
        processed=len(fft_ms),
        pc_dropped=st1.get("dropped", 0) - st0.get("dropped", 0),
        repeats=st1.get("repeats", 0) - st0.get("repeats", 0),
        frame_npts=sorted(npts), frame_srates=sorted(srates),
        nonfinite=nonfinite, nyquist_bad=nyq_bad,
        tone_hz=a.tone, tone_bad=tone_bad,
        peak_hz=round(float(np.median(peak_hz)), 1) if peak_hz else None)
    if b0 is not None:
        r["mbps"] = round((src.bytes - b0) / el / 1e6, 1)
    if fft_ms:
        r["fft_ms_med"] = round(float(np.median(fft_ms)), 1)
        r["fft_ms_p95"] = round(float(np.percentile(fft_ms, 95)), 1)


# -- GUI smoke test ---------------------------------------------------------

RE_STATUS = re.compile(
    r"([\d,]+) pts @ ([\d.]+) MSa/s\s+·\s+src ([\d.]+) fps, ([\d.]+) MB/s, "
    r"(\d+) dropped(?:, (\d+) STALE)?\s+·\s+draw ([\d.]+) fps\s+·\s+"
    r"fft (\d+) ms\s+·\s+frame (\d+) ms \(med (\d+), worst (\d+), (\d+) stalls\)")


def run_gui(a, cfg: dict, outdir: str, tag: str) -> dict:
    shot = os.path.join(outdir, f"{tag}.png")
    tcsv = os.path.join(outdir, f"{tag}-timing.csv")
    extra = ["--run-seconds", str(a.gui_seconds), "--screenshot", shot,
             "--timing-csv", tcsv]
    # Through run_fft.sh rather than fft_gui.py, so the launcher's environment
    # (Qt binding pin, XDG_SESSION_TYPE) is part of what gets tested.
    if a.synthetic:
        cmd = [RUN_FFT, "synthetic", *gui_args(a, cfg, extra)[2:]]
    else:
        cmd = [RUN_FFT, "tap", a.ip, "--channel", str(a.channel), *extra]
    logpath = os.path.join(outdir, f"{tag}-gui.log")
    r: dict = {}
    with open(logpath, "w") as log:
        try:
            # The tap's teardown is capped at 15 s inside StreamSource.stop,
            # and startup is inside --run-seconds, so this bound is generous.
            p = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                               timeout=a.gui_seconds + 90)
            r["gui_rc"] = p.returncode
        except subprocess.TimeoutExpired:
            r["gui_error"] = "GUI did not exit"
            return r
    text = open(logpath, errors="replace").read()
    m = None
    for line in text.splitlines():
        m = RE_STATUS.search(line) or m
    if m is None and "display timing --" in text:
        # Frames were drawn, but _finish() never printed: the window was
        # closed (by hand or by the window manager) before --run-seconds.
        # Seen on the first live run; the capture itself was healthy.
        r["gui_error"] = ("window closed before --run-seconds, so no status "
                          "line or screenshot")
    elif m is None:
        # _finish() prints whatever the status bar says, which on a failed
        # session is the "source waiting: ..." / "tap exited" text.
        tail = [ln for ln in text.splitlines() if ln.strip()][-3:]
        r["gui_error"] = "no frame reached the window: " + " | ".join(tail)[:200]
    else:
        r.update(gui_npts=int(m.group(1).replace(",", "")),
                 gui_src_fps=float(m.group(3)), gui_dropped=int(m.group(5)),
                 gui_stale=int(m.group(6) or 0),
                 gui_draw_fps=float(m.group(7)), gui_frame_med_ms=int(m.group(10)),
                 gui_frame_worst_ms=int(m.group(11)), gui_stalls=int(m.group(12)))
    r["screenshot"] = shot if os.path.exists(shot) else ""
    if os.path.exists(tcsv):
        with open(tcsv) as fh:
            rows = list(csv.DictReader(fh))
        # Skip the first rows: they include the first frame's range setup.
        rows = rows[5:] if len(rows) > 10 else rows
        for col in ("draw_ms", "work_ms", "fft_ms", "interval_ms"):
            vals = [float(x[col]) for x in rows if x.get(col) not in (None, "")]
            if vals:
                r[f"gui_{col}_med"] = round(float(np.median(vals)), 1)
    return r


# -- verdict ----------------------------------------------------------------

def judge(cfg: dict, r: dict) -> tuple[str, list[str]]:
    fail, warn = [], []
    if cfg.get("rejected"):
        return "SKIP", [cfg["rejected"]]
    if r.get("error"):
        fail.append(r["error"])
    else:
        if not r.get("frames"):
            fail.append("no frames in the measurement window")
        if len(r.get("frame_npts", [])) > 1:
            fail.append(f"point count changed mid-run: {r['frame_npts']}")
        if r.get("frame_npts") and r.get("tap_npts") and \
                r["frame_npts"][0] != r["tap_npts"]:
            fail.append(f"frames carry {r['frame_npts'][0]} pts but the tap "
                        f"primed {r['tap_npts']}")
        if r.get("frame_srates") and cfg.get("srate") and \
                not _close(r["frame_srates"][0], cfg["srate"], 0.005):
            fail.append(f"header rate {r['frame_srates'][0]:g} != SCPI "
                        f"{cfg['srate']:g}")
        if r.get("repeats"):
            fail.append(f"{r['repeats']} byte-identical repeat frames")
        if r.get("nonfinite"):
            fail.append(f"{r['nonfinite']} spectra with NaN/inf")
        if r.get("nyquist_bad"):
            fail.append(f"{r['nyquist_bad']} spectra with Nyquist misplaced")
        if r.get("tone_bad"):
            fail.append(f"{r['tone_bad']} spectra peaked away from the "
                        f"{r['tone_hz'] / 1e6:g} MHz tone (median peak "
                        f"{r['peak_hz'] / 1e6:g} MHz)")

        n = r["frame_npts"][0] if r.get("frame_npts") else 0
        if n and cfg.get("mdepth") and n != int(cfg["mdepth"]):
            # Not necessarily wrong -- the record may be what the screen holds
            # at a clamped rate -- but it is exactly what this sweep is for.
            warn.append(f"record {n} pts != MDEPth {int(cfg['mdepth'])}")
        if n and r.get("frame_srates"):
            acq = n / r["frame_srates"][0]
            span = 10 * cfg["timebase"]
            if not _close(acq, span, 0.02):
                warn.append(f"record spans {fmt_time(acq)}, screen "
                            f"{fmt_time(span)}")
        # Throughput is deliberately not judged: fps, FFT time and frames
        # dropped because the sender or the display could not keep up are
        # expected to fall off with record size, and are reported, not warned
        # on.  Only signs the capture itself misbehaved are.
        for k, what in (("arm_timeouts", "captures with no ReadNormTrace "
                                         "success in 2 s"),
                        ("export_errors", "exports that returned an error")):
            if r.get(k):
                warn.append(f"{r[k]} {what}")
    if "gui_rc" in r or "gui_error" in r:
        if r.get("gui_error"):
            fail.append("GUI: " + r["gui_error"])
        elif r.get("gui_rc"):
            fail.append(f"GUI exited {r['gui_rc']}")
        if r.get("gui_stale"):
            fail.append(f"GUI showed {r['gui_stale']} STALE")
        if r.get("gui_npts") and r.get("frame_npts") and \
                r["gui_npts"] != r["frame_npts"][0]:
            fail.append(f"GUI saw {r['gui_npts']} pts, headless "
                        f"{r['frame_npts'][0]}")
    return ("FAIL" if fail else "WARN" if warn else "PASS"), fail + warn


# -- driver -----------------------------------------------------------------

COLUMNS = [
    ("tb", 7), ("depth", 6), ("MDEPth", 9), ("MSa/s", 8), ("pts", 9),
    ("acq", 7), ("fps", 5), ("MB/s", 5), ("fft", 5), ("pcDrop", 6),
    ("armTO", 5), ("rep", 4), ("draw", 5), ("frMed", 5), ("drawMs", 6),
    ("verdict", 7),
]


def row_cells(res: dict) -> list[str]:
    c, r = res["config"], res["result"]
    n = r["frame_npts"][0] if r.get("frame_npts") else None
    sr = r["frame_srates"][0] if r.get("frame_srates") else c.get("srate")

    def g(v, f="{}"):
        return "-" if v is None else f.format(v)
    return [fmt_time(c["timebase_req"]), c["depth_req"],
            g(c.get("mdepth"), "{:.0f}"), g(sr and sr / 1e6, "{:g}"), g(n),
            g(n and sr and fmt_time(n / sr)), g(r.get("fps")), g(r.get("mbps")),
            g(r.get("fft_ms_med")), g(r.get("pc_dropped")),
            g(r.get("arm_timeouts")), g(r.get("repeats")),
            g(r.get("gui_draw_fps")), g(r.get("gui_frame_med_ms")),
            g(r.get("gui_draw_ms_med")), res["verdict"]]


def print_table(results: list[dict]) -> None:
    print()
    print("  ".join(f"{h:>{w}}" for h, w in COLUMNS))
    for res in results:
        print("  ".join(f"{v:>{w}}" for v, (_, w) in zip(row_cells(res), COLUMNS)))
        for note in res["notes"]:
            print(f"{'':>9}- {note}")


def write_results(outdir: str, results: list[dict]) -> None:
    with open(os.path.join(outdir, "results.json"), "w") as fh:
        json.dump(results, fh, indent=2, default=str)
    with open(os.path.join(outdir, "results.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([h for h, _ in COLUMNS] + ["notes"])
        for res in results:
            w.writerow(row_cells(res) + ["; ".join(res["notes"])])


def plan(a) -> list[dict]:
    cfgs = []
    for d in a.depths:
        n = parse_si(d)
        if n > LARGE_DEPTH and not a.allow_large:
            print(f"skipping depth {d}: above {depth_token(LARGE_DEPTH)} needs "
                  f"--allow-large (scope-side buffers are 3 x depth x 2 B)")
            continue
        for tb_text in a.timebases:
            tb = parse_si(tb_text, "s")
            cfg = {"timebase_req": tb, "depth_req": depth_token(n)}
            if a.synthetic:
                sr = min(n / (10 * tb), a.synthetic_max_srate)
                cfg.update(timebase=tb, mdepth=n, srate=sr,
                           expect_npts=int(n), expect_srate=sr)
            cfgs.append(cfg)
    return cfgs


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("ip", nargs="?", default="",
                    help="scope IP (default: the one run_fft.sh last used)")
    ap.add_argument("--timebases", default=DEFAULT_TIMEBASES,
                    help=f"comma list, s/div (default {DEFAULT_TIMEBASES})")
    ap.add_argument("--depths", default=DEFAULT_DEPTHS,
                    help=f"comma list of memory depths (default {DEFAULT_DEPTHS})")
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--tone", type=lambda t: parse_si(t, "Hz"), default=0.0,
                    metavar="HZ",
                    help="a tone known to be on the input, e.g. 1M; every "
                         "spectrum's strongest peak must land on it.  Catches "
                         "records whose samples do not match their sample rate")
    ap.add_argument("--seconds", type=float, default=10.0,
                    help="headless measurement window per point, after settling")
    ap.add_argument("--settle", type=float, default=3.0,
                    help="seconds discarded after the first frame")
    ap.add_argument("--warmup", type=float, default=60.0,
                    help="give up if no first frame arrives within this")
    ap.add_argument("--gui-seconds", type=float, default=30.0,
                    help="--run-seconds for the GUI phase; tap startup counts "
                         "against it, so leave ~15 s for that")
    ap.add_argument("--no-gui", dest="gui", action="store_false",
                    help="headless capture check only")
    ap.add_argument("--no-headless", dest="headless", action="store_false",
                    help="GUI smoke test only")
    ap.add_argument("--pause", type=float, default=3.0,
                    help="seconds between sessions, so the previous tap's "
                         "teardown and the listener's port are clear")
    ap.add_argument("--allow-large", action="store_true",
                    help=f"permit depths above {depth_token(LARGE_DEPTH)}")
    ap.add_argument("--synthetic", action="store_true",
                    help="no scope: emulate each point with the synthetic "
                         "source, which tests the PC half alone")
    ap.add_argument("--synthetic-max-srate", type=float, default=SYNTH_MAX_SRATE)
    ap.add_argument("--out", default="",
                    help="results directory (default /tmp/mho-sweep-<time>)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the matrix and a time estimate, then exit")
    a = ap.parse_args()
    a.timebases = [t for t in a.timebases.split(",") if t.strip()]
    a.depths = [d for d in a.depths.split(",") if d.strip()]

    cfgs = plan(a)
    if not cfgs:
        print("nothing to run")
        return 2
    per = (SESSION_OVERHEAD_S if not a.synthetic else 3.0) + \
        (a.settle + a.seconds if a.headless else 0.0) + \
        (a.gui_seconds + 10.0 if a.gui else 0.0) + a.pause
    print(f"{len(cfgs)} points x ({'headless' if a.headless else ''}"
          f"{' + ' if a.headless and a.gui else ''}{'GUI' if a.gui else ''}), "
          f"roughly {len(cfgs) * per / 60:.0f} min")
    if a.dry_run:
        for c in cfgs:
            print(f"  {fmt_time(c['timebase_req']):>6}/div  depth {c['depth_req']}")
        return 0

    if a.gui and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("no display: skipping the GUI phase")
        a.gui = False

    orig = None
    if not a.synthetic:
        a.ip = a.ip or fft_gui.load_settings().get("scope_ip", "")
        if not a.ip:
            ap.error("no scope IP given and none remembered")
        try:
            with Scope(host=a.ip, timeout=10.0) as sc:
                idn = sc.idn
                orig = read_state(sc)
        except (ScopeError, OSError) as e:
            print(f"cannot reach the scope at {a.ip}: {e}", file=sys.stderr)
            return 1
        print(f"{idn}\nstarting from {fmt_time(orig['timebase'])}/div, depth "
              f"{orig['mdepth_raw']}, {orig['srate'] / 1e6:g} MSa/s")

    outdir = a.out or time.strftime("/tmp/mho-sweep-%Y%m%d-%H%M%S")
    os.makedirs(outdir, exist_ok=True)
    print(f"results in {outdir}")

    results: list[dict] = []
    try:
        for i, cfg in enumerate(cfgs):
            tag = f"{fmt_time(cfg['timebase_req'])}-{cfg['depth_req']}"
            print(f"\n[{i + 1}/{len(cfgs)}] {fmt_time(cfg['timebase_req'])}/div, "
                  f"depth {cfg['depth_req']}", flush=True)
            r: dict = {}
            if not a.synthetic:
                try:
                    got = apply_state(a.ip, cfg["timebase_req"], cfg["depth_req"])
                except (ScopeError, OSError) as e:
                    cfg["rejected"] = f"SCPI failed: {e}"
                    got = None
                if got:
                    cfg.update(timebase=got["timebase"], mdepth=got["mdepth"],
                               srate=got["srate"])
                    print(f"  scope: {fmt_time(got['timebase'])}/div, depth "
                          f"{got['mdepth_raw']}, {got['srate'] / 1e6:g} MSa/s")
                    if not _close(got["timebase"], cfg["timebase_req"], 0.01):
                        cfg["rejected"] = (f"timebase became "
                                           f"{fmt_time(got['timebase'])}")
                    elif got["mdepth"] is None or \
                            not _close(got["mdepth"], parse_si(cfg["depth_req"]), 0.01):
                        cfg["rejected"] = f"depth became {got['mdepth_raw']}"
            if not cfg.get("rejected"):
                if a.headless:
                    r.update(run_headless(a, cfg, outdir, tag))
                    time.sleep(a.pause)
                if a.gui:
                    r.update(run_gui(a, cfg, outdir, tag))
                    time.sleep(a.pause)
            verdict, notes = judge(cfg, r)
            results.append({"config": cfg, "result": r, "verdict": verdict,
                            "notes": notes})
            print(f"  {verdict}" + "".join(f"\n    - {n}" for n in notes))
            write_results(outdir, results)       # keep what we have if killed
    except KeyboardInterrupt:
        print("\ninterrupted -- restoring the scope and writing what ran")
    finally:
        if orig is not None:
            restore_state(a.ip, orig)
        write_results(outdir, results)

    print_table(results)
    print(f"\nwrote {outdir}/results.csv and results.json")
    return 1 if any(res["verdict"] == "FAIL" for res in results) else 0


if __name__ == "__main__":
    sys.exit(main())

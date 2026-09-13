#!/usr/bin/env python3
"""Stream records to the PC from a capture loop running inside the scope app.

The scope's SCPI reply path costs ~110 ms per 1 Mpt record, marshalling and
framing a record the app already has in memory.  This injects libmhotap.so,
which arms the scope itself and reads each capture with the app's own
DrvWaveform_Export* functions -- no SCPI in the frame loop -- and sends it from
its own thread.  Every enabled channel comes back interleaved in one frame.
See the header of device/libmhotap.c for how a cycle is sequenced and why.

Run the PC receiver first (spectrum/fft_gui.py --source stream, or
spectrum/stream_client.py --sink), then:

    device/tap_stream.py <scope-ip> --pc-host <pc-ip>
"""
from __future__ import annotations

import argparse
import hashlib
import os
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import frida  # noqa: E402

# adb/frida plumbing only.  The speed patch proper stays in the mho-speed-patch
# repo -- the tap bypasses the SCPI path it accelerates, so it is not needed.
import adb as adbutil  # noqa: E402
from rigol_mho import Scope  # noqa: E402

TAP_JS = os.path.join(HERE, "mho_tap.js")
TAP_SO_LOCAL = os.path.join(HERE, "libmhotap.so")
TAP_SO_DEV_DIR = "/data/local/tmp"


def on_msg(m, _d):
    if m.get("type") == "error":
        print("  [tap error]", m.get("description"))
    else:
        p = m.get("payload")
        if p:
            print(" ", p)


def mask_str(mask: int) -> str:
    return "+".join(f"CH{c + 1}" for c in range(4) if mask >> c & 1) or "none"


def setup_readout(sc: Scope, channel: int):
    """Prime a deep-memory acquisition and read what the loop needs to know.

    Returns (points per channel, sample rate, {channel: (yinc, yorig, yref)},
    enabled channels).  The export reads every *enabled* channel regardless of
    :WAV:SOURce; `channel` is only switched on and used to check the record.
    """
    sc.write(":STOP"); sc.opc()
    sc.write(f":CHANnel{channel}:DISPlay ON")
    # AUTO sweep, never :SINGle -- single-shot waits forever for a trigger on a
    # quiet input.  The loop arms SINGLE natively, and AUTO still forces it.
    sc.write(":TRIGger:SWEep AUTO")
    sc.write(f":WAVeform:SOURce CHANnel{channel}")
    sc.write(":WAVeform:FORMat WORD")
    # A scope that has not acquired since boot returns a zero-length record.
    sc.write(":RUN"); time.sleep(0.4); sc.write(":STOP"); sc.opc()
    # Only the NORMal->RAW transition re-derives :WAV:STOP? to the real record.
    sc.write(":WAVeform:MODE NORMal")
    sc.write(":WAVeform:MODE RAW")
    sc.opc()
    got = sc.query(":WAVeform:SOURce?")
    if str(channel) not in got:
        raise SystemExit(f"scope kept source {got!r} -- is CH{channel} switched on?")
    npts = int(float(sc.query(":WAVeform:STOP?")))
    if npts <= 0:
        raise SystemExit("scope reports an empty record after priming")
    srate = float(sc.query(":ACQuire:SRATe?"))
    channels = [c for c in (1, 2, 3, 4)
                if sc.query(f":CHANnel{c}:DISPlay?").strip() == "1"]
    # Each channel's vertical scale, so the receiver can offer dBV/dBm instead
    # of only dBFS.  Per channel, because each has its own V/div and the export
    # is raw codes for all of them; :WAV:YINC? answers for the current
    # :WAV:SOURce, so walk the source over the enabled channels and put it
    # back.  Queried once here rather than per frame -- the loop never talks
    # SCPI -- so a V/div changed mid-session goes stale (docs/NEXT_WORK.md,
    # item 1).  A failure leaves that channel at zero, which the receiver
    # reads as "unknown".
    scales = {}
    for c in channels:
        try:
            sc.write(f":WAVeform:SOURce CHANnel{c}")
            got = sc.query(":WAVeform:SOURce?")
            if str(c) not in got:
                raise RuntimeError(f"source stayed {got.strip()!r}")
            scales[c] = (float(sc.query(":WAVeform:YINCrement?")),
                         float(sc.query(":WAVeform:YORigin?")),
                         float(sc.query(":WAVeform:YREFerence?")))
        except Exception as e:
            adbutil.log(f"no vertical scale for CH{c} ({e}); it stays in dBFS")
            scales[c] = (0.0, 0.0, 0.0)
    sc.write(f":WAVeform:SOURce CHANnel{channel}")
    return npts, srate, scales, channels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ip", help="scope IP (adb + SCPI)")
    ap.add_argument("--pc-host", required=True, help="this PC as the scope sees it")
    ap.add_argument("--pc-port", type=int, default=5560)
    ap.add_argument("--scpi-ip", default=None, help="scope IP for SCPI if different")
    ap.add_argument("--adb", default=None)
    ap.add_argument("--channel", type=int, default=1,
                    help="channel switched on at startup and used for the "
                         "vertical scale.  Every enabled channel is streamed")
    ap.add_argument("--seconds", type=float, default=0.0)
    ap.add_argument("--quiet-ui", action="store_true",
                    help="stop the scope redrawing its own waveform while the "
                         "tap streams (CApiPlotWave::setEnable(false)), and "
                         "restore it on exit.  That thread holds an A72 at "
                         "~99%% otherwise.  The scope's screen keeps its last "
                         "trace until this exits")
    ap.add_argument("--sleep-const", action="append", default=[],
                    metavar="OFF:EXPECT_US:NEW_US",
                    help="rewrite a hardcoded usleep constant in place, e.g. "
                         "0x3417fc:20000:10000.  Sleeps on the arm/readout "
                         "path: 0x3417fc (20 ms, ADC calibration), 0x30ef60 "
                         "(10 ms, CDrvScope::ReadNormTrace) and 0x2e945c (a "
                         "1000 MULTIPLIER in CDrvScope::run -- usleep(n*w9), "
                         "so scale it rather than set it).  Offsets are for "
                         "the build in /data/app, not the firmware image.  "
                         "The patch is refused unless the instruction really "
                         "is a movz/movk with EXPECT_US, and is restored on "
                         "exit.  Measured on the old SCPI loop: the ADC sleep "
                         "tolerates 20 -> 10 ms but returns stale records "
                         "at 1 ms")
    ap.add_argument("--quiet-logd", action="store_true",
                    help="stop logd for the session and start it again on "
                         "exit.  The app logs hard enough that the log "
                         "pipeline costs 0.62 of a core while streaming "
                         "(measured 2026-09-09: logd 52%%, logcatext 5%%, two "
                         "file drains 4%%).  Nothing is logged while it is "
                         "off, and if this process is killed hard logd stays "
                         "stopped until 'adb shell start logd' or a reboot")
    ap.add_argument("--drive-csv", default="",
                    help="with --drive-poll-ms, append one row per observed "
                         "cycle: t, cycle, arm, ReadNormTrace wait, lock wait, "
                         "export, whole cycle (ms), export errors, arm "
                         "timeouts.  This is how a regime change is "
                         "attributed to a phase rather than guessed at")
    ap.add_argument("--drive-poll-ms", type=float, default=0.0,
                    help="sample the on-scope loop this often (ms) and log any "
                         "cycle over 200 ms.  The loop only keeps the *last* "
                         "cycle's times, so the 2 s reports miss exactly the "
                         "outliers; 0 = off (default)")
    args = ap.parse_args()

    scpi_ip = args.scpi_ip or args.ip
    adb_path = adbutil.find_adb(args.adb)
    adbutil.connect(adb_path, args.ip)
    serial = f"{args.ip}:{adbutil.ADB_PORT}"
    adb = adbutil.Adb(adb_path, serial)
    adbutil.ensure_root(adb)
    adbutil.ensure_frida_server(adb, adbutil.frida_version(), None)

    if not os.path.exists(TAP_SO_LOCAL):
        raise SystemExit(f"{TAP_SO_LOCAL} not built -- see device/build_tap.sh")
    # The device filename carries a hash of the contents, for two reasons that
    # both cost the app its life when ignored:
    #
    #  * `adb push` rewrites the file in place, and overwriting a .so that a
    #    running process has mmap'd corrupts that mapping -- the pages it
    #    faults in later come from the new file at old offsets.  A rebuilt
    #    library pushed over a live one SIGSEGVs the app at 0x0 in
    #    gum-js-loop.  A distinct name is never written over a mapped file;
    #    the temp-then-rename below covers the same-name case too, since
    #    rename leaves the old inode intact for whoever still has it mapped.
    #  * mho_tap.js reuses an already-loaded module of the same name rather
    #    than dlopen'ing twice, so a fresh build pushed under the old name
    #    would silently keep running the *old* code in a process that had
    #    already mapped it.
    with open(TAP_SO_LOCAL, "rb") as fh:
        tag = hashlib.sha256(fh.read()).hexdigest()[:12]
    tap_so_dev = f"{TAP_SO_DEV_DIR}/libmhotap-{tag}.so"
    tmp = f"{tap_so_dev}.tmp"
    adb.push(TAP_SO_LOCAL, tmp)
    adb.shell(f"mv -f {tmp} {tap_so_dev} && chmod 755 {tap_so_dev}", root=True)
    adbutil.log(f"tap library: {os.path.basename(tap_so_dev)}")

    pid = adbutil.app_pid(adb)
    adbutil.log(f"attaching to com.rigol.scope (pid {pid})")
    dev = frida.get_device(serial, timeout=10)
    session = dev.attach(pid)
    tap = session.create_script(open(TAP_JS).read())
    tap.on("message", on_msg)
    tap.load()
    time.sleep(0.3)

    base = tap.exports_sync.load(tap_so_dev)
    adbutil.log(f"libmhotap.so loaded at {base}")

    sc = Scope(host=scpi_ip, timeout=30.0)
    npts, srate, scales, channels = setup_readout(sc, args.channel)
    nch = len(channels)
    mask = sum(1 << (c - 1) for c in channels)
    frame_bytes = npts * 2 * nch
    # tests/acq_sweep.py parses "CHn WORD: N pts" -- keep that shape.
    adbutil.log(f"CH{args.channel} WORD: {npts} pts ({npts*2} B/frame) "
                f"@ {srate/1e6:.0f} MSa/s")
    adbutil.log(f"channels: {'+'.join(f'CH{c}' for c in channels)}, "
                f"interleaved ({frame_bytes} B/frame)")
    # The export is what the app *samples* -- the shown channels plus a hidden
    # trigger source -- in 1, 2 or 4 slots, and can be wider than what is sent
    # (libmhotap.c layout_keep).  The loop reads the layout itself every
    # capture; this only sizes the buffers, with room for 4 slots where that
    # stays under 32 MB a buffer, so a layout that widens mid-session is still
    # exported rather than skipped.
    lay = tap.exports_sync.layout()
    slots = max(1, int(lay["count"]))
    cap_slots = 4 if npts * 2 * 4 <= 32_000_000 else max(slots, nch)
    adbutil.log(f"app samples {mask_str(int(lay['mask']))} in {slots} slot(s); "
                f"buffers sized for {cap_slots}")

    r = tap.exports_sync.start(args.pc_host, args.pc_port,
                               npts * 2 * cap_slots + 4096,
                               srate, frame_bytes, nch, mask)
    if not r.get("ok"):
        session.detach()
        raise SystemExit(f"tap start failed rc={r.get('rc')} -- is the PC "
                         f"receiver listening on {args.pc_host}:{args.pc_port}?")
    adbutil.log(f"tap streaming to {args.pc_host}:{args.pc_port}")
    # The header's own scale is all a one-channel frame needs; with several,
    # every channel's goes in the table after it, in interleave order.
    if scales[args.channel][0]:
        tap.exports_sync.set_y_scale(*scales[args.channel])
    if nch > 1:
        for i, c in enumerate(channels):
            tap.exports_sync.set_y_scale_ch(i, *scales[c])
    for c in channels:
        yi, yo, yr = scales[c]
        adbutil.log(f"CH{c} vertical scale: {yi:.6g} V/code, origin {yo:g}, "
                    f"ref {yr:g}" if yi else f"CH{c} vertical scale unknown")

    # The whole frame loop runs on the scope: arm, wait for the app to read
    # the capture, export, send.  No per-frame RPC and no SCPI.
    rc = tap.exports_sync.drive_start()
    if rc != 0:
        session.detach()
        raise SystemExit(f"on-scope capture loop failed to start (rc={rc})")
    adbutil.log("on-scope capture loop running (arm + ReadNormTrace + export, "
                "no SCPI)")

    stopping = {"v": False}
    signal.signal(signal.SIGINT, lambda *_: stopping.__setitem__("v", True))
    signal.signal(signal.SIGTERM, lambda *_: stopping.__setitem__("v", True))

    quiet_ui = {"on": False}
    if args.quiet_ui:
        tap.exports_sync.plot_hook()
        for _ in range(60):                  # doRender runs continuously
            if tap.exports_sync.plot_ready():
                break
            time.sleep(0.05)
        if tap.exports_sync.plot_ready():
            state = tap.exports_sync.plot_set(False)
            quiet_ui["on"] = True
            adbutil.log(f"scope waveform redraw disabled (flag={state}); "
                        f"it is restored on exit")
        else:
            adbutil.log("could not reach CApiPlotWave::doRender; leaving the "
                        "scope's redraw alone")

    sleep_consts = []
    for spec in args.sleep_const:
        try:
            off, want, to = spec.split(":")
            sleep_consts.append([int(off, 0), int(want, 0), int(to, 0)])
        except ValueError:
            raise SystemExit(f"bad --sleep-const {spec!r}, "
                             f"want OFF:EXPECT_US:NEW_US")
    patched = False
    if sleep_consts:
        r = tap.exports_sync.patch_sleep_consts(sleep_consts)
        for site in r.get("sites", []):
            if site.get("ok"):
                patched = True
                adbutil.log(f"patched +0x{site['off']:x}: "
                            f"{site['from']} -> {site['to']} us")
            else:
                # Loud, and on stderr: a moved offset means the run silently
                # loses the gain, and the only other trace of it is a line in
                # a temp log nobody reads.  A firmware update is the usual
                # cause -- the constant has to be re-derived.
                msg = (f"WARNING: could not patch +0x{site['off']:x} "
                       f"({site['why']}). That sleep is unchanged. The offset "
                       f"is firmware-specific and probably moved.")
                adbutil.log(msg)
                print(msg, file=sys.stderr, flush=True)
        if not r.get("ok"):
            adbutil.log(f"WARNING: patching failed: {r.get('error')}")

    # The scope's own logging is the single largest non-app consumer while we
    # stream.  It is the *ingestion* that costs, not the readers -- killing the
    # file drains alone only recovered 0.10 of a core, stopping logd all 0.62.
    logd = {"off": False}
    if args.quiet_logd:
        try:
            # Adb.shell returns a CompletedProcess, not a string.
            was = adb.shell("getprop init.svc.logd").stdout.strip()
            if was == "running":
                adb.shell("stop logd")
                logd["off"] = True
                adbutil.log("logd stopped for the session (~0.6 core); "
                            "restored on exit")
            else:
                adbutil.log(f"logd is {was or 'not running'}; leaving it alone")
        except Exception as e:
            adbutil.log(f"WARNING: could not stop logd ({e}); continuing")

    t0 = time.perf_counter()
    tlast = t0
    # Outlier watch: the loop overwrites its last-cycle times every cycle, so a
    # 2 s sample sees whatever the most recent (healthy) cycle did and the slow
    # one is gone.  Sampling faster and keeping the maxima is how to see them.
    poll = max(0.0, args.drive_poll_ms) / 1e3
    peak = {"cycle": 0.0, "cycles": 0, "slow": 0}
    dcsv = None
    if args.drive_csv and poll:
        dcsv = open(args.drive_csv, "w", buffering=1)
        dcsv.write("t,cycle,arm_ms,rnt_wait_ms,lock_wait_ms,export_ms,cycle_ms,"
                   "export_errors,arm_timeouts\n")
    try:
        while not stopping["v"]:
            if args.seconds and time.perf_counter() - t0 > args.seconds:
                break
            time.sleep(poll if poll else 0.5)
            if poll:
                dv = tap.exports_sync.drive_stats()
                cyc = int(dv["cycles"])
                if cyc != peak["cycles"]:
                    if dcsv is not None:
                        dcsv.write(
                            f"{time.perf_counter() - t0:.3f},{cyc},"
                            f"{dv['arm_ms']:.2f},{dv['rnt_wait_ms']:.2f},"
                            f"{dv['lock_wait_ms']:.2f},{dv['export_ms']:.2f},"
                            f"{dv['cycle_ms']:.2f},{int(dv['export_errors'])},"
                            f"{int(dv['arm_timeouts'])}\n")
                    peak["cycles"] = cyc
                    if dv["cycle_ms"] > 200.0:
                        peak["slow"] += 1
                        adbutil.log(f"slow cycle {cyc}: {dv['cycle_ms']:.0f} ms "
                                    f"(arm {dv['arm_ms']:.0f} + rnt "
                                    f"{dv['rnt_wait_ms']:.0f} + lock "
                                    f"{dv['lock_wait_ms']:.0f} + export "
                                    f"{dv['export_ms']:.0f})")
                peak["cycle"] = max(peak["cycle"], dv["cycle_ms"])
            if time.perf_counter() - tlast >= 2.0:
                st = tap.exports_sync.stats()
                dv = tap.exports_sync.drive_stats()
                # tests/acq_sweep.py parses this line (RE_TAPSTAT); change the
                # two together.
                adbutil.log(f"tap {st['fps']:5.2f} fps  {st['mbps']:5.1f} MB/s  "
                            f"in={int(st['frames_in'])} sent={int(st['frames_sent'])} "
                            f"dropped={int(st['dropped'])}  "
                            f"cycles={int(dv['cycles'])} "
                            f"arm={dv['arm_ms']:.0f}ms "
                            f"rnt={dv['rnt_wait_ms']:.0f}ms "
                            f"lock={dv['lock_wait_ms']:.0f}ms "
                            f"export={dv['export_ms']:.0f}ms "
                            f"cycle={dv['cycle_ms']:.0f}ms "
                            f"armTO={int(dv['arm_timeouts'])} "
                            f"expErr={int(dv['export_errors'])}"
                            + f" slots={int(dv['slots'])}:"
                              f"{mask_str(int(dv['slot_mask']))}"
                              f" skip={int(dv['layout_skips'])}"
                              f" compact={dv['compact_ms']:.1f}ms"
                            + (f"  peak cycle {peak['cycle']:.0f} ms, "
                               f"{peak['slow']} slow" if poll else ""))
                tlast = time.perf_counter()
    finally:
        if dcsv is not None:
            dcsv.close()
        st = tap.exports_sync.stats()
        el = time.perf_counter() - t0
        adbutil.log(f"stopped after {el:.1f}s; tap sent "
                    f"{int(st['frames_sent'])} frames, {st['fps']:.2f} fps, "
                    f"{st['mbps']:.1f} MB/s, {int(st['dropped'])} dropped")
        # Restore the scope's own display first: leaving a scope that will
        # not redraw is far worse than leaving a hook attached, and every
        # later step can throw.
        if quiet_ui["on"]:
            try:
                adbutil.log(f"scope waveform redraw restored "
                            f"(flag={tap.exports_sync.plot_set(True)})")
            except Exception as e:
                adbutil.log(f"WARNING: could not restore the scope's redraw ({e}); "
                            f"it comes back when the app restarts")
        if patched:
            try:
                n = tap.exports_sync.restore_sleep_consts().get("restored", 0)
                adbutil.log(f"usleep constants restored ({n} sites)")
            except Exception as e:
                adbutil.log(f"WARNING: could not restore usleep constants ({e}); "
                            f"they come back when the app restarts")
        if logd["off"]:
            try:
                adb.shell("start logd")
                adbutil.log("logd restarted")
            except Exception as e:
                adbutil.log(f"WARNING: could not restart logd ({e}) -- the "
                            f"scope is logging nothing until you run "
                            f"'adb shell start logd' or reboot it")
        # These three used to be silent, which made a teardown that was being
        # SIGKILLed at the parent's 15 s cap look identical to one that
        # finished: the tell was a leaked frida agent (session.detach never
        # ran) and a libmhotap still mapped.  Log each step so the log says
        # how far it got.
        t_td = time.perf_counter()
        try:
            tap.exports_sync.stop()
            adbutil.log(f"on-scope tap stopped ({time.perf_counter()-t_td:.1f}s)")
        except Exception as e:
            adbutil.log(f"WARNING: on-scope tap did not stop cleanly ({e})")
        try:
            sc.write(":RUN"); sc.close()
            adbutil.log(f"scope returned to RUN ({time.perf_counter()-t_td:.1f}s)")
        except Exception as e:
            adbutil.log(f"WARNING: could not put the scope back in RUN ({e})")
        try:
            session.detach()
            adbutil.log(f"frida session detached ({time.perf_counter()-t_td:.1f}s) "
                        f"-- teardown complete")
        except Exception as e:
            adbutil.log(f"WARNING: frida detach failed ({e}); the agent stays "
                        f"mapped in the app until it restarts")


if __name__ == "__main__":
    main()

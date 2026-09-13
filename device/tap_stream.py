#!/usr/bin/env python3
"""Stream 1 Mpt records to the PC by tapping them inside the scope app.

The SCPI reply path costs ~110 ms per frame marshalling and framing a record the
app already has in hand.  This injects libmhotap.so, hands it the raw pointer
from CApiWave::toWord, and zeroes the point count so the app produces an empty
reply -- the whole produce-and-frame cost disappears and the 2 MB goes out on
the tap's own thread, overlapping the next acquisition.

Run the PC receiver first (spectrum/fft_gui.py --source stream, or
spectrum/stream_client.py --sink), then:

    device/tap_stream.py <scope-ip> --pc-host <pc-ip> --channel 4
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


def trigger_read(sc, quiet=0.05):
    """Ask for the waveform purely to make the app produce it, and discard the
    reply.

    With the tap active the app marshals nothing, but the SCPI framing still
    computes the block length from the requested point range -- so the header
    announces 2,000,000 bytes while only the small stub actually arrives.  A
    normal block read would wait forever for the rest.  Read the header, then
    drain whatever really turns up.
    """
    sock = sc.sock
    sc.write(":WAVeform:DATA?")
    hdr = sc._recv_exact(1)
    if hdr != b"#":
        raise RuntimeError(f"expected block header, got {hdr!r}")
    ndig = int(sc._recv_exact(1))
    declared = int(sc._recv_exact(ndig))
    got = 0
    while True:
        chunk = 0
        try:
            sock.settimeout(quiet)
            while True:
                b = sock.recv(1 << 20)
                if not b:
                    break
                chunk += len(b)
        except Exception:
            pass
        finally:
            sock.settimeout(sc.timeout)
        got += chunk
        if chunk == 0:
            break
    return declared, got


def on_msg(m, _d):
    if m.get("type") == "error":
        print("  [tap error]", m.get("description"))
    else:
        p = m.get("payload")
        if p:
            print(" ", p)


def setup_readout(sc: Scope, channel: int, fmt: str):
    """Put the scope in deep-memory RAW mode and prime the acquisition."""
    sc.write(":STOP"); sc.opc()
    sc.write(f":CHANnel{channel}:DISPlay ON")
    # AUTO sweep, never :SINGle -- single-shot waits forever for a trigger on a
    # quiet input and truncates deep records.
    sc.write(":TRIGger:SWEep AUTO")
    sc.write(f":WAVeform:SOURce CHANnel{channel}")
    sc.write(f":WAVeform:FORMat {fmt}")
    # A scope that has not acquired since boot returns a zero-length block.
    sc.write(":RUN"); time.sleep(0.4); sc.write(":STOP"); sc.opc()
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
    # The vertical scale, so the receiver can offer dBV/dBm instead of only
    # dBFS.  Queried once here rather than per frame: it only changes when the
    # V/div does, and the tap deliberately never talks SCPI in the frame loop.
    # Any failure leaves it at zero, which the receiver reads as "unknown".
    yinc = yorig = yref = 0.0
    try:
        yinc = float(sc.query(":WAVeform:YINCrement?"))
        yorig = float(sc.query(":WAVeform:YORigin?"))
        yref = float(sc.query(":WAVeform:YREFerence?"))
    except Exception as e:
        adbutil.log(f"no vertical scale ({e}); the display stays in dBFS")
    return npts, srate, (yinc, yorig, yref)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ip", help="scope IP (adb + SCPI)")
    ap.add_argument("--pc-host", required=True, help="this PC as the scope sees it")
    ap.add_argument("--pc-port", type=int, default=5560)
    ap.add_argument("--scpi-ip", default=None, help="scope IP for SCPI if different")
    ap.add_argument("--adb", default=None)
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--format", default="WORD", choices=["WORD", "BYTE"])
    ap.add_argument("--seconds", type=float, default=0.0)
    ap.add_argument("--native-arm", action="store_true", default=True,
                    help="arm via DrvAcquire_SetState instead of :RUN/:STOP")
    ap.add_argument("--scpi-arm", dest="native_arm", action="store_false")
    ap.add_argument("--arm-runstop", action="store_true",
                    help="arm with native RUN/dwell/STOP instead of SINGLE")
    ap.add_argument("--dwell-ms", type=float, default=40.0,
                    help="dwell for --arm-runstop")
    ap.add_argument("--quiet-ui", action="store_true",
                    help="stop the scope redrawing its own waveform while the "
                         "tap streams (CApiPlotWave::setEnable(false)), and "
                         "restore it on exit.  Frees ~60%% of a core on a box "
                         "whose big cores are saturated, worth ~2 fps.  The "
                         "scope's screen keeps its last trace until this exits")
    ap.add_argument("--scpi-sleep-us", type=int, default=1000,
                    help="what --no-scpi-sleep shortens the 20 ms wait to, in "
                         "microseconds (default 1000; 0 removes it entirely)")
    ap.add_argument("--no-scpi-sleep", action="store_true",
                    help="shorten the hardcoded 20 ms sleep "
                         "CScpiParserWorker takes before answering any SCPI "
                         "command (see --scpi-sleep-us).  The tap pays it once "
                         "per frame on the :WAVeform:DATA? that triggers "
                         "production -- ~23%% of the cycle.  Restored on exit")
    ap.add_argument("--sleep-const", action="append", default=[],
                    metavar="OFF:EXPECT_US:NEW_US",
                    help="rewrite a hardcoded usleep constant in place, e.g. "
                         "0x3417fc:20000:10000.  Three sleeps fire once per "
                         "acquisition and cost ~40 ms of a 76 ms cycle: "
                         "0x3417fc (20 ms, ADC calibration), 0x30ef60 (10 ms, "
                         "CDrvScope::ReadNormTrace) and 0x2e945c (a 1000 "
                         "MULTIPLIER in CDrvScope::run -- usleep(n*w9), so "
                         "scale it rather than set it).  Offsets are for the "
                         "build in /data/app, not the firmware image.  The "
                         "patch is refused unless the instruction really is a "
                         "movz with EXPECT_US, and is restored on exit.  "
                         "Watch the repeat count: every frame is CRCed, so a "
                         "stale record shows up there.  Measured: the ADC "
                         "sleep tolerates 20 -> 10 ms but degrades below that "
                         "and returns stale records at 1 ms")
    ap.add_argument("--quiet-logd", action="store_true",
                    help="stop logd for the session and start it again on "
                         "exit.  The app logs hard enough that the log "
                         "pipeline costs 0.62 of a core while streaming "
                         "(measured 2026-09-09: logd 52%%, logcatext 5%%, two "
                         "file drains 4%%); stopping it took the box from "
                         "88.8%% to 81.7%% busy and 12.5 -> 13.4 fps.  Nothing "
                         "is logged while it is off, and if this process is "
                         "killed hard logd stays stopped until 'adb shell "
                         "start logd' or a reboot")
    ap.add_argument("--drive-csv", default="",
                    help="with --drive-poll-ms, append one row per observed "
                         "cycle: t, cycle, arm, wait-for-busy, busy, trigger, "
                         "empties, declared.  This is how a regime change is "
                         "attributed to a phase rather than guessed at")
    ap.add_argument("--drive-poll-ms", type=float, default=0.0,
                    help="sample the on-scope loop this often (ms) and log any "
                         "cycle whose arm or trigger phase was slow.  The tap "
                         "only keeps the *last* cycle's times, so the 2 s "
                         "reports miss exactly the outliers we are hunting; "
                         "0 = off (default), 100 is enough to catch them")
    args = ap.parse_args()

    scpi_ip = args.scpi_ip or args.ip
    adb_path = adbutil.find_adb(args.adb)
    adbutil.connect(adb_path, args.ip)
    serial = f"{args.ip}:{adbutil.ADB_PORT}"
    adb = adbutil.Adb(adb_path, serial)
    adbutil.ensure_root(adb)
    adbutil.ensure_frida_server(adb, adbutil.frida_version(), None)

    if not os.path.exists(TAP_SO_LOCAL):
        raise SystemExit(f"{TAP_SO_LOCAL} not built -- see stream/build_tap.sh")
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
    npts, srate, yscale = setup_readout(sc, args.channel, args.format)
    bps = 2 if args.format == "WORD" else 1
    adbutil.log(f"CH{args.channel} {args.format}: {npts} pts ({npts*bps} B/frame) "
           f"@ {srate/1e6:.0f} MSa/s")

    # The last argument is the whole record: above 1 Mpt the app produces it
    # in 1 Mpt chunks, and the tap only publishes once all of them are in.
    r = tap.exports_sync.start(args.pc_host, args.pc_port, npts * bps + 4096,
                               srate, bps, npts * bps)
    if not r.get("ok"):
        session.detach()
        raise SystemExit(f"tap start failed rc={r.get('rc')} -- is the PC "
                         f"receiver listening on {args.pc_host}:{args.pc_port}?")
    adbutil.log(f"tap streaming to {args.pc_host}:{args.pc_port} ({r['hooks']} hooks)")
    if yscale[0]:
        tap.exports_sync.set_y_scale(*yscale)
        adbutil.log(f"vertical scale: {yscale[0]:.6g} V/code, origin {yscale[1]:g}, "
                    f"ref {yscale[2]:g}")

    # Hand the whole frame loop to the scope: arming, triggering and draining
    # all happen there, so no per-frame RPC round trip to the host.
    rc = tap.exports_sync.drive_start(1 if args.arm_runstop else 0,
                                      int(args.dwell_ms * 1000))
    if rc != 0:
        session.detach()
        raise SystemExit(f"on-scope driver failed to start (rc={rc})")
    adbutil.log("on-scope capture loop running (arm + trigger + drain, no host RPC)")

    stopping = {"v": False}
    signal.signal(signal.SIGINT, lambda *_: stopping.__setitem__("v", True))
    signal.signal(signal.SIGTERM, lambda *_: stopping.__setitem__("v", True))

    scpi_sleep = {"off": False}
    if args.no_scpi_sleep:
        r = tap.exports_sync.scpi_sleep(False, args.scpi_sleep_us)
        if not r.get("ok"):
            adbutil.log(f"WARNING: leaving the SCPI sleep alone: {r.get('error')}")
        else:
            scpi_sleep["off"] = True
            adbutil.log(f"SCPI worker's 20 ms sleep cut to {r.get('us')} us at "
                   + ", ".join(r.get("sites", [])))

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

    # Third temporary change, same contract as the two above: the scope's own
    # logging is the single largest non-app consumer while we stream.  It is
    # the *ingestion* that costs, not the readers -- killing the file drains
    # alone only recovered 0.10 of a core, stopping logd recovered all 0.62.
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
                # loses ~1.6 fps, and the only other trace of it is a line in
                # a temp log nobody reads.  A firmware update is the usual
                # cause -- the constant has to be re-derived.
                msg = (f"WARNING: could not patch +0x{site['off']:x} "
                       f"({site['why']}). The scope-side ADC settling wait is "
                       f"unchanged, so this run is ~1.6 fps slower. The offset "
                       f"is firmware-specific and probably moved.")
                adbutil.log(msg)
                print(msg, file=sys.stderr, flush=True)
        if not r.get("ok"):
            adbutil.log(f"WARNING: patching failed: {r.get('error')}")


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
    n = 0
    # Outlier watch: the driver overwrites last_arm_ms every cycle, so a 2 s
    # sample sees whatever the most recent (healthy) cycle did and the slow one
    # is gone.  Sampling faster and keeping the maxima is the only way to see
    # them without rebuilding the .so.
    poll = max(0.0, args.drive_poll_ms) / 1e3
    peak = {"arm": 0.0, "trig": 0.0, "cycles": 0, "slow": 0}
    tpoll = t0
    dcsv = None
    if args.drive_csv and poll:
        dcsv = open(args.drive_csv, "w", buffering=1)
        dcsv.write("t,cycle,arm_ms,wait_busy_ms,busy_ms,trig_ms,empty,declared\n")
    try:
        while not stopping["v"]:
            if args.seconds and time.perf_counter() - t0 > args.seconds:
                break
            time.sleep(poll if poll else 0.5)
            n += 1
            if poll and time.perf_counter() - tpoll >= poll:
                tpoll = time.perf_counter()
                dv = tap.exports_sync.drive_stats()
                arm, trig = dv["arm_ms"], dv["trig_ms"]
                cyc = int(dv["cycles"])
                if cyc != peak["cycles"]:
                    if dcsv is not None:
                        dcsv.write(
                            f"{time.perf_counter() - t0:.3f},{cyc},{arm:.2f},"
                            f"{dv.get('wait_busy_ms', 0):.2f},"
                            f"{dv.get('busy_ms', 0):.2f},{trig:.2f},"
                            f"{int(dv['empty'])},{int(dv['declared'])}\n")
                    peak["cycles"] = cyc
                    if arm > 200.0 or trig > 200.0:
                        peak["slow"] += 1
                        adbutil.log(f"slow cycle {cyc}: arm {arm:.0f} ms "
                               f"(wait-for-busy {dv.get('wait_busy_ms', 0):.0f}"
                               f" + busy {dv.get('busy_ms', 0):.0f}), "
                               f"trigger {trig:.0f} ms, "
                               f"armTO={int(dv['arm_timeouts'])} "
                               f"empty={int(dv['empty'])}")
                peak["arm"] = max(peak["arm"], arm)
                peak["trig"] = max(peak["trig"], trig)
            if time.perf_counter() - tlast >= 2.0:
                st = tap.exports_sync.stats()
                dv = tap.exports_sync.drive_stats()
                adbutil.log(f"tap {st['fps']:5.2f} fps  {st['mbps']:5.1f} MB/s  "
                       f"in={int(st['frames_in'])} sent={int(st['frames_sent'])} "
                       f"dropped={int(st['dropped'])}  "
                       f"cycles={int(dv['cycles'])} "
                       f"arm={dv['arm_ms']:.0f}ms "
                       f"(wait {dv.get('wait_busy_ms', 0):.0f} + "
                       f"busy {dv.get('busy_ms', 0):.0f}) "
                       f"trig={dv['trig_ms']:.0f}ms "
                       f"empty={int(dv['empty'])} decl={int(dv['declared'])} "
                       f"armTO={int(dv['arm_timeouts'])} "
                       f"missedBusy={int(dv.get('missed_busy', 0))} "
                       f"chunks={int(st.get('chunks_in', 0))} "
                       f"partial={int(st.get('partial', 0))} "
                       f"incomplete={int(dv.get('incomplete', 0))} "
                       f"settle={dv.get('settle_ms', 0):.0f}ms "
                       f"recov={int(dv.get('recoveries', 0))}"
                       + (f"  peak arm {peak['arm']:.0f} ms / trig "
                          f"{peak['trig']:.0f} ms, {peak['slow']} slow cycles"
                          if poll else ""))
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
        if scpi_sleep["off"]:
            try:
                n = tap.exports_sync.scpi_sleep(True).get("skipped", 0)
                adbutil.log(f"SCPI worker's 20 ms sleep restored ({n} shortened)")
            except Exception as e:
                adbutil.log(f"WARNING: could not restore the SCPI sleep ({e}); "
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

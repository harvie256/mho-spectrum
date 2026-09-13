#!/usr/bin/env python3
"""Frame sources for the live FFT demo.

Three interchangeable sources, all yielding the same Frame objects:

  SyntheticSource  a signal generator -- lets the whole display pipeline be
                   built and tested with no scope attached.
  ScpiSource       drives the scope over SCPI.  Slowest, but built entirely
                   from proven parts (device/rigol_mho.py plus the speed
                   patch), so it cannot destabilise the app.
  StreamSource     the fast path: receives frames pushed by an on-scope tap.
                   See spectrum/stream_client.py.
"""
from __future__ import annotations

import os
import sys
import socket
import subprocess
import tempfile
import threading
import zlib
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "device"))

from frametime import SOURCE_FIELDS, FrameLog  # noqa: E402
from stream_client import Frame, StreamServer  # noqa: E402,F401


def _drain(sc, quiet: float = 0.0, max_wait: float = 1.0) -> int:
    """Discard anything the scope still owes us, and report how much.

    The SCPI parser services commands on a slow tick, so a reply (an `*OPC?`
    "1", or the tail of an abandoned block) can land after we have moved on.
    One stale byte offsets every later read and the next block-header parse
    fails outright.

    With quiet=0 this only discards what has already arrived, which is cheap but
    misses bytes still in flight.  With quiet>0 it keeps reading until the
    socket has been silent for that long -- slower, but the only way to be sure
    the connection is actually idle before starting a block read.
    """
    sock = sc.sock
    n = 0
    deadline = time.monotonic() + max_wait
    try:
        while True:
            sock.settimeout(quiet if quiet > 0 else 0.0)
            try:
                chunk = sock.recv(65536)
            except (BlockingIOError, InterruptedError):
                break
            except socket.timeout:
                break
            except OSError:
                break
            if not chunk:
                break
            n += len(chunk)
            if time.monotonic() > deadline:
                break
    finally:
        sock.settimeout(sc.timeout)
    return n


def _flush_until_quiet(sc, quiet: float = 0.05, budget: float = 2.0) -> int:
    """Keep draining until the scope stops sending, or the budget runs out.

    The unannounced residue after a block read is not a stray byte or two -- it
    has been measured at 46 KB and at 480 KB, and it is still arriving when the
    first drain returns.  A single fixed wait cannot cover that, so keep
    flushing until a whole quiet window passes with nothing new.
    """
    total = 0
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        n = _drain(sc, quiet=quiet, max_wait=budget)
        if n == 0:
            break
        total += n
    return total


def _brief(exc: BaseException, limit: int = 120) -> str:
    """Exception text safe to log: a desync error can carry a whole MB of
    binary payload in its message."""
    txt = str(exc)
    txt = "".join(c if 32 <= ord(c) < 127 else "." for c in txt)
    if len(txt) > limit:
        txt = txt[:limit] + f"... (+{len(str(exc)) - limit} chars)"
    return f"{type(exc).__name__}: {txt}"


class SyntheticSource:
    """A stand-in scope: sum of tones plus noise, in 12-bit-ish codes.

    Useful both for developing the display without hardware and as a known
    reference -- the peaks land where the maths says they should.
    """

    # Extra channels, so a multi-channel display can be checked with no scope:
    # each has tones of its own plus a weaker copy of CH1's 1 MHz carrier, so
    # both "different on each channel" and "common to all" show up.
    EXTRA_CHANNEL_TONES = {
        2: ((2.0e6, 0.4), (5.5e6, 0.08), (1.0e6, 0.05)),
        3: ((4.0e6, 0.3), (1.0e6, 0.02)),
        4: ((9.0e6, 0.2), (1.0e6, 0.01)),
    }
    # A vertical scale per channel, as the tap sends, so dBV/dBm and the
    # per-channel scale can be exercised with no scope.  CH1's is the MHO934's
    # own at 1 V/div (8 div over 60,000 codes, read 2026-09-13); the others are
    # 0.5 / 0.2 / 0.1 V/div, so a scale applied to the wrong channel moves its
    # trace visibly.  The signal is invented: the levels mean nothing else.
    CHANNEL_YINC = {1: 1.3333e-4, 2: 6.6667e-5, 3: 2.6667e-5, 4: 1.3333e-5}

    def __init__(self, npoints=1_000_000, sample_rate=50e6,
                 tones=((1.0e6, 0.5), (3.0e6, 0.15), (7.25e6, 0.05)),
                 noise=2e-4, fps_limit=20.0, channels=(1,)):
        self.npoints = npoints
        self.sample_rate = sample_rate
        self.tones = tones
        self.noise = noise
        self.channels = tuple(channels) or (1,)
        self.min_period = 1.0 / fps_limit if fps_limit else 0.0
        self._seq = 0
        self._last = 0.0
        self._t0 = None
        t = np.arange(npoints) / sample_rate
        self._bases = {}
        for ch in self.channels:
            base = np.zeros(npoints, dtype=np.float64)
            for f, a in (tones if ch == 1 else self.EXTRA_CHANNEL_TONES.get(ch, tones)):
                base += a * np.sin(2 * np.pi * f * t)
            self._bases[ch] = base
        self._base = self._bases[self.channels[0]]
        self._rng = np.random.default_rng(0xC0FFEE)
        self.timing = FrameLog("source(synthetic)", SOURCE_FIELDS)
        self._last_arrival: float | None = None

    def start(self):
        self._t0 = time.perf_counter()
        return self

    def stop(self):
        pass

    def get(self, timeout: float | None = None) -> Frame | None:
        return self.get_group(timeout)[0]

    def get_group(self, timeout: float | None = None) -> list[Frame]:
        t_wait = time.perf_counter()
        wait = self.min_period - (t_wait - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = t_gen = time.perf_counter()
        codes = {}
        for ch in self.channels:
            sig = self._bases[ch] + self._rng.normal(0.0, self.noise, self.npoints)
            codes[ch] = np.clip(32768 + sig * 32000, 0, 65535).astype("<u2")
        self._seq += 1
        now = time.perf_counter()
        # Synthetic frames are made on the *caller's* thread, so this work lands
        # inside the display interval rather than beside it -- worth recording,
        # because it makes the synthetic source a poor baseline for GUI stalls.
        self.timing.record(
            t=now,
            arr_gap_ms=((now - self._last_arrival) * 1e3
                        if self._last_arrival else 0.0),
            wait_ms=(t_gen - t_wait) * 1e3,
            read_ms=0.0,
            work_ms=(now - t_gen) * 1e3,
            loop_ms=(now - t_wait) * 1e3)
        self._last_arrival = now
        return [Frame(seq=self._seq, samples=codes[ch], sample_rate=self.sample_rate,
                      recv_time=now, yinc=self.CHANNEL_YINC.get(ch, 0.0),
                      yref=32768.0, channel=ch) for ch in self.channels]

    def stats(self):
        el = (time.perf_counter() - self._t0) if self._t0 else 0.0
        nbytes = self._seq * self.npoints * 2 * len(self.channels)
        return {"frames": self._seq, "dropped": 0,
                "fps": self._seq / el if el > 0 else 0.0,
                "mbps": nbytes / el / 1e6 if el > 0 else 0.0,
                "connected": True, "error": None, "elapsed": el}


class ScpiSource:
    """Drive the scope over SCPI in a background thread.

    Every frame pays a full arm -> acquire -> stop -> read cycle, because the
    deep record only exists while the scope is stopped -- there is no way to
    read deep memory from a free-running scope.  Expect ~4-5 fps at 1 Mpt; run
    patch/patch_scope.py alongside or it will be several times slower again.
    """

    def __init__(self, host: str, channel: int = 1, fmt: str = "WORD",
                 points: int = 0, rearm: bool = True,
                 native_device: str | None = None, fill_ms: float = 0.0):
        from rigol_mho import Scope  # local import: keeps numpy-only users clean
        self._Scope = Scope
        self.host, self.channel, self.fmt = host, channel, fmt
        self.points, self.rearm = points, rearm
        # Native arming (DrvAcquire_SetState) instead of :RUN/:STOP.  Faster,
        # and completion is observable so freshness is guaranteed rather than
        # hoped for.  Needs frida-server on the scope.
        self.native_device = native_device
        self.fill_ms = fill_ms
        self._acq = None
        self._latest: Frame | None = None
        self._lock = threading.Lock()
        self._new = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.frames = 0
        self.dropped = 0
        self.bytes = 0
        self.error: str | None = None
        self.last_warning: str | None = None
        self.resyncs = 0
        self.timeouts = 0
        self.short = 0
        self.stale_bytes = 0
        # Consecutive frames whose payload is byte-identical to the previous
        # one, i.e. the scope had not finished a new acquisition and we
        # re-read the old record.  Silently serving these is the worst
        # possible failure for a "live" display, so it is always counted.
        self.repeats = 0
        self._last_crc = None
        self._fresh_run = 0
        self.fill_s = 0.0
        self.t0: float | None = None
        self.sample_rate = 0.0
        # Per-frame acquisition timing: arm/dwell, block read, and the
        # post-read flush + CRC.  All of it happens on this thread, so a spike
        # here is the scope or the wire, not the display.
        self.timing = FrameLog("source(scpi)", SOURCE_FIELDS)
        self._last_arrival: float | None = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def get(self, timeout: float | None = None) -> Frame | None:
        if not self._new.wait(timeout):
            return None
        with self._lock:
            self._new.clear()
            return self._latest

    def stats(self):
        el = (time.perf_counter() - self.t0) if self.t0 else 0.0
        return {"frames": self.frames, "dropped": self.dropped, "elapsed": el,
                "fps": self.frames / el if el > 0 else 0.0,
                "mbps": self.bytes / el / 1e6 if el > 0 else 0.0,
                "connected": self.error is None, "error": self.error,
                "resyncs": self.resyncs, "timeouts": self.timeouts,
                "short": self.short, "stale": self.stale_bytes,
                "repeats": self.repeats, "fill_ms": self.fill_s * 1e3,
                "warning": self.last_warning}

    def _setup(self, sc):
        _drain(sc, quiet=0.05)
        sc.write(":STOP")
        sc.opc()
        sc.write(f":CHANnel{self.channel}:DISPlay ON")
        # Force AUTO sweep: in SINGle/NORMal the scope waits indefinitely for a
        # trigger, so with no qualifying edge every frame times out and the demo
        # silently produces nothing.  A live spectrum wants free-running frames.
        sc.write(":TRIGger:SWEep AUTO")
        sc.write(f":WAVeform:SOURce CHANnel{self.channel}")
        sc.write(f":WAVeform:FORMat {self.fmt}")
        # Only the NORMal->RAW transition re-derives :WAV:STARt/:STOP to the
        # real record; writing RAW while already RAW leaves a stale range.
        sc.write(":WAVeform:MODE NORMal")
        sc.write(":WAVeform:MODE RAW")
        sc.opc()
        # Verify the source took.  Drain first and retry: a late reply from the
        # slow SCPI tick can arrive mid-handshake, and reading it as the answer
        # makes a perfectly good channel look switched off.
        got = ""
        for _ in range(3):
            _drain(sc, quiet=0.05)
            got = sc.query(":WAVeform:SOURce?")
            if str(self.channel) in got:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(
                f"scope kept source {got!r} instead of CHANnel{self.channel}: "
                f"it silently ignores :WAV:SOURce for a channel that is "
                f"switched off")
        # Prime the acquisition.  A scope that has not acquired since boot holds
        # an empty record, and :WAV:DATA? then returns a zero-length block with
        # no error -- so run one generous acquisition before trusting the range.
        window = 10.0 * float(sc.query(":TIMebase:MAIN:SCALe?"))
        sc.write(":RUN")
        time.sleep(max(0.3, window * 1.5))
        sc.write(":STOP")
        sc.opc()
        # Only the NORMal->RAW transition re-derives the range to the record that
        # was actually stored.
        sc.write(":WAVeform:MODE NORMal")
        sc.write(":WAVeform:MODE RAW")
        sc.opc()

        _drain(sc, quiet=0.05)
        stop = int(float(sc.query(":WAVeform:STOP?")))
        if stop <= 0:
            raise RuntimeError(
                "scope reports an empty record after priming -- is the channel "
                "enabled and the timebase sane?")
        if self.points:
            stop = min(stop, self.points)
            sc.write(":WAVeform:STARt 1")
            sc.write(f":WAVeform:STOP {stop}")
            sc.opc()
        self.sample_rate = float(sc.query(":ACQuire:SRATe?"))
        return stop

    def _run(self):
        try:
            self._run_sessions()
        finally:
            # Detach Frida before the interpreter tears down, otherwise a late
            # message callback lands on a half-finalised module and raises
            # "'NoneType' object has no attribute 'loads'" during shutdown.
            if self._acq is not None:
                self._acq.close()
                self._acq = None

    def _run_sessions(self):
        attempts = 0
        while not self._stop.is_set():
            attempts += 1
            try:
                self._session()
            except Exception as e:
                # A desynced socket cannot be repaired in place, so the whole
                # session is rebuilt.  Give up only after repeated failures, so
                # one bad reply never ends the stream.
                self.resyncs += 1
                self.last_warning = _brief(e)
                if attempts >= 5:
                    self.error = f"gave up after {attempts} attempts: {_brief(e)}"
                    return
                time.sleep(0.5)
            else:
                return

    def _open_native(self):
        if not self.native_device or self._acq is not None:
            return
        from native_acq import NativeAcq
        self._acq = NativeAcq(self.native_device).open()

    def _session(self):
        self._open_native()
        with self._Scope(host=self.host, timeout=30.0) as sc:
            npts = self._setup(sc)
            bps = 2 if self.fmt == "WORD" else 1
            dt = np.dtype("<u2") if bps == 2 else np.dtype("u1")
            window = 10.0 * float(sc.query(":TIMebase:MAIN:SCALe?"))
            record = npts / self.sample_rate if self.sample_rate else 0.0
            fill_s = max(window, record) * 1.5 + 0.005
            if self.t0 is None:
                self.t0 = time.perf_counter()

            while not self._stop.is_set():
                t_wait = time.perf_counter()
                if self.rearm:
                    # The deep record only exists while the scope is STOPPED --
                    # reading while it runs returns a zero-length block -- so
                    # every frame needs a full acquire/stop cycle.
                    if self._acq is not None:
                        # Native: completion is observable, so the record is
                        # guaranteed new.
                        if not self._acq.single(3000):
                            self.timeouts += 1
                            continue
                    else:
                        # SCPI: deliberately NOT :SINGle (it waits forever for a
                        # trigger on a quiet input and truncates deep records).
                        # AUTO sweep + :RUN + a timed :STOP fills the record
                        # regardless -- but the wait has to be generous, and how
                        # generous is not derivable: a scope whose record spans
                        # 20 ms still needed >100 ms of RUN before a NEW
                        # acquisition appeared.  Rather than guess, adapt: grow
                        # the dwell whenever a frame comes back byte-identical
                        # to the last one, and settle once frames are fresh.
                        sc.write(":RUN")
                        time.sleep(fill_s)
                        sc.write(":STOP")
                        sc.opc()
                t_arm = time.perf_counter()
                raw = sc.query_block(":WAVeform:DATA?")
                t_read = time.perf_counter()
                # The scope sometimes transmits MORE than its #9 header
                # declares (observed: a full 2,000,000-byte block followed by
                # 46,012 unannounced bytes).  Left in place, that residue is
                # read as the next block's header and desyncs the stream for
                # good.  The cheap non-blocking check costs nothing when the
                # connection is clean; only pay the quiet-wait when there is
                # actually something to flush.
                stale = _drain(sc)
                if stale:
                    stale += _flush_until_quiet(sc)
                    self.stale_bytes += stale
                if len(raw) < npts * bps:
                    # short record: the acquisition had not filled yet
                    self.short += 1
                    continue
                crc = zlib.crc32(raw[:npts * bps])
                if crc == self._last_crc:
                    self.repeats += 1
                    if self.rearm and self._acq is None and not self.fill_ms:
                        fill_s = min(fill_s * 1.4 + 0.010, 1.5)
                        self.fill_s = fill_s
                        self._fresh_run = 0
                    self._last_crc = crc
                    continue          # never hand a stale record to the display
                self._last_crc = crc
                self._fresh_run = getattr(self, "_fresh_run", 0) + 1
                samples = np.frombuffer(raw[:npts * bps], dtype=dt)
                now = time.perf_counter()
                frame = Frame(seq=self.frames, samples=samples,
                              sample_rate=self.sample_rate,
                              recv_time=now)
                with self._lock:
                    if self._new.is_set():
                        self.dropped += 1
                    self._latest = frame
                    self._new.set()
                self.frames += 1
                self.bytes += len(raw)
                self.timing.record(
                    t=now,
                    arr_gap_ms=((now - self._last_arrival) * 1e3
                                if self._last_arrival else 0.0),
                    wait_ms=(t_arm - t_wait) * 1e3,
                    read_ms=(t_read - t_arm) * 1e3,
                    work_ms=(now - t_read) * 1e3,
                    loop_ms=(now - t_wait) * 1e3)
                self._last_arrival = now


def local_ip_towards(host: str) -> str:
    """Which of our addresses the scope will see us on.

    Saves the user from having to know, and gets it right when the control path
    (Wi-Fi) and the data path (USB gigabit) are different interfaces.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))          # no packets sent; just picks the route
        return s.getsockname()[0]
    finally:
        s.close()


class StreamSource(StreamServer):
    """The fast path: frames pushed by the on-scope tap (device/tap_stream.py).

    Optionally launches and supervises the scope-side tap, so the whole thing is
    one command.  The tap injects libmhotap.so, whose capture loop arms the
    scope and reads each capture with the app's own DrvWaveform_Export*
    functions -- no SCPI per frame -- and sends every enabled channel
    interleaved in one frame, which StreamServer splits back out.
    """

    def __init__(self, host="0.0.0.0", port=5560, scope_ip=None, pc_host=None,
                 channel=1, scpi_ip=None, python=None,
                 drive_poll_ms=0.0, quiet_ui=False, drive_csv="",
                 quiet_logd=False, sleep_consts=()):
        super().__init__(host, port)
        self.drive_poll_ms = drive_poll_ms
        self.drive_csv = drive_csv
        self.quiet_ui = quiet_ui
        self.quiet_logd = quiet_logd
        self.sleep_consts = list(sleep_consts or ())
        self.scope_ip = scope_ip
        self.pc_host = pc_host
        self.channel = channel
        self.scpi_ip = scpi_ip
        self.python = python or sys.executable
        self._proc = None
        self._tap_log = None

    def start(self):
        super().start()
        if not self.scope_ip:
            return self
        # Listener must be up before the scope tries to connect out.
        time.sleep(0.4)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = os.path.join(root, "device", "tap_stream.py")
        pc = self.pc_host or local_ip_towards(self.scpi_ip or self.scope_ip)
        cmd = [self.python, script, self.scope_ip,
               "--pc-host", pc, "--pc-port", str(self.port),
               "--channel", str(self.channel)]
        if self.scpi_ip:
            cmd += ["--scpi-ip", self.scpi_ip]
        if self.drive_poll_ms:
            cmd += ["--drive-poll-ms", str(self.drive_poll_ms)]
        if self.quiet_ui:
            cmd += ["--quiet-ui"]
        if self.quiet_logd:
            cmd += ["--quiet-logd"]
        for spec in self.sleep_consts:
            cmd += ["--sleep-const", spec]
        if self.drive_csv:
            cmd += ["--drive-csv", self.drive_csv]
        self._tap_log = tempfile.NamedTemporaryFile(
            prefix="mho-tap-", suffix=".log", delete=False, mode="w+")
        self._proc = subprocess.Popen(cmd, stdout=self._tap_log,
                                      stderr=subprocess.STDOUT)
        return self

    def tap_alive(self):
        return self._proc is None or self._proc.poll() is None

    def tap_log_tail(self, n=6):
        if not self._tap_log:
            return ""
        try:
            with open(self._tap_log.name) as f:
                return "".join(f.readlines()[-n:])
        except OSError:
            return ""

    def stop(self):
        super().stop()
        if self._proc is not None and self._proc.poll() is None:
            # SIGTERM lets tap_stream.py unwind: stop the on-scope loop, detach
            # Frida and put the scope back in RUN.
            self._proc.terminate()
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._tap_log:
            self._tap_log.close()

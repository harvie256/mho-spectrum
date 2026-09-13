#!/usr/bin/env python3
"""Receive waveform frames streamed from the scope.

The on-scope tap (stream/mho_stream.js) connects out to us and pushes a fixed
64-byte header followed by the raw record, so there is no parsing to do beyond
unpacking the header.

Run standalone as a throughput sink:
    python3 fftdemo/stream_client.py --sink
"""
from __future__ import annotations

import argparse
import socket
import collections
import struct
import zlib
import threading
import time
from dataclasses import dataclass

import numpy as np

from frametime import SOURCE_FIELDS, FrameLog

MAGIC = b"MHOFRAME"
HDR = 64
HDR_FMT = "<8sIIHHI5d"          # magic, seq, npoints, bps, flags, reserved, 5 doubles
assert struct.calcsize(HDR_FMT) == HDR, struct.calcsize(HDR_FMT)

DEFAULT_PORT = 5560


@dataclass
class Frame:
    seq: int
    samples: np.ndarray          # raw codes, uint16 (WORD) or uint8 (BYTE)
    sample_rate: float
    recv_time: float
    # volts = (code - yref) * yinc + yorig.  yinc == 0 means the source could
    # not tell us the vertical scale, and the display stays in dBFS.
    yinc: float = 0.0
    yorig: float = 0.0
    yref: float = 0.0
    # Scope channel this record came from (1-4).  Frames of one acquisition
    # share seq; get_group() returns them together.
    channel: int = 1

    @property
    def npoints(self) -> int:
        return self.samples.size

    @property
    def duration(self) -> float:
        return self.npoints / self.sample_rate if self.sample_rate else 0.0



class StreamServer:
    """Listens for the scope, decodes frames, keeps only the newest.

    The display must never fall behind the scope, so this deliberately drops
    older frames rather than queueing them; `dropped` counts what was skipped.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = DEFAULT_PORT):
        self.host, self.port = host, port
        self._latest: list[Frame] | None = None
        self._lock = threading.Lock()
        self._new = threading.Event()
        self._stop = threading.Event()
        self.frames = 0
        self.dropped = 0
        self.bytes = 0
        # (arrival time, bytes) for the trailing-window rate; a few hundred
        # entries covers RATE_WINDOW_S at any rate this link can reach.
        self._rate_hist: collections.deque = collections.deque(maxlen=4096)
        # A frame byte-identical to the previous one means the scope had not
        # finished a new acquisition and the record was re-read.  Counted on
        # every arriving frame, not just delivered ones.
        self.repeats = 0
        self._last_crc = None
        self.t0 = None
        self.connected = False
        self.error: str | None = None
        self._thread: threading.Thread | None = None
        # Per-frame receive timing.  Split out the blocking wait for the next
        # header (that is the scope's own frame period) from the payload read
        # (that is the wire), so a display stall can be blamed on -- or cleared
        # of -- the source without guessing.
        self.timing = FrameLog("source(stream)", SOURCE_FIELDS)
        self._last_arrival: float | None = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> "StreamServer":
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    # -- consumer ---------------------------------------------------------
    def get(self, timeout: float | None = None) -> Frame | None:
        """Return the newest undelivered frame (its first channel), or None."""
        group = self.get_group(timeout)
        return group[0] if group else None

    def get_group(self, timeout: float | None = None) -> list[Frame] | None:
        """Return every channel of the newest undelivered acquisition, or None."""
        if not self._new.wait(timeout):
            return None
        with self._lock:
            self._new.clear()
            return self._latest

    # Rate over the last RATE_WINDOW_S seconds, not since the start.  A run
    # takes several seconds to reach steady state (the first frame alone can be
    # 2 s), and a cumulative mean buries that ramp in every number: a 45 s run
    # measured 12.70 fps cumulative while the last seconds were running well
    # above that.  Comparing two configurations then compares their startups as
    # much as their throughput.  `fps_avg`/`mbps_avg` keep the old cumulative
    # figures for anything that wants a whole-run total.
    RATE_WINDOW_S = 5.0

    def _rate(self):
        """(fps, MB/s) over the trailing window, or None while it is not full."""
        with self._lock:
            hist = list(self._rate_hist)
        if len(hist) < 2:
            return None
        now = time.perf_counter()
        cut = now - self.RATE_WINDOW_S
        win = [h for h in hist if h[0] >= cut]
        if len(win) < 2:
            return None
        span = win[-1][0] - win[0][0]
        if span <= 0:
            return None
        # n-1 intervals between n timestamps
        nf = len(win) - 1
        nb = sum(h[1] for h in win[1:])
        return nf / span, nb / span / 1e6

    def stats(self) -> dict:
        el = (time.perf_counter() - self.t0) if self.t0 else 0.0
        cum_fps = self.frames / el if el > 0 else 0.0
        cum_mbps = self.bytes / el / 1e6 if el > 0 else 0.0
        r = self._rate()
        return {
            "frames": self.frames, "dropped": self.dropped,
            "elapsed": el,
            "fps": r[0] if r else cum_fps,
            "mbps": r[1] if r else cum_mbps,
            "fps_avg": cum_fps, "mbps_avg": cum_mbps,
            "window_s": self.RATE_WINDOW_S if r else el,
            "connected": self.connected,
            "error": self.error,
            "repeats": self.repeats,
        }

    # -- internals --------------------------------------------------------
    def _serve(self):
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            ls.bind((self.host, self.port))
        except OSError as e:
            self.error = f"bind {self.host}:{self.port}: {e}"
            return
        ls.listen(1)
        ls.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _peer = ls.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
            self.connected = True
            self.t0 = time.perf_counter()
            try:
                self._read_conn(conn)
            except (OSError, ValueError) as e:
                self.error = str(e)
            finally:
                conn.close()
                self.connected = False
        ls.close()

    @staticmethod
    def _recv_exact(conn, n: int, stop: threading.Event) -> bytes | None:
        buf = bytearray(n)
        view = memoryview(buf)
        got = 0
        while got < n:
            if stop.is_set():
                return None
            k = conn.recv_into(view[got:], n - got)
            if k == 0:
                return None
            got += k
        return bytes(buf)

    def _read_conn(self, conn):
        while not self._stop.is_set():
            t_wait = time.perf_counter()
            head = self._recv_exact(conn, HDR, self._stop)
            if head is None:
                return
            t_hdr = time.perf_counter()
            magic, seq, npts, bps, flags, chmask, srate, _xi, _yi, _yo, _yr = \
                struct.unpack(HDR_FMT, head)
            if magic != MAGIC:
                raise ValueError(f"bad frame magic {magic!r} -- stream desynced")
            if bps not in (1, 2) or not (0 < npts <= 1 << 28):
                raise ValueError(f"implausible frame header: {npts} pts x {bps} B")

            nbytes = npts * bps
            payload = self._recv_exact(conn, nbytes, self._stop)
            if payload is None:
                return
            t_read = time.perf_counter()
            dt = np.dtype("<u2") if bps == 2 else np.dtype("u1")
            samples = np.frombuffer(payload, dtype=dt)

            crc = zlib.crc32(payload)
            if crc == self._last_crc:
                self.repeats += 1
            self._last_crc = crc

            # Several channels of one acquisition arrive as one frame, samples
            # interleaved [a, b, a, b, ...] -- the layout the app's own export
            # hands back.  flags (low nibble) is the channel count, reserved is
            # the mask of which scope channels they are; both zero means one
            # channel, CH1, which is also what an older tap sends.
            nch = flags & 0xF if flags & 0xF > 1 else 1
            if samples.size % nch:
                raise ValueError(f"{samples.size} samples do not split into "
                                 f"{nch} channels -- stream desynced")
            chans = [c + 1 for c in range(8) if chmask >> c & 1]
            if len(chans) != nch:
                chans = list(range(1, nch + 1))
            now = time.perf_counter()
            # The header carries one vertical scale; per-channel scales are not
            # sent yet, so absolute units are only right while the channels
            # share a V/div.
            group = [Frame(seq=seq, samples=samples[i::nch] if nch > 1 else samples,
                           sample_rate=srate, recv_time=now, yinc=_yi,
                           yorig=_yo, yref=_yr, channel=chans[i])
                     for i in range(nch)]
            with self._lock:
                if self._new.is_set():
                    self.dropped += 1      # consumer never picked up the last one
                self._latest = group
                self._new.set()
            self.frames += 1
            self.bytes += HDR + nbytes
            with self._lock:
                self._rate_hist.append((now, HDR + nbytes))
            self.timing.record(
                t=now,
                arr_gap_ms=((now - self._last_arrival) * 1e3
                            if self._last_arrival else 0.0),
                wait_ms=(t_hdr - t_wait) * 1e3,
                read_ms=(t_read - t_hdr) * 1e3,
                work_ms=(now - t_read) * 1e3,
                loop_ms=(now - t_wait) * 1e3)
            self._last_arrival = now


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--sink", action="store_true", help="report throughput only")
    ap.add_argument("--seconds", type=float, default=0.0)
    args = ap.parse_args()

    srv = StreamServer(args.host, args.port).start()
    print(f"listening on {args.host}:{args.port} -- start the scope side now")
    t0 = time.time()
    last = 0
    try:
        while True:
            time.sleep(1.0)
            s = srv.stats()
            if s["error"]:
                print("error:", s["error"])
                break
            if s["frames"] != last:
                print(f"  {s['frames']:6d} frames  {s['fps']:5.2f} fps  "
                      f"{s['mbps']:6.2f} MB/s  repeats {s['repeats']}")
                last = s["frames"]
            if args.seconds and time.time() - t0 > args.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
        s = srv.stats()
        pct = 100.0 * s["repeats"] / max(1, s["frames"])
        print(f"\ntotal: {s['frames']} frames, {s['fps']:.2f} fps, "
              f"{s['mbps']:.2f} MB/s, {s['repeats']} repeats ({pct:.0f}% stale)")


if __name__ == "__main__":
    main()

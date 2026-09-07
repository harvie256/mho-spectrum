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
        self._latest: Frame | None = None
        self._lock = threading.Lock()
        self._new = threading.Event()
        self._stop = threading.Event()
        self.frames = 0
        self.dropped = 0
        self.bytes = 0
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
        """Return the newest undelivered frame, or None on timeout."""
        if not self._new.wait(timeout):
            return None
        with self._lock:
            self._new.clear()
            return self._latest

    def stats(self) -> dict:
        el = (time.perf_counter() - self.t0) if self.t0 else 0.0
        return {
            "frames": self.frames, "dropped": self.dropped,
            "elapsed": el,
            "fps": self.frames / el if el > 0 else 0.0,
            "mbps": self.bytes / el / 1e6 if el > 0 else 0.0,
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
            magic, seq, npts, bps, _flags, _res, srate, _xi, _yi, _yo, _yr = \
                struct.unpack(HDR_FMT, head)
            if magic != MAGIC:
                raise ValueError(f"bad frame magic {magic!r} -- stream desynced")
            if bps not in (1, 2) or not (0 < npts <= 1 << 28):
                raise ValueError(f"implausible frame header: {npts} pts x {bps} B")

            payload = self._recv_exact(conn, npts * bps, self._stop)
            if payload is None:
                return
            t_read = time.perf_counter()
            dt = np.dtype("<u2") if bps == 2 else np.dtype("u1")
            samples = np.frombuffer(payload, dtype=dt)

            crc = zlib.crc32(payload)
            if crc == self._last_crc:
                self.repeats += 1
            self._last_crc = crc

            now = time.perf_counter()
            frame = Frame(seq=seq, samples=samples, sample_rate=srate,
                          recv_time=now)
            with self._lock:
                if self._new.is_set():
                    self.dropped += 1      # consumer never picked up the last one
                self._latest = frame
                self._new.set()
            self.frames += 1
            self.bytes += HDR + npts * bps
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

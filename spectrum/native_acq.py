#!/usr/bin/env python3
"""Attach to the scope app and drive acquisition natively, skipping SCPI.

Arming over SCPI costs ~50 ms/frame of pure parser latency and, worse, a client
that does not wait long enough silently re-reads the previous record.  Calling
DrvAcquire_SetState / DrvAcquire_GetRunStatus directly makes one acquisition
cost ~43 ms and makes completion observable, so freshness is guaranteed rather
than hoped for.
"""
from __future__ import annotations

import os
import subprocess

import frida

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "native_acq.js")


class NativeAcq:
    """Native acquisition control for com.rigol.scope.

    Requires frida-server on the scope (patch/patch_scope.py provisions it).
    """

    def __init__(self, device: str, adb: str = "adb"):
        self.device = device
        self.adb = adb
        self._session = None
        self._script = None
        self.arm_ms = 0.0

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def open(self):
        pid = self._app_pid()
        dev = frida.get_device(self.device, timeout=10)
        self._session = dev.attach(pid)
        self._script = self._session.create_script(open(SCRIPT).read())
        self._script.on("message", self._on_message)
        self._script.load()
        return self

    def close(self):
        if self._session is not None:
            try:
                self._session.detach()
            except Exception:
                pass
            self._session = None
            self._script = None

    def _app_pid(self) -> int:
        out = subprocess.run(
            [self.adb, "-s", self.device, "shell", "su -c 'pidof com.rigol.scope'"],
            capture_output=True, text=True, timeout=20).stdout.split()
        if not out:
            raise RuntimeError("com.rigol.scope is not running")
        return int(out[0])

    @staticmethod
    def _on_message(msg, _data):
        if msg.get("type") == "error":
            print("  [native_acq]", msg.get("description"))

    # -- control ----------------------------------------------------------
    @property
    def status(self) -> int:
        """4 = armed, 3 = triggered, 0 = complete."""
        return self._script.exports_sync.status()

    def stop(self):
        self._script.exports_sync.stop()

    def run(self):
        self._script.exports_sync.run()

    def arm(self) -> float:
        """Start one acquisition and return immediately (ms spent in SetState).

        Returning early is the point: the capture then overlaps the previous
        frame's transfer instead of serialising behind it.
        """
        self.arm_ms = self._script.exports_sync.arm()
        return self.arm_ms

    def wait(self, timeout_ms: float = 2000.0) -> bool:
        return bool(self._script.exports_sync.wait(timeout_ms)["ok"])

    def single(self, timeout_ms: float = 2000.0) -> bool:
        """Arm and block until complete."""
        return bool(self._script.exports_sync.single(timeout_ms)["ok"])


def main():
    import argparse, statistics, time
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="172.30.188.217:55555")
    ap.add_argument("-n", type=int, default=10)
    args = ap.parse_args()
    with NativeAcq(args.device) as acq:
        acq.stop()
        ts = []
        for _ in range(args.n):
            t = time.perf_counter()
            ok = acq.single()
            ts.append(((time.perf_counter() - t) * 1e3, ok))
        good = [t for t, ok in ts if ok]
        print(f"{len(good)}/{args.n} acquisitions completed, "
              f"median {statistics.median(good):.1f} ms "
              f"-> {1000/statistics.median(good):.1f} acquisitions/s")
        acq.run()


if __name__ == "__main__":
    main()

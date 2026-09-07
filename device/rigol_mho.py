"""Helper for talking to a Rigol MHO900-series scope over raw SCPI sockets.

Verified against an MHO934 (firmware 00.01.00) on port 5555.

    from rigol_mho import Scope

    with Scope("172.30.188.217") as scope:
        print(scope.idn)
        scope.single()
        wf = scope.waveform(1)
        print(wf.volts[:10])
        scope.screenshot("shot.png")

Runnable as a CLI too -- see ``python3 rigol_mho.py --help``.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from dataclasses import dataclass

DEFAULT_HOST = os.environ.get("RIGOL_HOST", "172.30.188.217")
DEFAULT_PORT = int(os.environ.get("RIGOL_PORT", "5555"))

# The scope is reachable but slow on wifi, so the default is generous.
DEFAULT_TIMEOUT = 15.0

# :WAV:DATA? in RAW mode is read in slices this many points wide.
CHUNK_POINTS = 250_000


class ScopeError(Exception):
    """Any failure talking to the instrument."""


class ScopeTimeout(ScopeError):
    """No reply within the timeout.

    Usually means the query is not supported by this firmware -- the scope
    stays silent rather than reporting an error.
    """


@dataclass(frozen=True)
class Preamble:
    """Decoded ``:WAVeform:PREamble?`` response."""

    format: int          # 0=BYTE, 1=WORD, 2=ASCII
    type: int            # 0=NORMal, 1=MAXimum, 2=RAW
    points: int
    count: int
    xincrement: float
    xorigin: float
    xreference: float
    yincrement: float
    yorigin: float
    yreference: float

    @classmethod
    def parse(cls, text: str) -> "Preamble":
        parts = text.strip().split(",")
        if len(parts) != 10:
            raise ScopeError(f"unexpected preamble: {text!r}")
        nums = [float(p) for p in parts]
        return cls(
            format=int(nums[0]),
            type=int(nums[1]),
            points=int(nums[2]),
            count=int(nums[3]),
            xincrement=nums[4],
            xorigin=nums[5],
            xreference=nums[6],
            yincrement=nums[7],
            yorigin=nums[8],
            yreference=nums[9],
        )


@dataclass
class Waveform:
    """A captured trace plus the preamble needed to scale it."""

    source: str
    preamble: Preamble
    raw: list[int]

    @property
    def volts(self) -> list[float]:
        p = self.preamble
        return [(r - p.yorigin - p.yreference) * p.yincrement for r in self.raw]

    @property
    def times(self) -> list[float]:
        p = self.preamble
        return [p.xorigin + (i - p.xreference) * p.xincrement
                for i in range(len(self.raw))]

    @property
    def sample_rate(self) -> float:
        return 1.0 / self.preamble.xincrement

    def to_arrays(self):
        """Return ``(times, volts)`` as numpy arrays. Requires numpy."""
        import numpy as np

        p = self.preamble
        raw = np.asarray(self.raw, dtype=float)
        volts = (raw - p.yorigin - p.yreference) * p.yincrement
        times = p.xorigin + (np.arange(raw.size) - p.xreference) * p.xincrement
        return times, volts

    def to_csv(self, path: str) -> str:
        with open(path, "w") as fh:
            fh.write("time_s,volts\n")
            for t, v in zip(self.times, self.volts):
                fh.write(f"{t:.9e},{v:.6e}\n")
        return path

    def __len__(self) -> int:
        return len(self.raw)


class Channel:
    """Convenience accessor for ``scope.ch[1]``."""

    def __init__(self, scope: "Scope", index: int):
        self.scope = scope
        self.index = index

    def _q(self, leaf: str) -> str:
        return self.scope.query(f":CHANnel{self.index}:{leaf}?")

    @property
    def enabled(self) -> bool:
        return self._q("DISPlay") == "1"

    @enabled.setter
    def enabled(self, on: bool) -> None:
        self.scope.write(f":CHANnel{self.index}:DISPlay {1 if on else 0}")

    @property
    def scale(self) -> float:
        """Volts per division."""
        return float(self._q("SCALe"))

    @scale.setter
    def scale(self, volts_per_div: float) -> None:
        self.scope.write(f":CHANnel{self.index}:SCALe {volts_per_div:g}")

    @property
    def offset(self) -> float:
        return float(self._q("OFFSet"))

    @offset.setter
    def offset(self, volts: float) -> None:
        self.scope.write(f":CHANnel{self.index}:OFFSet {volts:g}")

    @property
    def coupling(self) -> str:
        return self._q("COUPling")

    @coupling.setter
    def coupling(self, mode: str) -> None:
        self.scope.write(f":CHANnel{self.index}:COUPling {mode}")

    @property
    def probe(self) -> float:
        return float(self._q("PROBe"))

    @probe.setter
    def probe(self, ratio: float) -> None:
        self.scope.write(f":CHANnel{self.index}:PROBe {ratio:g}")

    def measure(self, item: str) -> float:
        return self.scope.measure(item, self.index)

    def waveform(self, **kwargs) -> Waveform:
        return self.scope.waveform(self.index, **kwargs)

    def __repr__(self) -> str:
        return f"<Channel {self.index}>"


class Scope:
    """Raw-socket SCPI connection to the scope.

    Note the MHO934 listens on 5555, not the more common 5025.
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout: float = DEFAULT_TIMEOUT,
                 reconnect_on_timeout: bool = True):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.reconnect_on_timeout = reconnect_on_timeout
        self._sock: socket.socket | None = None
        self.ch = {i: Channel(self, i) for i in range(1, 5)}

    # ---- connection ----------------------------------------------------

    def open(self) -> "Scope":
        if self._sock is None:
            try:
                self._sock = socket.create_connection(
                    (self.host, self.port), timeout=self.timeout)
            except OSError as exc:
                raise ScopeError(
                    f"cannot connect to {self.host}:{self.port}: {exc}") from exc
            self._sock.settimeout(self.timeout)
        return self

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> "Scope":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def _reconnect(self) -> None:
        self.close()
        self.open()

    @property
    def sock(self) -> socket.socket:
        if self._sock is None:
            self.open()
        assert self._sock is not None
        return self._sock

    # ---- primitives ----------------------------------------------------

    def write(self, cmd: str) -> None:
        """Send a command that produces no reply."""
        self.sock.sendall(cmd.encode() + b"\n")

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self.sock.recv(min(65536, n - len(buf)))
            except socket.timeout as exc:
                self._handle_timeout()
                raise ScopeTimeout(
                    f"timed out after {len(buf)}/{n} bytes") from exc
            if not chunk:
                raise ScopeError("connection closed by instrument")
            buf += chunk
        return bytes(buf)

    def _recv_line(self) -> bytes:
        buf = bytearray()
        while not buf.endswith(b"\n"):
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout as exc:
                self._handle_timeout()
                raise ScopeTimeout(
                    "no reply (command may be unsupported)") from exc
            if not chunk:
                raise ScopeError("connection closed by instrument")
            buf += chunk
        return bytes(buf)

    def _handle_timeout(self) -> None:
        # A late reply would desync every later query, so drop the socket.
        if self.reconnect_on_timeout:
            try:
                self._reconnect()
            except ScopeError:
                pass

    def query(self, cmd: str) -> str:
        """Send a query and return the reply as stripped text."""
        self.write(cmd)
        return self._recv_line().decode(errors="replace").strip()

    def query_float(self, cmd: str) -> float:
        return float(self.query(cmd))

    def query_block(self, cmd: str) -> bytes:
        """Send a query returning an IEEE 488.2 definite-length block."""
        self.write(cmd)
        head = self._recv_exact(1)
        if head != b"#":
            raise ScopeError(f"expected block header, got {head!r}")
        ndigits = int(self._recv_exact(1))
        length = int(self._recv_exact(ndigits))
        data = self._recv_exact(length)
        self._recv_exact(1)  # trailing newline
        return data

    # ---- identity and state -------------------------------------------

    @property
    def idn(self) -> str:
        return self.query("*IDN?")

    def reset(self) -> None:
        self.write("*RST")

    def opc(self) -> bool:
        return self.query("*OPC?") == "1"

    def errors(self) -> list[str]:
        """Drain the error queue."""
        out = []
        for _ in range(32):
            msg = self.query(":SYSTem:ERRor?")
            out.append(msg)
            if msg.startswith("0,") or msg.startswith("+0,"):
                out.pop()
                break
        return out

    # ---- acquisition control -------------------------------------------

    def run(self) -> None:
        self.write(":RUN")

    def stop(self) -> None:
        self.write(":STOP")

    def single(self) -> None:
        self.write(":SINGle")

    def force_trigger(self) -> None:
        self.write(":TFORce")

    def autoscale(self) -> None:
        self.write(":AUToscale")

    @property
    def trigger_status(self) -> str:
        """One of TD, WAIT, RUN, AUTO, STOP."""
        return self.query(":TRIGger:STATus?")

    def wait_for_stop(self, timeout: float = 30.0, poll: float = 0.2) -> str:
        """Block until the acquisition finishes (or raise on timeout)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.trigger_status
            if status == "STOP":
                return status
            time.sleep(poll)
        raise ScopeTimeout(f"still {self.trigger_status} after {timeout}s")

    @property
    def timebase_scale(self) -> float:
        return self.query_float(":TIMebase:MAIN:SCALe?")

    @timebase_scale.setter
    def timebase_scale(self, seconds_per_div: float) -> None:
        self.write(f":TIMebase:MAIN:SCALe {seconds_per_div:g}")

    @property
    def sample_rate(self) -> float:
        return self.query_float(":ACQuire:SRATe?")

    @property
    def memory_depth(self) -> float:
        return self.query_float(":ACQuire:MDEPth?")

    @memory_depth.setter
    def memory_depth(self, depth) -> None:
        self.write(f":ACQuire:MDEPth {depth}")

    # ---- measurements ---------------------------------------------------

    def measure(self, item: str, source: int | str = 1) -> float:
        """One-shot measurement, e.g. ``scope.measure("VPP", 1)``.

        Common items: VPP, VMAX, VMIN, VAVG, VRMS, FREQuency, PERiod,
        RTIMe, FTIMe, PDUTy, PWIDth.
        """
        src = f"CHANnel{source}" if isinstance(source, int) else source
        return self.query_float(f":MEASure:ITEM? {item},{src}")

    # ---- screenshot ------------------------------------------------------

    def screenshot(self, path: str = "screenshot.png") -> str:
        """Grab the display as a PNG (1024x600 on the MHO934)."""
        data = self.query_block(":DISPlay:DATA?")
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    # ---- waveform capture -------------------------------------------------

    def preamble(self) -> Preamble:
        return Preamble.parse(self.query(":WAVeform:PREamble?"))

    def waveform(self, source: int | str = 1, mode: str = "NORMal",
                 fmt: str = "WORD", points: int | None = None,
                 auto_stop: bool = True) -> Waveform:
        """Capture a trace.

        ``mode="NORMal"`` returns the 1000 on-screen points; ``mode="RAW"``
        reads deep memory and requires a stopped acquisition (done for you
        when ``auto_stop`` is set). ``fmt="WORD"`` keeps the full 12-bit
        resolution; ``"BYTE"`` is half the transfer size at 8 bits.
        """
        src = f"CHANnel{source}" if isinstance(source, int) else source
        mode = mode.upper()
        fmt = fmt.upper()
        if fmt not in ("WORD", "BYTE"):
            raise ValueError("fmt must be WORD or BYTE")

        if mode.startswith("RAW") and auto_stop:
            self.stop()

        self.write(f":WAVeform:SOURce {src}")
        self.write(f":WAVeform:MODE {mode}")
        self.write(f":WAVeform:FORMat {fmt}")

        pre = self.preamble()
        total = points if points is not None else pre.points
        raw: list[int] = []
        width = 2 if fmt == "WORD" else 1

        start = 1
        while start <= total:
            stop = min(start + CHUNK_POINTS - 1, total)
            self.write(f":WAVeform:STARt {start}")
            self.write(f":WAVeform:STOP {stop}")
            data = self.query_block(":WAVeform:DATA?")
            expected = (stop - start + 1) * width
            if len(data) != expected:
                # Firmware clamps oversized requests; trust what came back.
                stop = start + len(data) // width - 1
            if fmt == "WORD":
                raw.extend(int.from_bytes(data[i:i + 2], "little")
                           for i in range(0, len(data) - 1, 2))
            else:
                raw.extend(data)
            if stop >= total or not data:
                break
            start = stop + 1

        return Waveform(source=src, preamble=pre, raw=raw)


# ---- CLI ------------------------------------------------------------------

def _cmd_idn(scope: Scope, args) -> None:
    print(scope.idn)


def _cmd_info(scope: Scope, args) -> None:
    print(f"idn          {scope.idn}")
    print(f"trigger      {scope.trigger_status}")
    print(f"timebase     {scope.timebase_scale:g} s/div")
    print(f"sample rate  {scope.sample_rate:g} Sa/s")
    print(f"memory depth {scope.memory_depth:g} pts")
    for i, ch in scope.ch.items():
        if ch.enabled:
            print(f"CH{i}          {ch.scale:g} V/div, "
                  f"offset {ch.offset:g} V, {ch.coupling}, {ch.probe:g}x")


def _cmd_screenshot(scope: Scope, args) -> None:
    path = scope.screenshot(args.output)
    print(f"wrote {path} ({os.path.getsize(path)} bytes)")


def _cmd_wave(scope: Scope, args) -> None:
    wf = scope.waveform(args.channel, mode=args.mode, fmt=args.format)
    volts = wf.volts
    print(f"{len(wf)} points @ {wf.sample_rate:g} Sa/s, "
          f"min {min(volts):.4f} V, max {max(volts):.4f} V")
    if args.output:
        print(f"wrote {wf.to_csv(args.output)}")


def _cmd_meas(scope: Scope, args) -> None:
    print(scope.measure(args.item, args.channel))


def _cmd_send(scope: Scope, args) -> None:
    cmd = " ".join(args.scpi)
    if cmd.rstrip().endswith("?"):
        print(scope.query(cmd))
    else:
        scope.write(cmd)


def _cmd_repl(scope: Scope, args) -> None:
    print(f"connected to {scope.idn}")
    print("type SCPI (queries ending in ? print a reply), or 'quit'")
    while True:
        try:
            line = input("scpi> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("quit", "exit"):
            break
        try:
            if line.endswith("?"):
                print(scope.query(line))
            else:
                scope.write(line)
        except ScopeError as exc:
            print(f"error: {exc}", file=sys.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Rigol MHO934 SCPI helper")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("idn", help="print *IDN?").set_defaults(func=_cmd_idn)
    sub.add_parser("info", help="summarise scope state").set_defaults(func=_cmd_info)

    p = sub.add_parser("screenshot", help="save the display as PNG")
    p.add_argument("output", nargs="?", default="screenshot.png")
    p.set_defaults(func=_cmd_screenshot)

    p = sub.add_parser("wave", help="capture a trace")
    p.add_argument("channel", type=int, choices=[1, 2, 3, 4])
    p.add_argument("--mode", default="NORMal", help="NORMal or RAW")
    p.add_argument("--format", default="WORD", help="WORD or BYTE")
    p.add_argument("-o", "--output", help="write CSV here")
    p.set_defaults(func=_cmd_wave)

    p = sub.add_parser("meas", help="one-shot measurement")
    p.add_argument("item", help="VPP, FREQuency, VRMS, ...")
    p.add_argument("channel", type=int, nargs="?", default=1)
    p.set_defaults(func=_cmd_meas)

    p = sub.add_parser("send", help="send a raw SCPI command or query")
    p.add_argument("scpi", nargs="+")
    p.set_defaults(func=_cmd_send)

    sub.add_parser("repl", help="interactive SCPI prompt").set_defaults(func=_cmd_repl)

    args = parser.parse_args(argv)
    try:
        with Scope(args.host, args.port, args.timeout) as scope:
            args.func(scope, args)
    except ScopeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Per-frame timing instrumentation for the live FFT demo.

The demo holds a good *average* frame rate and then stalls for a fraction of a
second, which is exactly what an average hides.  So this measures every frame
individually and, when one is slow, says *where* it went.

The key measurement is not the elapsed time -- it is elapsed time paired with
the GUI thread's own CPU time (`time.thread_time`).  A 300 ms frame that burnt
290 ms of CPU is work, and the fix is to do less of it.  A 300 ms frame that
burnt 4 ms of CPU means the thread was not running at all: descheduled by the
kernel, or waiting on the GIL while the receiver thread copied 2 MB and CRC'd
it.  Those two need opposite fixes, and nothing short of per-frame CPU
accounting tells them apart.

Each display interval is therefore decomposed as

    interval = work + paint + idle
    (of which the thread actually ran for `cpu`)

with `work` the timer callback's own measured phases (source poll, FFT,
reduce+setData, status text), `paint` the Qt repaint, and `idle` whatever is
left -- event-loop wait if the source had nothing to give, starvation if it
did.  `empty_ticks` counts timer callbacks that found no frame, which separates
"the scope was late" from "we were blocked".

Also tracked, because both are classic causes of exactly this symptom:
  * GC pauses, via `gc.callbacks`, attributed to the interval they overlap.
  * involuntary context switches for the calling thread (`ru_nivcsw`), which
    rise when the scheduler is taking the CPU away.
"""
from __future__ import annotations

import csv
import gc
import time
from collections import deque

import numpy as np

try:                                   # Linux-only, and only a nice-to-have
    import resource
    _RU_THREAD = getattr(resource, "RUSAGE_THREAD", None)
except ImportError:                    # pragma: no cover - non-POSIX
    resource = None
    _RU_THREAD = None


def thread_cpu() -> float:
    """CPU seconds consumed by the calling thread."""
    return time.thread_time()


def nivcsw() -> int:
    """Involuntary context switches for the calling thread (0 if unavailable).

    Rises when the kernel preempts us -- i.e. when something else on the box is
    taking the CPU.  Process-wide `RUSAGE_SELF` would count the receiver
    thread's switches too and blur the very distinction we are after, so use
    `RUSAGE_THREAD` and give up rather than substitute it.
    """
    if resource is None or _RU_THREAD is None:
        return 0
    return resource.getrusage(_RU_THREAD).ru_nivcsw


class FrameLog:
    """A ring of per-frame records, plus the percentile view of them.

    Percentiles, not means: the whole complaint is about the tail.  The ring is
    bounded so a demo can be left running for an hour without growing without
    bound, and `n` keeps counting past the ring so the reports say how much
    they are summarising.
    """

    def __init__(self, name: str, fields: tuple[str, ...], capacity: int = 8192):
        self.name = name
        self.fields = tuple(fields)
        self.capacity = capacity
        self.rows: deque[dict] = deque(maxlen=capacity)
        self.n = 0

    def record(self, **row) -> dict:
        row.setdefault("t", time.perf_counter())
        row.setdefault("i", self.n)
        self.rows.append(row)
        self.n += 1
        return row

    # -- views ------------------------------------------------------------
    def col(self, key: str) -> np.ndarray:
        return np.array([r[key] for r in self.rows if r.get(key) is not None],
                        dtype=np.float64)

    def stats(self, key: str) -> dict:
        v = self.col(key)
        if v.size == 0:
            return {}
        return {"n": v.size, "med": float(np.median(v)),
                "p95": float(np.percentile(v, 95)),
                "p99": float(np.percentile(v, 99)),
                "max": float(v.max()), "mean": float(v.mean()),
                "sum": float(v.sum())}

    def worst(self, key: str, n: int = 5) -> list[dict]:
        rows = [r for r in self.rows if r.get(key) is not None]
        return sorted(rows, key=lambda r: r[key], reverse=True)[:n]

    def report(self, fields: tuple[str, ...] | None = None,
               title: str | None = None) -> str:
        fields = fields or self.fields
        held = len(self.rows)
        head = title or f"{self.name} timing"
        seen = f"{self.n} frames" + (f" (last {held} kept)" if self.n > held else "")
        out = [f"{head} -- {seen}",
               f"  {'field':<16}{'med':>9}{'p95':>9}{'p99':>9}{'max':>10}"]
        for f in fields:
            s = self.stats(f)
            if not s:
                continue
            out.append(f"  {f:<16}{s['med']:9.1f}{s['p95']:9.1f}"
                       f"{s['p99']:9.1f}{s['max']:10.1f}")
        return "\n".join(out)

    def worst_report(self, key: str, fields: tuple[str, ...] | None = None,
                     n: int = 5) -> str:
        fields = fields or self.fields
        rows = self.worst(key, n)
        if not rows:
            return ""
        out = [f"slowest {len(rows)} by {key}:"]
        for r in rows:
            bits = "  ".join(f"{f} {r[f]:.1f}" if isinstance(r.get(f), float)
                             else f"{f} {r[f]}"
                             for f in fields if r.get(f) is not None)
            out.append(f"  #{r.get('i', -1):<6} {bits}")
            why = explain(r)
            if why:
                out.append(f"          -> {why}")
        return "\n".join(out)

    def write_csv(self, path: str) -> int:
        if not self.rows:
            return 0
        keys: list[str] = []
        for r in self.rows:                    # union, first-seen order
            for k in r:
                if k not in keys:
                    keys.append(k)
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            w.writerows(self.rows)
        return len(self.rows)


class GCWatch:
    """Records every garbage collection pause, so stalls can be attributed.

    A 1 Mpt demo allocates several megabyte-sized arrays per frame; a gen-2
    collection walking that heap is a textbook cause of an occasional freeze,
    and it is invisible to any timer inside the frame because it can strike
    between them.
    """

    def __init__(self, capacity: int = 4096):
        self.pauses: deque[tuple[float, float, int]] = deque(maxlen=capacity)
        self._t0: float | None = None
        self.installed = False

    def install(self) -> "GCWatch":
        if not self.installed:
            gc.callbacks.append(self._cb)
            self.installed = True
        return self

    def remove(self):
        if self.installed:
            try:
                gc.callbacks.remove(self._cb)
            except ValueError:
                pass
            self.installed = False

    def _cb(self, phase, info):
        if phase == "start":
            self._t0 = time.perf_counter()
        elif self._t0 is not None:
            self.pauses.append((self._t0, time.perf_counter(),
                                int(info.get("generation", -1))))
            self._t0 = None

    def between(self, t0: float, t1: float) -> tuple[int, float]:
        """(count, milliseconds) of GC pause overlapping the window [t0, t1]."""
        n = 0
        ms = 0.0
        for a, b, _gen in reversed(self.pauses):
            if b <= t0:
                break                          # ring is time-ordered
            lo, hi = max(a, t0), min(b, t1)
            if hi > lo:
                n += 1
                ms += (hi - lo) * 1e3
        return n, ms * 1.0

    def summary(self) -> str:
        if not self.pauses:
            return "gc: no collections recorded"
        d = np.array([(b - a) * 1e3 for a, b, _ in self.pauses])
        gens = [g for _, _, g in self.pauses]
        by = {g: gens.count(g) for g in sorted(set(gens))}
        return (f"gc: {len(d)} collections {by}, total {d.sum():.0f} ms, "
                f"max {d.max():.1f} ms")


class StallDetector:
    """Flags intervals that are out of family with the recent ones.

    A fixed threshold is wrong for a demo that runs anywhere from 4 fps (SCPI)
    to 11 fps (tap) to 60 fps (synthetic), so the default adapts: an interval is
    a stall when it exceeds both a multiple of the running median and a floor
    above it.  The floor stops normal jitter at high frame rates from being
    reported as stalls.
    """

    def __init__(self, threshold_ms: float = 0.0, factor: float = 2.5,
                 floor_ms: float = 20.0, window: int = 200, warmup: int = 20):
        self.threshold_ms = threshold_ms
        self.factor = factor
        self.floor_ms = floor_ms
        self.warmup = warmup
        self.recent: deque[float] = deque(maxlen=window)
        self.count = 0
        self.worst = 0.0

    @property
    def median(self) -> float:
        return float(np.median(self.recent)) if self.recent else 0.0

    def limit(self) -> float:
        if self.threshold_ms > 0:
            return self.threshold_ms
        m = self.median
        return max(m * self.factor, m + self.floor_ms)

    def check(self, value_ms: float) -> bool:
        if len(self.recent) < self.warmup:
            self.recent.append(value_ms)
            return False
        hit = value_ms > self.limit()
        self.recent.append(value_ms)
        if hit:
            self.count += 1
            self.worst = max(self.worst, value_ms)
        return hit


def explain(row: dict) -> str:
    """One line saying which of the usual suspects this frame looks like.

    Deliberately a heuristic over the recorded numbers rather than a verdict:
    it points at the right measurement to look at next.  Everything is judged
    against `base_ms`, the running median interval at the time, because in a
    healthy frame the source gap *is* the interval and the FFT *is* most of the
    work -- saying so about every frame would be noise.  What matters is which
    term grew when the interval did.
    """
    iv = row.get("interval_ms")
    if not iv:
        return ""
    base = row.get("base_ms") or 0.0
    if base and iv < base * 1.25:
        return ""                          # in family; nothing to attribute
    excess = iv - base                     # how much of this frame is unusual
    cpu = row.get("cpu_ms", 0.0) or 0.0
    work = row.get("work_ms", 0.0) or 0.0
    paint = row.get("paint_ms", 0.0) or 0.0
    gcms = row.get("gc_ms", 0.0) or 0.0
    src = row.get("src_gap_ms")
    empty = row.get("empty_ticks")
    # A *blocking* get (the headless loop) spends the wait inside the call, so
    # it looks like a thread that is not running.  It is not: it is a thread
    # waiting for a frame, which is the source's problem, not the scheduler's.
    blocked = (row.get("get_ms") or 0.0) > 0.5 * iv
    bits = []
    if gcms > 0.25 * excess:
        bits.append(f"GC pause {gcms:.0f} ms")
    if src and src > 0.7 * iv and (not base or src > base * 1.25):
        bits.append(f"source late: {src:.0f} ms between arrivals"
                    + (f" vs {base:.0f} ms typical" if base else ""))
    elif blocked:
        bits.append(f"waited {row['get_ms']:.0f} ms in get() -- no frame to have")
    elif (empty is not None and empty <= 1 and cpu < 0.35 * iv
            and iv - work - paint > 20):
        bits.append(f"thread not running: {cpu:.0f} ms CPU in {iv:.0f} ms, "
                    f"no polls missed -- GIL or scheduler")
    if work > 0.6 * iv or (base and work > base):
        slow = max((k for k in ("fft_ms", "draw_ms", "get_ms", "status_ms")
                    if row.get(k)), key=lambda k: row[k], default=None)
        bits.append(f"compute {work:.0f} ms"
                    + (f" (mostly {slow[:-3]} {row[slow]:.0f} ms)" if slow else ""))
    if paint > 0.3 * excess:
        bits.append(f"repaint {paint:.0f} ms")
    if not bits and iv - work - paint > 0.5 * iv:
        bits.append(f"{iv - work - paint:.0f} ms unaccounted, thread had "
                    f"{cpu:.0f} ms CPU")
    return "; ".join(bits) or "unattributed"


def stall_line(row: dict, t0: float) -> str:
    """Compact one-line stall report for the console."""
    def g(k, d=0.0):
        v = row.get(k)
        return d if v is None else v
    parts = [f"stall {g('interval_ms'):7.1f} ms at t={row.get('t', 0) - t0:6.1f}s",
             f"cpu {g('cpu_ms'):6.1f}", f"work {g('work_ms'):6.1f}"]
    if row.get("paint_ms") is not None:
        parts.append(f"paint {g('paint_ms'):5.1f}")
    parts += [f"idle {g('idle_ms'):7.1f}", f"gc {g('gc_ms'):5.1f}"]
    if row.get("empty_ticks") is not None:
        parts.append(f"polls {int(g('empty_ticks')):3d}")
    if row.get("src_gap_ms") is not None:
        parts.append(f"src {g('src_gap_ms'):7.1f}")
    if row.get("nivcsw"):
        parts.append(f"preempt {int(g('nivcsw')):3d}")
    return "  ".join(parts) + f"  -> {explain(row)}"


# Fields the sources record, so every source reports the same shape.
SOURCE_FIELDS = ("arr_gap_ms", "wait_ms", "read_ms", "work_ms", "loop_ms")

DISPLAY_FIELDS = ("interval_ms", "cpu_ms", "work_ms", "paint_ms", "idle_ms",
                  "get_ms", "fft_ms", "draw_ms", "status_ms", "gc_ms",
                  "age_ms", "src_gap_ms", "empty_ticks", "nivcsw")

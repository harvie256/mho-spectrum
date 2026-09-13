#!/usr/bin/env python3
"""One SpectrumEngine per scope channel, driven as one.

Channels from the same acquisition are there to be compared, so every analysis
setting -- window, averaging, max hold, reset -- applies to all of them at
once.  Only what is physically per channel is kept apart: the averaging and
peak-hold buffers, and the DC calibration.

The method and attribute names match SpectrumEngine's, so the panels are wired
to this exactly as they were to a single engine.  A channel here is an *input*;
when real trace modes arrive (clear-write / max / min / average as separate
traces, see CLAUDE.md), a trace becomes (channel, mode) over this rather than
something this has to be unpicked for.
"""
from __future__ import annotations

from spectrum import SpectrumEngine


class ChannelEngines:
    def __init__(self, window: str = "hann", averaging: int = 1,
                 peak_hold: bool = False, workers: int = -1):
        self._window = window
        self._averaging = max(1, averaging)
        self._peak_hold = peak_hold
        self._workers = workers
        self.engines: dict[int, SpectrumEngine] = {}

    def engine(self, ch: int) -> SpectrumEngine:
        """The engine for a channel, created on first sight with the current
        settings -- a channel switched on mid-session starts in step."""
        e = self.engines.get(ch)
        if e is None:
            e = SpectrumEngine(self._window, self._averaging, self._peak_hold,
                               self._workers)
            self.engines[ch] = e
        return e

    # -- settings shared by every channel ---------------------------------
    @property
    def window_name(self) -> str:
        return self._window

    def set_window(self, name: str):
        self._window = name
        for e in self.engines.values():
            e.set_window(name)

    @property
    def averaging(self) -> int:
        return self._averaging

    @averaging.setter
    def averaging(self, n: int):
        self._averaging = max(1, int(n))
        for e in self.engines.values():
            e.averaging = self._averaging

    @property
    def peak_hold(self) -> bool:
        return self._peak_hold

    @peak_hold.setter
    def peak_hold(self, on: bool):
        self._peak_hold = bool(on)
        for e in self.engines.values():
            e.peak_hold = self._peak_hold

    def reset(self):
        for e in self.engines.values():
            e.reset()

    # -- per-channel state --------------------------------------------------
    def clear_dc(self):
        for e in self.engines.values():
            e.clear_dc()

    def capture_dc(self, frames) -> dict[int, float]:
        return {f.channel: self.engine(f.channel).capture_dc(f.samples)
                for f in frames}

    @property
    def dc_offset(self) -> float:
        """The largest calibrated offset, for the status line (0 = none)."""
        return max((e.dc_offset for e in self.engines.values()),
                   key=abs, default=0.0)

    def process(self, frames) -> dict:
        """Spectra for one acquisition's frames, keyed by channel."""
        return {f.channel: self.engine(f.channel).process(f.samples, f.sample_rate)
                for f in frames}

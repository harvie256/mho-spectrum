# mho-spectrum — working notes

A PC-side spectrum analyser fed by a Rigol MHO934 oscilloscope. Read `README.md`
first for layout and quick start; this file is the things that are easy to get
wrong.

## Running it

```bash
./run_fft.sh synthetic                          # no scope needed
./run_fft.sh synthetic --headless --seconds 3   # no window; prints frame timing
./run_fft.sh synthetic --run-seconds 4 --screenshot /tmp/x.png    # smoke test
./run_fft.sh tap <scope-ip>                     # live, fastest path
```

Name the source. A bare `./run_fft.sh`, or one with options but no source,
goes through `--choose`: a dialog, or with `--headless` the remembered
settings, which may well be the scope.

The venv at `.venv/` is the one `run_fft.sh` uses. **The synthetic source
exercises the whole display path**, so nearly all UI work can be built and
verified with no scope attached — use it.

## Roadmap

`docs/SPECTRUM_ANALYSER_FEATURES.md` is the plan: 199 features from real
analysers (Rigol RSA, Keysight X-series, R&S, Tektronix RTSA, SDR tools),
each scored Status / Effort / Value against this code, then a five-phase order.
Currently 54 Done, 19 Partial, 111 Missing, 15 N/A on this hardware.

**Phase 1 is done** except the blind-time readout, which is not built. Phase 2
(multiple traces, finishing absolute units, the measurement suite) is next; the
trace-mode restructure below is its first real obstacle.

## Traps

* **`Spectrum.resolution` is bin spacing, not RBW.** It is `sample_rate / n`.
  Use `Spectrum.rbw` for a bandwidth: it is `enbw_bins × resolution`, and
  `window_enbw()` computes ENBW from the actual window array rather than a
  table, so changing the coefficients cannot leave a stale constant behind
  (measured: rect 1.00, hann 1.50, Blackman-Harris 2.00, flat-top 3.77). Both
  are shown in the annotation block, labelled differently on purpose —
  Keysight and Siglent both document conflating them as a classic error.
* **Absolute units come from a scale queried once, at tap startup.**
  `device/tap_stream.py` reads `:WAV:YINC?`/`YOR?`/`YREF?` over SCPI before
  streaming and hands them to `mhotap_set_yscale()`; `libmhotap.c` writes them
  at header offsets 40/48/56. Zero means unknown, and `analysis.unit_offset_db`
  then leaves dBV/dBm reading as dBFS. So the scale goes stale if V/div changes
  mid-session, and the `scpi` source (`ScpiSource`) never fills `yinc` at all —
  it is dBFS-only. dBm assumes 50 Ω.
* **The tap is sequenced on the app's own event and lock — never fix a race
  on it with a delay.** `libmhotap.c`'s capture loop arms SINGLE, waits for
  `CDrvScope::ReadNormTrace` to return 0 (a return hook in `mho_tap.js`), takes
  `CDrvScope::LockConfig`, then calls `DrvWaveform_ExportInit/ExportData/
  ExportBack` — the functions `:WAV:DATA?` itself ends up in — straight into the
  send buffer. Both waits are load-bearing: exporting before ReadNormTrace
  succeeds races the app's own readout (a 1 M export went 3.4 → 65 ms), and
  exporting before the lock is free races the `SetState` calls `CDrvScope::run`
  makes under it (the c2h read blocked 1 s, returned `-5`, and the app sat at
  `-3` for 2 s). The SCPI-driven loop this replaced papered over the same races
  with three delays — a 20 ms short-capture floor, deep-record settle
  hysteresis, a 120 ms two-channel floor — and lost fps for it; all are gone.
  Measured 2026-09-13 over USB gigabit: one channel 16.2 fps at 2 ms/div 1 M,
  19.9 at 100 µs/div 10 k, 14.3 at 1 GSa/s, 1.72 at 10 M (the link). Check depth
  or rate work with `tests/acq_sweep.py --tone`; timelines in
  `docs/SCOPE_INTERNALS.md`.
* **Every enabled channel is streamed, interleaved, whether you look at it or
  not.** `ExportData` returns `[CH1, CH2, CH1, CH2, …]` for all channels switched
  on, so enabling a second one doubles the bytes: CH1+CH2 at 1 M runs 8.7 fps,
  capped by the ~35 MB/s USB link (the loop itself does ~10 captures/s and drops
  the rest on the scope). The header carries the count (offset 18) and a channel
  mask (offset 20); `StreamServer` splits the frame and `get_group()` returns one
  `Frame` per channel. There is one vertical scale per frame, so dBV/dBm are
  only right while the channels share a V/div.
* **Peak hold is applied after averaging**, in the same chain — it is
  max-hold-of-the-average, not an independent trace. Real trace modes
  (clear-write / max / min / average / view / blank as separate traces) are a
  restructure of `SpectrumEngine`, not a checkbox.
* **Averaging is exponential-only and never completes** — there is no
  average-count that terminates, which is what bench analysers do.
* **Don't claim RTSA behaviour in the UI.** A 1 Mpt record at 50 MSa/s is
  20 ms of signal, and the frame period is ~62 ms (16.2 fps, export loop,
  2026-09-13), so ~32% is observed and ~68% is blind — halve that again with a
  second channel enabled. An earlier note here said "under 1%", which was for
  a much shorter record and is long stale. Better, but still
  not real-time: persistence and spectrogram are fine and worth building;
  100% POI and frequency-mask trigger are not achievable here.
* **Spurs at multiples of fs/16 are an ADC interleave artifact**, not signal
  harmonics. They set the ~60 dB SFDR floor. Annotate, never "fix".

## GUI layout

`spectrum/fft_gui.py` is argparse plus the headless harness; the window lives in
`window.py`, and under it: `viewmodel.py` (FreqView/AmpScale — the axis
arithmetic, deliberately Qt-free and the place to test it), `panels.py` (the
FREQ/AMPT/BW/MARKER/VIEW control groups, each emitting signals and holding no
reference to the engine), `plots.py` (spectrum pane and timing strip),
`markers.py`, `analysis.py` (peak search, spur frequencies, CSV), and
`channels.py` (`ChannelEngines`: one `SpectrumEngine` per channel with
`SpectrumEngine`'s own method names, so the panels wire to it unchanged).

Channels are an *input*, not a trace. The window processes each acquisition as
a group (`self.frames` / `self.spectra`) and the plot overlays every channel
in the scope's colours, but peaks, markers, zoom-to-signal and the annotation
block still follow one active channel (`self.active_ch`, the first). When real
trace modes arrive, a trace should be (channel, mode) over `ChannelEngines`.
`./run_fft.sh synthetic --synthetic-channels 2` exercises all of it.

`window.py` owns the frame loop and the per-frame instrumentation, which is
what catches a new feature's cost — that is how the peak search's 12–17 ms was
found. If you add per-frame work, look at `draw_ms` / `work_ms` in the exit
report before and after.

Two performance traps live in that loop:

* **A peak threshold below the noise floor costs more than the FFT.** scipy
  filters by height before computing prominences, so a threshold in the noise
  makes every noise bin a candidate: 12–17 ms/frame versus 2.5 ms above it,
  against an ~8 ms FFT. `analysis.auto_threshold()` is the default and tracks
  the floor.
* **Peaks are only computed while the peaks tab is showing**, and at most every
  300 ms.

## Device layer

`device/` is everything that talks to the scope. `tap_stream.py` injects
`libmhotap.so` via Frida, primes the acquisition over SCPI (the only SCPI it
sends), and starts the on-scope capture loop; `adb.py` is the plumbing
underneath (connect, root, provision frida-server, find the app pid).

The tap makes up to three temporary changes to the running scope, all restored
on exit. Two are on by default: it pauses the scope's own waveform redraw (that
plot thread holds an A72 at ~99%), and it stops logd (0.62 of a core). The
third, cutting the ADC settling wait on the arm path from 20 ms to 10 ms
(`ADC_SETTLE_SPEC` in `fft_gui.py`), is **off by default**
(`set_defaults(tap_adc_sleep=False)`); its ~2 fps was measured on the old SCPI
loop and needs re-measuring against the export loop before it is turned back
on. See `--no-tap-quiet-ui`, `--tap-keep-logd` and `--tap-keep-adc-sleep`.

The loop's stats line (`arm= rnt= lock= export= cycle= armTO= expErr=`) splits
each cycle by phase; `tests/acq_sweep.py` parses it (`RE_TAPSTAT`), so change
the two together. Probing the live scope has side effects worth avoiding:
`:MEASure:ITEM?` switches that measurement on (clear with `:MEASure:CLEar`),
and `:ACQuire:MDEPth` only takes while running — restore and read back any
setting a test touches.

Rebuilding the tap needs the NDK: `ANDROID_NDK=... device/build_tap.sh`.

## The other repo

`~/Source/rigol` (mho-speed-patch) is separate and this does not depend on it.
It owns the speed patch — TCP buffer sizing, A72 affinity, worker nice levels —
which accelerates *SCPI readout*, a path the tap bypasses entirely.

Two files here are related to it by copy, not by import: `device/adb.py` is the
adb/frida half of its `patch/patch_scope.py`, and `device/rigol_mho.py` is a
copy of its SCPI client. A fix about *reaching the device* likely belongs in
both; a fix about the spectrum app belongs only here.

## Conventions

Match the existing style: dense explanatory comments that say *why*, especially
where a value was measured rather than chosen. Numbers in comments are
measurements — if you change behaviour that invalidates one, re-measure or say
it is stale. Keep `run_fft.sh --help` accurate; its usage text is the header
comment, printed by `sed`, so line numbers there matter.

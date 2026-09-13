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
Currently 54 Done, 18 Partial, 112 Missing, 15 N/A on this hardware.

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
* **Above 1 Mpt the record arrives in 1 Mpt chunks, and the read has to wait.**
  `CApiWave::toWord` is called once per chunk (10 calls at 10 M), and a
  `:WAV:DATA?` sent the instant the capture goes idle gets one chunk — then
  every later cycle gets one chunk too, until a ~200 ms pause clears it.
  `libmhotap.c` reassembles by record size and adapts the read delay (200 ms
  to recover, 40 ms once whole; measured at 10 M only, scaled per Mpt
  elsewhere). Before that, 10 M shipped 1 Mpt fragments labelled 500 MSa/s: a
  1 MHz tone read as 10 MHz, at "6 fps, 12 MB/s". Check any depth work with
  `tests/acq_sweep.py --tone`; details in `docs/SCOPE_INTERNALS.md`.
* **Fast timebases need the read held back, whatever the depth.** At 100 µs/div
  the capture is idle ~1 ms after arming; a `:WAV:DATA?` sent then makes the
  app wait ~2 s for a waveform that never comes, and the queued re-arms drain
  as empty blocks — a 2.0–2.1 s stall. It is capture *time*, not points (1 k,
  10 k, 1 M all did it). `libmhotap.c` holds the read until `SHORT_CAPTURE_US`
  (20 ms) after the arm; measured 8 stalls/25 s at 0 ms, 1 at 10 ms, 0 at
  20 ms, and 4–5 → 17 fps. Longer captures are already past it and pay nothing.
* **Peak hold is applied after averaging**, in the same chain — it is
  max-hold-of-the-average, not an independent trace. Real trace modes
  (clear-write / max / min / average / view / blank as separate traces) are a
  restructure of `SpectrumEngine`, not a checkbox.
* **Averaging is exponential-only and never completes** — there is no
  average-count that terminates, which is what bench analysers do.
* **Don't claim RTSA behaviour in the UI.** A 1 Mpt record at 50 MSa/s is
  20 ms of signal, and the frame period is ~65 ms, so ~31% is observed and
  ~69% is blind (measured 2026-09-13; an earlier note here said "under 1%",
  which was for a much shorter record and is long stale). Better, but still
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
`markers.py`, `analysis.py` (peak search, spur frequencies, CSV).

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
`libmhotap.so` via Frida and starts it streaming; `adb.py` is the plumbing
underneath (connect, root, provision frida-server, find the app pid).

The tap can make four temporary changes to the running scope, all restored on
exit. Three are on by default: it pauses the scope's own waveform redraw (that
plot thread holds an A72 at ~99%) and shortens the app's hardcoded 20 ms
per-SCPI-command sleep to 1 ms — together worth 11.3 → 13.9 fps — and it stops
logd. The fourth, cutting the ADC settling wait in the arm path from 20 ms to
10 ms (`ADC_SETTLE_SPEC` in `fft_gui.py`, ~2 fps), is **temporarily off by
default** (`set_defaults(tap_adc_sleep=False)`) while it was suspected of the
stalls. See `--no-tap-quiet-ui`, `--tap-keep-scpi-sleep`, `--tap-keep-logd` and
`--tap-keep-adc-sleep`, and the `run_fft.sh` header for the measured gains.

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

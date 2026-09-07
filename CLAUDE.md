# mho-spectrum — working notes

A PC-side spectrum analyser fed by a Rigol MHO934 oscilloscope. Read `README.md`
first for layout and quick start; this file is the things that are easy to get
wrong.

## Running it

```bash
./run_fft.sh                          # synthetic — no scope needed
./run_fft.sh --headless --seconds 3   # no window; prints frame timing
./run_fft.sh --run-seconds 4 --screenshot /tmp/x.png    # smoke test
./run_fft.sh tap <scope-ip>           # live, fastest path
```

The venv at `.venv/` is the one `run_fft.sh` uses. **The synthetic source
exercises the whole display path**, so nearly all UI work can be built and
verified with no scope attached — use it.

## Roadmap

`docs/SPECTRUM_ANALYSER_FEATURES.md` is the plan: 199 features from real
analysers (Rigol RSA, Keysight X-series, R&S, Tektronix RTSA, SDR tools),
each scored Status / Effort / Value against this code, then a five-phase order.
Currently 21 Done, 17 Partial, 146 Missing, 15 N/A on this hardware.

**Phase 1 is the current work**: ENBW-corrected RBW readout; a detector
selector on `reduce_for_display()`; reference level and dB/div replacing the
hardcoded −160…+5; centre/span and start/stop entry; markers with delta and
next-peak; peak table with CSV export; clipping annunciation; ADC-spur
annotation at k·fs/16; and a blind-time readout that states plainly this is
not an RTSA. All PC-side, all synthetic-testable.

## Traps

* **`Spectrum.resolution` is bin spacing, not RBW.** It is `sample_rate / n`.
  The status bar correctly says `Hz/bin`, but there is no ENBW in the codebase
  and hann's true RBW is ~1.5× the bin spacing. Fix this before building
  anything that quotes dBm/Hz, noise markers, or channel power — Keysight and
  Siglent both document conflating the two as a classic error.
* **Absolute units are blocked in the tap, not the GUI.** `device/libmhotap.c`
  memsets its header and writes only magic, seq, sample count, bytes-per-sample
  and sample rate. No `yincrement`/`yorigin`/`yreference`, so the stream carries
  no vertical scale and the display can only be dBFS. `device/rigol_mho.py`
  (SCPI) does have them. dBm/dBV over the tap needs a header change plus a
  rebuilt `.so` pushed to the scope — treat it as a device-layer task.
* **Peak hold is applied after averaging**, in the same chain — it is
  max-hold-of-the-average, not an independent trace. Real trace modes
  (clear-write / max / min / average / view / blank as separate traces) are a
  restructure of `SpectrumEngine`, not a checkbox.
* **Averaging is exponential-only and never completes** — there is no
  average-count that terminates, which is what bench analysers do.
* **Don't claim RTSA behaviour in the UI.** ~500 µs of signal every ~75–90 ms
  is under 1% duty cycle. Persistence and spectrogram are fine and worth
  building; 100% POI and frequency-mask trigger are not achievable here.
* **Spurs at multiples of fs/16 are an ADC interleave artifact**, not signal
  harmonics. They set the ~60 dB SFDR floor. Annotate, never "fix".

## Device layer

`device/` is everything that talks to the scope. `tap_stream.py` injects
`libmhotap.so` via Frida and starts it streaming; `adb.py` is the plumbing
underneath (connect, root, provision frida-server, find the app pid).

The tap makes two temporary changes to the running scope app, both restored on
exit and both on by default because they are worth 11.3 → 13.9 fps: it pauses
the scope's own waveform redraw (that plot thread is ~60% of a core) and
shortens the app's hardcoded 20 ms per-SCPI-command sleep to 1 ms. See
`--no-tap-quiet-ui` and `--tap-keep-scpi-sleep`.

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

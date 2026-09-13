# mho-spectrum

A spectrum analyser for the PC, fed by a Rigol MHO934 oscilloscope.

The scope captures; this does the maths and owns the display. Records arrive
either over SCPI or — much faster — through an in-app *tap* injected into the
scope's own process. The tap runs its capture loop there: it arms the scope,
reads each capture with the app's own export functions, and sends every
enabled channel to the PC, with no SCPI per frame. On a 1 Mpt record that is
~16 fps for one channel and ~8.7 fps for two, overlaid on one plot.

This started life as `fftdemo/` in the
[mho-speed-patch](../rigol) repo and outgrew it. That repo still owns the speed
patch itself and the reverse-engineering behind it; see
[Relationship to mho-speed-patch](#relationship-to-mho-speed-patch).

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

./run_fft.sh                       # pick source + IP in a dialog (remembered)
./run_fft.sh synthetic             # synthetic signal, no scope needed
./run_fft.sh tap                   # live off the scope, asks for the IP
./run_fft.sh tap 192.168.23.20     # ... or give it, and it is remembered
./run_fft.sh scpi 192.168.23.20    # live over plain SCPI, slower
./run_fft.sh stream                # listen for a tap started elsewhere
./run_fft.sh --help
```

`--headless` runs without a window; `--run-seconds N --screenshot out.png`
captures a frame and exits, which is how the smoke tests work.

## Layout

| Path | What it is |
|---|---|
| `run_fft.sh` | launcher — picks a source, runs the app out of `.venv` |
| `spectrum/fft_gui.py` | entry point: CLI, source construction, headless harness |
| `spectrum/window.py` | the main window — wiring and the frame loop |
| `spectrum/viewmodel.py` | `FreqView` / `AmpScale` — the axis arithmetic, Qt-free |
| `spectrum/panels.py` | control groups: FREQ / AMPT / BW-DET / MARKER / VIEW |
| `spectrum/plots.py` | the spectrum pane and the frame-timing strip |
| `spectrum/markers.py` | markers, deltas, and the marker table |
| `spectrum/analysis.py` | peak search, ADC spur frequencies, CSV export |
| `spectrum/spectrum.py` | `SpectrumEngine` — windowing, FFT, averaging, dB, detectors |
| `spectrum/channels.py` | `ChannelEngines` — one engine per scope channel, driven as one |
| `spectrum/sources.py` | interchangeable frame sources: `synthetic`, `scpi`, `stream`/`tap` |
| `spectrum/stream_client.py` | wire protocol + listener for the on-scope tap; splits multi-channel frames |
| `spectrum/frametime.py` | per-frame timing: where a slow frame went |
| `spectrum/native_acq.py` | drives acquisition via `DrvAcquire_*` on the scope |
| `device/tap_stream.py` | injects the tap, primes the scope, starts the capture loop |
| `device/libmhotap.c` | the tap itself — capture loop and sender, runs *inside* the scope app (aarch64) |
| `device/mho_tap.js` | Frida script that loads the tap and hands it the app's entry points |
| `device/build_tap.sh` | cross-compiles `libmhotap.so` (needs `$ANDROID_NDK`) |
| `device/rigol_mho.py` | SCPI client (stdlib only) |
| `device/adb.py` | adb/frida plumbing: connect, root, frida-server, find the app pid |
| `tests/acq_sweep.py` | sweeps timebase × memory depth: tap capture checks + GUI smoke test at each point |
| `docs/SPECTRUM_ANALYSER_FEATURES.md` | **the roadmap** — 199 features scored against what exists |

## Where the project is going

`docs/SPECTRUM_ANALYSER_FEATURES.md` surveys what real spectrum analysers do
(Rigol RSA, Keysight X-series, R&S, Tektronix RTSA, and the SDR tools) and
scores all 199 features against this codebase: **54 Done, 19 Partial, 111
Missing, 15 N/A on this hardware.** It ends with a five-phase build order.

**Phase 1 — make the existing display honest and measurable — is done,** bar
the blind-time (duty-cycle) readout, which is not built yet. It turned an FFT
viewer into something whose numbers can be quoted: ENBW-correct RBW alongside
the bin spacing, seven bin-to-pixel detectors, reference level and dB/div with
auto-scale, centre/span and start/stop entry, markers with deltas and peak
stepping, a peak table, full-resolution CSV export, clipping annunciation, and
ADC-spur marks at k·fs/16. All of it is exercised by the synthetic source — no
scope required.

**Phase 2 — traces, measurements and absolute units** is next: independent
traces with the Active/View/Blank model and the measurement suite (channel
power, OBW, THD, SFDR, SINAD/ENOB). Absolute units are partly there: the tap
already carries the vertical scale and dBV/dBm (50 Ω) are offered, but the
scale is read once at startup and the SCPI source has none.

Two findings worth knowing before you touch the code:

* **`Spectrum.resolution` is bin spacing, not RBW.** It is `sample_rate / n`;
  the real bandwidth is `Spectrum.rbw`, which is `enbw_bins × resolution`
  (hann 1.50, Blackman-Harris 2.00, flat-top 3.77, computed from the window
  itself rather than tabulated). Both are on screen, labelled differently.
  Anything claiming dBm/Hz, a noise marker or channel power must use the RBW.
* **Absolute amplitude units depend on a scale sent once, at startup.**
  `device/tap_stream.py` queries `yincrement`/`yorigin`/`yreference` over SCPI
  before streaming and `device/libmhotap.c` carries them in every record header.
  That lets the AMPT units box offer dBV and dBm (the latter assumes 50 Ω). The
  scale is not refreshed if V/div changes mid-session, and the plain `scpi`
  source does not pass it through, so it is dBFS-only.

## What this hardware can and cannot do

Being accurate about this saves chasing features that cannot exist here:

* **No tuner or mixer.** The analyser sees DC to Nyquist and nothing above it.
  There is no RF centre-frequency tuning in the swept-analyser sense.
* **Not a real-time analyser.** A 1 Mpt record at 50 MSa/s is 20 ms of signal
  and arrives every ~62 ms, so roughly a third of wall-clock time is observed
  and two thirds is missed. Persistence and spectrogram displays are worth
  building, but 100% probability-of-intercept and frequency-mask triggering
  are not achievable — do not let the UI imply otherwise.
* **Dynamic range is set by the scope's front end.** SFDR is ~60 dB, limited
  by ADC interleave spurs at multiples of fs/16 (they are a hardware artifact,
  not harmonics of the signal — annotating them is a Phase 1 item).

## Relationship to mho-speed-patch

The [mho-speed-patch](../rigol) repo (`~/Source/rigol`) is a separate product:
it makes the scope's *SCPI readout* fast by resizing TCP buffers, pinning the
readout threads to the RK3399's A72 cores, and adjusting worker nice levels.

This repo does not use it and does not need it. The tap bypasses the SCPI reply
path the patch exists to accelerate. The only overlap is device plumbing —
`device/adb.py` is the adb/frida half of that repo's `patch/patch_scope.py`,
extracted; and `device/rigol_mho.py` is a copy of its SCPI client, which it
still uses for its own tools. Both copies are expected to stay similar, so a
fix about *reaching the device* probably belongs in both.

Running the speed patch alongside this is only useful for the plain `scpi`
source, where it does make readout faster.

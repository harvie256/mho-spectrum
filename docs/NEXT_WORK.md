# Next work

Written 2026-09-13 at the end of the session that replaced the tap's SCPI loop
with direct export (commit `ca7f932`). Read `CLAUDE.md` first — its traps
section describes how the export loop is sequenced and why it must never get a
delay.

**Where things stand.** The on-scope loop arms SINGLE, waits for
`CDrvScope::ReadNormTrace` to return 0, takes `CDrvScope::LockConfig`, and calls
`DrvWaveform_Export*` straight into the send buffer. Every enabled channel is
streamed interleaved; the PC splits the frame and overlays the channels.
Confirmed live on the rebuilt library: **17 fps one channel, 8.8 fps CH1+CH2**
at 2 ms/div, 1 Mpt, over the USB gigabit adapter. `ca7f932` is committed but
not pushed.

Targets that still apply (from the user): full speed in single-channel mode at
every depth and timebase, and at least half of that with two channels. Fix
races on the app's events and locks, never with delays.

**Update, second session (same day).** Item 2 is done (below), extended to
3 and 4 channels, and it turned up a real bug: `ExportData` interleaves the
channels the app *samples* — shown channels plus a hidden trigger source — in
1, 2 or 4 slots, so three channels on, or any channel shown without the trigger
channel, was silently mixed or mislabelled. The tap now reads
`GetDrvParam(0)`'s count and mask under `LockConfig` every capture
(`layout_keep`); all 15 combinations and a trigger-on-CH2 set are verified live.
See `docs/SCOPE_INTERNALS.md`, "What the export interleaves". Not committed.

---

## 1. Robustness: settings changed while streaming (do first)

*Progress:* the export layout (channel count and mask) is now read from the app
every capture, and a capture whose layout lost a streamed channel is skipped and
counted (`skip=` in the tap's stats line; `tests/acq_sweep.py` warns on it). The
PC is not told, though — a session whose streamed channel was switched off just
stops delivering frames. Record length, sample rate and vertical scale are still
startup-only.

The loop sizes everything once, at startup, from what `setup_readout` saw:
points per channel, sample rate, channel count and mask, vertical scale. None
of it is re-checked, and `ExportData` exports **whatever is enabled now**. So,
untested but inferred from the code:

* **A channel switched on or off mid-session** changes the interleave.
  `ExportData` still writes `total` samples, now split across a different
  number of channels, and the PC splits by the old count — channels are
  silently mislabelled or mixed. This is the worst failure available: wrong
  data that looks plausible.
* **Depth or timebase changed mid-session**: record length and sample rate no
  longer match `rec_bytes` and the header's sample rate, so frequencies are
  scaled wrongly or the export is truncated.

**Approach.** Read the app's own state each cycle, inside the `LockConfig`
section, before exporting. The getters are all in `libscope-auklet.so`:
`Drv_GetScope()` → `CDrvScope::GetDrvParam(unsigned)` → `CDrvParam::GetChanCount()`,
`GetChanMask()`, `GetRecordLen()`, `CDrvParam::GetSampleRate()` (names from the
disassembly; resolve and verify before use — `DrvWaveform_ExportData` itself
reads `GetChanCount`/`GetRecordLen`, see `CDrvScope::ExportData`). On a change:
either adopt it (reallocate within `slot_cap`, or publish a "reconfigure" frame
flag and let `tap_stream.py` restart), or stop with a clear log line. Prefer
adopting when it fits the ring; a restart takes ~15 s.

**Verify**: stream, then toggle CH2, change V/div, change timebase and depth
from the scope's front panel. Check each channel's tone with
`/tmp`-style frame dumps (the `framecheck.py` approach: split, FFT, peak,
CH1/CH2 correlation) — a 1 MHz on CH1 and a different tone on CH2 makes
mislabelling obvious.

## 2. Multi-channel display — done (second session)

* **Per-channel vertical scale**: `tap_stream.py` walks `:WAV:SOURce` over the
  shown channels at startup; a table after the header (flag `0x100` at offset
  18). Live: CH2 at 2 V/div beside CH1 at 1 V/div read within 0.01 dB of the
  scope's Vrms.
* **TRACE tab**: active channel (what peaks, zoom-to-signal, annotation, new
  markers and the peaks CSV follow) and per-channel show/hide (hidden channels
  keep averaging).
* **Markers per channel**, a ch column, cross-channel deltas; marker colours
  moved off the channel colours. `MarkerPanel`'s criteria signals fixed.
* **Wide trace CSV** with each channel's volts-per-code.
* **`tests/acq_sweep.py --channels 1,2,3 --tones 1.7M,3M`**: switches channels
  (restored), judges each channel against its own tone, reports every channel's
  peak.
* Checked by an offscreen window script at 1 and 4 synthetic channels (43
  checks) and live at 1–4 channels.

Left over, small: marker labels near the left edge are clipped by the plot
(pre-existing); the PC side does not learn about `skip=` (item 1).

## 3. Throughput

Two channels at 1 M are **link-limited** (~35 MB/s USB2 ceiling): the loop runs
~10 captures/s and the scope drops the rest. 10 M one channel is too.

* **One channel with the trigger on a hidden channel** exports two slots and
  runs 12.4–13.3 fps against 17.5 when it is the trigger source. Worth a line in
  the UI or the tap log ("trigger on CH1, which is hidden: costs ~25%").
* **Slot compaction got slower**: `compact=` reads 6–13 ms (2 → 1) and 17–34 ms
  (4 → n) with the per-capture layout build, against 4–8 ms in the earlier
  startup-rule build. Last-cycle figure only — sample it (`--drive-poll-ms`), then
  look at thread placement and first-touch of the larger (4-slot) buffers
  before optimising the loop.

* **Packing, revisited.** The lossless 12-bit packing (2.00 → 1.50 MB, see
  `docs/SCOPE_INTERNALS.md` "Tried and removed") bought nothing when the loop
  was the limit; now the link is, at 2 channels and at 10 M, it should buy up
  to ~33%. Its encode cost 7.9 ms/frame on the A72 — measure against the
  headroom the export loop freed.
* **ADC settling patch** (`--tap-keep-adc-sleep`, default off): its +16% was
  measured on the SCPI loop. Re-measure on the export loop, one channel and
  two, and turn it on only if it still helps with 0 repeats.
* **100 Mb onboard port** numbers on the export loop, for the docs.
* Remeasure `tap_stream.py --drive-poll-ms 10 --drive-csv` phase breakdown at a
  few settings and put it in `SCOPE_INTERNALS.md`.

## 4. Soak and regression

* 10+ minute runs, one and two channels: fps stable, `armTO`/`expErr` 0, app
  pid unchanged, scope settings restored afterwards.
* `tests/acq_sweep.py` over timebase × depth with the new stats regex, `--tone`.
* Stop/start the tap repeatedly (the slot buffers are deliberately never freed —
  check memory on the scope over many restarts).

## 5. Later: analysis the second channel makes possible

The user chose "compare spectra" for now. Cross-spectrum, transfer function
(H1 = Sxy/Sxx, gain and phase) and coherence need the complex FFT kept and
averaged per channel pair (`SpectrumEngine.process` discards phase at
`np.abs`). Averaging is exponential-only, which biases coherence — see
`CLAUDE.md`. Design channels-as-inputs so a trace can become
(channel, mode) or (channel pair, measurement).

## 6. Small known issues

* ~~`panels.py` `MarkerPanel.set_unit` connects signals inside `set_unit`~~ —
  fixed in the second session.
* Two volts formulas disagree: the tap/receiver `(code − yref) × yinc + yorig`
  vs `rigol_mho.Waveform.volts` `(r − yorigin − yreference) × yincrement`.
  Check against `:MEASure:VAVG?` on a DC offset (then `:MEASure:CLEar`).
* Stale paths: `spectrum/native_acq.js`, `analysis.py`, `panels.py` and
  `fft_gui.py`'s docstring mention `docs/STREAMING.md` / `fftdemo/`, which live
  in mho-speed-patch (`sources.py` and `stream_client.py` fixed).
* Don't use the scope's `:MEASure` queries in tests: they switch measurements
  on, and `:MEASure:CLEar ALL` is rejected (`-108`) — clear is `:MEASure:CLEar`.
* `ca7f932` not pushed to `origin` (github.com/harvie256/mho-spectrum).

## Working notes for the session

* **Scope**: `192.168.23.20` (USB gigabit adapter, `eth1`) or `.30` on the
  onboard 100 Mb port. adb on 55555, SCPI on 5555. Snapshot and restore any
  setting a test touches; `:ACQuire:MDEPth` only takes while running.
* **Synthetic first**: `./run_fft.sh synthetic --synthetic-channels 2` covers the
  whole PC side with no scope.
* **Auto mode**: the previous session's heavy Frida/disassembly work tripped
  Claude Code's auto-mode safety check, after which builds and commits were
  refused for the rest of the session. If it happens, switch to the default
  permission mode or run the command with `!`.

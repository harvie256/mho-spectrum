# Spectrum analyser features: a menu to build from

What this is: a survey of what professional spectrum analysers actually put in front of a
user, graded against `spectrum/` as it stands, so we can pick items off a list rather than
invent a feature set. Terminology is taken from vendor documentation (Rigol DSA800 /
RSA5000, Keysight X-Series and Infiniium, R&S FSW / RTO, Siglent SSA3000X / SDS,
Tektronix MDO and RSA / SignalVu, Anritsu MS2090A, Signal Hound Spike) and from the
software analysers whose UI conventions are worth stealing (GQRX, SDRangel, SDRuno,
baudline, Spectrum Lab, gr-fosphor, GNU Radio's QT sinks).

## What a spectrum analyser UI is expected to do

Strip away the RF front end and an analyser is four things: a way to say **which
frequencies you want to look at** (centre/span or start/stop), a way to say **how the
amplitude axis is scaled and what its units mean** (reference level, dB/div, dBm/dBV/V),
a way to trade **resolution against speed and variance** (RBW, VBW, sweep time,
detectors, averaging), and a way to **get numbers out** (markers, peak tables, channel
power, OBW, THD, limit lines). Everything else — spectrograms, persistence displays,
mask triggers — is layered on top of those four.

The important architectural split is between **swept-tuned** and **FFT / real-time**
analysers. A swept analyser tunes a local oscillator across the span and pushes the IF
through an analogue (or digitally-modelled) Gaussian filter whose −3 dB width *is* the
RBW; sweep time follows `T ≈ k·Span/RBW²`, and everything the instrument shows is a
consequence of that one filter dragged across the band. An FFT analyser digitises a block
of time and transforms it; there is no filter and no sweep, so **RBW is not a setting, it
is a consequence**: `Δf = fs/N` is the bin spacing, and the effective resolution
bandwidth is `RBW ≈ ENBW_window × fs/N` (ENBW: rectangular 1.0, Hann 1.5,
Blackman-Harris ≈2.00, flat-top ≈3.82). Vendors split three ways on how to present that.
Rigol, Siglent and Keysight's InfiniiVision show RBW as a *read-only derived number*;
Keysight's Infiniium and R&S let you *set* RBW and back-drive the acquisition
(R&S even offers a "record length controlled" vs "RBW controlled" toggle with a
"required acquisition time" readout); Tektronix's Spectrum View decouples it entirely and
tells you the implied window width via `Spectrum Time width = FFT window factor / RBW`.

A **real-time analyser (RTSA)** is an FFT analyser with one extra property: it transforms
*every* sample, with ≥50% overlap between frames, indefinitely and without gaps. That is
what buys the two headline claims — 100% **probability of intercept** for events above a
stated duration (Tektronix RSA6000: 3.7 µs at 110 MHz span / 10 MHz RBW; Keysight PXA
RT2: 3.57 µs; R&S FSVR: 24 µs), and **density** as a real measurement (percent of time a
frequency/amplitude cell was occupied) rather than a picture. The 50% overlap is not
arbitrary: without it, a transient landing at a frame boundary is tapered to nothing by
the window, and with it there is always a frame positioned to catch it.

### What that means for a PC app fed by a streaming scope tap

We are an FFT analyser, and we are emphatically **not** a real-time one. Being accurate
about which is which matters more than the feature count.

**The honest constraints of this setup:**

- **Not gap-free, and not close.** The deep record only exists while the scope is
  stopped, so every frame is a full arm → acquire → stop → read cycle. At 1 Mpt / 2 GSa/s
  the record spans **500 µs** and arrives every **~75–90 ms** (11–14 fps, measured). Duty
  cycle is therefore **under 1%**: we are blind for >99% of wall-clock time. A signal must
  persist for roughly a whole frame period to be *reliably* seen at all. Any claim of POI,
  100% intercept, real-time bandwidth, or gap-free monitoring is false here, and any
  "density" number we compute is a statement about our own duty cycle, not the signal's.
  The duty cycle scales with record duration, not just point count: at 1 Mpt / 50 MSa/s
  the record is 20 ms against a ~62 ms frame period, so ~32% observed and ~68% blind
  (measured 2026-09-13, one channel; a second enabled channel roughly halves it).
  Better, still not gap-free. Anritsu's practice of showing POI and minimum-detectable-duration as live status
  readouts is the right instinct; ours would read "≈75 ms" and should say so.
- **No tuner, no mixer, no preselector.** The scope digitises baseband directly, so the
  analysable range is **DC to Nyquist** and nothing else. There is no RF centre frequency
  in the analyser sense — "centre/span" here is a *view* over a fixed baseband spectrum,
  not a retune. Anything above Nyquist aliases into the band; anything above the front
  end's analogue rolloff is attenuated before we see it.
- **Sample rate sets the ceiling; record length sets the floor.** Maximum analysable
  frequency is `fs/2`; finest resolution is `ENBW × fs/N`. Both are properties of the
  scope's timebase and memory depth, which we can drive over SCPI but not exceed. The two
  fight each other: 2 GSa/s buys 1 GHz of span and costs you 2 kHz bins at 1 Mpt.
- **The scope's front end sets dynamic range, not the FFT.** 12-bit ADC, and measured on
  this instrument: ADC time-interleaving spurs at **−60 dBFS** at fs/8 and fs/4 (see
  mho-speed-patch's `docs/STREAMING.md`), harmonic products around −67 to −76 dBFS, and a per-bin noise
  floor near −90 dBFS. Process gain from a 500k-bin FFT makes the *floor* look excellent;
  **SFDR is ~60 dB and no amount of averaging improves it**. A real analyser's step
  attenuator and preamp exist precisely to manage this trade, and our only equivalent
  lever is the scope's V/div.
- **Channels share the link.** The tap streams every enabled channel from the same
  acquisition, interleaved, so N channels cost N× the bytes: CH1+CH2 at 1 Mpt is 8.7 fps
  against 16.2 for one, capped by the ~35 MB/s USB link. Correlated views
  (cross-spectrum, transfer function, coherence) are now arithmetic on data we have;
  none is built yet.
- **We hold the whole record, which is our unfair advantage.** 1 Mpt of raw samples per
  frame means gated FFT, zero-span/time-domain panes, digital down-conversion for narrow
  spans, re-windowing without re-acquiring, and Welch-style segmentation are all just
  arithmetic on data we already have. That is exactly what a swept analyser cannot do,
  and it is where the best value/effort ratio sits.

**Achievable / achievable-with-caveats / not possible, at a glance:**

| Genuinely achievable | Achievable with caveats | Not physically possible |
|---|---|---|
| RBW as a real control (via transform size), ENBW-correct RBW readout, detectors, trace modes, markers, peak tables, spectrogram, persistence bitmap, channel power / OBW / ACPR / THD / SFDR / SINAD / ENOB, limit lines and mask *testing*, gated FFT, zero-span pane, DDC zoom, CSV and raw-record export | Absolute units (dBm/dBV) — the tap now carries the preamble's vertical scale, read once at startup, so a V/div change mid-session goes stale; dBm needs a stated reference impedance; "attenuation" — only as scope V/div over SCPI; VBW — as inter-frame or inter-bin smoothing, not a real video filter; phase noise — limited by the scope's own clock; spectrogram time axis — sparse and irregular, must be labelled as such | 100% POI / any real-time BW claim; gap-free spectrogram; hardware frequency-mask *trigger* with pre-trigger capture; quantitative DPX density; CISPR quasi-peak / EMI compliance; RF tuning above Nyquist; preselector; tracking generator / normalise; mechanical or electronic step attenuation |

---

## Frequency control

| Feature | What it does | Vendor terminology / notes | Status | Effort | Value |
|---|---|---|---|---|---|
| Centre frequency + span entry | Numeric boxes setting the view's midpoint and width | `Center Freq` / `Span` — universal; Siglent publishes the identity `Center = (Start+End)/2`, `Span = End − Start` | Done | – | – |
| Start / stop frequency entry | The same view expressed as its two edges, kept in sync with centre/span | `Start Freq` / `Stop Freq` (all vendors); Keysight exposes both forms interconvertibly | Done | – | – |
| Full span | Snap the view to the whole analysable range (here, DC → Nyquist) | `Full Span` (all) | Done | – | – |
| Last span | Return to the previous span for overview↔detail toggling | `Last Span` (Rigol/Keysight/Siglent/Anritsu) | Missing | S | Med |
| Span zoom in/out | Halve or double the span about the centre in one gesture | `Zoom In`/`Zoom Out` (Rigol ×½/×2), `Span Up/Down 1-2-5` (Anritsu) | Done | – | – |
| CF step | Arrow/knob increment for centre frequency, auto-coupled to span/10 | `CF Step` Auto/Man; `CF → Step` loads the current CF as the step, the "walk the harmonics" idiom | Missing | S | Med |
| Mouse pan and wheel zoom | Drag the plot to pan, wheel to zoom, drag an axis to scale that axis alone | GQRX/SDRangel: drag the frequency scale to pan, scroll it to stretch; SDRangel scrolls the dB scale for range | Done | – | – |
| Drag-a-box zoom | Rubber-band a rectangle to zoom to it | GNU Radio QT sinks: left-drag box zoom, right-click zooms out one step, Ctrl+right-click zooms fully out | Partial | S | Med |
| Zoom to signal (one-shot) | Frame the view on the strongest non-DC component | Closest vendor analogue is Rigol/Keysight `Auto Tune` (full-span hunt, then centre) | Done | – | – |
| Signal track (continuous) | Re-centre every frame on a peak near the marker, holding a drifting carrier on screen | `Signal Track` (Rigol/Keysight/Siglent), `Signal Tracking` with Tracking Bandwidth + Threshold (R&S). Distinct from `Cont Peak`, which moves the *marker*, not the view | Missing | S | Med |
| Log frequency axis | Logarithmic X, for wideband and EMI-style views | `X Scale Lin/Log` (Rigol/Siglent); R&S needs FSW-K54 for it | Done | – | – |
| Truncate the frequency scale | Drop non-significant leading digits when zoomed into a narrow span | SDRangel "Truncate frequency scale" — at a 1 GHz Nyquist a 10 kHz window otherwise wastes the axis printing the same prefix | Missing | S | Low |
| Frequency offset | Display-only arithmetic shift of every frequency readout | `Freq Offset` (Rigol/Keysight), ±1 THz (R&S) — for external converters; here, for probe/mixer front ends | Missing | S | Low |
| Zoom FFT / digital down-conversion | Mix a narrow band to baseband, decimate, and transform it — fine resolution over a narrow span for a fraction of the work | SDR#'s Zoom FFT plugin; SDRuno's whole SP2 pane. **The highest-leverage numerical idea for this hardware**: a 10 kHz look currently costs a full 1 Mpt FFT for ~5 useful bins | Missing | M | High |
| RF centre-frequency tuning | Retune a mixer to move the analysis band | No tuner exists; "centre frequency" here can only ever be a view over baseband | N/A (hardware) | – | – |

---

## Amplitude and scaling

| Feature | What it does | Vendor terminology / notes | Status | Effort | Value |
|---|---|---|---|---|---|
| Reference level | Sets the top-of-graticule value the trace is drawn against | `Ref Level` (all). Replaced the hardcoded −160…+5 dBFS window | Done | – | – |
| Scale per division | dB per vertical division | `Scale/Div` 0.1–20 dB (Rigol/Keysight/Siglent); R&S expresses it as a total `Range`, default 100 dB | Done | – | – |
| Auto-scale / auto-range | One button that sets the amplitude window from what is actually on screen | Rigol distinguishes `Auto Scale` (ref level only) from `Auto Range` (ref level + mixer level); the SDR heuristic is "noise floor at the bottom, strongest signal a margin below the top" | Partial | S | High |
| Y units: dBFS | Power relative to a full-scale sine | Not a vendor unit — it is the honest one when nothing is calibrated. Already normalised by coherent gain so a full-scale sine reads 0 dBFS in any window | Done | – | – |
| Y units: V, dBV, dBmV, dBµV | Absolute voltage units derived from the scope's own preamble scaling | `Units` (Rigol/Keysight/Siglent/R&S). The tap now sends every enabled channel's `yincrement`/`yorigin`/`yreference` (queried once at startup; one channel's in the header, several as a table after it) and the VIEW tab offers **dBV** as a constant offset from dBFS, per channel (`analysis.unit_offset_db`) — CH2 at 2 V/div beside CH1 at 1 V/div read within 0.01 dB of the scope's Vrms. V, dBmV and dBµV are not offered; the SCPI source sends no scale, so it stays in dBFS, and the synthetic source's scales are invented | Partial | S | High |
| Y units: dBm / W | Power units, requiring a stated reference impedance | Siglent requires an "External Load" value; R&S states results "refer to a 50 Ω terminating resistor". Scope inputs are not 50 Ω by default — must be user-declared, never assumed. **dBm is offered, but at a fixed 50 Ω** (`analysis.DBM_OHMS`, stated in the unit label and tooltip, not settable); no W | Partial | S | Med |
| Power-density units (dBm/Hz, V/√Hz) | Normalise the trace to 1 Hz so the noise floor is comparable across RBW | Infiniium `DBMHZ`/`VRTHZ`; GQRX offers a "per RBW / per √Hz" denominator toggle. Needs the ENBW-correct RBW to be right | Missing | S | Med |
| Reference level offset | Add a constant to all amplitude readouts to compensate external gain/loss | `Ref Offset` ±300 dB (Rigol), `Reference Level Offset` ±200 dB (R&S). The trace does not move on screen | Done | – | – |
| Amplitude correction table | Frequency-dependent offset table for probes, cables, antennas | `Correction` with Antenna/Cable/Other/User tables, 200 points, `Freq Interp Lin/Log` (Rigol); `Transducer Factor` (R&S) | Missing | M | Low |
| DC offset capture / clear | Measure the input's DC offset once and subtract it thereafter | No direct vendor analogue (they have no DC bin). Deliberately not a per-frame mean subtraction, because real DC is signal — see mho-speed-patch's `docs/STREAMING.md` | Done | – | – |
| Input attenuation / vertical scale | Manage front-end headroom versus noise floor | On a real analyser: `Input Atten` + `RF Preamp` + `Max Mixer Level`, coupled by `Ref ≤ Atten − PA − MaxMix`. Our only equivalent is driving the scope's `:CHANnel:SCALe` over SCPI | Missing | M | High |
| Overload / clipping annunciation | Warn when the input is clipping the ADC | Rigol's `UNCAL`/over-range annunciators; baudline counts `Clips` outright. Cheap: count codes at 0 and full-scale per frame | Done | – | – |
| Normalise against a stored reference | Subtract a through-connection trace so the display reads response, not absolute level | `Normalize` + `Stor Ref` + `Norm Ref Lvl/Pos` (Rigol/Siglent) — requires a tracking generator to be meaningful as a vendor feature | N/A (hardware) | – | – |
| Preamp / step attenuator / preselector | Front-end gain and filtering ahead of the mixer | No such hardware on a scope input | N/A (hardware) | – | – |
| Noise floor extension | Subtract a characterised model of the instrument's own noise from the result | Keysight `Noise Floor Extension`, R&S `Noise Cancellation`. Would need a characterised terminated-input floor per sample rate — possible, but low priority | Missing | L | Low |

---

## Bandwidth: RBW, VBW and their FFT equivalents

| Feature | What it does | Vendor terminology / notes | Status | Effort | Value |
|---|---|---|---|---|---|
| Bin spacing readout | Show `Δf = fs/N`, the raw spacing between FFT points | Keysight is explicit that this is **not** RBW: "describes the actual space between FFT points… but it doesn't account for the actual resolution bandwidth". Shown as `Δf … Hz/bin` in the annotation block, next to the true RBW | Done | – | – |
| True RBW readout | Show `RBW = ENBW_window × fs/N` alongside the bin spacing | Siglent HD distinguishes RBW ("3 dB bandwidth… related to the window factors") from Δf explicitly. **One multiply; the single cheapest correctness win in this document** | Done | – | – |
| RBW as a control | Let the user set RBW; choose the transform size (and hence sub-record length) to honour it | Keysight Infiniium: "the change in resolution bandwidth is achieved by changing the horizontal scale"; R&S offers `Record length controlled` vs `RBW controlled` with a **"required acquisition time"** readout | Missing | M | High |
| Span/RBW ratio coupling | Auto-derive RBW from the span by a fixed ratio | `Span/RBW Ratio`, default **106** (Rigol), **100** (R&S), **300** (Anritsu), **1000** (Tektronix MDO/Spectrum View). No industry constant — make it a setting with a stated default | Missing | S | Med |
| Window selection | Trade main-lobe width against sidelobe suppression and amplitude accuracy | Have: rectangular, Hann, Blackman-Harris, flat-top | Done | – | – |
| More windows (Kaiser, Gaussian, Hamming, Nuttall) | Fill out the window set, particularly Kaiser (Tektronix calls it "closest to the traditional Gaussian RBW") | R&S RTO ships 7, SDRangel 9, baudline 11 with adjustable beta. `spectrum.WINDOWS` still holds only the four above | Missing | S | Low |
| Window characteristics table | Show the selected window's ENBW, sidelobe suppression and worst-case scallop loss | Siglent prints the table outright (Rect −13 dB / 3.9 dB error … Flattop −93 dB / <0.1 dB); baudline publishes per-window optimal overlap. ENBW is shown in the annotation block; sidelobe and scallop figures are tabulated in `spectrum.WINDOW_NOTES` but not yet displayed | Partial | S | Med |
| VBW / trace smoothing | Low-pass the detected trace to make weak signals visible without changing resolution | On a swept analyser this is a real post-detector filter; in an FFT app the honest equivalents are inter-frame smoothing (what our averaging already does) and R&S's separate `Smoothing` (1–50% aperture moving average across bins). Keep them distinct in the UI | Missing | S | Med |
| VBW/RBW ratio | Couple the smoothing to the resolution | `V/R Ratio` (Rigol/Siglent) is VBW÷RBW; R&S's `RBW/VBW` is the **reciprocal** with named presets Sine[1/1], Pulse[0.1], Noise[10]. Label the direction explicitly or it will be wrong | Missing | S | Low |
| Acquisition time readout | Show the record duration `N/fs` — the FFT analyser's analogue of sweep time | R&S shows `Required acquisition time`; Tektronix shows `Spectrum Time width = FFT window factor / RBW` | Done | – | – |
| Frame / overlap processing | Split an over-long record into overlapping sub-transforms and combine them | R&S `Frame Arithmetics` (Off / **Envelope** / Average), `Overlap Factor`, and a **`Frame coverage`** percentage — "the percentage of the trace that was analyzed". The coverage readout is unusually honest UI and directly applicable | Missing | M | Med |
| Sweep time and its coupling law | `T ≈ k·Span/RBW²`, plus `UNCAL` when violated | There is no sweep. Record length is the analogue and is already exposed | N/A (hardware) | – | – |
| EMI / CISPR filter shapes | −6 dB filter bandwidths at 200 Hz / 9 kHz / 120 kHz | `Filter Type: Gauss / EMI` (Rigol), `CISPR (6 dB)` (R&S K54). Meaningless without the matching detectors and a gap-free dwell | N/A (hardware) | – | – |

---

## Traces

| Feature | What it does | Vendor terminology / notes | Status | Effort | Value |
|---|---|---|---|---|---|
| Multiple simultaneous traces | Several independently-configured traces on one plot | 4 (Rigol DSA800), 6 (Keysight/R&S/Rigol RSA5000), 3–4 (Siglent/Anritsu). Currently there is exactly one curve | Missing | M | High |
| Trace mode: clear write | Redraw from scratch every frame | `Clear Write` (all) | Done | – | – |
| Trace mode: max hold | Keep the highest value seen per bin | `Max Hold` (all). We have it, but as a global engine flag applied *after* averaging, so enabling both gives max-hold-of-the-average — vendors treat them as separate traces | Partial | S | High |
| Trace mode: min hold | Keep the lowest value seen per bin | `Min Hold` (all); GQRX offers max and min hold together and resets both on zoom/retune | Missing | S | Med |
| Trace mode: average | Average N frames into the trace | Have exponential power averaging. See the Averaging table for what is missing | Partial | S | High |
| Trace mode: view / blank | Freeze a trace on screen, or hide it without discarding it | `View` / `Blank` (R&S/Siglent). Rigol RSA5000 and Keysight decompose this cleanly into two booleans — `Trace Update` × `Trace Display` — giving Active / View / Blank / Back-end as the four corners. **That is the model to implement** | Missing | S | Med |
| Peak-hold decay | Let a max-hold trace fade at a set dB/second instead of holding forever | baudline offers a decay rate; far more usable on a live stream than infinite hold | Missing | S | Med |
| Reference / memory trace | Store the current trace to a slot, display it, and compare against it later | SDRangel's M1/M2 slots with CSV import/export; Rigol `Stor Ref` | Missing | S | High |
| Trace math | Arithmetic between traces or against a constant | `A−B`, `A+Const` (Rigol DSA800); R&S exposes a `Trace Math Mode: Lin / Log / Power` because the domain the arithmetic happens in matters — get it wrong and A−B on noise is off by several dB | Missing | S | Med |
| Per-trace detector assignment | Each trace gets its own bin-reduction detector | Per-trace on Rigol RSA5000/Keysight/R&S; R&S auto-couples detector to trace mode (Max Hold→Positive Peak, Average→RMS) | Missing | S | Med |
| Trace labels / colours | Name and colour each trace | `Trace Labels` (R&S); GNU Radio puts line colour/width/style in the right-click menu | Missing | S | Low |
| Reset / restart | Clear averaging and hold state | `Average Reset` (Rigol), `Restart` (Keysight). Have a single "reset avg/peak" button | Done | – | – |

---

## Detectors (bin-to-pixel reduction)

`reduce_for_display()` now takes a `detector=` argument; the seven marked Done below
(`spectrum.DETECTORS`) are one combo box over the same code path. At 500k bins
across ~2000 columns each column covers ~250 bins, so detector choice is exactly as
meaningful here as on a real analyser — Tektronix describes the MDO4000's identically:
"reduces that FFT output into a 1,000 pixel-wide display… the choices are +peak, sample,
average, and −peak".

| Feature | What it does | Vendor terminology / notes | Status | Effort | Value |
|---|---|---|---|---|---|
| Positive peak | Take the maximum bin in each display bucket | `Pos Peak` / `Max Peak`. Overstates noise, and on a real analyser the error grows with dwell — never use it for a noise number. Ours also plots the max *at its true frequency*, which matters (see mho-speed-patch's `docs/STREAMING.md`) | Done | – | – |
| Negative peak | Take the minimum bin in each bucket | `Neg Peak` / `Min Peak`. Separates CW from impulsive interference; understates level on a noisy carrier | Done | – | – |
| Sample | Take one bin per bucket | `Sample` (all). Misses narrow tones outright — the classic way to lose a comb — and reads **2.50 dB** below true RMS on Gaussian noise, not 1.05 dB: a single bin's dB value carries the same log-of-exponential bias as video averaging. 1.05 dB is the *voltage*-averaging figure. Measured at −2.65 dB in `reduce_for_display` | Done | – | – |
| Average (RMS / power) | Take the RMS of every bin in the bucket | `RMS` (R&S), `Average (RMS)` (Rigol), `Pwr(RMS)` (Keysight). **The only correct detector for a noise or channel-power number**, because it needs no crest-factor assumption | Done | – | – |
| Average (voltage) | Linear envelope mean over the bucket | `Voltage Avg` / `Averaging Type = Voltage`. Right for AM and pulse envelope shape; 1.05 dB below RMS on Gaussian noise | Done | – | – |
| Average (log-power / video) | Mean of the dB values in the bucket | `Video Avg` (Rigol), `Log-Pwr` (Keysight). **Under-reads noise by 2.50 dB** (1.05 + 1.45). Best for *seeing* a CW tone near the floor, wrong for any power number | Done | – | – |
| Normal (rosenfell) | Alternate max and min per bucket when the signal both rose and fell, else show the peak | `Normal` — Rigol and Siglent both spell out "also called rosenfell". Can display a peak one bucket right of its true position | Missing | S | Low |
| Min/max envelope pair | Draw both extremes of every bucket as a filled band | R&S `Auto Peak` (unconditional max **and** min, joined by a vertical line — *not* the same algorithm as Normal); baudline calls it "min/max-pair". Shows noise-band thickness without lying | Done | – | – |
| Detector annunciator | Show which detector is active, compactly | Rigol's status-bar letters: N normal, V voltage-avg, P pos-peak, p neg-peak, S sample, R RMS, blue = auto-coupled, white = manual | Done | – | – |
| Quasi-peak / CISPR average | CISPR 16-1 weighted detectors for EMI compliance | `Quasi-Peak`, `CISPR Average`. Defined by charge/discharge/meter time constants over a continuous dwell; a gapped acquisition (<1% duty cycle at 2 GSa/s, ~31% at best) makes the result meaningless | N/A (hardware) | – | – |

---

## Averaging

Vendors distinguish four mechanisms that UIs routinely conflate; keeping them separate is
worth doing from the start. (1) VBW video filtering, (2) trace averaging across sweeps,
(3) within-bucket averaging by the RMS detector, (4) measurement-count averaging inside a
Meas function. What `SpectrumEngine` currently does is (2), in the power domain — which is
the vendor-correct default, and worth saying so in the UI.

| Feature | What it does | Vendor terminology / notes | Status | Effort | Value |
|---|---|---|---|---|---|
| Power / RMS averaging | Average linear power across frames | `Pwr(RMS)` (Keysight), `Power Average` (Siglent), `Avg Mode: RMS` (Rigol RSA5000). Correct for noise and modulated signals; this is what we do | Done | – | – |
| Log-power / video averaging | Average the dB values across frames | `Log-Pwr` (Keysight), `Video Avg` (Rigol DSA800). Pulls a CW tone out of the floor visually; reads noise 2.50 dB low | Missing | S | Med |
| Voltage averaging | Average the linear magnitude across frames | `Voltage Avg` / `Avg Mode: Scalar` | Missing | S | Low |
| Average count | How many frames the average spans | `Average Times` 1–1000 (Rigol), `Average/Hold Number` (Keysight), `Sweep/Average Count` 0–200000 (R&S). Ours is 1–64 | Done | – | – |
| Exponential vs repeat weighting | Rolling exponential average versus an arithmetic average of N that then restarts | `Avg Mode: Exponential / Repeat` (Rigol RSA5000). Ours is exponential-only (α = 1/N) and never "completes", so there is no "average done" state and no settled-result semantics | Partial | S | Med |
| Average restart | Reset the accumulator | `Average Reset` / `Restart` | Done | – | – |
| Bin smoothing | Moving average across neighbouring bins, independent of frame averaging | `Smoothing` 1–50% aperture (R&S only); Spectrum Lab does it as a Gaussian kernel across bins | Missing | S | Low |
| Separate spectrum and waterfall smoothing | Different time constants for the trace and the waterfall | SDR#'s `S-Attack`/`S-Decay` vs `W-Attack`/`W-Decay` — you often want a heavily averaged trace over a raw, responsive waterfall | Missing | S | Low |

---

## Markers

Built in Phase 1: up to 8 markers, shift+click to place and right-click to remove, a
delta marker whose reference keeps its absolute readout, peak search with next/left/right,
and a marker table. The status line's running "peak N MHz @ N dBFS" remains as a
global-maximum readout alongside them.

| Feature | What it does | Vendor terminology / notes | Status | Effort | Value |
|---|---|---|---|---|---|
| Normal marker | A placeable point on a trace reading out frequency and amplitude | `Normal` / `Position` marker (all). Shift+click to place, right-click to remove (SDRangel's gestures); SDRangel's Shift+right-click clear-all is a `clear markers` button here instead | Done | – | – |
| Multiple markers | Several markers at once, each assignable to a trace | 4–8 typical (Rigol/Siglent/Anritsu/Signal Hound), 12 (Keysight), 17 (R&S) | Done | – | – |
| Delta marker | Read the difference between a marker and a reference | `Delta`. Rigol's taxonomy is the most complete and worth copying: `Delta` (reference frozen in X *and* Y), `Delta Pair` (both movable, reference Y tracks), `Span Pair` (both move together) | Done | – | – |
| Fixed / reference marker | Pin a reference point that other markers are measured against | `Fixed` (Rigol/Keysight/Siglent), `Reference Fixed` (R&S). Tektronix's rule: the reference marker's own readout stays **absolute** regardless of the delta setting | Partial | S | Med |
| Marker table | Tabulate every active marker: number, trace, X, Y, function, result | `Mkr Table` (Rigol, 8 rows); R&S auto-shows it above 2 active markers — a good default | Done | – | – |
| Peak search | Move the marker to the highest point | `Peak Search` (all) | Done | – | – |
| Next / next-left / next-right peak | Step the marker between qualifying peaks | `Next Peak`, `Next Pk Right/Left` (Keysight), `Peak Right/Left` (Rigol) | Done | – | – |
| Minimum search, peak-to-peak | Find the minimum, or mark max and min as a delta pair simultaneously | `Min Search`, `Peak Peak` / `Pk-Pk Search` | Missing | S | Med |
| Continuous peak search | Re-run the peak search over the channel after every frame | `Cont Peak` (Rigol) — deliberately distinct from Signal Track, which moves the *view* instead | Missing | S | Med |
| Marker → centre / ref level / start / stop | Push the marker's value into a frequency or amplitude setting | `Mkr→CF`, `Mkr→Ref Lvl`, `MkrΔ→Span`, `Mkr→CF Step` (all vendors) | Partial | S | Med |
| Noise marker | Normalise the level at the marker to a 1 Hz bandwidth, reading dBm/Hz | `Noise Mkr` (Rigol), `Marker Noise` (Keysight/Anritsu). Needs RMS or sample detection plus the ENBW-correct noise bandwidth to be right | Missing | S | High |
| Band power / band density marker | Integrate power over a draggable sub-band, optionally divided by its width | `Band Function: Noise / Band Power / Band Density` with `Band Span/Left/Right` (Rigol RSA5000 — enabling it force-selects the RMS detector) | Missing | S | High |
| N dB down bandwidth | Measure the width between the two points N dB below the marker, with Q | `N dB BW` (Rigol/Siglent), `n dB down` with `Q-factor` (R&S). One key gives you filter and resonator bandwidth | Missing | S | Med |
| Marker on harmonics | Place markers automatically at n×f of the reference | Siglent `Marker on Harmonics`. For scope work — clocks, switchers — more useful than generic peak search | Missing | S | High |
| Marker readout modes | Read the X axis as frequency, period, Δtime or 1/Δtime | `Readout: Frequency / Period / ΔTime / 1/ΔTime` (Rigol). 1/Δtime is the idiom for burst repetition rate | Missing | S | Low |
| Marker crosshair line | Draw a full-width/height line at the marker | `Line State` (Rigol RSA5000) — makes an off-screen marker usable | Missing | S | Low |
| Frequency counter marker | Count zero crossings in a gate for a far more precise frequency than the bin | `Freq Count` (Rigol, 1 Hz), `Marker Counter` + `Gate Time` (Keysight), 0.01 Hz (Siglent). We hold the raw record, so this is genuinely doable by interpolation or zero-crossing | Missing | M | Med |
| Marker on spectrogram | Place a marker in the waterfall and read frequency, amplitude **and time** | Signal Hound reads all three; Tektronix adds a date/timestamp that can be shown independently. Rigol adds `Couple Marker Trace` — does the marker ride the scrolling history or stay pinned to its absolute time? | Missing | M | Med |

---

## Peak search and peak table

| Feature | What it does | Vendor terminology / notes | Status | Effort | Value |
|---|---|---|---|---|---|
| Peak threshold | Minimum absolute level for a maximum to count as a peak | `PK Thresh` (Rigol, default −90 dBm), `Peak Threshold` (Keysight/Siglent) | Done | – | – |
| Peak excursion | How far the trace must fall between peaks for them to be separate peaks | `PK Excursn` (Rigol, default 10 dB), `Peak Excursion` 0–80 dB default 6 dB (R&S). Keysight's phrasing: "the change in level that must occur — in other words, hysteresis" | Done | – | – |
| Peak table | A sortable table of every qualifying peak | Peak counts: 10 (Rigol), 11 (Keysight/Tektronix), 15 (Rigol MSO5000), 16 (Siglent/Spike), 100 (LeCroy). Note neither Keysight nor Tektronix says "peak table" — they say *peak markers* | Done | – | – |
| Sort by amplitude or frequency | Order the table either way | `Table Order: Amp Order / Freq Order` (Rigol MSO5000) — the closest vendor phrasing | Partial | S | Med |
| Peak table CSV export | Write the peak list to file | Rigol MSO5000 exports the peak table directly; R&S has `Export Peak List` | Done | – | – |
| Display-line filter | Show only peaks above (or below) a draggable horizontal line | `Pk Readout: Normal / >DL / <DL` (Rigol) — one draggable line doubles as the table filter. Elegant | Missing | S | Low |
| Exclude DC / LO | Suppress the DC bin from peak searches | `Exclude LO` (R&S); Rigol does it unconditionally. Done as `find_spectrum_peaks(exclude_dc_hz=)`, sized at 10 RBWs (min 100 Hz) so it shrinks with the bins | Done | – | – |

---

## Displays and layout

| Feature | What it does | Vendor terminology / notes | Status | Effort | Value |
|---|---|---|---|---|---|
| Spectrum plot | The line trace itself, with grid and axis labels | Present, pyqtgraph, with peak-preserving reduction over the visible span | Done | – | – |
| Spectrogram / waterfall | Frequency on X, time on Y, colour = amplitude | Universal. Depths: 8,192 traces (Rigol), 100,000 frames (R&S), 125,000 lines (Tektronix). **Our time axis will be sparse and irregular at ~12 fps and must be labelled with real timestamps, not implied continuity** | Missing | M | High |
| Time-slice recall from the waterfall | Click a line in the spectrogram to show that moment's spectrum in the trace pane | Universal; Rigol addresses history by index *or* time (`Trace Time = Trace Number × Acquisition Time`). Anritsu allows six cursors each feeding a separate trace | Missing | M | Med |
| Persistence / density bitmap | A 2-D (frequency × amplitude) hit-count grid that charges on hits and decays over time | Tektronix DPX "bitmap database"; gr-fosphor's charge/discharge model; Rigol's `Color Grade` is a weak single-trace version. **The rendering technique works on any stream of frames** — only the quantitative *density* number needs gap-free acquisition | Missing | M | High |
| Persistence controls | Intensity, variable vs infinite persistence, decay time, clear | Tektronix `Intensity` 1–100%, `Dot Persistence` Variable (100 ms–60 s) or Infinite, plus a `Clear` button; R&S `Persistence` 0–8 s + `Persistence Granularity` | Missing | S | Med |
| Density colour mapping with gamma | Map hit counts to colour with a non-linear curve so rare events stay visible | Tektronix `Max`/`Min`/`Curve` (>1 concentrates resolution on low densities); Rigol `Curve Nonlinearity` defaults to 75 of ±100. R&S puts a **probability histogram of power levels under the colour-map dialog** so you can place the low threshold above the noise floor by eye | Missing | S | Med |
| Waterfall colour maps | Perceptually sensible palettes | GQRX ships Viridis, Turbo, Plasma, White-hot, Black-hot; R&S names Rainbow / Temp Color / Monochrome. Ship perceptually-uniform maps, not jet | Missing | S | Med |
| Contrast: min/max dB sliders | Dual-handle sliders setting the dB window the palette spans, separately for plot and waterfall, with an optional lock | GQRX's exact design (−160…0 dB, defaults −120…−20); baudline calls it the `Color Aperture`. Beats a single contrast knob because it maps to things the user can reason about | Missing | S | Med |
| Auto-contrast | Set the colour range from percentiles of the current frame | R&S `Find Threshold`; the published heuristic is "noise floor just visible as speckles" — directly automatable | Missing | S | Med |
| Split / full / exclusive layout modes | Named presets for which panes are visible | Siglent's exact terminology: `Split Screen / Full Screen / Exclusive`. R&S documents that disabling the time domain is specifically how you buy refresh rate | Missing | S | Med |
| Pane split control | Set the spectrum:waterfall ratio explicitly | GQRX uses a numeric 0–100% slider (better than a bare splitter — it persists and is exact); SDRuno right-drags the frequency scale; fosphor binds it to `q`/`e` | Missing | S | Low |
| Zero span / time-domain pane | Show the raw record as amplitude vs time alongside the spectrum | Vendors' `Zero Span`; Tektronix's `Amplitude vs Time`. We already hold the samples — this is nearly free, and it is the prerequisite for gating | Missing | S | High |
| Spectrum-time indicator | Draw a bar on the time-domain trace showing which samples produced the current spectrum | Tektronix's orange `Spectrum Time` bar, width `= FFT window factor / RBW`, greyed out when it falls outside valid acquisition. **The single most valuable scope-specific idea in the survey** | Missing | S | High |
| Frame-timing strip | Per-frame interval, GUI CPU and source gap on a log axis | No vendor analogue — this is ours, and it is better instrumentation than any of them ship | Done | – | – |
| Frequency vs time / phase vs time | Instantaneous frequency and phase of the record over time | Tektronix's `Frequency vs Time` / `Phase vs Time`, from a CORDIC on the I/Q. Needs a DDC to be meaningful for a real signal | Missing | M | Low |
| IQ constellation / vector | Demodulated symbol view | Requires DDC plus symbol timing recovery — a whole application, not a feature | Missing | L | Low |
| 3-D spectrogram | Perspective waterfall | SDRangel (OpenGL), LeCroy (256 stacked spectra in 3-D). Looks good, measures nothing | Missing | M | Low |
| Pause / freeze | Stop updating but keep the display interactive for zooming and measuring | Every GNU Radio sink has it in the right-click menu. Freeze-then-zoom at full bin resolution is the natural workflow for a stream | Done | – | – |

---

## Real-time-specific features

The honest position: **the display techniques in this table all work on our stream; the
acquisition guarantees do not.** Persistence bitmaps, spectrograms and mask *testing* are
renderers over whatever frames arrive. POI, gap-free monitoring, quantitative density and
hardware mask triggering are properties of the acquisition pipeline, and ours is
gapped — under 1% duty cycle at 2 GSa/s, ~31% at 50 MSa/s, never 100%.

| Feature | What it does | Vendor terminology / notes | Status | Effort | Value |
|---|---|---|---|---|---|
| Persistence bitmap rendering | Accumulate frames into a frequency × amplitude hit grid and decay it | See the Displays table. Uniform per-cell accumulate-and-decay — trivially vectorisable in NumPy, and a natural GPU shader later | Missing | M | High |
| Statistical traces over the bitmap | Derive +peak / −peak / average line traces from the bitmap's columns | Tektronix queries the bitmap for `+Peak`, `−Peak`, `Average (VRMS)`, each with `Function: Normal / Average / Hold`; Keysight overlays a white live trace | Missing | S | Med |
| Duty-cycle / blind-time annunciation | State plainly what fraction of wall-clock time we are actually observing | Anritsu shows **POI** and **MIN DETECT** as live status readouts; R&S publishes `T_MaxMiss100`, "the longest event that can be 100% missed". Ours would read ~99% blind at 2 GSa/s, ~69% at 50 MSa/s — **say it rather than imply otherwise**. Not built: nothing in `spectrum/` computes or shows it (the Annunciators strip holds only clipping) | Missing | S | High |
| Gap marking in the spectrogram | Draw the discontinuities in the history explicitly | R&S draws **black lines** for gaps and notes that "the history depth cannot be converted to time"; Keysight timestamps every trace for the same reason. Mandatory for us, not optional | Missing | S | High |
| Achieved-overlap readout | Show how much of the record each transform shares with its neighbours | Tektronix's live `Overlap` and `Spectrums/line` readouts. Becomes meaningful once we segment a record into sub-transforms | Missing | S | Med |
| Frequency mask editing | Draw a mask by hand or from a trace, with X/Y margins and offsets | `Build From Trace` + `X/Y Margin` + `Auto draw` (Tektronix), `Build Mask from Trace` (max 20 points, Keysight), 3–1001 points (R&S). Every vendor converges on the same workflow, so it is what users expect | Missing | M | Med |
| Mask testing on received frames | Check each arriving frame against the mask and log violations with timestamps | Vendors implement this as a *trigger*; for us it is a post-hoc test. Tektronix's on-violation actions are the model: beep, stop, save trace, save picture, save acquisition | Missing | M | Med |
| Mask relative to centre / ref level | Express mask points as offsets so the mask survives retuning and rescaling | `X Relative to CF` / `Y Relative to RL` (Keysight); `X/Y Axis Type: Fixed/Relative` (Rigol) | Missing | S | Low |
| "Trigger on this" gesture | Right-click a spot on the density display; the threshold is set to 80% of the density measured there | Tektronix's `Trigger On This™`. The single best interaction idea in the RTSA survey; adaptable as "alarm when this cell lights up" | Missing | M | Low |
| Frequency mask trigger (acquisition) | Arm acquisition on a spectral mask violation, with pre-trigger capture | Universally a real-time-*option* feature (Tektronix, Keysight RTSA, R&S Real-Time, Rigol RTSA, Siglent SSA3000X-R). Needs a gap-free pipeline and a circular pre-trigger buffer; we have neither | N/A (hardware) | – | – |
| Probability of intercept / real-time BW | Guarantee that any event above a stated duration is captured with full amplitude accuracy | Requires every sample transformed with ≥50% overlap, indefinitely. With a gapped acquisition (<1% duty cycle at 2 GSa/s, ~31% at 50 MSa/s) this is not approximable | N/A (hardware) | – | – |
| Quantitative density (% of time occupied) | Report what fraction of the measurement period a cell was occupied | Tektronix: "a clean CW tone gives 100%, a pulse on for 1 µs in every 1 ms reads 0.1%". With gaps this measures our own duty cycle, not the signal's — render the bitmap, but do not put a percentage on it | N/A (hardware) | – | – |
| Gap-free long-duration monitoring | Spectrogram with no missing lines over hours or days | Tektronix DPXogram: "no gaps in the spectral lines, even for monitoring periods that can last for several days" | N/A (hardware) | – | – |

---

## Triggering and gating

| Feature | What it does | Vendor terminology / notes | Status | Effort | Value |
|---|---|---|---|---|---|
| Free run | Take whatever the source delivers | `Free Run` (all). The SCPI source deliberately forces `:TRIGger:SWEep AUTO`, because SINGle/NORMal waits forever on a quiet input | Done | – | – |
| Scope trigger passthrough | Expose the scope's own edge/pulse trigger settings in the app | The scope has a full trigger system we currently override. Useful for looking at a repeatable event; costs frame rate when the trigger is sparse | Missing | M | Med |
| Level / power trigger (PC-side) | Only display and accumulate frames whose peak or band power exceeds a threshold | Vendors' `Video`, `IF Power`, `RF Burst`. For us this is frame *selection*, not acquisition arming — it cannot recover a missed event, only filter the ones we got | Missing | S | Med |
| Trigger holdoff / rearm | Ignore triggers for a period after one fires | `Trigger Holdoff` 100 µs–500 ms (Rigol); R&S distinguishes `Stop on Trigger` (rare interferers) from `Auto Rearm` (periodic signals) | Missing | S | Low |
| Time-qualified trigger | Qualify on event duration: shorter than T1, longer than T1, between T1 and T2 | Tektronix `Time Qualified`, T1/T2 each 0–10 s; Keysight `TQT` | Missing | M | Low |
| Gated FFT | Restrict the transform to a user-selected region of the captured record | R&S: "restrict the spectrum analysis to a user-defined region… to correlate unwanted emissions to fast switching edges in switched-mode power supplies or to data transfers on bus interfaces". Keysight `:FUNCtion:GATing` with `:STARt`/`:STOP`. **With a 1 Mpt record in hand this is a draggable region plus one re-FFT — the killer feature for a scope-fed analyser** | Missing | M | High |
| Gate visualisation | Draw the gate on the time-domain trace and re-transform live as it is dragged | Keysight `Gate View`; Tektronix's Time Overview with a red bar for the spectrum window and a blue bar for the analysis window | Missing | S | High |
| Pre-trigger capture | Retain data from before the trigger event | Needs a continuously-filled circular buffer at acquisition rate. The scope has one internally; we see only whole stopped records | N/A (hardware) | – | – |
| Gated sweep (LO/video) | Gate a swept measurement to a portion of a repetitive signal | Meaningless without a sweep; the FFT-domain equivalent is gated FFT, above | N/A (hardware) | – | – |

---

## Measurements

All of these are arithmetic over a spectrum we already compute. They are what turn a
viewer into an instrument, and they are almost all `S`.

| Feature | What it does | Vendor terminology / notes | Status | Effort | Value |
|---|---|---|---|---|---|
| Channel power | Integrate power over a specified channel bandwidth | `Chan Pwr` (Rigol), `Channel Power` (Keysight/Siglent/Anritsu); Keysight scopes expose `:MEASure:FFT:CPOWer` | Missing | S | High |
| Occupied bandwidth | Bandwidth containing a specified percentage of total power | `OBW` (all), 10–99.9% (R&S), default 99%. Siglent offers a `dBc` method as an alternative to `%` | Missing | S | High |
| Emission bandwidth / x-dB down | Width between the two points X dB below the peak | `EBW` (Rigol), `n dB down` with Q-factor (R&S) | Missing | S | Med |
| Adjacent channel power ratio | Main-channel power versus the adjacent channels' | `ACP` (Rigol/Anritsu), `ACPR` (Siglent), `ACLR` (Keysight/R&S). Parameters: main BW, adjacent BW, channel spacing | Missing | S | Med |
| Total harmonic distortion | Find the fundamental, sum the harmonics, report THD | `Harmo Dist` up to 10th order with THD (Rigol), `Harmonics` with THD% (Siglent). Directly relevant to scope work | Missing | S | High |
| SFDR | Ratio of the fundamental to the largest spur | baudline's Distortion group. **Would have quantified the fs/8 and fs/4 interleave spurs immediately** (measured −60 dBFS, mho-speed-patch's `docs/STREAMING.md`) | Missing | S | High |
| SNR / SINAD / ENOB | Signal-to-noise, signal-to-noise-and-distortion, and the effective bits implied | baudline computes all four alongside THD and SFDR. ENOB is the honest headline number for a 12-bit scope front end | Missing | S | High |
| Third-order intercept | Two tones in, IM3 products and the extrapolated intercept out | `TOI` (Rigol/Keysight/Siglent), `Third order intercept` (R&S). Reports lower/upper tone and lower/upper 3rd-order products | Missing | S | Med |
| Spurious search | Sweep a table of frequency ranges, each with its own limit, and report violations | `Spurious Emissions` (Keysight/R&S), `Spur Search` up to 20 ranges. Overlaps heavily with peak table + limit lines | Missing | M | Med |
| Carrier-to-noise ratio | Carrier power versus noise power in specified bandwidths | `C/N Ratio` with offset frequency, noise BW and carrier BW (Rigol); `C/No` normalises to 1 Hz (R&S) | Missing | S | Med |
| Phase noise | dBc/Hz versus offset from the carrier, on a log-frequency plot | R&S marker function (auto-selects Sample detector and VBW = 0.1×RBW). Achievable in form, but **the floor will be the scope's own clock and the result must be labelled as a lower bound** | Missing | M | Low |
| Time-domain power | Peak / average / RMS power over a region of the record | `T-Power` with Peak/Average/RMS types (Rigol/Siglent), zero-span only. We hold the record, so it is a region selection plus three reductions | Missing | S | Med |
| Noise floor estimate line | Draw a rolling median or percentile across bins as a floor reference | Spectrum Lab's average-spectrum and reference-curve overlays. Makes "is that a signal?" answerable at a glance | Missing | S | Med |
| Pulse measurements | Width, rise/fall time, PRI, PRF, duty factor, droop, ripple | Tektronix `Pulse Table` / `Pulse Trace` / `Pulse Statistics`. A 1 Mpt record at 2 GSa/s resolves these well within the 500 µs window | Missing | M | Low |
| CCDF / amplitude statistics | Complementary CDF of instantaneous power — the crest-factor picture | `Power Stat CCDF` (Keysight), `APD/CCDF` (R&S); baudline's Histogram display is the same idea | Missing | S | Low |
| AM/FM demodulation and audio | Listen to the signal at the marker | `Demod` with volume and dwell (Rigol), `Marker Demodulation` (R&S). Needs a DDC and audio output; the gapped acquisition makes continuous listening impossible | Missing | L | Low |
| EMI pre-compliance mode | Scan table, CISPR detectors, standards limit library, peak-then-final-measurement workflow, report export | Rigol's EMI mode and Siglent's `Sequence` are the reference designs. Requires quasi-peak/CISPR-average detectors over a continuous dwell | N/A (hardware) | – | – |

---

## Limit lines, masks and pass/fail

| Feature | What it does | Vendor terminology / notes | Status | Effort | Value |
|---|---|---|---|---|---|
| Upper / lower limit lines | Draw a piecewise-linear boundary and fail the trace for crossing it | `Limit: Upper / Lower` (all), 200 points (Rigol/R&S), 8 active (R&S). Anritsu's `Add Vertical` inserts a linked step-pair, essential for masks with vertical edges | Missing | M | Med |
| Pass/fail judgement | Continuous test with an on-screen verdict | `Pass/Fail (P/F)` with split-screen results (Rigol), `Limit Check` (R&S) | Missing | S | Med |
| Margin | A softer threshold inside the limit that warns without failing | `Margin State` + `Margin` (Rigol/Keysight); R&S shows `MARG` for a margin violation versus `Fail` for a limit violation | Missing | S | Low |
| Build limit from trace | Generate a limit envelope from the current trace plus an offset | `Build From Trace` + `Build` (Rigol), `Limit Envelope` (Anritsu, generates both lines at once) | Missing | S | Med |
| Fail actions | Stop, beep, or save on a violation | `Fail Stop` (Rigol), `Buzzer` (Siglent), `Save On…` auto-save on alarm (Anritsu); Tektronix's action list also saves the acquisition and a screenshot | Missing | S | Med |
| Display line | A plain draggable horizontal reference with no judgement attached | `Display Line` (Rigol/Keysight/Siglent); R&S has H1/H2 horizontal and V1–V4 vertical lines. Rigol also uses it as the peak-table filter | Missing | S | Med |
| Standards limit library | Preloaded CISPR/FCC/EN limit lines | `Load Std Lim` (Siglent), EN55022 Class B AV/QP (Rigol EMI). Meaningless without compliance detectors | N/A (hardware) | – | – |

---

## Calibration and corrections

| Feature | What it does | Vendor terminology / notes | Status | Effort | Value |
|---|---|---|---|---|---|
| Preamble-based volts scaling | Convert raw ADC codes to volts using the scope's own `yincrement`/`yorigin`/`yreference` | Implemented for SCPI in `device/rigol_mho.py`. The tap now sends them per channel (`device/tap_stream.py` queries each enabled channel's once at setup; `mhotap_set_yscale` / `mhotap_set_yscale_ch` store them, the latter as a table after the header) and `stream_client.Frame` carries its own channel's; the GUI converts each channel with its own `yinc` for dBV/dBm. Still partial: read once, so a V/div change mid-session is not picked up, and `ScpiSource` does not populate them | Partial | M | High |
| Probe attenuation factor | Account for a ×1/×10/×100 probe | Scope-side `:CHANnel:PROBe`; on an analyser this lives in the correction table. Read it once at setup | Missing | S | Med |
| Reference impedance | Declare the impedance used for dBm and watts | Siglent's "External Load"; R&S's 50 Ω note. Must be user-declared for a scope input, never assumed. Currently stated but fixed: 50 Ω, shown as `dBm (50Ω)` | Partial | S | Med |
| Correction / transducer table | Frequency-dependent amplitude correction from a CSV | `Correction` tables with `Freq Interp Lin/Log` (Rigol); `Transducer Factor` (R&S) | Missing | M | Low |
| ADC spur annotation | Mark the known interleave spurs at k·fs/16 so they are not mistaken for harmonics | No vendor analogue — this is specific to this instrument, documented and measured in mho-speed-patch's `docs/STREAMING.md`. Cheap, and it removes the exact confusion the README warns about | Done | – | – |
| Self-alignment / internal cal | Instrument self-calibration against an internal reference | The scope has its own self-cal; the PC side has nothing to align | N/A (hardware) | – | – |

---

## Acquisition and source control

| Feature | What it does | Vendor terminology / notes | Status | Effort | Value |
|---|---|---|---|---|---|
| Channel selection at runtime | Switch the analysed channel without restarting | Among the channels being streamed, yes: the TRACE tab picks the active channel the readouts follow and which are drawn. Which channels are streamed is still fixed when the tap starts (npts and buffers are sized at priming), so enabling another means restarting the tap | Partial | M | High |
| Sample rate / timebase control | Set the scope's sample rate from the app, which sets Nyquist | Vendors' span control implies this; here it is a genuine acquisition change over SCPI. Also the lever for distinguishing real harmonics from interleave spurs — they move with fs, harmonics do not | Missing | M | High |
| Memory depth / record length control | Set points per record, which sets RBW | R&S's "RBW controlled" mode does exactly this; it is what makes `RBW` a real setting rather than a readout | Missing | L | High |
| Frame rate cap | Limit how often frames are processed, to leave CPU for other work | GQRX's `FFT Rate` 5–60 fps with a red dropped-frame indicator; SDRangel's FPS cap defaults to 20 with "NL" for unlimited | Partial | S | Low |
| Dropped-frame annunciation | Show frames dropped, stale repeats, and source gap | Present in the status line and the timing strip, and better than any vendor's | Done | – | – |
| Stale-record rejection | Never display a record the scope did not re-acquire | Payloads are CRC'd and byte-identical repeats are counted and discarded. No vendor needs this; we do, and it is already right | Done | – | – |
| Multi-channel simultaneous analysis | Analyse two or more channels at once from the same acquisition | Keysight's MXR RTSA mode does per-channel centre frequencies over a shared span; Tektronix Spectrum View has a DDC per FlexChannel. Ours: the tap sends every displayed channel (1–4) from one acquisition, reading the app's own sampled-channel layout each capture, and the plot overlays them in the scope's colours (1 M: 17.5 / 8.8 / 5.85 / 4.39 fps for 1–4 channels, link-limited from two). Each channel carries its own vertical scale; a TRACE tab picks the active channel and which are shown; markers sit on their own channel and deltas compare channels; the trace CSV has a column per channel. Still partial: shared span only, which channels are streamed is fixed when the tap starts, and scales go stale if V/div changes mid-session | Partial | M | Med |

---

## Data management: state, export, record and remote

| Feature | What it does | Vendor terminology / notes | Status | Effort | Value |
|---|---|---|---|---|---|
| Save / recall state | Persist every setting and restore it | `State (.sta)` files plus `Register 1–16` quick slots with save timestamps (Rigol); `Quick Save/Recall` (R&S). Currently CLI arguments, plus the last source and scope IP remembered in `~/.config/mho-spectrum/settings.json` | Partial | S | Med |
| Preset | Return to a known default configuration | `Preset` with `Factory / User1–User6` (Rigol); `Mode Preset` vs `Restore Defaults` (Keysight) | Missing | S | Med |
| Screenshot | Save a PNG of the window | A `screenshot` button in the VIEW tab, and `--screenshot` with `--run-seconds` for smoke tests. GNU Radio puts "save figure" in the right-click menu | Done | – | – |
| Trace CSV export | Write the displayed trace to file | `Measurement Data (.csv)` (Rigol), `Export Trace to ASCII File` with configurable decimal and column separators (R&S — because European locales use comma decimals). pyqtgraph's right-click export gives the *reduced* curve only. With several channels, one frequency column and a power column per channel, each channel's volts-per-code in the header | Done | – | – |
| Full-resolution spectrum export | Write all 500k bins, not the display reduction | The reduction is lossy by design; a measurement export must not be | Done | – | – |
| Reference trace import/export | Save and reload stored traces as CSV | SDRangel imports and exports its M1/M2 memory traces | Missing | S | Med |
| Raw record capture to disk | Save the underlying samples, with metadata, for offline re-analysis | The scope-side equivalent of IQ recording. GQRX and Signal Hound both write **SigMF** (`.sigmf-meta` JSON + `.sigmf-data`); Tektronix uses `.tiq`. A timestamped raw record plus a sidecar lets an event be re-windowed and re-transformed later | Missing | M | High |
| Save on event | Auto-save the trace, screenshot or record when a limit or mask is violated | Tektronix's `Actions` tab (save acquisition / trace / picture, with a max-files cap); Anritsu's `Save On…` | Missing | S | Med |
| Continuous logging | Log power, peak or SNR at an interval to CSV for unattended runs | SDRuno's "PWR & SNR TO CSV"; Spectrum Lab fires a script on every `new_spectrum`. For drift, thermal and intermittent-spur hunts this beats any display feature | Missing | S | Med |
| Replay of saved records | Load a saved raw record and drive the whole display from it | Signal Hound's `.shr` replay keeps "markers, min/max/avg traces, channel power, occupied bandwidth, persistence, and spectrogram views" working. Also the best possible regression harness for this app | Missing | M | Med |
| Headless / throughput mode | Run with no display, for measurement and diagnosis | Rigol's `Scr State` off exists for the same reason (stop rendering to go faster). Ours is a full instrumented harness | Done | – | – |
| Remote control of the app | Drive settings and read results programmatically | Every analyser has SCPI over LAN. SDRangel streams FFT frames over a WebSocket; Spectrum Lab embeds an HTTP server and a scripting interpreter | Missing | L | Low |

---

## UI ergonomics and annotation

| Feature | What it does | Vendor terminology / notes | Status | Effort | Value |
|---|---|---|---|---|---|
| Instrument-style status annotation | Show ref level, scale/div, RBW, VBW, detector, trace mode, units and acquisition time as a fixed annotation block | Rigol DSA800 enumerates all 35 on-screen elements; R&S splits a `channel bar` (Ref Level, Att, RBW, VBW, Mode) from a `diagram footer` (CF/Span, Pts). Split the same way: an annotation block (ref level, dB/div, centre/span, RBW with ENBW, Δf, detector, averaging, acquisition time) over a telemetry line (points, MSa/s, fps, frame timing) | Done | – | – |
| Manual-setting indicator | Mark any parameter the user has taken out of auto | Rigol prints a `*` next to every manually-set parameter; the detector letter is blue when auto-coupled, white when manual. Cheap, and it prevents an entire class of confusion | Missing | S | Med |
| Toolbar controls | Window, averaging, peak hold, log frequency, DC capture/clear, span buttons, timing strip | Present, now as tabbed control groups in `panels.py` rather than one toolbar | Done | – | – |
| Right-click context menu | Line style, grid, autoscale, pause, save figure, export | pyqtgraph provides a default menu (view-all, axis modes, export). GNU Radio's sinks add pause and FFT settings to theirs — it is where users look first, and it keeps the toolbar small | Partial | S | Med |
| Keyboard shortcuts | Drive the common actions from the keyboard | fosphor: `z` zoom, `a`/`d` move, `s`/`w` width, `q`/`e` pane split, space pause, arrows for dB/div and offset | Missing | S | Med |
| Scroll a frequency digit to step it | Hover a digit in a frequency box and wheel it to change that decade | Universal across SDR#, GQRX and SDRuno; the most precise tuning affordance ever designed for a spectrum display | Missing | S | Low |
| Frequency snap grid | Snap entry to a chosen step (1 Hz, 1 kHz, 12.5 kHz…) | SDRuno's Tuning Step menu with per-mode defaults; CubicSDR snaps to a clicked digit's place value | Missing | S | Low |
| Colour theme | Light/dark, and a print-friendly palette | `Print palette Gray/Color` (Rigol); Siglent has a normal/inverse screenshot mode | Missing | S | Low |
| User annotations | Place text labels on the plot at known frequencies | Siglent allows 10 on-screen notes; SDR#'s Frequency Manager draws saved-frequency labels with adjustable transparency. For scope work: label the clock, the switcher, the ADC spurs | Missing | S | Med |
| Live settings changes without clearing history | Change FFT size or window without wiping the waterfall | Spectrum Lab changes FFT size 256–65536 "without stopping analysis or erasing prior waterfall history" | Missing | M | Low |
| Reduced-resolution live, full resolution on stop | Render cheaply while running, then redraw at full fidelity when paused | Tektronix's DPXogram runs at 500×267 live and redraws at up to 4001 points/line on Stop. Our display reduction already does the equivalent per-frame; the "on pause, go full" half is missing | Partial | S | Med |
| Soft-key menu hierarchy | The FREQ / AMPT / BW / TRACE / MARKER / MEAS structure itself | Worth adopting as the top-level organisation once there are more than a dozen controls — it is the mental model every user of an analyser already has. Adopted as a tab bar: FREQ / AMPT / BW / DET / MARKER / VIEW (`window.py`). No TRACE or MEAS tab yet, because nothing exists to go in them | Partial | M | Med |

---

## Suggested implementation order

### Phase 1 — Make the existing display honest and measurable — **done**

Everything here builds directly on `SpectrumEngine` and `reduce_for_display()`, needs no
architectural change, and fixes things that are currently *wrong* rather than merely
absent.

Built. The GUI was decomposed first — `run_gui()` was a single 475-line function of
closures and could not absorb thirty new controls — into `viewmodel.py` (the numbers,
Qt-free), `panels.py` (the FREQ/AMPT/BW/MARKER/VIEW control groups), `plots.py` (the
panes), `markers.py`, `analysis.py` (peaks, spurs, units, CSV) and `window.py` (the
wiring and the frame loop). Frame timing is unchanged: 50 ms median interval, 8 ms FFT,
0.4 ms reduction, measured against the same synthetic source before and after.

Three things the work turned up that the tables above now reflect:

* **The sample detector reads 2.50 dB low on noise, not the 1.05 dB this document
  originally claimed** — a single bin's dB value carries the same log-of-exponential bias
  as video averaging, and 1.05 dB is the *voltage*-averaging figure. Measured at −2.65 dB
  against a known floor, alongside RMS at −0.03 and voltage-average at −1.06.
* **A peak threshold below the noise floor is the most expensive thing the display can
  do.** scipy applies the height filter before computing prominences, so a threshold in
  the noise makes every noise bin a candidate: 12–17 ms per frame against an ~8 ms FFT.
  The threshold therefore defaults to auto, tracking the floor — the largest of N
  exponential bins sits `10·log10(ln N) + 1.6` dB above the median, 12.8 dB at N = 500k,
  which matched the measurement exactly.
* **Autoscale must not snap dB/div to the 1-2-5 ladder.** A dBFS floor near −120 under a
  0 dBFS peak needs ~13.6 dB/div, and the next 1-2-5 step is 20 — a 200 dB graticule for
  135 dB of signal. The ladder exists for physical graticules with printed per-division
  values; here the axis labels are computed, so it only wastes screen.

- True RBW readout (`ENBW × fs/N`) shown next to the existing bin spacing, with a window
  characteristics table (*only ENBW is shown; sidelobe/scallop are in `WINDOW_NOTES`
  but not displayed*)
- Detector selector on the display reduction: +peak (have), −peak, sample, RMS/average,
  min/max envelope pair
- Reference level, dB/div and auto-scale, replacing the hardcoded −160…+5 dBFS window
- Centre/span and start/stop numeric entry, kept in sync
- Markers: normal, delta, peak search, next/left/right peak, threshold and excursion,
  marker table, marker→centre
- Peak table with sort and CSV export (*sort exists as `find_spectrum_peaks(sort_by=)`,
  not in the UI*)
- Full-resolution trace CSV export, and a screenshot button
- Clipping/overload annunciation, and ADC-interleave-spur annotation at k·fs/16
- Duty-cycle / blind-time readout, stating plainly that this is not a real-time analyser
  (*not built — carried forward*)

*Rationale: highest value per hour, and it turns a viewer into something whose numbers can
be trusted and quoted.*

### Phase 2 — Traces, measurements and absolute units

- Multiple traces with the `Trace Update × Trace Display` model (Active/View/Blank), each
  with its own mode and detector; min hold; stored reference trace with trace math
- Log-power and voltage averaging alongside the existing power averaging; repeat vs
  exponential weighting
- Preamble plumbed through the tap (`yincrement`/`yorigin`/`yreference`), giving V, dBV,
  dBµV and — with a declared reference impedance — dBm and power-density units
  (*started: the tap sends the scale once at startup and dBV and dBm at a fixed 50 Ω are
  offered; V, dBµV, a settable impedance and density units remain*)
- Measurement suite: channel power, OBW, THD, SFDR, SNR/SINAD/ENOB, TOI, C/N, noise
  marker, band power marker
- Manual-setting indicators on the instrument-style annotation block (*the block itself
  was built in Phase 1*)

*Rationale: these are the numbers people actually want out of a spectrum analyser, and
almost all of them are small once the trace and units machinery exists.*

### Phase 3 — Use the record we already hold

The scope-specific advantage, and the part no swept analyser can do.

- Zero-span / time-domain pane showing the raw record
- Gated FFT: a draggable region over the time trace that re-transforms live
- Spectrum-time bar drawn on the time trace, greyed when invalid
- RBW as a real control by choosing the transform size within the record, with the implied
  acquisition time shown; Span/RBW ratio coupling
- Frame/overlap processing over the record, with an honest frame-coverage percentage
- Zoom FFT via digital down-conversion for narrow spans
- Pause/freeze that keeps the display interactive (*done: `freeze` in the VIEW tab*)

*Rationale: turns 1 Mpt of samples per frame from a display cost into the feature set.*

### Phase 4 — Time-domain history and density

- Spectrogram/waterfall with **real timestamps**, explicit gap marking, and time-slice
  recall into the trace pane
- Persistence/density bitmap with intensity, variable/infinite decay, clear, and a gamma
  curve on the colour map — rendered, but with no density percentage claimed
- Colour maps, min/max dB contrast sliders with plot/waterfall lock, auto-contrast
- Split / full / exclusive layout modes with a persistent pane-split control
- Statistical +peak/−peak/average traces derived from the bitmap

*Rationale: the biggest visible upgrade, and the one that makes intermittent signals
findable — but only worth doing after the trace and annotation model is settled, since
every pane needs to share it.*

### Phase 5 — Automation, capture and long runs

- Save/recall state, quick registers, and presets
- Limit lines and mask editing with pass/fail, margin, build-from-trace, and fail actions
- Mask testing on arriving frames with timestamped violation logging
- Raw record capture to disk with metadata (SigMF or an equivalent sidecar), and replay
  that drives the whole display — which doubles as the regression harness
- Continuous CSV logging of power/peak/SNR for unattended runs
- Changing which channels are streamed without restarting the tap, and scope
  sample-rate/memory-depth control from the app
- Keyboard shortcuts, colour theme, user annotations, soft-key menu reorganisation

*Rationale: what makes it usable for a real investigation that runs longer than a sitting,
and replay is worth building for testing alone.*

---

## Deliberately out of scope

- **Probability of intercept, real-time bandwidth, gap-free monitoring** — the acquisition is
  gapped (under 1% duty cycle at 2 GSa/s, ~31% at 50 MSa/s); the numbers would be fiction.
- **Quantitative DPX density (percent of time occupied)** — with gaps this measures our
  duty cycle, not the signal's. Render the bitmap; do not put a percentage on it.
- **Hardware frequency mask trigger and pre-trigger capture** — needs a continuously
  filled circular buffer at acquisition rate; we only ever see whole stopped records.
- **Quasi-peak, CISPR average, RMS-average detectors and EMI pre-compliance mode** —
  defined by charge/discharge time constants over a continuous dwell we do not have.
  A limit-line "did my change help?" workflow gives most of the practical value honestly.
- **RF centre-frequency tuning above Nyquist, preselector, image rejection** — there is no
  mixer; the analysable range is DC to `fs/2`, full stop.
- **Step attenuator, preamp, max-mixer-level coupling** — no such hardware on a scope
  input; the scope's V/div is the only headroom control and belongs in Acquisition.
- **Tracking generator, normalise, VSWR, distance-to-fault, scalar/vector network
  analysis** — all require a source we do not have.
- **Sweep time, sweep-time rules, `UNCAL`, gated sweep** — there is no sweep; record
  length and gated FFT are the correct analogues and are already in the tables.
- **Signal classification, spectrum occupancy monitoring, coverage/interference mapping** —
  built on continuous wideband monitoring plus GNSS; both absent.
- **Full demodulation applications (AM/FM audio, constellation, EVM, symbol tables)** —
  a separate application, and unusable anyway on a gapped acquisition.
- **Instrument self-alignment and internal calibration** — the scope does its own; the PC
  side has no reference to align against.

# Inside the scope: firmware layout, the acquisition path, and where the frame time goes

What this is: what the MHO934 looks like from the inside, taken from an eMMC image and
from measurements on the live instrument, written down because almost none of it is
derivable from this repo's own code. Two halves: **static** (what the firmware contains
and what the app's native core actually calls) and **measured** (what the CPU and the
link are doing while we stream). Where the two disagree, the measurement wins — that
happened more than once while this was being written, and the corrections are recorded
rather than quietly dropped.

Measurements are from **2026-09-08**, firmware `1.5.3`, on
`RIGOL TECHNOLOGIES,MHO934,MHO9B275202463,00.01.00`. Numbers here are measurements, not
targets; re-measure before trusting one that a change might have invalidated.

## The platform

RK3399: four Cortex-A53 (`cpu0..3`) and two Cortex-A72 (`cpu4..5`), running **Android
7.1.2**, kernel **4.4.126 #23 SMP PREEMPT** (built 2025-08-15). `adb` is on **port
55555**, not the default — 5555 is SCPI. `adb` is root, and `/system/xbin` has both
`strace` and `simpleperf`.

## Unpacking the firmware image

The backup at `~/rigol-backup/mho934-mmcblk0-*.img.zst` is 785 MiB compressed and
**29.15 GiB** raw (31,299,993,600 bytes). It has **no partition table** — `fdisk` and
`sfdisk` both find nothing. The layout lives in a Rockchip `parameter` block at file
offset `0x400000`, as a `CMDLINE:` string containing `mtdparts=`, alongside
`MACHINE_MODEL:rk3399_rigol`.

**The offsets in that cmdline are not where the partitions are.** Every one is shifted
**+0x2000 sectors (4 MiB)** — the loader region ahead of them. Carving at the nominal
offsets yields garbage that `file` reports as `data`, which is what makes this worth
writing down. Corrected, in 512-byte sectors:

| partition | sector | size | contents |
|---|---|---|---|
| kernel | 0x12000 | 24 MiB | Rockchip `KRNL` header, then an **uncompressed** arm64 `Image` at +8 |
| boot | 0x1E000 | 32 MiB | |
| system | 811008 | 2 GiB | ext4, label `system` |
| rigol | 5047360 | 500 MiB | ext4, label `rigol` |
| userdata | 6299648 | ~26 GiB | ext4, effectively empty |

Both filesystems come out unprivileged with `debugfs -R "rdump / dest" rigol.img` (it
warns about ownership; the files land anyway). No loop mount, no root needed.

`/rigol` is the whole instrument stack: `app/{Sparrow,Launcher,Webcontrol}.apk`,
`driver/` (22 `.ko`), `tools/`, `shell/` (the boot scripts), `FPGA/sparrow3_K160T.bit`,
`MCU/`, and the embedded help PDFs. `shell/load_pcie.sh` insmods `pcie-rockchip.ko` then
**`xdma.ko` — the stock Xilinx XDMA Reference Driver 2019.2.51** — and chmods
`/dev/xdma0_*`.

## The acquisition path

The app's native core is `lib/arm64-v8a/libscope-auklet.so` inside `Sparrow.apk`: 12 MB,
NDK r21, stripped, but with a large `.dynsym` that makes static work practical. There is
no aarch64 disassembler in the base install; `binutils-aarch64-linux-gnu` provides
`aarch64-linux-gnu-objdump`.

A census of every `BL` that lands in the PLT (10,249 PLT entries, 7.4 MiB of `.text`,
164,676 resolved call sites) gives the shape of it:

| import | call sites |
|---|---|
| `usleep` | 488 |
| `ioctl` | 37 |
| `close` / `open` | 45 / 7 |
| `mmap` / `munmap` | 2 / 1 |
| `poll` / `select` | 2 / 4 |
| `read`, `write`, `pread`, `readv` | **not imported at all** |

That last row is the point: **no bulk waveform data ever crosses a `read()`**. The path is
two mappings and a pair of ioctls.

* **`Dev_PCIeInit`** opens `/dev/xdma0_bypass` (`O_RDWR|O_SYNC`, retrying 30,000× at
  `usleep(1000)` — a 30 s boot budget) and `mmap`s a **16 MiB** window. Every FPGA
  register access goes through here: `Dev_ReadRegister` / `Dev_WriteRegister` call
  **only** `pthread_mutex_lock`/`unlock` around a load/store into that mapping. 103
  callers reach it through `DevAcquireSpu_WriteRegister` alone. Register I/O costs no
  syscall — but note the single mutex, which matters under load (below).
* **`DrvDMA_Init`** (from `CDrvScope::Start`) opens **`/dev/dma_auklet`** — a custom
  Rigol character device, hence the ioctl magic `'a'` — and `mmap`s **256 MiB** shared,
  split in half: `DrvDMA_GetSrcPtr()` = base, `DrvDMA_GetDstPtr()` = base + 128 MiB.
* **`DrvDMA_Copy(int,int,int)`** is 188 bytes containing exactly two ioctls,
  `_IOR('a',101,8)` = `0x80086165` then `_IOR('a',99,8)` = `0x80086163`, returning −5 on
  failure. No payload crosses the boundary; it is trigger-and-status over a buffer both
  sides already share.
* **`/dev/xdma0_c2h_0` is the real data path** — see the correction below. Its fd global
  is referenced only by the opener and `Dev_Close`, which misled an earlier draft of this
  document into calling it dead code.

### Correction (2026-09-09): the mmap path above is NOT the live data path

`/dev/dma_auklet` **does not exist on the instrument** — nothing creates it (absent from
all 22 `.ko` files, the kernel `Image`, `boot.img`, and both filesystems except the APK).
So `DrvDMA_Init` fails, `DrvDMA_GetSrcPtr()` and `DrvDMA_GetDstPtr()` both return
**NULL**, and a Frida hook on `DrvDMA_Copy` never fires during live acquisition. The
whole `dma_auklet` mmap-plus-two-ioctls path is dead code on this hardware.

What actually moves the waveform, measured by hooking libc on the running app:

```
read fd=83   19.7 calls/s   37.51 MB/s     fd 83 -> /dev/xdma0_c2h_0
```

One ordinary `read()` of ~1.9 MB (1 Mpt x 2 bytes) per acquisition at ~20 Hz, straight
off the Xilinx XDMA c2h streaming channel. `/dev/xdma0_bypass` is open as fd 75 and still
serves the register window as described above.

The app's `.dynsym` imports no `read`, which is what led the static pass astray — the call
does not come through `libscope-auklet.so`'s PLT. Two lessons: the absence of an import is
not the absence of a syscall, and **the scope acquires at ~20 Hz / 37.5 MB/s internally,
comfortably above the 14.3 fps / 28.5 MB/s we can ship** — the link is the limit, not the
instrument.

### "DeInterleave" does not de-interleave

`CDrvScope::DeInterleave` (5,020 B) and `CDrvScope::DeMemInterleave` (2,580 B) have **no
per-sample loop**: 18 and 15 byte/halfword element ops respectively, **zero SIMD
instructions**, and two logical `memcpy`s each. The 173 `bl` in the former are
`CScopeWfm` setters (`setRange`, `setTracePoint`, `setMemPoint`, `setSPUCompress` …),
each appearing ~7 times — a loop over traces. It is an orchestrator: fire `DrvDMA_Copy`,
copy the record out of the mapped buffer, populate metadata.

The actual lane reordering happens below userspace — FPGA, DMA descriptors, or the
`dma_auklet` driver; the static evidence cannot tell which. **So there is no software
de-interleave to offload to the PC.** Library-wide the heaviest byte-level loops are all
in string/table/CAN-decoder/zlib code, nothing in the waveform path.

### The SCPI reply path, for contrast

`CApiWave::toWord(uchar*, int)` is 260 bytes: construct an `RByteArray`, then loop
`i` from 0 to `2n` calling `RByteArray::append(ptr, 1)` — **one byte at a time**, ~2M
calls for a 1 Mpt record. That is the ~110 ms per frame the tap exists to bypass.

What feeds it (static, 2026-09-13): `:WAV:DATA?` → `CApiWave::getData`, which asks for
the record in chunks of `1,000,000 / GetChanCount()` points → `getWfmData` →
`getMemoryData(buf, start, count)`, which does `DrvWaveform_ExportInit(0)`,
`DrvWaveform_ExportData(start × channels, count × channels, buf, false)` and
`DrvWaveform_ExportBack()`, then picks the requested channel out of the result with a
byte loop (every other uint16 with two channels on) and hands it to `toFormat`. So one
export already holds **every enabled channel, interleaved**. `CDrvScope::ExportData`
programs `DevAcquireSPU_SetWaveRange`/`SetTxInfo`/`TxFrmHead`, calls
`RequestNormTrace`, and reads the samples with `DevAnalyzeTrace_Read` — an ioctl and a
blocking `read()` on the c2h stream.

### How the tap reads now: direct export

The tap calls those three export functions itself, from its own thread, into its send
buffer — no SCPI in the frame loop, no byte loop, no copy (`export_main` in
`device/libmhotap.c`). *When* to call them came from timestamping the app's own
functions with an observe-only Frida hook (arm, `ReadNormTrace` enter/leave/return,
`ExportData`, `getMemoryData`, `toWord`, per thread):

* **The acquisition thread polls `CDrvScope::ReadNormTrace`** from `CDrvScope::run`
  every ~10 ms: `-3` until a capture is in, `0` once it has read it for its own display
  (success at a median 87 ms after the arm with one channel, 101 ms with two).
* **The old SCPI-driven tap ran a cycle out of step.** Its `:WAV:DATA?` export started
  after the next arm, all 90 of 90 overlapped an arm and a `ReadNormTrace`, the arm
  blocked 36 ms on `LockConfig`, and a 1 M export took **65 ms** — against **3.4 ms**
  when nothing overlaps it.
* **`ReadNormTrace` success is necessary but not sufficient.** `CDrvScope::run` holds
  `CDrvScope::LockConfig` around `ReadNormTrace` *and* the `SetState` calls after it,
  which reprogram the same SCU/SPU registers an export uses. A prototype that exported
  on the success alone failed ~8 cycles in 60: the export blocked 1,005 ms in the c2h
  read and returned `-5`, the app's next `ReadNormTrace` failed the same way after 1 s,
  and it then returned `-3` for 2 s. Taking `LockConfig` around the export fixed it
  outright (lock wait ~1 ms); the export functions do not take that lock themselves.

So a cycle is: `SetState(3)` → wait for a `ReadNormTrace` success (return hook in
`mho_tap.js` → `mhotap_rnt_done`) → `LockConfig` → export in 1 M-per-channel chunks →
`UnlockConfig` → publish. Measured 2026-09-13, USB gigabit, 25–30 s per point, 0 export
errors, 0 timeouts, 0 repeated frames:

```
one channel   100 us/div  10 k  10 MSa/s    19.9 fps
              100 us/div   1 M   1 GSa/s    14.3 fps
                2 ms/div 100 k   5 MSa/s    18.2 fps
                2 ms/div   1 M  50 MSa/s    16.2 fps  (arm 23, wait 33, export 5 ms)
               20 ms/div  10 M  50 MSa/s     1.72 fps (34 MB/s: the link; 47 dropped)
CH1+CH2         2 ms/div   1 M  50 MSa/s     8.7 fps  (wait 87, export 7 ms; ~10
                                                        captures/s, link carries 8.7)
```

Frames were checked offline: a 1 MHz tone on CH1 peaks at 1.000000 MHz at every setting
above, and in the two-channel frame CH2 peaks at 2.999999 MHz with a CH1/CH2
correlation of 0.000 — the interleave is split correctly. The same settings on the SCPI
loop gave 17 (with a 20 ms delay), 13–14.5, and 1.81 fps.

### History: the SCPI-driven loop and its delays

Until 2026-09-13 the tap made the app produce each record by sending `:WAV:DATA?` over
loopback and caught it in a `toWord` hook. Every race above then surfaced as a
separate symptom, each fixed with a delay; they are recorded because the symptoms are
what you see if anything ever reads the record over SCPI again.

* **Deep records arrive in chunks, and a read too early latches broken.** At 10 M one
  query made ten 1 M `toWord` calls; a query within microseconds of idle got one chunk,
  and every later cycle did too until a ~200 ms pause (settle hysteresis 200 → 40 ms).
  Before reassembly, 10 M shipped 1 M fragments labelled 500 MSa/s: a 1 MHz tone read
  as 10 MHz.
* **Another enabled channel splits a 1 M record too** — `getData`'s
  `1,000,000 / channels` — into calls of 500,000 (three under plain SCPI, the later ones
  370–850 ms after the query). A tap sized from depth dropped 84 of 86 records
  (13 → 0.09 fps); the fix needed a 2 s record wait and a 120 ms read floor (a pinned
  40 ms latched into a 15.7 s gap) and still only reached 4.35 fps.
* **Fast timebases stalled 2 s.** At 100 us/div the capture is idle ~1 ms after arming;
  a query then made the app hold it ~2 s, and queued re-arms drained as empty blocks.
  Swept: 0 ms 8 stalls/25 s, 10 ms 1, 20 ms 0, so a 20 ms floor after the arm.

Those floors also cost single-channel speed (14–14.5 fps fell to ~13). Direct export
needs none of them.

### Sleep cadence

488 `usleep` sites; 402 take a runtime-computed argument. The ones on the frame path:

```
CDrvScope::run          usleep(10000)  usleep(5000)  usleep(30000)
                        usleep(100000)        <- roll-scan branch only
                        usleep(N * 1000)      <- main loop tail, N computed per iteration
CApiPlotWave::run       usleep(50000)  usleep(2000) x3
CApiPlotWave::doRender  usleep(50000) x2      <- the early-return the tap's quiet-UI takes
```

The call graph behind all of this (11,159 functions, 41,253 edges) resolves **direct `BL`
only**, so virtual dispatch is invisible and every reachability claim is a lower bound.

## What it actually does while streaming

Use `device/cpuwatch.sh <ip>` — one row per sample, everything as percent of **one** core.

Idle (scope on its own UI, no streaming):

```
busy 35-37% of 6 cores    app 1.54-1.57 cores    cpu4 ~100%, entirely task_plot_wave
```

Streaming 1 Mpt through the tap, sustained over a 995-frame run:

```
time       c0   c1   c2   c3   c4   c5   busy   usr   sys    app   hottest thread
06:40:41   90   87   80   85   99   99  89.7%   26%   49%   3.97   Thread-8 91%
06:40:50   90   82   85   85   98   99  89.8%   25%   50%   4.05   Thread-8 92%
06:41:32   92   83   85   82   99   99  90.0%   26%   49%   4.15   Thread-8 91%
```

**It is CPU bound, and the cost is kernel time.** Per-thread over a window inside the
stream: app total **3.71 cores — user 1.17, sys 2.53, i.e. 68% kernel**.

```
  total    user     sys  sys%   thread
  79.2%   12.5%   66.7%    84%  Thread-8   (tap/frida)
  71.7%    2.5%   69.2%    97%  Thread-11  tid=6671
  67.5%   10.0%   57.5%    85%  Thread-8   (tap/frida)
  37.5%    3.3%   34.2%    91%  com.rigol.scope tid=6050
  30.8%   28.3%    2.5%     8%  RenderThread
```

`task_plot_wave` drops **below 1%** while streaming, so the tap's quiet-UI works exactly
as documented. (At idle that same thread holds an A72 at 98.7–100%.)

Sampling `/proc/<tid>/syscall` across all threads during a stream: **`ioctl` appears
nowhere** except idle `binder_thread_read` waits — consistent with two ioctls per frame.
What is visible is **futex contention**: `Thread-11` sits in `futex_wait_queue_me` in 42%
of samples at 97% sys, and many threads are futex-blocked. `gum-js-loop` (Frida's JS
bridge) is idle at ~3%, so the instrumentation's script side is not the cost, though its
native threads are among the hottest.

### Where the kernel time comes from

`simpleperf` **cannot attribute kernel time on this build** — see the traps below; any
per-module number it reports is fiction. What can be measured are the interrupt
counters, which need no symbols. Idle vs streaming, 14 s each (2026-09-09):

```
source                      idle/s  streaming/s     delta/s
arch_timer                   17482        10849       -6632
xhci-hcd:usb3 (the NIC)         35         2799       +2764
GICv3-23 0  (FPGA/DMA)           0         1562       +1562
GICv3-23 1  (FPGA/DMA)           0          906        +906
IPI rescheduling              1609         2162        +553
softirq TASKLET                 24         2704       +2680
softirq NET_RX                   2         1542       +1540
softirq NET_TX                   2         1404       +1402
```

Streaming adds ~**8,400 USB/network events per second** against ~60/s idle. That is the
transport, and it is where the kernel time goes. The acquisition path is separately
visible as the two `GICv3-23` lines — zero at idle, ~2,470/s streaming, roughly 200
interrupts per frame. `arch_timer` *falls* by 6,600/s while streaming because quiet-UI
parks the plot thread and its `usleep` timers stop, which is independent confirmation
that the pause works.

**Consequence for compression:** the dominant CPU cost scales with bytes on the wire, so
compressing the stream attacks the transfer ceiling *and* the CPU load with one change.
It does not need spare CPU to be worth doing — it creates it.

### The log pipeline: 0.62 of a core, now reclaimed

The scope logs hard enough that its own logging was the largest non-app CPU consumer
while streaming. Measured 2026-09-09 over 15 s windows inside a stream:

| | baseline | drains killed | logd stopped |
|---|---|---|---|
| fps | 12.48 | 12.88 | **13.42** |
| machine busy | 88.8% | 87.9% | **81.7%** |
| log pipeline | 0.62 cores | 0.52 cores | **0.00 cores** |

Breakdown at baseline: `logd` 52% of a core, `logcatext` 5%, two `logcat` file drains 4%.
Three processes drain the log to eMMC, all started by `shell/start_rigol_app.sh`:

```
logcatext -n 20 -r 5000 -b main -b system -b radio -b events -b crash -b kernel -f /data/logs/aplog
logcat -b kernel -f /data/logs/tools_log/dmesg_1.txt -r 1024 -n 2
logcat -b all    -f /data/logs/tools_log/logcat_1.txt -r 1024 -n 4
```

**It is the ingestion that costs, not the readers** — killing both file drains recovered
only 0.10 of a core, while stopping `logd` recovered all 0.62. So the fix is `stop logd`,
and it is now one of the tap's temporary changes (`--quiet-logd` in `tap_stream.py`, on by
default from `fft_gui.py`/`run_fft.sh`, disabled with `--tap-keep-logd`), restarted on exit alongside the
redraw. Nothing is logged on the scope while it streams. If the tap is killed
hard, `logd` stays stopped until `adb shell start logd` or a reboot.

Note that `logcatext` is respawned by a supervisor when killed, but the two `logcat`
drains are not — once killed they stay dead until the next boot.

## The transport is a USB2 ceiling

Throughput measures **22.8–26.6 MB/s** at 11.4–13.3 fps, with a per-frame budget (medians,
86-frame run) of `loop 72.7 ms = wait 44.6 + read 58.3 + work 0.9` — so roughly **57 ms
of a ~72–80 ms frame is the 2 MB payload on the wire**, about 35 MB/s.

That is not a TCP tuning problem. The link is:

```
usb3 (root, 480 Mbit) -- 3-1  "USB2.0 Hub" (480)
                          \__ 3-1.4  Realtek 0bda:8153 "USB 10/100/1000 LAN", r8152
usb4 (root, 5000 Mbit) -- (nothing attached)
```

The **RTL8153 is a USB 3.0 gigabit NIC negotiated at USB 2.0**, because it is behind a
USB2 hub on the USB2 root controller. 35 MB/s ≈ 280 Mbit/s is the practical bulk ceiling
for USB2. `eth0` (the scope's own port, `motorcomm.ko` / YT8512) is down — its
`speed=10 duplex=half` is a stale reading on a dead interface, not a real link.

**This is the ceiling, and there is no way around it on this hardware.** The `usb4` root
hub the SoC exposes is not wired to any connector: the instrument physically has only
USB2 sockets and a 100 MbE port, both tested (2026-09-09). So the USB2 adapter at
~35 MB/s is already the fastest transport available — the built-in 100 MbE caps at
12.5 MB/s, which is worse. An earlier draft of this document recommended moving the
adapter to a USB3 port; that was wrong, and no such port exists.

At 2 MB per frame a 35 MB/s ceiling puts a hard bound of ~17.5 fps on 1 Mpt streaming
even if everything else were free, against 11.4–13.3 fps measured on the SCPI-driven
loop (16.2 fps on direct export, 2026-09-13 — close to that bound, and at 10 M or with
two channels the link is plainly the limit). That leaves two levers: **send fewer
bytes** (direct export reads 16-bit words only; the old `toByte` 8-bit route went with
the SCPI loop, so this would now mean a PC-agreed 8- or 12-bit encoding on the scope —
see the packing trial below, which bought nothing at 1 channel), or **overlap transfer
with acquisition** so the frame period tends to `max(wait, read)` rather than their sum.

Open and unmeasured: whether the 57 ms payload is bus-limited or **CPU**-limited. The
scope is 89% busy with 2.5 cores in the kernel while it streams, so 35 MB/s may not be
the bus's limit at all. A raw link throughput test with the app out of the picture would
settle it; note there is no `nc` on the device, so it needs `dd` and something listening,
or one of `/rigol/tools/{tcpsvd,ftpd}`.

## Traps that cost real time

* **`--run-seconds` is not the headless duration.** `fft_gui.py` headless uses
  `args.seconds` (**default 0, treated as 10 s**); `--run-seconds` is the windowed smoke test. So
  `--headless --run-seconds 150` streams for ~7 s and silently ignores the flag. Several
  measurements here were originally taken *outside* the streaming burst because of this,
  and produced the confident and completely wrong conclusion that streaming adds no CPU.
  Use `--headless --seconds 90`.
* **`ps -A` does not exist on this toolbox** — it errors `bad pid '-A'` and, through a
  pipe, produces empty output that reads as "the process is gone". Use plain `ps`.
* **Toolbox `top` normalises CPU% across all six cores**, so a thread pegging one core
  reads ~16%, not 100% — the opposite convention to Linux `top` and to `cpuwatch.sh`.
* **`/proc/<tid>/syscall` reports `running` for any task currently on-CPU**, whether it
  is in userspace or inside a syscall. The histogram is trustworthy for blocked threads
  and under-reports for hot ones; `utime`/`stime` is the authority on the user/kernel split.
* **`bash -n` does not check an embedded `awk` program.** It is inside single quotes, so
  bash never parses it — a deleted brace passes `bash -n` and fails only when run. An
  apostrophe in an awk comment (`window's`) terminates the quoting and breaks the script.
* **`simpleperf` cannot attribute kernel time on this build, and lies quietly about
  it.** `/proc/kallsyms` contains no module symbols at all (`grep -c ' \[xdma\]'` = 0)
  and simpleperf builds no kernel map, so every kernel-text sample (`_text` =
  `0xffffff8008080000`) falls into the nearest-lower module range. `usbtmc_dev` at
  `0xffffff8001260000` is the highest-based module and swallowed 43% of all cycles;
  `xdma` took another 20%. Both numbers are meaningless — nothing was even plugged into
  the USB device port. Setting `kptr_restrict=0` does not fix it. The tells: no
  `[kernel.kallsyms]` DSO in the report at all, and idle and streaming profiles that are
  proportionally identical despite 2.5x different load. Only the *aggregate* kernel share
  is usable, and it agrees with `utime`/`stime`. Use interrupt counters instead.
* **`simpleperf record` here takes a bare command, not `--duration`**:
  `simpleperf record -a -f 500 -o FILE sleep 12`.
* **Frida attach can kill the app.** One injection produced
  `SIGSEGV, code 2 (SEGV_ACCERR) ... #04 /memfd:frida-agent-64.so` in a syscall-heavy
  thread — a hook installed while that thread was executing the target. Versions matched
  (17.17.0 both sides), so it is a race, not skew. The Watchdog restarts the app within
  ~15 s and the tap's temporary changes die with the process, so recovery is automatic.

## Stale numbers elsewhere in this repo

The scope's plot thread was once described here and in `CLAUDE.md` as "~60% of a core";
measured 2026-09-08 it is **98.7–100% of an A72** at idle, and the docs now say so.
Every fps figure in the sections below this one (the log pipeline, 12-bit packing, A53
pinning, the acquisition cycle and the ADC patch) was measured on the **SCPI-driven
loop** that direct export replaced on 2026-09-13. The mechanisms still hold; the numbers
need re-measuring against the export loop before they are relied on.

## Tried and removed: 12-bit packing, and A53 pinning

Both were implemented, measured and then deleted. Recorded here so nobody
spends the time again.

**12-bit packing.** The FPGA scales 12-bit ADC codes into 16-bit words with a
calibrated gain of ~16.2, so representable values sit on a lattice whose steps
are only ever 16 or 17. That makes `v>>4` injective and the low nibble a
function of the bucket, giving a *lossless* 2.00 MB -> 1.50 MB encoding (a
~1.2 kB nibble table plus 12-bit codes; verified exact on live frames). Note a
straight affine inverse does **not** work -- `round((v-vmin)/16.2)`
mis-reconstructs 26-72% of samples by 1-3 counts.

It was removed because it does not buy frame rate: three alternating runs gave
raw 13.36 fps median against packed 12.92, with the wire dropping 26.4 -> 19.4
MB/s and nothing to show for it. The link is not the constraint -- the scope
reads ~1.9 MB per acquisition at ~20 Hz internally while we ship ~15 fps. The
encode also cost 7.88 ms per frame, of which ~4.0 ms is the nibble-table build,
a scatter that A72 NEON cannot vectorise (SVE could; this CPU cannot).

**A53 pinning.** Pinning the tap's own threads to cpu0-3 left the two A72s to
the acquisition path. It made no difference to the mean (15.15 vs 15.11 fps)
but did cut run-to-run spread sharply (stdev 0.65 vs 1.83, worst run 14.48 vs
9.48). Removed with the packing: not enough CPU headroom exists for either to
pay, and a flag that only buys consistency was not worth the surface.

## The acquisition cycle: where the frame time goes

*This breakdown is of the old SCPI-driven loop (2026-09-09). The export loop's own is in
"How the tap reads now" above — at 2 ms/div 1 M: arm 23, ReadNormTrace wait 33, lock
~1, export 5 ms, 62 ms a cycle — and `tap_stream.py --drive-poll-ms --drive-csv` now
records those phases per cycle.*

From the tap's own instrumentation at the time, 761 cycles, medians. Note `arm_ms` as
logged then was `t2 - t0` and *included* the busy wait, so the `set_state` call alone
is `arm - wait_busy - busy`, computed per cycle:

```
set_state(SINGLE)     31.6 ms   arming
wait for busy          0.02 ms
busy (the capture)    21.0 ms   <- 1 Mpt @ 50 MSa/s = 20 ms of real signal
trigger + drain       17.4 ms   SCPI round trip to make the app emit
------------------------------------
sum                   70.0 ms   (measured period 76.0 -> 13.2 cycles/s)
```

**72% of the cycle is not signal capture.** Hooking `usleep` and histogramming by
return address, three sleeps fire exactly once per acquisition:

```
20000us  CCalibration_ADC::DrvCalibration_SetAdcStary   movz at +0x3417fc
10000us  CDrvScope::ReadNormTrace                       movz at +0x30ef60
10000us  CDrvScope::run  (usleep(n * 1000), multiplier) movz at +0x2e945c
 1000us  CScpiParserWorker::addRemoteEvent              was cut 20->1 by the tap (SCPI
                                                         loop only; export sends no SCPI)
```

~40 ms of the 76 ms cycle is the app asleep on hardcoded timers.

### What is worth changing: only the ADC one, and only halfway

Each constant is a plain `mov w<rd>, #imm` a couple of instructions before the `bl`,
so it can be rewritten in place — restored on exit, and refused unless the instruction
really is a movz/movk with the expected immediate (`patchSleepConsts` in `mho_tap.js`;
`tap_stream.py --sleep-const OFF:EXPECT_US:NEW_US`). `fft_gui.py` applies the ADC site
as `ADC_SETTLE_SPEC` (`0x3417fc:20000:10000`), undone with `--tap-keep-adc-sleep`; its
default is **off**. It was switched off while suspected of stalls that turned out to be
the SCPI loop's races, and the table below was measured on that loop, so re-measure it
against direct export before turning it back on.

| | fps | |
|---|---|---|
| baseline | 13.19 / 12.53 | |
| ADC 20 → 10 ms | **14.97** (15.18 in a repeat) | clean, **+16%** |
| ADC 20 → 5 ms | 7.11 | **1 repeat — stale record** |
| ADC 20 → 2 ms | 8.60 | clean but far worse |
| ADC 10 ms + ReadNormTrace 10 → 2 ms | 13.61 | worse than ADC alone |
| ... + run multiplier 1000 → 200 | 14.17 | still worse than ADC alone |

So 20 ms was conservative and half of it is free, but the settling requirement is real
and sits between 5 and 10 ms — below that the scope starts handing back stale records,
which the receiver catches by CRC. **The other two sleeps should be left alone**: they
are yields, and shortening them costs throughput on an already CPU-saturated box.

### Two traps this cost us

**Patch, do not hook.** An `Interceptor` on `usleep` puts a trampoline on ~65,000 calls
a second to catch the 13 that matter. Measured with a deliberately bogus offset that
never matched: **13.28 → 11.39 fps, -1.89**. That is larger than most of the effects
being measured, and it silently inverted the first sweep's conclusions. A `movz`
immediate rewrite costs nothing at runtime and is far safer than rewriting the
`bl usleep` itself (which killed the app when previously tried): same instruction, same
register, only the constant changes.

**The firmware image is not the running build.** The app runs from
`/data/app/com.rigol.scope-2/base.apk` (`libscope-auklet.so`, 12,453,760 bytes), *not*
the `/rigol/app/Sparrow.apk` copy in the eMMC image (12,362,968 bytes). Addresses and
symbol names differ between them, which is why static analysis against the image kept
pointing at instructions that were not there — `RunScope`'s textbook `usleep(20000)`
never fires; the real one is in an ADC calibration routine. **Pull the APK from
`/data/app` before doing any address-level work.**

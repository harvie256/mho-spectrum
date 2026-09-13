'use strict';
/*
 * mho_tap.js -- inject libmhotap.so and feed it the raw record.
 *
 * CApiWave::toWord/toByte(uchar *src, int n) receive a pointer to the record
 * before any SCPI framing -- the whole record up to 1 Mpt, and consecutive
 * 1 Mpt chunks above that, which libmhotap reassembles (start() passes the
 * record size for exactly this).  We hand that pointer to the native tap
 * (which copies it and ships it from its own thread) and then zero the point
 * count, so the app's marshalling and SCPI framing do no work at all and the
 * reply is an empty block.
 *
 * Everything expensive lives in libmhotap.so.  The only per-frame work here is
 * one NativeFunction call -- the same shape as the speed patch's per-read
 * malloc/memcpy calls, which are reliable.  Doing the socket work in JS instead
 * aborts the app (see docs/STREAMING.md).
 *
 * Do NOT load this together with patch/mho_speed_patch.js: both hook toWord,
 * the patch zeroes x2 first, and the tap would then see npoints == 0.  The
 * patch is also pointless here -- with marshalling skipped there is no per-byte
 * loop left to fix.
 */

var LIB = 'libscope-auklet.so';
var mod = Process.getModuleByName(LIB);

/* libc, for substituting the app's result buffer (see the hook below) */
function G(n) { return Module.getGlobalExportByName(n); }
var _malloc = new NativeFunction(G('malloc'), 'pointer', ['ulong']);

/* RByteArray is a std::vector<char>: +0 __begin_, +8 __end_, +16 __end_cap_.
 * A function returning one gets the destination in x8 (AArch64 sret). */
var STUB_BYTES = 16;

var tap = null;
var fn = {};

function resolve(m, name) {
    /* A freshly dlopened module does not always answer getExportByName here,
     * so fall back to the process-wide lookup and finally to a scan. */
    try { var a = m.getExportByName(name); if (a && !a.isNull()) return a; } catch (e) {}
    try { var g = Module.getGlobalExportByName(name); if (g && !g.isNull()) return g; } catch (e) {}
    var found = null;
    m.enumerateExports().forEach(function (e) { if (e.name === name) found = e.address; });
    if (found) return found;
    var names = m.enumerateExports().map(function (e) { return e.name; });
    throw new Error('missing export ' + name + '; module has [' +
                    names.slice(0, 20).join(', ') + ']');
}

function loadTap(soPath) {
    var m = null;
    /* dlopen is idempotent: if a previous run already mapped it, reuse that.
     * Match on the basename we were actually given, not a fixed one -- the
     * host names the file after its contents, so a rebuilt library arrives
     * under a new name and must not be confused with the old one still
     * mapped in this process. */
    var soName = soPath.split('/').pop();
    try { m = Process.getModuleByName(soName); } catch (e) {}
    if (m === null) m = Module.load(soPath);

    fn.init  = new NativeFunction(resolve(m, 'mhotap_init'),
                                  'int', ['pointer', 'int', 'long', 'double', 'int']);
    fn.frame = new NativeFunction(resolve(m, 'mhotap_frame'),
                                  'int', ['pointer', 'long']);
    fn.stats = new NativeFunction(resolve(m, 'mhotap_stats'),
                                  'void', ['pointer']);
    fn.close = new NativeFunction(resolve(m, 'mhotap_close'),
                                  'void', []);
    fn.yscale = new NativeFunction(resolve(m, 'mhotap_set_yscale'),
                                  'void', ['double', 'double', 'double']);
    fn.rate  = new NativeFunction(resolve(m, 'mhotap_set_rate'),
                                  'void', ['double']);
    fn.record = new NativeFunction(resolve(m, 'mhotap_set_record'),
                                   'void', ['long']);
    fn.driveStart = new NativeFunction(resolve(m, 'mhotap_drive_start'),
                                       'int', ['pointer', 'pointer', 'int', 'int']);
    fn.driveStop  = new NativeFunction(resolve(m, 'mhotap_drive_stop'),
                                       'void', []);
    fn.driveStats = new NativeFunction(resolve(m, 'mhotap_drive_stats'),
                                       'void', ['pointer']);
    fn.driveSettle = new NativeFunction(resolve(m, 'mhotap_drive_settle'),
                                        'void', ['int']);
    tap = m;
    return m.base.toString();
}

var statsBuf = null;
var driveBuf = null;
var plotSelf = null;                 /* CApiPlotWave*, captured from doRender */
var plotListener = null;
var plotFns = null;
var scpiListener = null;             /* usleep hook, while suppressing */
var patched_sites = [];              /* movz immediates we rewrote */
var scpiSkipped = 0;
var scpiUs = 1000;                   /* what the 20 ms sleep becomes */
var hooked = false;
var enabled = false;
var seen = 0;

/* k = bytes per sample for each converter */
var TARGETS = {
    '_ZN8CApiWave6toWordEPhi': 2,
    '_ZN8CApiWave6toByteEPhi': 1
};

function installHooks() {
    if (hooked) return 0;
    var n = 0;
    Object.keys(TARGETS).forEach(function (sym) {
        var k = TARGETS[sym];
        var addr;
        try { addr = mod.getExportByName(sym); }
        catch (e) { return; }
        Interceptor.attach(addr, {
            onEnter: function (args) {
                this.tapped = false;
                if (!enabled) return;
                var npts = args[2].toInt32();
                if (npts <= 0) return;
                seen++;
                fn.frame(args[1], npts * k);
                /* Skip the app's own conversion -- we already have the record.
                 * Zeroing the count makes the stock per-byte loop do nothing. */
                this.ret = this.context.x8;
                this.context.x2 = ptr(0);
                this.tapped = true;
            },
            onLeave: function () {
                if (!this.tapped) return;
                /* An empty vector leaves __begin_ NULL, and a downstream
                 * ArbBin(int, void*) asserts "NULL != p" and aborts the app.
                 * Hand back a small real allocation instead: the reply is then
                 * a tiny valid block and none of the 2 MB marshalling happens.
                 * libc++ on bionic allocates with malloc, so the vector's own
                 * destructor frees this correctly. */
                var buf = _malloc(STUB_BYTES);
                if (buf.isNull()) return;
                this.ret.writePointer(buf);
                this.ret.add(8).writePointer(buf.add(STUB_BYTES));
                this.ret.add(16).writePointer(buf.add(STUB_BYTES));
            }
        });
        n++;
    });
    hooked = true;
    return n;
}

rpc.exports = {
    load: function (soPath) { return loadTap(soPath); },
    start: function (host, port, maxBytes, srate, bps, recordBytes) {
        var rc = fn.init(Memory.allocUtf8String(host), port, maxBytes, srate, bps);
        if (rc !== 0) return { ok: false, rc: rc };
        /* Before the hooks go live, so the first chunk is already assembled. */
        if (recordBytes) fn.record(recordBytes);
        var n = installHooks();
        enabled = true;
        return { ok: true, hooks: n };
    },
    setRate: function (srate) { fn.rate(srate); return true; },
    /* volts = (code - yref) * yinc + yorig.  Without it the receiver has no
     * way to label the axis in anything but dBFS. */
    setYScale: function (yinc, yorig, yref) {
        fn.yscale(yinc, yorig, yref); return true;
    },
    /* Leave the hooks installed but inert -- detaching them is riskier than
     * letting them fall through. */
    pause: function () { enabled = false; return true; },
    resume: function () { enabled = true; return true; },
    stats: function () {
        if (!statsBuf) statsBuf = Memory.alloc(10 * 8);
        fn.stats(statsBuf);
        var o = [];
        for (var i = 0; i < 10; i++) o.push(statsBuf.add(i * 8).readDouble());
        return { frames_in: o[0], frames_sent: o[1], dropped: o[2],
                 bytes: o[3], elapsed: o[4], fps: o[5], mbps: o[6],
                 send_errors: o[7], chunks_in: o[8], partial: o[9],
                 hook_calls: seen };
    },
    /* Run the whole frame loop on the scope: arm, trigger over loopback,
     * drain.  Nothing crosses the RPC boundary per frame. */
    driveStart: function (armMode, dwellUs) {
        /* Hand over the acquisition entry points: the app loads
         * libscope-auklet.so from inside the APK, so the library cannot
         * dlopen it by soname.
         *
         * Poll with DrvAcquire_GetRunStatus even though it is a *destructive*
         * read -- it calls DevSystemSCU_clrStatus twice, and clears the
         * hardware status latch as a side effect.  That looks like something
         * worth avoiding, and DrvAcquire_GerRunStatusWithoutClear is the
         * identical function minus those two calls, but swapping it in is
         * measurably worse: the busy status then stays asserted for ~280 ms
         * instead of ~22 ms (it is waiting for someone else to clear it), and
         * the demo drops from 10.1 fps with 1 stall to 4.9 fps with 97.
         *
         * So the clear is not incidental, it is the acknowledge: reading is
         * how this status is advanced.  Do not "fix" this. */
        return fn.driveStart(mod.getExportByName('_Z19DrvAcquire_SetStatej'),
                             mod.getExportByName('_Z23DrvAcquire_GetRunStatusRj'),
                             armMode | 0, dwellUs | 0);
    },
    /* --- the scope's own waveform redraw -------------------------------
     * CApiPlotWave::doRender() opens with `if (!getEnalbe()) { usleep(50000);
     * ...; return; }`, so clearing that flag makes the plot thread skip the
     * whole render path and idle at 20 Hz.  Worth ~2 fps to the tap on a box
     * whose big cores are saturated, and the scope's own display is not much
     * use while the spectrum is on the PC anyway.
     *
     * setEnable is a member function, so the instance has to come from
     * somewhere: hook doRender (it runs continuously), take `this` from the
     * first call, then drop the hook -- a per-frame hook is not worth keeping
     * once the flag is set. */
    plotHook: function () {
        if (plotListener || plotSelf) return true;
        plotListener = Interceptor.attach(
            mod.getExportByName('_ZN12CApiPlotWave8doRenderEv'), {
                onEnter: function (args) {
                    if (plotSelf === null) plotSelf = args[0];
                }
            });
        return true;
    },
    plotReady: function () {
        return plotSelf === null ? null : plotSelf.toString();
    },
    plotSet: function (on) {
        if (plotSelf === null) return null;
        if (plotListener) { plotListener.detach(); plotListener = null; }
        if (!plotFns) {
            plotFns = {
                set: new NativeFunction(
                    mod.getExportByName('_ZN12CApiPlotWave9setEnableEb'),
                    'void', ['pointer', 'bool']),
                get: new NativeFunction(
                    mod.getExportByName('_ZN12CApiPlotWave9getEnalbeEv'),
                    'bool', ['pointer'])
            };
        }
        plotFns.set(plotSelf, on ? 1 : 0);
        return plotFns.get(plotSelf) ? 1 : 0;
    },
    /* --- the SCPI worker's 20 ms sleep ---------------------------------
     * CScpiBackWorker sleeps a hardcoded 20 ms in the path that answers a
     * command, so every SCPI query costs a flat ~24 ms (measured: 400 queries
     * at randomised phase, p10 23.7 / p90 25.2 ms, and exactly one
     * usleep(20000) per query when usleep is counted).  The tap pays it once
     * per frame on the :WAVeform:DATA? that triggers record production --
     * about 23% of an 88 ms cycle spent asleep.
     *
     * Hooked, not patched.  Rewriting the two `bl usleep` instructions is the
     * surgical fix and costs nothing at runtime, but it killed the app: any
     * throw on gum-js-loop takes the process down with it, so the safe shape
     * here is the one already proven in this app -- an Interceptor on usleep
     * -- with the body wrapped so an error is returned rather than raised.
     *
     * usleep is called ~65k times a second here, almost all of it usleep(0)
     * from one spinning thread, so the filter checks the duration first (an
     * integer compare) and only then the return address.  If the offsets ever
     * stop matching, nothing is skipped and the tap simply runs as before. */
    /* --- patch hardcoded usleep constants -------------------------------
     * Three sleeps fire exactly once per acquisition and account for ~40 ms of
     * a 76 ms cycle against 21 ms of real capture (measured 2026-09-09 by
     * histogramming usleep return addresses while streaming).  Each one takes
     * its argument from a plain `mov w<rd>, #imm` a couple of instructions
     * earlier, so the constant can be rewritten in place:
     *
     *   +0x3417fc  mov w8,#0x4e20 (20000)  CCalibration_ADC::DrvCalibration_SetAdcStary
     *   +0x30ef60  mov w0,#0x2710 (10000)  CDrvScope::ReadNormTrace
     *   +0x2e945c  mov w9,#0x3e8  (1000)   CDrvScope::run -- a MULTIPLIER,
     *                                      usleep(n * w9), so scale it instead
     *   +0x68506c  mov w0,#0x4e20 (20000)  CScpiParserWorker::addRemoteEvent
     *                                      (already handled by scpiSleep)
     *
     * Patched, not hooked.  An Interceptor on usleep costs a trampoline on
     * ~65,000 calls a second to catch the 13 that matter: measured -1.89 fps
     * (13.28 -> 11.39) with a deliberately bogus offset that never matched,
     * more than the sleeps were worth.  A movz rewrite costs nothing at
     * runtime.  It is also much safer than rewriting the `bl usleep` itself
     * (which killed the app when tried): same instruction, same register,
     * only the immediate changes, and Memory.patchCode handles the I-cache.
     *
     * Offsets are for the build in /data/app/com.rigol.scope-2/base.apk --
     * NOT the copy in the firmware image, which is a different build with
     * different addresses.  Every patch verifies the instruction really is a
     * movz with the expected immediate before touching it, so a stale offset
     * is refused rather than corrupting code.
     */
    patchSleepConsts: function (specs) {
        var done = [];
        try {
            specs.forEach(function (sp) {
                var off = sp[0], want = sp[1], to = sp[2];
                var addr = mod.base.add(off);
                var orig = addr.readU32();
                /* movz and movk share a layout: sf/opc, hw at 21-22, imm16
                 * at 5-20, Rd at 0-4.  Accept both, because a constant over
                 * 16 bits is built as movz+movk and the interesting half may
                 * be in either (CCalibration's 100000 is movz #0x86a0 then
                 * movk #1,lsl#16).  Preserve everything but the immediate. */
                var opc = orig & 0x7F800000;
                var isMovz = opc === 0x52800000;
                var isMovk = opc === 0x72800000;
                var imm = (orig >>> 5) & 0xFFFF;
                var rd = orig & 0x1F;
                if ((!isMovz && !isMovk) || imm !== want) {
                    done.push({ off: off, ok: false,
                                why: 'expected movz/movk #' + want +
                                     ', found 0x' + (orig >>> 0).toString(16) });
                    return;
                }
                var keep = orig & ~(0xFFFF << 5);       /* opcode, hw, Rd */
                var patched = ((keep | ((to & 0xFFFF) << 5)) >>> 0);
                Memory.patchCode(addr, 4, function (pw) { pw.writeU32(patched); });
                patched_sites.push({ addr: addr, orig: orig, off: off });
                done.push({ off: off, ok: true, from: imm, to: to, rd: rd });
            });
            return { ok: true, sites: done };
        } catch (e) { return { ok: false, error: String(e), sites: done }; }
    },
    restoreSleepConsts: function () {
        var n = 0;
        patched_sites.forEach(function (p) {
            try {
                Memory.patchCode(p.addr, 4, function (pw) { pw.writeU32(p.orig); });
                n++;
            } catch (e) {}
        });
        patched_sites = [];
        return { ok: true, restored: n };
    },

    scpiSleep: function (on, us) {
        try {
            if (on) {
                if (scpiListener) { scpiListener.detach(); scpiListener = null; }
                return { ok: true, sleeping: true, skipped: scpiSkipped };
            }
            scpiUs = (us === undefined || us === null) ? 1000 : (us | 0);
            if (scpiListener) return { ok: true, sleeping: false, already: true };
            /* CScpiParserWorker::addRemoteEvent+0x88: queue the command,
             * usleep(20000), then notify CScpiExecuteWorker.  Straight-line
             * code -- no loop, no retry -- so this is a fixed pause, not a
             * poll, which is why the reply latency is flat rather than spread.
             *
             * Found by symbolising the return address of every usleep(20000)
             * while the tap ran, not by reading the disassembly: the static
             * survey pointed at CScpiBackWorker, which never fires on this
             * path, and a filter aimed there skipped exactly 0 sleeps.
             *
             * Shortened rather than removed.  Something downstream may be
             * relying on the delay, and 1 ms still recovers 19 of the 20. */
            var ret = [mod.base.add(0x685070 + 4)];
            scpiListener = Interceptor.attach(
                Module.getGlobalExportByName('usleep'), {
                    onEnter: function (args) {
                        if (args[0].toInt32() !== 20000) return;
                        var r = this.returnAddress;
                        for (var i = 0; i < ret.length; i++) {
                            if (r.equals(ret[i])) {
                                args[0] = ptr(scpiUs);
                                scpiSkipped++;
                                break;
                            }
                        }
                    }
                });
            return { ok: true, sleeping: false, us: scpiUs,
                     sites: ret.map(function (p) { return p.toString(); }) };
        } catch (e) {
            return { ok: false, error: String(e) };
        }
    },
    scpiSkipped: function () { return scpiSkipped; },
    driveStop:  function () { fn.driveStop(); return true; },
    driveSettle: function (us) { fn.driveSettle(us | 0); return true; },
    driveStats: function () {
        if (!driveBuf) driveBuf = Memory.alloc(13 * 8);
        fn.driveStats(driveBuf);
        return { cycles: driveBuf.readDouble(),
                 arm_timeouts: driveBuf.add(8).readDouble(),
                 trigger_errors: driveBuf.add(16).readDouble(),
                 arm_ms: driveBuf.add(24).readDouble(),
                 trig_ms: driveBuf.add(32).readDouble(),
                 empty: driveBuf.add(40).readDouble(),
                 declared: driveBuf.add(48).readDouble(),
                 wait_busy_ms: driveBuf.add(56).readDouble(),
                 busy_ms: driveBuf.add(64).readDouble(),
                 missed_busy: driveBuf.add(72).readDouble(),
                 incomplete: driveBuf.add(80).readDouble(),
                 settle_ms: driveBuf.add(88).readDouble(),
                 recoveries: driveBuf.add(96).readDouble() };
    },
    stop: function () { enabled = false; fn.driveStop(); fn.close(); return true; }
};

console.log('mho_tap loaded (idle until start())');

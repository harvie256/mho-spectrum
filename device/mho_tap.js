'use strict';
/*
 * mho_tap.js -- inject libmhotap.so and hand it the app's acquisition entry
 * points.
 *
 * The capture loop lives in libmhotap.so and calls the app's own
 * DrvWaveform_Export* functions from its own thread (see the header there).
 * This script only resolves addresses, binds the library's functions, and adds
 * one hook: the return of CDrvScope::ReadNormTrace, which tells the loop a
 * capture has been read in.  Nothing bulk happens in the Frida JS runtime --
 * doing socket work here aborted the app in an earlier attempt.
 *
 * Everything else below is optional and restored on exit by tap_stream.py: the
 * scope's own waveform redraw (plotSet) and the hardcoded usleep constants on
 * the arm/readout path (patchSleepConsts).
 */

var LIB = 'libscope-auklet.so';
var mod = Process.getModuleByName(LIB);

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

    fn.init = new NativeFunction(resolve(m, 'mhotap_init'),
                                 'int', ['pointer', 'int', 'long', 'double', 'int']);
    fn.stats = new NativeFunction(resolve(m, 'mhotap_stats'), 'void', ['pointer']);
    fn.close = new NativeFunction(resolve(m, 'mhotap_close'), 'void', []);
    fn.yscale = new NativeFunction(resolve(m, 'mhotap_set_yscale'),
                                   'void', ['double', 'double', 'double']);
    fn.yscaleCh = new NativeFunction(resolve(m, 'mhotap_set_yscale_ch'),
                                     'void', ['int', 'double', 'double', 'double']);
    fn.rate = new NativeFunction(resolve(m, 'mhotap_set_rate'), 'void', ['double']);
    fn.record = new NativeFunction(resolve(m, 'mhotap_set_record'), 'void', ['long']);
    fn.channels = new NativeFunction(resolve(m, 'mhotap_set_channels'),
                                     'void', ['int', 'uint']);
    fn.exportSetup = new NativeFunction(resolve(m, 'mhotap_export_setup'),
                                        'void', ['pointer', 'pointer', 'pointer',
                                                 'pointer', 'pointer', 'pointer',
                                                 'pointer', 'pointer', 'pointer']);
    fn.rntDone = new NativeFunction(resolve(m, 'mhotap_rnt_done'), 'void', ['int']);
    fn.driveStart = new NativeFunction(resolve(m, 'mhotap_drive_start'),
                                       'int', ['pointer']);
    fn.driveStop = new NativeFunction(resolve(m, 'mhotap_drive_stop'), 'void', []);
    fn.driveStats = new NativeFunction(resolve(m, 'mhotap_drive_stats'),
                                       'void', ['pointer']);
    tap = m;
    return m.base.toString();
}

var statsBuf = null;
var driveBuf = null;
var rntListener = null;              /* ReadNormTrace return */
var plotSelf = null;                 /* CApiPlotWave*, captured from doRender */
var plotListener = null;
var plotFns = null;
var patched_sites = [];              /* movz/movk immediates we rewrote */

var pfns = null;
function paramFns() {
    if (!pfns) {
        pfns = {
            scope: new NativeFunction(mod.getExportByName('_Z12Drv_GetScopev'),
                                      'pointer', []),
            get: new NativeFunction(mod.getExportByName('_ZN9CDrvScope11GetDrvParamEj'),
                                    'pointer', ['pointer', 'uint']),
            count: new NativeFunction(mod.getExportByName('_ZN9CDrvParam12GetChanCountEv'),
                                      'uint', ['pointer']),
            mask: new NativeFunction(mod.getExportByName('_ZN9CDrvParam11GetChanMaskEv'),
                                     'uint', ['pointer'])
        };
    }
    return pfns;
}

function readDoubles(buf, n) {
    var o = [];
    for (var i = 0; i < n; i++) o.push(buf.add(i * 8).readDouble());
    return o;
}

rpc.exports = {
    load: function (soPath) { return loadTap(soPath); },
    /* Open the link to the PC and size the ring.  recordBytes is one frame:
     * points x 2 x channels; mask says which channels (bit 0 = CH1). */
    start: function (host, port, maxBytes, srate, recordBytes, nch, mask) {
        var rc = fn.init(Memory.allocUtf8String(host), port, maxBytes, srate, 2);
        if (rc !== 0) return { ok: false, rc: rc };
        fn.record(recordBytes);
        fn.channels(nch | 0, mask >>> 0);
        return { ok: true };
    },
    setRate: function (srate) { fn.rate(srate); return true; },
    /* What ExportData would interleave right now: CDrvParam's slot count
     * (1, 2 or 4) and sampled-channel mask on Drv_GetScope()->GetDrvParam(0).
     * Read here only to size buffers at startup; the capture loop reads the
     * same getters under LockConfig every cycle. */
    layout: function () {
        var p = paramFns().get(paramFns().scope(), 0);
        return { count: paramFns().count(p), mask: paramFns().mask(p) };
    },
    /* volts = (code - yref) * yinc + yorig.  Without it the receiver has no
     * way to label the axis in anything but dBFS. */
    setYScale: function (yinc, yorig, yref) {
        fn.yscale(yinc, yorig, yref); return true;
    },
    /* The same for one channel, i = its place in the interleave.  With several
     * channels these go out as a table after the header. */
    setYScaleCh: function (i, yinc, yorig, yref) {
        fn.yscaleCh(i | 0, yinc, yorig, yref); return true;
    },
    stats: function () {
        if (!statsBuf) statsBuf = Memory.alloc(8 * 8);
        fn.stats(statsBuf);
        var o = readDoubles(statsBuf, 8);
        return { frames_in: o[0], frames_sent: o[1], dropped: o[2],
                 bytes: o[3], elapsed: o[4], fps: o[5], mbps: o[6],
                 send_errors: o[7] };
    },
    /* Start the on-scope capture loop.  Hands over the export entry points
     * CApiWave::getMemoryData uses, the LockConfig pair on Drv_GetScope(), and
     * DrvAcquire_SetState for arming; then hooks ReadNormTrace's return so the
     * loop exports only once the app has read the capture in.  The app loads
     * libscope-auklet.so from inside the APK, so the library cannot dlopen it
     * by soname -- the addresses have to come from here. */
    driveStart: function () {
        fn.exportSetup(mod.getExportByName('_Z22DrvWaveform_ExportInitj'),
                       mod.getExportByName('_Z22DrvWaveform_ExportDatajjPtb'),
                       mod.getExportByName('_Z22DrvWaveform_ExportBackv'),
                       mod.getExportByName('_Z12Drv_GetScopev'),
                       mod.getExportByName('_ZN9CDrvScope10LockConfigEv'),
                       mod.getExportByName('_ZN9CDrvScope12UnlockConfigEv'),
                       mod.getExportByName('_ZN9CDrvScope11GetDrvParamEj'),
                       mod.getExportByName('_ZN9CDrvParam12GetChanCountEv'),
                       mod.getExportByName('_ZN9CDrvParam11GetChanMaskEv'));
        if (!rntListener) {
            rntListener = Interceptor.attach(
                mod.getExportByName('_ZN9CDrvScope13ReadNormTraceEi'), {
                    onLeave: function (r) { if (r.toInt32() === 0) fn.rntDone(0); }
                });
        }
        return fn.driveStart(mod.getExportByName('_Z19DrvAcquire_SetStatej'));
    },
    driveStop: function () { fn.driveStop(); return true; },
    driveStats: function () {
        if (!driveBuf) driveBuf = Memory.alloc(12 * 8);
        fn.driveStats(driveBuf);
        var o = readDoubles(driveBuf, 12);
        return { cycles: o[0], arm_timeouts: o[1], export_errors: o[2],
                 arm_ms: o[3], rnt_wait_ms: o[4], lock_wait_ms: o[5],
                 export_ms: o[6], cycle_ms: o[7], compact_ms: o[8],
                 layout_skips: o[9], slots: o[10], slot_mask: o[11] };
    },
    /* --- the scope's own waveform redraw -------------------------------
     * CApiPlotWave::doRender() opens with `if (!getEnalbe()) { usleep(50000);
     * ...; return; }`, so clearing that flag makes the plot thread skip the
     * whole render path and idle at 20 Hz.  That thread holds an A72 at ~99%
     * otherwise, on a box whose big cores are saturated, and the scope's own
     * display is not much use while the spectrum is on the PC anyway.
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
    /* --- patch hardcoded usleep constants -------------------------------
     * Sleeps that fire once per acquisition, each taking its argument from a
     * plain `mov w<rd>, #imm` a couple of instructions earlier, so the
     * constant can be rewritten in place:
     *
     *   +0x3417fc  mov w8,#0x4e20 (20000)  CCalibration_ADC::DrvCalibration_SetAdcStary
     *   +0x30ef60  mov w0,#0x2710 (10000)  CDrvScope::ReadNormTrace
     *   +0x2e945c  mov w9,#0x3e8  (1000)   CDrvScope::run -- a MULTIPLIER,
     *                                      usleep(n * w9), so scale it instead
     *
     * All three sit on the arm -> ReadNormTrace path the capture loop waits
     * on.  The cost figures (~40 ms of a 76 ms cycle; the ADC wait 20 -> 10 ms
     * worth +16%) were measured 2026-09-09 on the old SCPI-driven loop and
     * need re-measuring against this one.
     *
     * Patched, not hooked.  An Interceptor on usleep costs a trampoline on
     * ~65,000 calls a second to catch the few that matter: measured -1.89 fps
     * (13.28 -> 11.39) with a deliberately bogus offset that never matched.  A
     * movz rewrite costs nothing at runtime, and is much safer than rewriting
     * the `bl usleep` itself (which killed the app when tried): same
     * instruction, same register, only the immediate changes, and
     * Memory.patchCode handles the I-cache.
     *
     * Offsets are for the build in /data/app/com.rigol.scope-2/base.apk --
     * NOT the copy in the firmware image, which is a different build with
     * different addresses.  Every patch verifies the instruction really is a
     * movz/movk with the expected immediate before touching it, so a stale
     * offset is refused rather than corrupting code.
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
    /* Loop first, then the link: the loop writes into the ring. */
    stop: function () { fn.driveStop(); fn.close(); return true; }
};

console.log('mho_tap loaded (idle until start())');

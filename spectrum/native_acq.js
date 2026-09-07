'use strict';
/*
 * native_acq.js -- drive the scope's acquisition directly, bypassing SCPI.
 *
 * WHY: arming over SCPI is dominated by command latency, not by the hardware.
 * The parser services commands on a ~25 ms tick, so `:RUN` ... `:STOP` costs
 * ~50 ms of pure latency on top of the ~33 ms the scope actually needs, and a
 * client that does not wait long enough silently re-reads the previous record.
 * Measured natively: SetState(3) returns in ~24 ms and the capture completes
 * ~20 ms later, so one guaranteed-fresh acquisition costs ~43 ms.
 *
 * DELIBERATELY MINIMAL: two NativeFunctions, no sockets, no Interceptor, no
 * per-frame work in the JS runtime.  Doing bulk I/O from a Frida JS hook aborts
 * this app (see docs/STREAMING.md); this script only calls two small getters
 * and a setter, on demand, from RPC.
 *
 *   DrvAcquire_SetState(uint)       1 = STOP, 2 = RUN, 3 = SINGLE
 *   DrvAcquire_GetRunStatus(uint&)  4 = armed, 3 = triggered, 0 = complete
 */
var m = Process.getModuleByName('libscope-auklet.so');

var tsbuf = Memory.alloc(16);
var cgt = new NativeFunction(Module.getGlobalExportByName('clock_gettime'),
                             'int', ['int', 'pointer']);
function now() {
    cgt(1, tsbuf);
    return tsbuf.readU64().valueOf() + tsbuf.add(8).readU64().valueOf() / 1e9;
}

var out = Memory.alloc(8);
var setState = new NativeFunction(
    m.getExportByName('_Z19DrvAcquire_SetStatej'), 'int', ['uint']);
var getStatus = new NativeFunction(
    m.getExportByName('_Z23DrvAcquire_GetRunStatusRj'), 'int', ['pointer']);

var STOP = 1, RUN = 2, SINGLE = 3;

function status() {
    out.writeU32(0xffffffff);
    getStatus(out);
    return out.readU32();
}

rpc.exports = {
    status: status,
    stop:  function () { setState(STOP);   return true; },
    run:   function () { setState(RUN);    return true; },
    /* Arm one acquisition and return immediately, so the caller can overlap the
     * capture with the previous frame's transfer. */
    arm:   function () { var t = now(); setState(SINGLE);
                         return (now() - t) * 1e3; },
    /* Block until the armed acquisition reports complete. */
    wait:  function (timeout_ms) {
        var t0 = now();
        var sawBusy = false;
        while ((now() - t0) * 1e3 < timeout_ms) {
            var v = status();
            if (v !== 0) sawBusy = true;
            else if (sawBusy) return { ok: true, ms: (now() - t0) * 1e3 };
        }
        return { ok: false, ms: (now() - t0) * 1e3 };
    },
    /* Arm and wait in one round trip -- one RPC instead of two. */
    single: function (timeout_ms) {
        var t0 = now();
        setState(SINGLE);
        var sawBusy = false;
        while ((now() - t0) * 1e3 < timeout_ms) {
            var v = status();
            if (v !== 0) sawBusy = true;
            else if (sawBusy) return { ok: true, ms: (now() - t0) * 1e3 };
        }
        return { ok: false, ms: (now() - t0) * 1e3 };
    }
};
console.log('native_acq ready');

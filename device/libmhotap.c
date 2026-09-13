/*
 * libmhotap.so -- in-app waveform tap for the Rigol MHO934.
 *
 * WHY: the scope's own SCPI reply path costs ~110 ms per 1 Mpt record -- the app
 * marshals it into an RByteArray one byte at a time and frames it as a SCPI
 * block -- and the record does not need any of it.  :WAV:DATA? ends up in
 * CApiWave::getMemoryData, which reads acquisition memory with three plain
 * functions: DrvWaveform_ExportInit, DrvWaveform_ExportData and
 * DrvWaveform_ExportBack.  ExportData fills a uint16 buffer with *every enabled
 * channel interleaved* ([a, b, a, b, ...]; getMemoryData then picks one out with
 * a byte loop).  This library calls the same three from its own thread,
 * straight into the buffer it sends: no SCPI in the frame loop, no marshalling,
 * no copy, and every channel for the cost of one read.
 *
 * It owns the capture loop (export_main), the ring and the sender thread.  The
 * injector (mho_tap.js) hands over the app's entry points and reports each
 * successful CDrvScope::ReadNormTrace -- the only per-frame crossing into
 * Frida's JS runtime.
 *
 * Build: device/build_tap.sh (NDK clang, aarch64, -O2 -shared -fPIC -pthread).
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define HDR_BYTES 64
#define MAGIC     "MHOFRAME"
#define NSLOTS    3

typedef struct {
    unsigned char *buf;
    long           len;
    uint32_t       seq;
} slot_t;

static struct {
    int              fd;
    int              running;
    int              stop;
    pthread_t        th;
    pthread_mutex_t  m;
    pthread_cond_t   can_send;
    slot_t           slots[NSLOTS];
    long             slot_cap;
    int              head, tail, count;

    uint32_t         seq;
    double           srate;
    /* Vertical scale, so the PC can show absolute units instead of dBFS.
     * volts = (code - yref) * yinc + yorig, the SCPI convention.  Zero means
     * "unknown" and the receiver stays in dBFS.  One scale for the frame: with
     * several channels it is only right while they share a V/div. */
    double           yinc, yorig, yref;
    int              bps;
    long             rec_bytes;      /* one frame: points x bps x channels */
    int              nch;            /* channels interleaved in each frame */
    uint32_t         chmask;         /* which scope channels, bit 0 = CH1 */

    /* stats */
    unsigned long    frames_in, frames_sent, frames_dropped, bytes_sent;
    unsigned long    send_errors;
    double           t0;
} G;

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

static int send_all(int fd, const void *buf, size_t n) {
    const char *p = buf;
    size_t off = 0;
    while (off < n) {
        ssize_t k = send(fd, p + off, n - off, MSG_NOSIGNAL);
        if (k > 0) { off += (size_t)k; continue; }
        if (k < 0 && (errno == EINTR || errno == EAGAIN)) continue;
        return -1;
    }
    return 0;
}

static void *sender_main(void *arg) {
    (void)arg;
    /* Named so it can be told apart in cpuwatch/top: an unnamed pthread here
     * inherits whatever created it (a frida thread), which made it impossible
     * to see which core the encode and the send were landing on. */
    pthread_setname_np(pthread_self(), "mhotap-send");
    unsigned char hdr[HDR_BYTES];
    for (;;) {
        pthread_mutex_lock(&G.m);
        while (G.count == 0 && !G.stop)
            pthread_cond_wait(&G.can_send, &G.m);
        if (G.count == 0 && G.stop) { pthread_mutex_unlock(&G.m); break; }
        slot_t *sl = &G.slots[G.tail];
        long len = sl->len;
        uint32_t seq = sl->seq;
        pthread_mutex_unlock(&G.m);

        /* Layout (little-endian), shared with spectrum/stream_client.py:
         *   0 magic   8 seq   12 samples in the frame (all channels)   16 bps
         *  18 channel count, 0 = one (as an older tap sends)
         *  20 channel mask, bit 0 = CH1 (0 = unknown, receiver numbers 1..n)
         *  24 sample rate   32 unused   40/48/56 yinc/yorig/yref */
        memset(hdr, 0, sizeof hdr);
        memcpy(hdr, MAGIC, 8);
        uint32_t u32 = seq;                            memcpy(hdr + 8,  &u32, 4);
        u32 = (uint32_t)(len / (G.bps ? G.bps : 2));   memcpy(hdr + 12, &u32, 4);
        uint16_t u16 = (uint16_t)G.bps;                memcpy(hdr + 16, &u16, 2);
        u16 = (uint16_t)(G.nch > 1 ? G.nch : 0);       memcpy(hdr + 18, &u16, 2);
        u32 = G.chmask;                                memcpy(hdr + 20, &u32, 4);
        double d = G.srate;                            memcpy(hdr + 24, &d, 8);
        d = G.yinc;                                    memcpy(hdr + 40, &d, 8);
        d = G.yorig;                                   memcpy(hdr + 48, &d, 8);
        d = G.yref;                                    memcpy(hdr + 56, &d, 8);

        if (send_all(G.fd, hdr, HDR_BYTES) != 0 ||
            send_all(G.fd, sl->buf, (size_t)len) != 0) {
            G.send_errors++;
        } else {
            G.frames_sent++;
            G.bytes_sent += HDR_BYTES + (unsigned long)len;
        }

        pthread_mutex_lock(&G.m);
        G.tail = (G.tail + 1) % NSLOTS;
        G.count--;
        pthread_mutex_unlock(&G.m);
    }
    return NULL;
}

/* ------------------------------------------------------------ capture loop */

typedef int  (*setstate_fn)(unsigned);
typedef int  (*exinit_fn)(unsigned);
typedef int  (*exdata_fn)(unsigned, unsigned, uint16_t *, int);
typedef void (*exback_fn)(void);

static struct {
    pthread_t     th;
    int           running;
    int           stop;
    setstate_fn   set_state;
    unsigned long cycles, arm_timeouts, export_errors;
    /* The last cycle, split by phase -- see export_main for what each is. */
    double        arm_ms, rnt_wait_ms, lock_wait_ms, export_ms, cycle_ms;
} D;

/* The app's entry points, handed over by mho_tap.js before the loop starts,
 * and the ReadNormTrace success count its return hook feeds.  Kept apart from D
 * because mhotap_drive_start clears D. */
static struct {
    exinit_fn        init;
    exdata_fn        data;
    exback_fn        back;
    void          *(*get_scope)(void);
    void           (*lock)(void *);
    void           (*unlock)(void *);
    pthread_mutex_t  m;
    pthread_cond_t   c;
    unsigned long    ok;
} X = { .m = PTHREAD_MUTEX_INITIALIZER, .c = PTHREAD_COND_INITIALIZER };

void mhotap_export_setup(void *init, void *data, void *back,
                         void *get_scope, void *lock, void *unlock) {
    X.init = (exinit_fn)init;
    X.data = (exdata_fn)data;
    X.back = (exback_fn)back;
    X.get_scope = (void *(*)(void))get_scope;
    X.lock = (void (*)(void *))lock;
    X.unlock = (void (*)(void *))unlock;
}

/* Called from the ReadNormTrace return hook, on the app's acquisition thread,
 * inside CDrvScope::run's LockConfig section: it must only signal. */
void mhotap_rnt_done(int ret) {
    if (ret != 0) return;
    pthread_mutex_lock(&X.m);
    X.ok++;
    pthread_cond_broadcast(&X.c);
    pthread_mutex_unlock(&X.m);
}

/* Samples per channel per ExportData call, as the app itself chunks it:
 * CApiWave::getData asks for 1,000,000 / channels per call and getMemoryData
 * multiplies back up, so one call is 1 M interleaved samples per channel. */
#define EXPORT_CHUNK_PER_CH 1000000L
/* No ReadNormTrace success this long after arming means the capture never
 * completed (no trigger, a changed setting); arm again rather than hang. */
#define EXPORT_RNT_WAIT_S   2

/* One cycle, and why each step is where it is.  Everything here is sequenced on
 * the app's own events and lock; there is no fixed delay anywhere.
 *
 *  1. SetState(3) -- SINGLE.
 *
 *  2. Wait for CDrvScope::ReadNormTrace to return 0.  The app's acquisition
 *     thread (CDrvScope::run) polls it every ~10 ms: -3 until a capture is in,
 *     0 once it has read the capture for its own display.  Exporting before
 *     that overlaps its readout and the next arm: the old SCPI tap did exactly
 *     that and a 1 M export took 65 ms instead of 3.4 ms (timeline, 2026-09-13).
 *
 *  3. Take CDrvScope::LockConfig, the lock run() holds around ReadNormTrace
 *     *and* the SetState calls after it, which reprogram the SCU/SPU registers
 *     an export also uses.  The success in step 2 is not yet safe: an export
 *     started straight after it sometimes blocked 1 s in the c2h read and
 *     returned -5, the app's next ReadNormTrace failed the same way, and it sat
 *     at -3 for 2 s.  Waiting on the lock takes as long as run() needs -- ~1 ms
 *     in practice -- and the export functions do not take it themselves.
 *
 *  4. ExportInit / ExportData / ExportBack per chunk, into the ring slot.
 *
 * Measured with this loop (2026-09-13, USB gigabit link, 25-30 s each, 0 export
 * errors, 0 timeouts, 0 repeats; 1 MHz on CH1 read back at 1.000000 MHz at
 * every setting, 3 MHz on CH2 separate from it):
 *
 *   one channel  100 us/div 10 k 19.9 fps   100 us/div 1 M 14.3
 *                2 ms/div 100 k 18.2        2 ms/div 1 M 16.2 (arm 23, wait 33,
 *                                           export 5 ms)
 *                20 ms/div 10 M 1.72 (34 MB/s: the link, not the loop)
 *   CH1+CH2      2 ms/div 1 M 8.7 fps (wait 87, export 7 ms; the loop runs
 *                ~10 captures/s and the link carries 8.7) */
static void *export_main(void *arg) {
    (void)arg;
    pthread_setname_np(pthread_self(), "mhotap-export");
    int bps = G.bps ? G.bps : 2;
    long total = G.rec_bytes / bps;                /* interleaved samples */
    long chunk = EXPORT_CHUNK_PER_CH * (G.nch > 0 ? G.nch : 1);
    void *scope = X.get_scope();
    while (!D.stop) {
        double t0 = now_s();
        pthread_mutex_lock(&X.m);
        unsigned long ok0 = X.ok;
        pthread_mutex_unlock(&X.m);

        D.set_state(3);
        double t1 = now_s();

        struct timespec dl;
        clock_gettime(CLOCK_REALTIME, &dl);
        dl.tv_sec += EXPORT_RNT_WAIT_S;
        int got = 1;
        pthread_mutex_lock(&X.m);
        while (X.ok == ok0 && !D.stop)
            if (pthread_cond_timedwait(&X.c, &X.m, &dl) == ETIMEDOUT) { got = 0; break; }
        pthread_mutex_unlock(&X.m);
        if (D.stop) break;
        double t2 = now_s();
        if (!got) { D.arm_timeouts++; D.cycles++; continue; }

        /* slots[head] is never touched by the sender while count < NSLOTS, so
         * the export can write into it without holding G.m.  A full ring means
         * the link is behind: skip this capture rather than block the loop. */
        pthread_mutex_lock(&G.m);
        int full = (G.count == NSLOTS);
        unsigned char *buf = G.slots[G.head].buf;
        G.frames_in++;
        if (full) G.frames_dropped++;
        pthread_mutex_unlock(&G.m);
        if (full) { D.cycles++; continue; }

        int err = 0;
        X.lock(scope);
        double t3 = now_s();
        for (long off = 0; off < total && !err; off += chunk) {
            long n = total - off < chunk ? total - off : chunk;
            X.init(0);
            if (X.data((unsigned)off, (unsigned)n,
                       (uint16_t *)(void *)(buf + off * bps), 0) != 0)
                err = 1;
            X.back();
        }
        X.unlock(scope);
        double t4 = now_s();
        D.cycles++;
        if (err) { D.export_errors++; continue; }

        pthread_mutex_lock(&G.m);
        slot_t *sl = &G.slots[G.head];
        sl->len = total * bps;
        sl->seq = G.seq++;
        G.head = (G.head + 1) % NSLOTS;
        G.count++;
        pthread_cond_signal(&G.can_send);
        pthread_mutex_unlock(&G.m);

        D.arm_ms       = (t1 - t0) * 1e3;
        D.rnt_wait_ms  = (t2 - t1) * 1e3;
        D.lock_wait_ms = (t3 - t2) * 1e3;
        D.export_ms    = (t4 - t3) * 1e3;
        D.cycle_ms     = (now_s() - t0) * 1e3;
    }
    return NULL;
}

/* The acquisition entry points are passed in rather than dlsym'd: the app
 * loads libscope-auklet.so straight out of the APK, so dlopen by bare soname
 * fails (RTLD_NOLOAD returns NULL).  The injector already has the module
 * resolved, so it hands over the addresses. */
int mhotap_drive_start(void *set_state) {
    if (D.running) return 0;
    memset(&D, 0, sizeof D);
    D.set_state = (setstate_fn)set_state;
    if (!D.set_state) return -2;
    if (!X.init || !X.data || !X.back || !X.get_scope || !X.lock || !X.unlock)
        return -6;
    if (!G.running || G.rec_bytes <= 0 || G.rec_bytes > G.slot_cap) return -3;
    D.running = 1;
    if (pthread_create(&D.th, NULL, export_main, NULL) != 0) {
        D.running = 0; return -5;
    }
    return 0;
}

void mhotap_drive_stop(void) {
    if (!D.running) return;
    D.stop = 1;
    pthread_join(D.th, NULL);
    D.running = 0;
}

void mhotap_drive_stats(double *out /* 8 doubles */) {
    if (!out) return;
    out[0] = (double)D.cycles;
    out[1] = (double)D.arm_timeouts;
    out[2] = (double)D.export_errors;
    out[3] = D.arm_ms;
    out[4] = D.rnt_wait_ms;
    out[5] = D.lock_wait_ms;
    out[6] = D.export_ms;
    out[7] = D.cycle_ms;
}

/* ---------------------------------------------------------------- public */

int mhotap_init(const char *host, int port, long max_bytes, double srate, int bps) {
    if (G.running) return 0;
    memset(&G, 0, sizeof G);
    G.slot_cap = max_bytes;
    G.srate = srate;
    G.bps = bps ? bps : 2;
    G.nch = 1;

    for (int i = 0; i < NSLOTS; i++) {
        G.slots[i].buf = malloc((size_t)max_bytes);
        if (!G.slots[i].buf) return -1;
    }
    pthread_mutex_init(&G.m, NULL);
    pthread_cond_init(&G.can_send, NULL);

    G.fd = socket(AF_INET, SOCK_STREAM, 0);
    if (G.fd < 0) return -2;
    /* Stock tcp_wmem max is 110 KB; without a big buffer every frame would
     * block in tcp_sendmsg until the wire drained, serialising send with the
     * next acquisition -- exactly what this design exists to avoid. */
    int sndbuf = 4 << 20;
    setsockopt(G.fd, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof sndbuf);
    int one = 1;
    setsockopt(G.fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);

    struct sockaddr_in sa;
    memset(&sa, 0, sizeof sa);
    sa.sin_family = AF_INET;
    sa.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, host, &sa.sin_addr) != 1) { close(G.fd); G.fd = -1; return -3; }
    if (connect(G.fd, (struct sockaddr *)&sa, sizeof sa) != 0) {
        close(G.fd); G.fd = -1; return -4;
    }

    G.t0 = now_s();
    G.running = 1;
    if (pthread_create(&G.th, NULL, sender_main, NULL) != 0) {
        close(G.fd); G.fd = -1; G.running = 0; return -5;
    }
    return 0;
}

/* Bytes in one frame: points x bytes per sample x channels.  Set after init,
 * before mhotap_drive_start. */
void mhotap_set_record(long nbytes) {
    pthread_mutex_lock(&G.m);
    G.rec_bytes = (nbytes > 0 && nbytes <= G.slot_cap) ? nbytes : 0;
    pthread_mutex_unlock(&G.m);
}

/* How many channels ExportData interleaves, and which ones (bit 0 = CH1).
 * It exports whatever is enabled on the scope, so this must match that. */
void mhotap_set_channels(int nch, unsigned mask) {
    G.nch = nch > 0 ? nch : 1;
    G.chmask = mask;
}

void mhotap_set_rate(double srate) { G.srate = srate; }

void mhotap_set_yscale(double yinc, double yorig, double yref) {
    G.yinc = yinc; G.yorig = yorig; G.yref = yref;
}

/* Fills caller-provided storage so the injector needs no struct layout. */
void mhotap_stats(double *out /* 8 doubles */) {
    if (!out) return;
    double el = now_s() - G.t0;
    out[0] = (double)G.frames_in;
    out[1] = (double)G.frames_sent;
    out[2] = (double)G.frames_dropped;
    out[3] = (double)G.bytes_sent;
    out[4] = el;
    out[5] = el > 0 ? G.frames_sent / el : 0.0;
    out[6] = el > 0 ? G.bytes_sent / el / 1e6 : 0.0;
    out[7] = (double)G.send_errors;
}

/* Stop the capture loop (mhotap_drive_stop) first: it writes into the ring. */
void mhotap_close(void) {
    pthread_mutex_lock(&G.m);
    if (!G.running) { pthread_mutex_unlock(&G.m); return; }
    G.running = 0;
    G.stop = 1;
    pthread_cond_broadcast(&G.can_send);
    pthread_mutex_unlock(&G.m);

    pthread_join(G.th, NULL);
    if (G.fd >= 0) close(G.fd);
    G.fd = -1;
    /* Deliberately NOT freeing the slot buffers: the library stays mapped for
     * the life of the process, and keeping the allocations removes any chance
     * of a late writer touching freed memory.  NSLOTS frames' worth (6 MB at
     * 1 Mpt, 60 MB at 10 M, twice that with two channels) is the premium. */
}

/*
 * libmhotap.so -- in-app waveform tap for the Rigol MHO934.
 *
 * WHY: the SCPI reply path costs ~110 ms per 1 Mpt frame -- the app marshals
 * the record into an RByteArray (28.5 MB/s even patched) and frames it as a
 * SCPI block before a byte reaches the wire.  But CApiWave::toFormat already
 * receives a pointer to the COMPLETE record.  Copying 2 MB out of that pointer
 * costs ~1.5 ms at DRAM speed, so if we take it there and send it ourselves the
 * entire produce-and-frame path becomes dead weight and can be skipped.
 *
 * This library owns the socket, the ring and the sender thread.  The injector
 * (stream/mho_tap.js) only has to call mhotap_frame() once per record -- the
 * same shape as the speed patch's per-read malloc/memcpy NativeFunction calls,
 * which have been reliable.  Nothing bulk happens in the Frida JS runtime,
 * which is what aborted the app in the earlier attempt (see docs/STREAMING.md).
 *
 * Build:
 *   $NDK/aarch64-linux-android30-clang -O2 -shared -fPIC -pthread \
 *       stream/libmhotap.c -o libmhotap.so
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#include <dlfcn.h>

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
    pthread_cond_t   can_send, can_fill;
    slot_t           slots[NSLOTS];
    long             slot_cap;
    int              head, tail, count;

    uint32_t         seq;
    double           srate;
    int              bps;

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

        memset(hdr, 0, sizeof hdr);
        memcpy(hdr, MAGIC, 8);
        uint32_t u32 = seq;                            memcpy(hdr + 8,  &u32, 4);
        u32 = (uint32_t)(len / (G.bps ? G.bps : 2));   memcpy(hdr + 12, &u32, 4);
        uint16_t u16 = (uint16_t)G.bps;                memcpy(hdr + 16, &u16, 2);
        double d = G.srate;                            memcpy(hdr + 24, &d, 8);

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
        pthread_cond_signal(&G.can_fill);
        pthread_mutex_unlock(&G.m);
    }
    return NULL;
}

/* ------------------------------------------------------- capture driver
 *
 * Running the frame loop in here removes every per-frame round trip to the
 * host: arming, triggering and draining all happen on the scope.  The trigger
 * still goes through SCPI on loopback because that is what makes the app
 * produce a record -- but with the hook active it marshals nothing, so the
 * reply is a stub and the loopback traffic is negligible.
 */

typedef int (*setstate_fn)(unsigned);
typedef int (*getstatus_fn)(unsigned *);

static struct {
    int          fd;               /* loopback SCPI */
    pthread_t    th;
    int          running;
    int          stop;
    setstate_fn  set_state;
    getstatus_fn get_status;
    unsigned long cycles, arm_timeouts, trigger_errors, empty_replies;
    unsigned long missed_busy;     /* completion never observed as busy->idle */
    double       last_arm_ms, last_trig_ms;
    /* The arm phase split at the busy edge.  A long arm is either the app
     * failing to *start* the acquisition (it is short of CPU) or the
     * acquisition genuinely taking that long, and the two want opposite
     * fixes -- one number cannot tell them apart. */
    double       last_wait_busy_ms, last_busy_ms;
    long         last_declared;
    int          arm_mode;         /* 0 = SINGLE, 1 = RUN/dwell/STOP */
    int          dwell_us;
} D;

static int recv_some(int fd, void *b, size_t n, int ms) {
    struct timeval tv = { ms / 1000, (ms % 1000) * 1000 };
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    return (int)recv(fd, b, n, 0);
}

/* Read the reply header and discard whatever really arrives.  The framing
 * announces the full record length even though the tap left only a stub, so a
 * length-driven read would wait forever.  Returns the DECLARED length, which
 * tells us whether the scope thought it had a record at all. */
/* How long to wait for the trigger reply's header before giving up.
 *
 * This was 40 ms, which sat right on top of the distribution instead of clear
 * of it.  Measured over 2200 cycles: the reply arrives at a median of 20 ms,
 * but 33% of *successful* cycles take more than 35 ms and p99 is 49.5 ms,
 * because the latency is quantised by the app's ~25 ms servicing tick and
 * lands a tick later whenever the phase drifts.  Every abandoned reply cost a
 * cycle with no record (empty_replies, declared = -1), and they came in runs
 * of 11-20, which is what a "2 second stall" actually was.
 *
 * Waiting longer costs nothing when the reply is prompt -- the wait ends when
 * the data arrives, not when the timeout expires. */
#define REPLY_HDR_MS 120

static long drain_reply(int fd) {
    char sink[4096];
    long declared = -1;
    int k = recv_some(fd, sink, 2, REPLY_HDR_MS);  /* '#' + ndigits */
    if (k == 2 && sink[0] == '#') {
        int ndig = sink[1] - '0';
        if (ndig >= 1 && ndig <= 9) {
            char lb[16] = {0};
            if (recv_some(fd, lb, ndig, REPLY_HDR_MS) == ndig)
                declared = atol(lb);
        }
    }
    for (;;) {
        int n = recv_some(fd, sink, sizeof sink, 8);
        if (n <= 0) break;
    }
    return declared;
}

static void *driver_main(void *arg) {
    (void)arg;
    const char *req = ":WAVeform:DATA?\n";
    unsigned st = 0;
    while (!D.stop) {
        double t0 = now_s();
        int sawBusy = 0, ok = 0;
        if (D.arm_mode == 1) {
            /* Free-run for a fixed dwell then stop.  No SCPI tick in the way,
             * so the dwell is real -- unlike :RUN/:STOP from a host, where the
             * parser can process both after the dwell has already elapsed. */
            D.set_state(2);
            usleep((useconds_t)D.dwell_us);
            D.set_state(1);
            ok = 1;
        } else {
            /* 3 = SINGLE.  Completion is observable, so the record is
             * guaranteed new. */
            D.set_state(3);
        }
        double t1 = now_s();
        double t_busy = 0.0;
        if (ok) goto armed;
        while (now_s() - t1 < 2.0) {
            st = 0xffffffffu;
            D.get_status(&st);
            if (st != 0) {
                if (!sawBusy) t_busy = now_s();
                sawBusy = 1;
            }
            else if (sawBusy) { ok = 1; break; }
            else if (now_s() - t1 > 0.100) {
                /* 100 ms and never once seen busy.  Either the acquisition
                 * finished inside the gap between two polls, or it never
                 * started.  Waiting out the full 2 s timeout for that is the
                 * worst of both: it is a visible freeze *and* it throws the
                 * cycle away.  Proceed instead, and count it -- the receiver
                 * CRCs every frame, so if this ever does serve a stale record
                 * it shows up as a repeat rather than passing silently. */
                D.missed_busy++;
                ok = 1;
                break;
            }
            /* Poll fast at first, then back off.
             *
             * The backoff is what matters for load: at 200 us throughout,
             * this thread and the sender took ~1.7 cores on a box whose two
             * A72s were already saturated (measured with bench/scope_load.py),
             * and hammering get_status into the app's own acquisition driver
             * at 5 kHz is what made the arm phase grow the longer the loop
             * ran.  At 2 ms the demo holds 10.3 fps for a full minute instead
             * of decaying to 6.
             *
             * The fast head is what keeps that safe.  Completion is only
             * accepted after busy has actually been seen, so a busy window
             * that falls entirely between two polls costs the full 2 s
             * timeout.  A 1 Mpt record at 50 MSa/s is 20 ms of capture and
             * measured busy is ~24 ms, which says 2 ms polling is ample --
             * and yet a flat 2 ms poll produced exactly one arm_timeout in
             * 510 frames.  The measurement is the authority, not the
             * reasoning: 5 ms at 200 us costs 25 polls per cycle and closes
             * it. */
            usleep((now_s() - t1) < 0.005 ? 200 : 2000);
            if (D.stop) break;
        }
        if (!ok) { D.arm_timeouts++; if (D.stop) break; continue; }
armed:
        ;
        double t2 = now_s();
        D.last_wait_busy_ms = t_busy ? (t_busy - t1) * 1e3 : 0.0;
        D.last_busy_ms = t_busy ? (t2 - t_busy) * 1e3 : 0.0;

        if (send_all(D.fd, req, strlen(req)) != 0) { D.trigger_errors++; break; }
        D.last_declared = drain_reply(D.fd);
        if (D.last_declared <= 0) D.empty_replies++;
        D.cycles++;
        D.last_arm_ms = (t2 - t0) * 1e3;
        D.last_trig_ms = (now_s() - t2) * 1e3;
    }
    return NULL;
}

/* The acquisition entry points are passed in rather than dlsym'd: the app
 * loads libscope-auklet.so straight out of the APK, so dlopen by bare soname
 * fails (RTLD_NOLOAD returns NULL).  The injector already has the module
 * resolved, so let it hand over the addresses. */
int mhotap_drive_start(void *set_state, void *get_status,
                       int arm_mode, int dwell_us) {
    if (D.running) return 0;
    memset(&D, 0, sizeof D);
    D.arm_mode = arm_mode;
    D.dwell_us = dwell_us > 0 ? dwell_us : 40000;

    D.set_state  = (setstate_fn)set_state;
    D.get_status = (getstatus_fn)get_status;
    if (!D.set_state || !D.get_status) return -2;

    D.fd = socket(AF_INET, SOCK_STREAM, 0);
    if (D.fd < 0) return -3;
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof sa);
    sa.sin_family = AF_INET;
    sa.sin_port = htons(5555);
    inet_pton(AF_INET, "127.0.0.1", &sa.sin_addr);
    if (connect(D.fd, (struct sockaddr *)&sa, sizeof sa) != 0) {
        close(D.fd); D.fd = -1; return -4;
    }
    int one = 1;
    setsockopt(D.fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);

    D.running = 1;
    if (pthread_create(&D.th, NULL, driver_main, NULL) != 0) {
        close(D.fd); D.fd = -1; D.running = 0; return -5;
    }
    return 0;
}

void mhotap_drive_stop(void) {
    if (!D.running) return;
    D.stop = 1;
    pthread_join(D.th, NULL);
    if (D.fd >= 0) close(D.fd);
    D.fd = -1;
    D.running = 0;
}

void mhotap_drive_stats(double *out /* 10 doubles */) {
    if (!out) return;
    out[0] = (double)D.cycles;
    out[1] = (double)D.arm_timeouts;
    out[2] = (double)D.trigger_errors;
    out[3] = D.last_arm_ms;
    out[4] = D.last_trig_ms;
    out[5] = (double)D.empty_replies;
    out[6] = (double)D.last_declared;
    out[7] = D.last_wait_busy_ms;
    out[8] = D.last_busy_ms;
    out[9] = (double)D.missed_busy;
}

/* ---------------------------------------------------------------- public */

int mhotap_init(const char *host, int port, long max_bytes, double srate, int bps) {
    if (G.running) return 0;
    memset(&G, 0, sizeof G);
    G.slot_cap = max_bytes;
    G.srate = srate;
    G.bps = bps ? bps : 2;

    for (int i = 0; i < NSLOTS; i++) {
        G.slots[i].buf = malloc((size_t)max_bytes);
        if (!G.slots[i].buf) return -1;
    }
    pthread_mutex_init(&G.m, NULL);
    pthread_cond_init(&G.can_send, NULL);
    pthread_cond_init(&G.can_fill, NULL);

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

/* Called from the hook, on the app's readout thread.  Must be cheap and must
 * never block: a memcpy into a free slot, or drop the frame if the sender has
 * not kept up.  Dropping beats stalling the scope's own readout thread. */
int mhotap_frame(const void *src, long nbytes) {
    if (nbytes <= 0) return -1;

    /* The copy runs under the lock on purpose.  Checking G.running outside it
     * and copying afterwards is a use-after-free waiting to happen: close()
     * can free the slots in between, and a hook already past the check then
     * writes into freed memory.  (That crashed the app with SIGSEGV at 0x0 in
     * gum-js-loop.)  The sender does not hold the lock while sending, so the
     * ~1.5 ms memcpy costs it nothing. */
    pthread_mutex_lock(&G.m);
    if (!G.running || nbytes > G.slot_cap) {
        pthread_mutex_unlock(&G.m);
        return -1;
    }
    G.frames_in++;
    if (G.count == NSLOTS) {
        /* Sender has not kept up.  Drop rather than stall the scope's own
         * readout thread, which is what we are running on. */
        G.frames_dropped++;
        pthread_mutex_unlock(&G.m);
        return 1;
    }
    slot_t *sl = &G.slots[G.head];
    memcpy(sl->buf, src, (size_t)nbytes);
    sl->len = nbytes;
    sl->seq = G.seq++;
    G.head = (G.head + 1) % NSLOTS;
    G.count++;
    pthread_cond_signal(&G.can_send);
    pthread_mutex_unlock(&G.m);
    return 0;
}

void mhotap_set_rate(double srate) { G.srate = srate; }

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

void mhotap_close(void) {
    pthread_mutex_lock(&G.m);
    if (!G.running) { pthread_mutex_unlock(&G.m); return; }
    /* Clearing running under the lock means any hook still in flight has
     * already finished its copy, and none can start afterwards. */
    G.running = 0;
    G.stop = 1;
    pthread_cond_broadcast(&G.can_send);
    pthread_mutex_unlock(&G.m);

    pthread_join(G.th, NULL);
    if (G.fd >= 0) close(G.fd);
    G.fd = -1;
    /* Deliberately NOT freeing the slot buffers: the hook stays installed for
     * the life of the process, so keeping the allocations alive removes any
     * remaining chance of a use-after-free.  3 x 2 MB is a cheap insurance
     * premium against crashing the scope. */
}

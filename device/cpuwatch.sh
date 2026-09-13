#!/usr/bin/env bash
# Per-core and per-thread CPU on the scope, one row per sample.
#
# Usage:  device/cpuwatch.sh <scope-ip> [interval-s] [process-name]
#
#   device/cpuwatch.sh 192.168.23.20            # 2 s, com.rigol.scope
#   device/cpuwatch.sh 192.168.23.20 5          # 5 s window
#   device/cpuwatch.sh 192.168.23.20 2 com.rigol.launcher
#
# Run it alongside a streaming benchmark to see what the scope is doing while
# the frame loop runs.  Measured 2026-09-08: idle sits at ~1.55 cores and 35%
# busy, and a 1 Mpt tap stream takes it to ~4.1 cores and ~90% busy, of which
# sys alone is ~49% -- about 2.9 cores alone in the kernel.
#
# Sample inside the burst or you will measure nothing.  run_fft.sh --run-seconds
# counts from process start and ~40 s of that is frida/dex2oat setup, so a
# 45 s run streams for only ~5 s and an 8 s sampling window can miss it
# entirely and report idle numbers.  Ask for 150 s to get ~100 s of stream.
# The row layout exists for this reason: the burst is obvious as a row, and
# was invisible when this printed a block per sample.
#
# Every number is percent of ONE core, like Linux top.  Note that toolbox top
# on the scope uses the other convention -- it divides by all six cores, so a
# thread pegging one core reads ~16% there and 100% here.
#
# Columns: c0..c5 per-core busy, busy/usr/sys for the machine as a whole, app
# = the watched process in cores, then its hottest thread.  A thread holding a
# whole core (task_plot_wave usually does) shows as a pinned 100 in one of the
# c* columns and a ~1.0 floor under app.
set -euo pipefail

ip="${1:-}"
interval="${2:-2}"
procname="${3:-com.rigol.scope}"

usage() { sed -n '2,29p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
if [[ -z "$ip" || "$ip" == "-h" || "$ip" == "--help" ]]; then usage; exit 0; fi

# The scope runs network adb on 55555, not the default 5555 (that is SCPI).
port=55555
serial="$ip:$port"
command -v adb >/dev/null || { echo "adb not found" >&2; exit 1; }
adb connect "$serial" >/dev/null 2>&1 || true
adb -s "$serial" get-state >/dev/null 2>&1 || {
    echo "cannot reach $serial -- is the scope up and adb enabled?" >&2; exit 1; }

# USER_HZ, for turning stat ticks into seconds.  getconf is not on the device,
# and this kernel (4.4.126 arm64) is the usual 100.  If a future build changes
# it, every number here scales wrong by the same factor -- so check it before
# trusting a surprising result.
hz=100

# `ps -A` is not valid on this toolbox ("bad pid '-A'") and fails *silently*
# through a pipe, which reads as "process gone".  Plain ps only.
find_pid() {
    adb -s "$serial" shell "ps | grep '${procname}\$'" 2>/dev/null \
        | tr -s ' ' | cut -d' ' -f2 | head -1 | tr -d '\r'
}

# One adb round trip per snapshot: uptime, the per-core lines, then every
# thread's stat.  Thread names can contain spaces ("POSIX timer 1"), so the tid
# is emitted separately and comm is recovered from the parens in the stat line
# rather than by field position -- comm may also contain ')', hence the scan
# back from the end for the closing one.
snapshot() {
    adb -s "$serial" shell "echo U \$(cat /proc/uptime); grep ^cpu /proc/stat;
        for t in /proc/$1/task/*; do echo \"X \${t##*/} \$(cat \$t/stat 2>/dev/null)\"; done" 2>/dev/null
}

pid="$(find_pid)"
[[ -n "$pid" ]] || { echo "no process matching '$procname' on $ip" >&2; exit 1; }
echo "# $procname pid=$pid on $ip, ${interval}s samples, % of one core -- ctrl-c to stop"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
row=0

while :; do
    snapshot "$pid" > "$tmp/a"
    sleep "$interval"
    snapshot "$pid" > "$tmp/b"

    # A restart (the Watchdog does this within ~15 s of a crash) invalidates
    # the tids, so re-resolve and skip the window rather than print nonsense.
    now="$(find_pid)"
    if [[ "$now" != "$pid" ]]; then
        echo "# $procname restarted: pid $pid -> ${now:-gone}"
        pid="$now"; row=0
        [[ -n "$pid" ]] || { sleep "$interval"; pid="$(find_pid)"; }
        continue
    fi

    # Reprint the header every 20 rows so a long run stays readable when it
    # has scrolled past the top of the terminal.
    hdr=0; (( row % 20 == 0 )) && hdr=1
    row=$((row + 1))

    awk -v hz="$hz" -v hdr="$hdr" '
    function pct(d, dt) { return 100 * (d / hz) / dt }
    {
        gsub(/\r/, "")                       # adb shell line endings
        f = FILENAME
        if ($1 == "U") { up[f] = $2; next }
        if ($1 ~ /^cpu/) {
            key = $1
            # cpu: user nice system idle iowait irq softirq steal
            busy = $2 + $3 + $4 + $7 + $8          # idle ($5) and iowait ($6) are not busy
            tot  = busy + $5 + $6
            if (f == a) { c_busy[key] = busy; c_tot[key] = tot; c_u[key] = $2; c_s[key] = $4 }
            else { d_busy[key] = busy - c_busy[key]; d_tot[key] = tot - c_tot[key]
                   d_u[key] = $2 - c_u[key]; d_s[key] = $4 - c_s[key] }
            next
        }
        if ($1 == "X") {
            tid = $2
            p = 0
            for (i = length($0); i > 1; i--) if (substr($0, i, 1) == ")") { p = i; break }
            r = substr($0, p + 2)                  # everything after "pid (comm) "
            split(r, g, " ")
            ut = g[12]; st = g[13]
            cs = index($0, "(")
            nm = substr($0, cs + 1, p - cs - 1)
            if (f == a) { t_u[tid] = ut; t_s[tid] = st; seen[tid] = 1 }
            # A thread that appears only in the second snapshot has no baseline,
            # so its lifetime CPU would read as a whole window of load.  Skip it;
            # it shows up properly on the next interval.
            else if (tid in seen) { du[tid] = ut - t_u[tid]; ds[tid] = st - t_s[tid]
                   name[tid] = nm }
        }
    }
    END {
        dt = up[b] - up[a]
        if (dt <= 0) { print "# (bad sample window)"; exit }
        n = 0
        for (i = 0; i < 64; i++) if (("cpu" i) in d_tot) n = i + 1
        if (hdr) {
            printf "%-8s", "time"
            for (i = 0; i < n; i++) printf "%5s", "c" i
            printf "%7s%6s%6s%7s   %s\n", "busy", "usr", "sys", "app", "hottest thread"
        }
        printf "%-8s", ts
        for (i = 0; i < n; i++) {
            k = "cpu" i
            printf "%5.0f", (d_tot[k] > 0) ? 100 * d_busy[k] / d_tot[k] : 0
        }
        mb = d_tot["cpu"] > 0 ? 100 * d_busy["cpu"] / d_tot["cpu"] : 0
        mu = d_tot["cpu"] > 0 ? 100 * d_u["cpu"]    / d_tot["cpu"] : 0
        ms = d_tot["cpu"] > 0 ? 100 * d_s["cpu"]    / d_tot["cpu"] : 0
        for (t in du) {
            v = pct(du[t] + ds[t], dt)
            app += v
            if (v > topv) { topv = v; topn = name[t] }
        }
        printf "%6.1f%%%5.0f%%%5.0f%%%7.2f   %s %.0f%%\n", mb, mu, ms, app / 100, topn, topv
    }
    ' a="$tmp/a" b="$tmp/b" ts="$(date +%T)" "$tmp/a" "$tmp/b"
done

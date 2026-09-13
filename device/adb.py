#!/usr/bin/env python3
"""adb.py -- adb + frida plumbing for talking to a Rigol MHO/DHO scope.

Extracted from the mho-speed-patch repo's patch/patch_scope.py, which remains
the home of the *speed patch itself* (TCP buffer sizing, A72 affinity, worker
nice levels).  None of that is here and none of it is needed: the tap bypasses
the SCPI reply path the patch exists to accelerate.

What is here is only the device plumbing the tap needs to get going:
  * find adb, connect over network adb (port 55555), get root
  * download/push/start a frida-server matching the installed frida client
  * find the app's real pid (skipping ptrace-stopped forks)

If you change something here that is genuinely about *reaching the device*
rather than about the spectrum app, the same fix probably belongs in
patch_scope.py too -- these two copies are expected to stay similar.
"""
import lzma
import os
import shutil
import subprocess
import sys
import time
import urllib.request

APP = "com.rigol.scope"
ADB_PORT = 55555
SCPI_PORT = 5555
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # repo root; device/ lives one level down
DEV_FS = "/data/local/tmp/frida-server"
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "mho-speed-patch")

def log(msg):
    print(f"[device] {msg}", flush=True)


def die(msg, code=1):
    print(f"[device] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


# --- adb helpers -------------------------------------------------------------
def find_adb(explicit):
    if explicit:
        return explicit
    for base in (ROOT, HERE):
        local = os.path.join(base, "platform-tools", "adb")
        if os.path.exists(local):
            return local
    found = shutil.which("adb")
    if found:
        return found
    die("adb not found. Install Android platform-tools, put them next to this "
        "script, or pass --adb /path/to/adb")


class Adb:
    def __init__(self, adb, serial):
        self.adb = adb
        self.serial = serial

    def _run(self, args, timeout=60, **kw):
        # capture_output waits for EOF on the pipes, not for the command to
        # exit -- anything left holding the device-side stdout/stderr (a
        # daemon, say) would block us forever. The timeout is the backstop;
        # commands that spawn daemons must also redirect their stdio (see
        # ensure_frida_server).
        try:
            return subprocess.run([self.adb, "-s", self.serial, *args],
                                  capture_output=True, text=True,
                                  timeout=timeout, **kw)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                args, 124, stdout="", stderr=f"adb timed out after {timeout}s")

    def raw(self, args, timeout=60, **kw):
        try:
            return subprocess.run([self.adb, *args], capture_output=True,
                                  text=True, timeout=timeout, **kw)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                args, 124, stdout="", stderr=f"adb timed out after {timeout}s")

    def shell(self, cmd, root=False, timeout=60):
        if root:
            cmd = f"su -c '{cmd}'"
        return self._run(["shell", cmd], timeout=timeout)

    def push(self, local, remote):
        return self._run(["push", local, remote])


# --- frida-server provisioning ----------------------------------------------
def frida_version():
    try:
        import frida
        return frida.__version__
    except ImportError:
        die("the 'frida' Python module is not installed. Run: pip install frida")


def fetch_frida_server(version):
    """Return a local path to frida-server-<version>-android-arm64 (cached)."""
    os.makedirs(CACHE, exist_ok=True)
    dest = os.path.join(CACHE, f"frida-server-{version}-android-arm64")
    if os.path.exists(dest) and os.path.getsize(dest) > 1_000_000:
        return dest
    name = f"frida-server-{version}-android-arm64.xz"
    url = f"https://github.com/frida/frida/releases/download/{version}/{name}"
    log(f"downloading {name} (~25 MB, cached for next time) ...")
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            comp = r.read()
    except Exception as e:
        die(f"could not download frida-server for {version} ({e}). "
            f"Download it yourself and pass --frida-server, or "
            f"pip install a frida whose server has a matching release.")
    with open(dest, "wb") as f:
        f.write(lzma.decompress(comp))
    return dest


def ensure_frida_server(adb, version, local_path):
    # Reuse a running server iff its version matches our client.
    running = adb.shell("pidof frida-server", root=True).stdout.strip()
    if running:
        ver = adb.shell(f"{DEV_FS} --version", root=True).stdout.strip()
        if ver == version:
            log(f"frida-server {ver} already running -- reusing")
            return
        log(f"running frida-server {ver!r} != client {version}; restarting")
        adb.shell("pkill frida-server", root=True)
        time.sleep(1)

    src = local_path or fetch_frida_server(version)
    # push if the on-device copy differs (by size, cheap check)
    dev_ok = False
    st = adb.shell(f"stat -c %s {DEV_FS} 2>/dev/null || true").stdout.strip()
    if st.isdigit() and int(st) == os.path.getsize(src):
        dev_ok = True
    if not dev_ok:
        log("pushing frida-server to /data/local/tmp ...")
        r = adb.push(src, DEV_FS)
        if r.returncode != 0:
            die(f"push failed: {r.stderr.strip()}")
    adb.shell(f"chmod 755 {DEV_FS}", root=True)
    # -D daemonizes so it survives the adb shell exiting. The stdio redirect is
    # what stops us hanging: the daemon inherits adb's stdout/stderr pipes, and
    # capture_output waits for those to reach EOF, so without </dev/null and
    # >/dev/null the launch call blocks forever even though the server started.
    adb.shell(f"{DEV_FS} -D </dev/null >/dev/null 2>&1", root=True, timeout=20)
    for _ in range(20):
        time.sleep(0.3)
        if adb.shell("pidof frida-server", root=True).stdout.strip():
            log(f"frida-server {version} started")
            return
    die("frida-server did not come up")


# --- scope access ------------------------------------------------------------
def connect(adb_path, ip):
    serial = f"{ip}:{ADB_PORT}"
    r = subprocess.run([adb_path, "connect", serial], capture_output=True, text=True)
    out = (r.stdout + r.stderr).lower()
    if "connected" not in out and "already" not in out:
        die(f"adb connect {serial} failed: {r.stdout.strip()} {r.stderr.strip()}")
    return Adb(adb_path, serial)


def ensure_root(adb):
    # Cheap path first.  `adb root` restarts adbd and costs a fixed one-second
    # reconnect wait below, and it runs on every launch even though the daemon
    # is already root for every run after the first -- measured 2026-09-09 as
    # 1.15 s of a 4.3 s time-to-first-frame, the single largest slice of it.
    try:
        if adb.shell("id", timeout=5).stdout.find("uid=0") >= 0:
            log("adbd already root")
            return
    except Exception:
        pass
    # Try `adb root` (works on these debuggable builds); else confirm `su` works.
    adb.raw(["-s", adb.serial, "root"])
    time.sleep(1)
    subprocess.run([adb.adb, "connect", adb.serial], capture_output=True, text=True)
    if adb.shell("id").stdout.strip().find("uid=0") >= 0:
        log("root via 'adb root'")
        return
    if "uid=0" in adb.shell("id", root=True).stdout:
        log("root via 'su'")
        return
    die("could not get root on the scope (neither 'adb root' nor 'su' worked)")


def _pids(adb):
    for how in (dict(root=True), dict(root=False)):
        out = adb.shell(f"pidof {APP}", **how).stdout.strip().split()
        pids = [int(p) for p in out if p.isdigit()]
        if pids:
            return pids
    return []


def _main_pid(adb, pids):
    """Pick the real app process out of what pidof returned.

    `pidof` can return more than one: a failed Frida injection leaves behind a
    forked child stuck in `t` (tracing stop), which keeps the app's name and
    its copy-on-write pages.  Attaching to *that* fails with "loader crashed
    with signal 11", which is a thoroughly misleading way to be told you picked
    the wrong pid.  The real process is the one that is not ptrace-stopped and
    has all the threads.
    """
    if len(pids) == 1:
        return pids[0]
    best, best_thr = None, -1
    for pid in pids:
        st = adb.shell(f"grep -E \"^(State|Threads):\" /proc/{pid}/status",
                       root=True).stdout
        if " t (" in st or " T (" in st:
            continue                      # traced/stopped fork, not the app
        thr = 0
        for line in st.splitlines():
            if line.startswith("Threads:"):
                thr = int(line.split()[1])
        if thr > best_thr:
            best, best_thr = pid, thr
    return best if best is not None else pids[0]


def current_pid(adb):
    """Live pid of the app, or None. Unlike app_pid() this never exits: it runs
    inside the watch loop, where a momentarily absent app is not fatal."""
    try:
        pids = _pids(adb)
        return _main_pid(adb, pids) if pids else None
    except Exception:
        return None


def app_pid(adb):
    pids = _pids(adb)
    if pids:
        pid = _main_pid(adb, pids)
        if pid:
            if len(pids) > 1:
                log(f"note: {len(pids)} {APP} pids {pids}; using {pid} "
                    f"(the others are stopped forks)")
            return pid
    die(f"{APP} is not running on the scope")

#!/usr/bin/env bash
# Start the live FFT spectrum demo using the local .venv.
#
# Usage:  ./run_fft.sh [source] [extra fft_gui.py args...]
#
#   ./run_fft.sh                        synthetic signal, no scope needed
#   ./run_fft.sh synthetic
#   ./run_fft.sh scpi <scope-ip> [--channel N]
#   ./run_fft.sh stream [--port 5560]
#   ./run_fft.sh tap <scope-ip> [--channel N]     <- fastest, ~14 fps
#
# The 'tap' source makes two changes to the running scope app while it streams,
# both restored when the demo exits:
#   * its waveform redraw is paused (that plot thread is ~60% of a core, and
#     the scope's screen holds its last trace until exit)  --no-tap-quiet-ui
#   * the hardcoded 20 ms sleep it takes before answering any SCPI command is
#     shortened to 1 ms, which the tap pays once per frame
#                                                          --tap-keep-scpi-sleep
# Together they are worth 11.3 -> 13.9 fps and remove the stalls.
#
# Any further arguments are passed straight through to fft_gui.py, so
# --average, --window, --points, --headless, --run-seconds etc. all work.
#
# Frame timing is on by default: a log-scale strip of the last 300 frame
# intervals sits under the spectrum, slow frames are reported on stdout as they
# happen, and a summary is printed on exit.  --timing-csv PATH keeps the raw
# per-frame rows, --stall-ms overrides the adaptive threshold, and (with the
# tap) --tap-poll-ms 100 catches slow on-scope acquisition cycles.
#
# The 'tap' source is the fast one: it injects libmhotap.so into the scope app
# and streams records straight out of it, bypassing the SCPI reply path
# (~8-9 fps at 1 Mpt, every frame verified new).  It needs frida-server, which
# it provisions itself, and device/libmhotap.so, built by device/build_tap.sh.
#
# For the plain 'scpi' source, the readout is faster if you first run
# patch/run_patch.sh from the mho-speed-patch repo in another window.  The
# 'tap' source does not need it -- it bypasses the SCPI path entirely.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py="$root/.venv/bin/python"

usage() { sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

if [[ ! -x "$py" ]]; then
    echo "error: $py not found." >&2
    echo "create it from the repo root with:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

# The GUI needs scipy/pyqtgraph/PyQt5 on top of what the patch tools need, and
# the import error you get otherwise is not obvious.
if ! "$py" -c 'import numpy, scipy, pyqtgraph' >/dev/null 2>&1; then
    echo "error: the FFT demo needs numpy, scipy and pyqtgraph." >&2
    echo "install them with:" >&2
    echo "  $root/.venv/bin/pip install -r $root/requirements.txt" >&2
    exit 1
fi

args=()
case "${1:-synthetic}" in
    synthetic)
        args=(--source synthetic)
        shift || true
        ;;
    scpi)
        shift
        if [[ $# -lt 1 || "$1" == -* ]]; then
            echo "error: 'scpi' needs the scope IP, e.g. $0 scpi 192.168.23.20" >&2
            exit 2
        fi
        args=(--source scpi --host "$1")
        shift
        ;;
    stream)
        args=(--source stream)
        shift
        ;;
    tap)
        shift
        if [[ $# -lt 1 || "$1" == -* ]]; then
            echo "error: 'tap' needs the scope IP, e.g. $0 tap 172.30.188.217" >&2
            exit 2
        fi
        args=(--tap "$1")
        shift
        ;;
    -*)
        # no source given, just options -- let fft_gui.py apply its own default
        ;;
    *)
        echo "error: unknown source '${1}' (want: synthetic, scpi, stream)" >&2
        exit 2
        ;;
esac

exec "$py" "$root/spectrum/fft_gui.py" "${args[@]}" "$@"

#!/usr/bin/env bash
# Start the live FFT spectrum demo using the local .venv.
#
# Usage:  ./run_fft.sh [source] [extra fft_gui.py args...]
#
#   ./run_fft.sh                        pick source + scope IP in a dialog,
#                                       defaulting to last time's -- no
#                                       arguments needed for normal use
#
# Or name a source directly:
#   ./run_fft.sh tap <scope-ip>         live off the scope, fastest, ~16 fps
#   ./run_fft.sh scpi <scope-ip>        live over plain SCPI, slower
#   ./run_fft.sh synthetic              synthetic signal, no scope needed
#   ./run_fft.sh stream [--port 5560]   listen for a tap started elsewhere
#
# The 'tap' source runs its capture loop inside the scope app (no SCPI per
# frame) and streams every enabled channel.  It changes the running scope in
# these ways, all restored when the demo exits:
#   * its waveform redraw is paused (that plot thread holds an A72 at ~99%,
#     and the scope's screen holds its last trace)          --no-tap-quiet-ui
#   * logd is stopped: the scope's own logging is the largest non-app CPU
#     consumer while streaming, worth 0.62 of a core           --tap-keep-logd
#   * optionally, the ADC settling wait in the arm path is cut 20 -> 10 ms --
#     currently OFF by default (fft_gui.py)              --tap-keep-adc-sleep
# Measured 2026-09-13 over USB gigabit: one channel 16.2 fps at 2 ms/div 1 Mpt
# (19.9 at 100 us/div 10 k), CH1+CH2 8.7 fps.  Nothing is logged on the scope
# while it streams.
#
# Any further arguments are passed straight through to fft_gui.py, so
# --average, --window, --points, --headless, --run-seconds etc. all work.
#
# The measurement controls are in the window, grouped as an analyser's soft keys
# (FREQ / AMPT / BW-DET / MARKER / VIEW).  --detector picks the bin-to-pixel
# reduction (+peak default; use rms for any noise or power number), and
# --ref-level / --db-per-div pin the amplitude axis instead of auto-scaling it.
#
# A log-scale strip of the last 300 frame intervals sits under the spectrum
# (--no-timing-strip hides it).  --timing-report prints a per-frame breakdown
# on exit, and --timing-csv PATH keeps the raw rows.
#
set -euo pipefail

# Qt prints
#   "Warning: Ignoring XDG_SESSION_TYPE=wayland on Gnome..."
# on every launch of a GNOME Wayland session.  It is cosmetic -- and it is
# printed whatever QT_QPA_PLATFORM is set to, including wayland itself, so
# choosing a platform does not silence it.  Qt only reads this variable as a
# hint; the actual connection comes from DISPLAY/WAYLAND_DISPLAY, so dropping
# it for the child is safe and leaves platform selection alone.
unset XDG_SESSION_TYPE

# Pin the Qt binding.  pyqtgraph picks one from whatever is installed, and the
# choice is not cosmetic: on PyQt5, qt-material 2.17 fails to register its icon
# resources (its add_fonts() throws "name 'QFontDatabase' is not defined"), so
# every checkbox and radio button renders as a bare label with no indicator and
# Qt logs a failed SVG open on each repaint.  PyQt6 loads them correctly.
: "${PYQTGRAPH_QT_LIB:=PyQt6}"
export PYQTGRAPH_QT_LIB

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py="$root/.venv/bin/python"

usage() { sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

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
case "${1:-choose}" in
    choose)
        # No arguments at all: ask for source and IP in a dialog, defaulting
        # to whatever was used last time.
        args=(--choose)
        ;;
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
        if [[ $# -ge 1 && "$1" != -* ]]; then
            args=(--tap "$1")
            shift
        else
            # No IP given: ask for it in a dialog, prefilled with last time's.
            args=(--choose)
        fi
        ;;
    -*)
        # Options but no source: still no source, so still ask.  Falling
        # through to fft_gui's own default here is how `./run_fft.sh --headless`
        # silently ran the synthetic generator instead of the scope.
        args=(--choose)
        ;;
    *)
        echo "error: unknown source '${1}' (want: synthetic, scpi, stream, tap)" >&2
        exit 2
        ;;
esac

exec "$py" "$root/spectrum/fft_gui.py" "${args[@]}" "$@"

#!/usr/bin/env bash
# Cross-compile the in-app tap for the scope (aarch64).
#
# Only libmhotap.so is built here.  mhostreamd -- the older standalone
# streaming daemon -- stays in the mho-speed-patch repo; the tap replaced it.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${ANDROID_NDK:=$HOME/opt/android-ndk-r27c}"
ndk="$ANDROID_NDK/toolchains/llvm/prebuilt/linux-x86_64/bin"
[[ -d "$ndk" ]] || { echo "NDK not found at $ANDROID_NDK" >&2; exit 1; }
"$ndk/aarch64-linux-android30-clang" -O2 -shared -fPIC -pthread \
    "$here/libmhotap.c" -o "$here/libmhotap.so"
echo "built: $here/libmhotap.so"

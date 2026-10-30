#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$ROOT/build/qemu"
SRC="$WORK/qemu-11.1.3"
BUILD="$WORK/build-ios-tci"
PREFIX="$WORK/ios-prefix"
SDK="$(xcrun --sdk iphoneos --show-sdk-path)"
MESON="${ASTER_MESON:-$WORK/build-tci5/pyvenv/bin/meson}"
test -x "$MESON" || { echo 'Set ASTER_MESON to an existing Meson executable. No tools were installed.' >&2; exit 1; }
bash "$ROOT/tools/qemu/prepare_sources.sh"
test "$(cat "$SRC/VERSION")" = 11.1.3
python3 "$ROOT/tools/qemu/patch_ios.py" "$SRC"
test -f "$PREFIX/lib/libglib-2.0.a" || { echo 'Run tools/build_qemu_ios_deps.sh first.' >&2; exit 1; }
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig" PKG_CONFIG_PATH=""
mkdir -p "$BUILD"
cd "$BUILD"
"$SRC/configure" --target-list=aarch64-softmmu --enable-tcg-interpreter \
  --cc="$(xcrun -f clang)" --cxx="$(xcrun -f clang++)" --cpu=aarch64 \
  --cross-prefix="" --python="$(dirname "$MESON")/python3" \
  --extra-cflags="-target arm64-apple-ios18.0 -isysroot $SDK -O2 -DASTER_EMBEDDED" \
  --extra-ldflags="-target arm64-apple-ios18.0 -isysroot $SDK" \
  --disable-tools --disable-docs --disable-guest-agent --disable-plugins \
  --disable-capstone --disable-gtk --disable-sdl --disable-vnc --disable-opengl \
  --disable-hvf --disable-cocoa --disable-slirp --disable-pixman --disable-werror \
  --disable-curl --disable-gnutls --disable-nettle --disable-gcrypt \
  --disable-gio --disable-zstd --disable-bzip2 --disable-lzfse \
  --disable-vhost-net --disable-rust --disable-module-upgrades \
  -Dpvg=disabled -Dlibssh=disabled -Dauto_features=disabled
ninja -j"${ASTER_BUILD_JOBS:-4}" qemu-system-aarch64-unsigned
python3 "$ROOT/tools/qemu/package_ios.py"

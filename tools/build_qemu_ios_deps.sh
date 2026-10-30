#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$ROOT/build/qemu"
SRC="$WORK/ios-sources"
PREFIX="$WORK/ios-prefix"
MESON="${ASTER_MESON:-$WORK/build-tci5/pyvenv/bin/meson}"
SDK="$(xcrun --sdk iphoneos --show-sdk-path)"
JOBS="${ASTER_BUILD_JOBS:-4}"
mkdir -p "$SRC" "$PREFIX" "$WORK/ios-deps"
test -x "$MESON" || { echo "Set ASTER_MESON to an existing Meson executable." >&2; exit 1; }

fetch() {
  local name="$1" url="$2" hash="$3"
  test -f "$SRC/$name" || curl -fL --max-time 120 "$url" -o "$SRC/$name"
  echo "$hash  $SRC/$name" | shasum -a 256 -c -
}
fetch glib-2.84.4.tar.xz https://download.gnome.org/sources/glib/2.84/glib-2.84.4.tar.xz 8a9ea10943c36fc117e253f80c91e477b673525ae45762942858aef57631bb90
fetch libffi-3.4.8.tar.gz https://github.com/libffi/libffi/releases/download/v3.4.8/libffi-3.4.8.tar.gz bc9842a18898bfacb0ed1252c4febcc7e78fa139fd27fdc7a3e30d9d9356119b
fetch pcre2-10.45.tar.bz2 https://github.com/PCRE2Project/pcre2/releases/download/pcre2-10.45/pcre2-10.45.tar.bz2 21547f3516120c75597e5b30a992e27a592a31950b5140e7b8bfde3f192033c4
for archive in glib-2.84.4.tar.xz libffi-3.4.8.tar.gz pcre2-10.45.tar.bz2; do
  tar xf "$SRC/$archive" -C "$SRC"
done
fetch proxy-libintl.tar.gz https://github.com/frida/proxy-libintl/archive/33934de09af6a6627eb44e310a8079df009abdbb.tar.gz 4ab5af85ec6124cb526a15c58bbc0877d241807e30855779808dbf9292d861aa
if [ ! -d "$SRC/glib-2.84.4/subprojects/proxy-libintl" ]; then
  tar xf "$SRC/proxy-libintl.tar.gz" -C "$SRC/glib-2.84.4/subprojects"
  mv "$SRC/glib-2.84.4/subprojects/proxy-libintl-33934de09af6a6627eb44e310a8079df009abdbb" "$SRC/glib-2.84.4/subprojects/proxy-libintl"
fi
export CC="$(xcrun -f clang)" CXX="$(xcrun -f clang++)"
export CFLAGS="-target arm64-apple-ios18.0 -isysroot $SDK -O2"
export CXXFLAGS="$CFLAGS" LDFLAGS="-target arm64-apple-ios18.0 -isysroot $SDK"
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig" PKG_CONFIG_PATH=""
for dep in libffi-3.4.8 pcre2-10.45; do
  mkdir -p "$WORK/ios-deps/$dep"
  (
    cd "$WORK/ios-deps/$dep"
    if [ "$dep" = libffi-3.4.8 ]; then
      "$SRC/$dep/configure" --host=aarch64-apple-darwin --prefix="$PREFIX" --disable-shared --enable-static --disable-exec-static-tramp
    else
      "$SRC/$dep/configure" --host=aarch64-apple-darwin --prefix="$PREFIX" --disable-shared --enable-static --disable-jit --disable-pcre2grep --disable-pcre2test
    fi
    make -j"$JOBS"
    make install
  )
done
cat > "$WORK/ios-deps/cross.ini" <<EOF
[binaries]
c = ['$CC', '-target', 'arm64-apple-ios18.0', '-isysroot', '$SDK']
cpp = ['$CXX', '-target', 'arm64-apple-ios18.0', '-isysroot', '$SDK']
ar = '$(xcrun -f ar)'
strip = '$(xcrun -f strip)'
pkg-config = '$(command -v pkg-config)'
[host_machine]
system = 'darwin'
cpu_family = 'aarch64'
cpu = 'arm64'
endian = 'little'
[properties]
needs_exe_wrapper = true
EOF
"$MESON" setup "$WORK/ios-deps/glib" "$SRC/glib-2.84.4" \
  --cross-file "$WORK/ios-deps/cross.ini" --prefix "$PREFIX" --libdir lib \
  --default-library static --buildtype release --wrap-mode nodownload \
  -Dtests=false -Dintrospection=disabled -Dnls=disabled -Dlibmount=disabled \
  -Dselinux=disabled -Dxattr=false -Dman-pages=disabled -Dsysprof=disabled
ninja -C "$WORK/ios-deps/glib" -j"$JOBS" glib/libglib-2.0.a gmodule/libgmodule-2.0.a subprojects/proxy-libintl/libintl.a
python3 - "$SRC" "$WORK/ios-deps/glib" "$PREFIX" <<'PY'
from pathlib import Path
import shutil
import sys
src, build, prefix = map(Path, sys.argv[1:])
include = prefix / 'include/glib-2.0'
for base in [src / 'glib-2.84.4', build]:
    for component in ['glib', 'gmodule']:
        for header in (base / component).rglob('*.h'):
            target = include / header.relative_to(base)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(header, target)
    for name in ['glib/glib.h', 'glib/glib-unix.h', 'gmodule/gmodule.h']:
        header = base / name
        if header.is_file():
            include.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(header, include / header.name)
config = prefix / 'lib/glib-2.0/include'
config.mkdir(parents=True, exist_ok=True)
shutil.copyfile(build / 'glib/glibconfig.h', config / 'glibconfig.h')
for name in ['glib-2.0', 'gmodule-2.0', 'gmodule-export-2.0', 'gmodule-no-export-2.0', 'gthread-2.0']:
    shutil.copyfile(build / f'meson-private/{name}.pc', prefix / f'lib/pkgconfig/{name}.pc')
for archive in ['glib/libglib-2.0.a', 'gmodule/libgmodule-2.0.a', 'subprojects/proxy-libintl/libintl.a']:
    shutil.copyfile(build / archive, prefix / 'lib' / Path(archive).name)
PY

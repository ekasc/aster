#!/usr/bin/env bash
# Reproduce the host TCI build that Phase 2 step 1 used.
#
# This builds QEMU's aarch64 system emulator with the TCI interpreter backend
# (no executable code cache) using only libraries already on the machine.
# It is the same configuration that will be cross-compiled for iOS.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/build/qemu/qemu-11.1.3"
BUILD="$ROOT/build/qemu/build-host-tci"
VERSION="11.1.3"

if [ ! -d "$SRC" ]; then
  mkdir -p "$ROOT/build/qemu"
  cd "$ROOT/build/qemu"
  curl -sSLO "https://download.qemu.org/qemu-$VERSION.tar.xz"
  tar xf "qemu-$VERSION.tar.xz"
fi

# Homebrew keeps libffi and pixman out of the default search path.
export PKG_CONFIG_PATH="/opt/homebrew/opt/libffi/lib/pkgconfig:/opt/homebrew/opt/glib/lib/pkgconfig:/opt/homebrew/opt/pixman/lib/pkgconfig:/opt/homebrew/opt/pcre2/lib/pkgconfig"

mkdir -p "$BUILD"
cd "$BUILD"
"$SRC/configure" \
  --target-list=aarch64-softmmu \
  --enable-tcg-interpreter \
  --disable-tools --disable-docs --disable-guest-agent --disable-plugins \
  --disable-capstone --disable-gtk --disable-sdl --disable-vnc --disable-opengl \
  --disable-werror -Dpvg=disabled -Dlibssh=disabled \
  --extra-cflags="-I/opt/homebrew/opt/libffi/include" \
  --extra-ldflags="-L/opt/homebrew/opt/libffi/lib" \
  --prefix="$BUILD/install"
ninja -j"$(sysctl -n hw.ncpu)"

cat <<EOF

Built: $BUILD/qemu-system-aarch64

Boot a Linux kernel under the interpreter:

  cd $ROOT/build/qemu
  curl -sSLO https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/aarch64/netboot/vmlinuz-virt
  curl -sSLO https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/aarch64/netboot/initramfs-virt
  $BUILD/qemu-system-aarch64 -M virt -cpu max -m 512 -nographic \\
    -kernel vmlinuz-virt -initrd initramfs-virt -append "console=ttyAMA0"

Confirmed with 11.1.3: Linux 6.6.134 reaches the initramfs shell.
EOF

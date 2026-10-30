#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="$ROOT/build/qemu"
mkdir -p "$WORK/images"
fetch() {
  local path="$1" url="$2" hash="$3"
  test -f "$path" || curl -fL --max-time 180 "$url" -o "$path"
  echo "$hash  $path" | shasum -a 256 -c -
}
fetch "$WORK/qemu-11.1.3.tar.xz" https://download.qemu.org/qemu-11.1.3.tar.xz 5a8289c7940dc77d3873c81a6c29ba3afe1446fc9d36e77fa1658cdbecb84b22
if [ ! -d "$WORK/qemu-11.1.3" ]; then
  tar xf "$WORK/qemu-11.1.3.tar.xz" -C "$WORK"
fi
fetch "$WORK/images/vmlinuz-virt" https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/aarch64/netboot/vmlinuz-virt 573d0eb4713fc7b631e7242a95fa9d048c60365993658b24d5106a69d0a0e7f2
fetch "$WORK/images/initramfs-virt" https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/aarch64/netboot/initramfs-virt 90f5159c2cbf81b5d6ad7b14a314815589a7077d0e8fa4e8aa0403c3fef458ae

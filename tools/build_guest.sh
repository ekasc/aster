#!/usr/bin/env bash
# Build freestanding AArch64 guest payloads into fixed-address ELF images.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/build/guest"
mkdir -p "$OUT"

CLANG="$(xcrun --find clang)"
COMMON=(
  --target=aarch64-none-elf
  -O2
  -ffreestanding
  -fno-builtin
  -fno-stack-protector
  -fno-pic
  -mcmodel=small
  -mgeneral-regs-only
  -mno-outline-atomics
  -fno-asynchronous-unwind-tables
  -fno-unwind-tables
  -Wall
  -Wextra
  -Wno-unused-parameter
)

"$CLANG" "${COMMON[@]}" -c "$ROOT/guest/runtime.c" -o "$OUT/runtime.o"

for src in hello threads demo fp fault_unmapped fault_rodata; do
  EXTRA=()
  # fp.c must be allowed to use FP/SIMD registers; the other payloads are
  # compiled general-registers-only so any accidental vectorisation shows up as
  # a link-time failure instead of a silent interpreter fault.
  if [ "$src" = "fp" ]; then
    FILTERED=()
    for flag in "${COMMON[@]}"; do
      [ "$flag" = "-mgeneral-regs-only" ] || FILTERED+=("$flag")
    done
    EXTRA=("${FILTERED[@]}")
  else
    EXTRA=("${COMMON[@]}")
  fi
  "$CLANG" "${EXTRA[@]}" -c "$ROOT/guest/$src.c" -o "$OUT/$src.o"
  python3 "$ROOT/tools/elflink.py" \
    --entry _start --base 0x40000000 \
    --map "$OUT/$src.map" \
    -o "$OUT/$src.elf" "$OUT/runtime.o" "$OUT/$src.o"
done

echo "guest payloads in $OUT"

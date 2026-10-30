#!/usr/bin/env bash
# Build and run the host-side correctness suite and the measurement harness.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/build/host"
mkdir -p "$OUT"

CXX="clang++"
CORE=(
  "$ROOT/core/src/memory.cpp"
  "$ROOT/core/src/cpu.cpp"
  "$ROOT/core/src/elf.cpp"
  "$ROOT/core/src/machine.cpp"
  "$ROOT/core/src/platform.cpp"
)
FLAGS=(-std=c++17 -O2 -g -Wall -Wextra -Wno-unused-parameter -I "$ROOT/core/include")

"$CXX" "${FLAGS[@]}" "${CORE[@]}" "$ROOT/core/tests/host_tests.cpp" \
  -o "$OUT/aster_tests" -lpthread
"$CXX" "${FLAGS[@]}" "${CORE[@]}" "$ROOT/core/tests/bench.cpp" \
  -o "$OUT/aster_bench" -lpthread

"$ROOT/tools/build_guest.sh"
"$OUT/aster_tests" "$ROOT/build/guest"
echo
"$OUT/aster_bench" "$ROOT/build/guest"

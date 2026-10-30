#pragma once

#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

#include "aster/memory.h"
#include "aster/types.h"

namespace aster {

// Minimal ELF64/AArch64 loader for flat, fixed-address guest images.
//
// Phase 1 images are ET_EXEC with a fixed link address inside guest RAM. The
// Phase 2 engine boots a Linux kernel Image instead, so no dynamic loader and
// no PIE support is needed here.
struct ElfImage {
  uint64_t entry = 0;
  uint64_t load_low = 0;
  uint64_t load_high = 0;
  std::string interpreter;  // PT_INTERP contents, if any (unexpected)
  bool ok = false;
  std::string error;
};

ElfImage loadElf64AArch64(GuestMemory& memory, const uint8_t* data, size_t size);

}  // namespace aster

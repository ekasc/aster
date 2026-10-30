// Aster — Android testing runtime for iOS.
// Portable guest-execution core. No platform frameworks in this header set.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace aster {

// Guest physical address where the machine's RAM is based. Keeping the guest
// image at 1 GiB leaves the low 4 KiB..1 GiB range free for device MMIO, which
// is the layout the Phase 2 QEMU "virt" board also uses.
constexpr uint64_t kRamBase = 0x40000000ULL;

constexpr uint32_t kGuestPageSize = 4096;

enum class Access : uint8_t { Read, Write, Execute };

enum class FaultKind : uint8_t {
  None,
  Unmapped,
  Permissions,
  Unaligned,        // reserved for future MMU work
  UndefinedInstruction,
  Hypercall,
  HostAbort,
  StackOverflow,
  ThreadFailed,
};

struct Fault {
  FaultKind kind = FaultKind::None;
  uint64_t pc = 0;
  uint64_t address = 0;
  uint32_t instruction = 0;
  size_t length = 0;
  Access access = Access::Read;
  std::string detail;

  bool ok() const { return kind == FaultKind::None; }
};

inline const char* faultKindName(FaultKind k) {
  switch (k) {
    case FaultKind::None: return "none";
    case FaultKind::Unmapped: return "unmapped";
    case FaultKind::Permissions: return "permissions";
    case FaultKind::Unaligned: return "unaligned";
    case FaultKind::UndefinedInstruction: return "undefined-instruction";
    case FaultKind::Hypercall: return "bad-hypercall";
    case FaultKind::HostAbort: return "host-abort";
    case FaultKind::StackOverflow: return "stack-overflow";
    case FaultKind::ThreadFailed: return "thread-failed";
  }
  return "unknown";
}

}  // namespace aster

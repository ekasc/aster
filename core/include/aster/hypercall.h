// Guest <-> host hypercall ABI.
//
// Guest code executes `hvc #0`. Registers:
//   x0 = call id (see HypercallId)
//   x1..x5 = arguments
//   x0 = result, x1 = auxiliary result
//
// On iOS the host app is a signed, sandboxed process. HVC is a synchronous
// trap into Aster's interpreter; it is not a kernel facility and needs no
// entitlement.
#pragma once

#include <cstdint>

namespace aster {

enum HypercallId : uint32_t {
  kHyperPing = 0,       // -> x0 = kHyperMagic
  kHyperWrite = 1,      // fd, ptr, len -> x0 = bytes written
  kHyperExit = 2,       // code -> does not return
  kHyperTicks = 3,      // -> x0 = nanoseconds since machine start
  kHyperSpawn = 4,      // entry, arg, stack_top -> x0 = tid (0 = failure)
  kHyperYield = 5,      // -> x0 = 0
  kHyperThreadExit = 6, // code -> does not return
  kHyperRead = 7,       // fd, ptr, len -> x0 = bytes read (stdin unsupported: 0)
  kHyperSleepNs = 8,    // ns -> x0 = 0
  kHyperDebug = 9,      // ptr, len -> x0 = 0, writes to host debug channel
  kHyperCpuCount = 10,  // -> x0 = guest-visible cpu count
};

constexpr uint32_t kHyperMagic = 0x41535452;  // "ASTR"

constexpr int kFdStdin = 0;
constexpr int kFdStdout = 1;
constexpr int kFdStderr = 2;
constexpr int kFdDebug = 3;

}  // namespace aster

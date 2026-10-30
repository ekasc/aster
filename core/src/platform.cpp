#include "aster/machine.h"

#if defined(__APPLE__)
#include <mach/mach.h>
#endif

namespace aster {

HostMemory hostMemoryUsage() {
  HostMemory out;
#if defined(__APPLE__)
  mach_task_basic_info_data_t info;
  mach_msg_type_number_t count = MACH_TASK_BASIC_INFO_COUNT;
  const kern_return_t kr = task_info(mach_task_self(), MACH_TASK_BASIC_INFO,
                                     reinterpret_cast<task_info_t>(&info), &count);
  if (kr == KERN_SUCCESS) {
    out.resident_bytes = info.resident_size;
    out.peak_resident_bytes = info.resident_size_max;
  }
#endif
  return out;
}

}  // namespace aster

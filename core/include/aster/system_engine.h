#pragma once
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    double wall_seconds;
    double userspace_seconds;
    double cpu_percent;
    uint64_t peak_rss_bytes;
    int reached_userspace;
    int cancelled;
    int timed_out;
} aster_system_result;

/* Single-shot per app process. run blocks its caller, never the UIKit thread.
 * RAM is restricted to 128..768 MiB; timeout to 1..600 seconds.
 * Serial output is written verbatim to log_path and the process console.
 * Cancellation is cooperative, including requests made during qemu_init.
 */
int aster_system_run(const char *kernel, const char *initramfs,
                     const char *log_path, unsigned ram_mib,
                     unsigned timeout_seconds, aster_system_result *result);
void aster_system_cancel(void);

#ifdef __cplusplus
}
#endif

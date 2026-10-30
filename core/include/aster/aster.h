// Stable C interface between the iOS application (Swift) and the Aster core.
//
// The ABI is plain C so the same header works from Swift, Objective-C++ and the
// host test harness. No C++ types cross the boundary.
#ifndef ASTER_ASTER_H
#define ASTER_ASTER_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define ASTER_ABI_VERSION 1u

typedef void (*aster_console_callback)(const char* bytes, size_t length,
                                       void* user);

typedef struct aster_options {
  uint32_t abi_version;
  uint32_t cpu_count;
  uint64_t ram_size;
  uint64_t max_instructions;
  aster_console_callback console;
  void* user;
} aster_options;

enum aster_fault_kind {
  ASTER_FAULT_NONE = 0,
  ASTER_FAULT_UNMAPPED = 1,
  ASTER_FAULT_PERMISSIONS = 2,
  ASTER_FAULT_UNALIGNED = 3,
  ASTER_FAULT_UNDEFINED_INSTRUCTION = 4,
  ASTER_FAULT_HYPERCALL = 5,
  ASTER_FAULT_HOST_ABORT = 6,
  ASTER_FAULT_STACK_OVERFLOW = 7,
  ASTER_FAULT_THREAD_FAILED = 8,
};

typedef struct aster_result {
  int32_t ok;
  int32_t exit_code;
  int32_t fault_kind;
  uint32_t threads_created;
  uint64_t fault_pc;
  uint64_t fault_address;
  uint32_t fault_instruction;
  uint32_t reserved;
  uint64_t instructions;
  uint64_t wall_ns;
  uint64_t guest_ram_bytes;
  uint64_t peak_rss;
  uint64_t current_rss;
  char message[512];
} aster_result;

void aster_default_options(aster_options* out);

// Runs one ELF image to completion (or fault) inside the caller's process.
// Returns 0 when the image executed to a clean exit, -1 on load/argument
// failure, and leaves the guest fault description in `out`.
int aster_run_elf(const uint8_t* data, size_t size,
                  const aster_options* options, aster_result* out);

// Measured execution-policy probe. These calls do not execute the mapping:
// they record what the kernel allows a signed app process to request. On stock
// iOS the JIT and RWX requests are expected to fail; the RW->RX request may
// succeed at the mmap/mprotect layer while still being unexecutable because the
// pages carry no code signature.
typedef struct aster_exec_policy {
  int32_t map_jit_result;   // 0 on success, -1 on failure
  int32_t map_jit_errno;
  int32_t rwx_result;
  int32_t rwx_errno;
  int32_t rw_then_rx_result;
  int32_t rw_then_rx_errno;
  uint32_t host_page_size;
} aster_exec_policy;

void aster_probe_execution_policy(aster_exec_policy* out);

// Attempts to execute one unsigned, RX-mapped page containing `mov w0,#1; ret`.
// Returns 1 if the instruction executed. On stock iOS this is expected to
// terminate the process in the code-signing fault path; that termination is the
// result, and callers must treat a non-return as evidence rather than a bug.
int aster_probe_unsigned_execution(void);

const char* aster_engine_name(void);
uint32_t aster_guest_page_size(void);
uint32_t aster_host_page_size(void);

#ifdef __cplusplus
}
#endif

#endif  // ASTER_ASTER_H

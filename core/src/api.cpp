#include "aster/aster.h"

#include <libkern/OSCacheControl.h>
#include <sys/mman.h>
#include <unistd.h>

#include <cerrno>

#include <cstring>
#include <new>

#include "aster/machine.h"

namespace {

int toPublic(aster::FaultKind kind) {
  switch (kind) {
    case aster::FaultKind::None: return ASTER_FAULT_NONE;
    case aster::FaultKind::Unmapped: return ASTER_FAULT_UNMAPPED;
    case aster::FaultKind::Permissions: return ASTER_FAULT_PERMISSIONS;
    case aster::FaultKind::Unaligned: return ASTER_FAULT_UNALIGNED;
    case aster::FaultKind::UndefinedInstruction:
      return ASTER_FAULT_UNDEFINED_INSTRUCTION;
    case aster::FaultKind::Hypercall: return ASTER_FAULT_HYPERCALL;
    case aster::FaultKind::HostAbort: return ASTER_FAULT_HOST_ABORT;
    case aster::FaultKind::StackOverflow: return ASTER_FAULT_STACK_OVERFLOW;
    case aster::FaultKind::ThreadFailed: return ASTER_FAULT_THREAD_FAILED;
  }
  return ASTER_FAULT_NONE;
}

void copyMessage(char* dst, size_t size, const std::string& src) {
  if (size == 0) return;
  const size_t n = src.size() < size - 1 ? src.size() : size - 1;
  std::memcpy(dst, src.data(), n);
  dst[n] = '\0';
}

}  // namespace

extern "C" {

void aster_default_options(aster_options* out) {
  if (out == nullptr) return;
  std::memset(out, 0, sizeof(*out));
  out->abi_version = ASTER_ABI_VERSION;
  out->cpu_count = 4;
  out->ram_size = 256ull * 1024 * 1024;
}

int aster_run_elf(const uint8_t* data, size_t size,
                  const aster_options* options, aster_result* out) {
  if (out == nullptr) return -1;
  std::memset(out, 0, sizeof(*out));
  if (data == nullptr || size == 0) {
    copyMessage(out->message, sizeof(out->message), "empty guest image");
    return -1;
  }
  if (options != nullptr && options->abi_version != ASTER_ABI_VERSION) {
    copyMessage(out->message, sizeof(out->message), "ABI version mismatch");
    return -1;
  }

  aster::MachineOptions native;
  if (options != nullptr) {
    native.cpu_count = options->cpu_count ? options->cpu_count : 1;
    native.ram_size = options->ram_size ? static_cast<size_t>(options->ram_size)
                                        : (256ull * 1024 * 1024);
    native.max_instructions = options->max_instructions;
    if (options->console != nullptr) {
      aster_console_callback cb = options->console;
      void* user = options->user;
      native.console_sink = [cb, user](const char* bytes, size_t length) {
        cb(bytes, length, user);
      };
    }
  } else {
    native.cpu_count = 4;
    native.ram_size = 256ull * 1024 * 1024;
  }

  // The machine is heap-allocated: its working set is dominated by guest RAM,
  // and keeping it off the caller's stack keeps the C ABI safe to call from a
  // Swift concurrency task.
  aster::Machine* machine = new (std::nothrow) aster::Machine(native);
  if (machine == nullptr) {
    copyMessage(out->message, sizeof(out->message), "out of memory");
    return -1;
  }

  std::string error;
  int status = 0;
  if (!machine->init(&error)) {
    copyMessage(out->message, sizeof(out->message), error);
    status = -1;
  } else if (!machine->loadElf(data, size, &error)) {
    copyMessage(out->message, sizeof(out->message), error);
    status = -1;
  } else {
    const aster::RunResult result = machine->run();
    out->ok = result.ok ? 1 : 0;
    out->exit_code = result.exit_code;
    out->fault_kind = toPublic(result.fault.kind);
    out->fault_pc = result.fault.pc;
    out->fault_address = result.fault.address;
    out->fault_instruction = result.fault.instruction;
    out->threads_created = result.threads_created;
    out->instructions = result.instructions;
    out->wall_ns = result.wall_ns;
    out->guest_ram_bytes = result.guest_ram_bytes;
    out->peak_rss = result.host_peak_rss;
    out->current_rss = result.host_current_rss;
    copyMessage(out->message, sizeof(out->message),
                result.ok ? result.message
                          : result.message + " | " + result.fault.detail);
    status = result.ok ? 0 : -1;
  }

  delete machine;
  return status;
}

void aster_probe_execution_policy(aster_exec_policy* out) {
  if (out == nullptr) return;
  std::memset(out, 0, sizeof(*out));
  const long page = sysconf(_SC_PAGESIZE);
  out->host_page_size = page > 0 ? static_cast<uint32_t>(page) : 0;
  const size_t size = out->host_page_size ? out->host_page_size : 16384;

#ifndef MAP_JIT
#define MAP_JIT 0x0800
#endif

  errno = 0;
  void* jit = mmap(nullptr, size, PROT_READ | PROT_WRITE,
                   MAP_PRIVATE | MAP_ANON | MAP_JIT, -1, 0);
  out->map_jit_errno = errno;
  out->map_jit_result = (jit == MAP_FAILED) ? -1 : 0;
  if (jit != MAP_FAILED) munmap(jit, size);

  errno = 0;
  void* rwx = mmap(nullptr, size, PROT_READ | PROT_WRITE | PROT_EXEC,
                   MAP_PRIVATE | MAP_ANON, -1, 0);
  out->rwx_errno = errno;
  out->rwx_result = (rwx == MAP_FAILED) ? -1 : 0;
  if (rwx != MAP_FAILED) munmap(rwx, size);

  errno = 0;
  void* rw = mmap(nullptr, size, PROT_READ | PROT_WRITE,
                  MAP_PRIVATE | MAP_ANON, -1, 0);
  if (rw == MAP_FAILED) {
    out->rw_then_rx_result = -1;
    out->rw_then_rx_errno = errno;
  } else {
    // Deliberately do not jump to this page. A non-zero result here is the
    // syscall's answer, not proof that executing the page would be permitted.
    errno = 0;
    const int rc = mprotect(rw, size, PROT_READ | PROT_EXEC);
    out->rw_then_rx_errno = errno;
    out->rw_then_rx_result = (rc == 0) ? 0 : -1;
    munmap(rw, size);
  }
}

int aster_probe_unsigned_execution(void) {
  const long page_size = sysconf(_SC_PAGESIZE);
  const size_t size = page_size > 0 ? static_cast<size_t>(page_size) : 16384;
  void* page = mmap(nullptr, size, PROT_READ | PROT_WRITE,
                    MAP_PRIVATE | MAP_ANON, -1, 0);
  if (page == MAP_FAILED) return -1;
  const uint32_t code[2] = {0x52800020u,  // mov w0, #1
                            0xD65F03C0u}; // ret
  std::memcpy(page, code, sizeof(code));
  if (mprotect(page, size, PROT_READ | PROT_EXEC) != 0) {
    munmap(page, size);
    return -2;
  }
  sys_icache_invalidate(page, sizeof(code));
  int (*fn)(void) = reinterpret_cast<int (*)(void)>(page);
  const int result = fn();
  munmap(page, size);
  return result;
}

const char* aster_engine_name(void) {
  return "Aster AArch64 interpreter (no JIT)";
}

uint32_t aster_guest_page_size(void) { return aster::kGuestPageSize; }

uint32_t aster_host_page_size(void) {
  const long size = sysconf(_SC_PAGESIZE);
  return size > 0 ? static_cast<uint32_t>(size) : 0;
}

}  // extern "C"

#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "aster/cpu.h"
#include "aster/elf.h"
#include "aster/memory.h"
#include "aster/types.h"

namespace aster {

struct MachineOptions {
  size_t ram_size = 256ULL * 1024 * 1024;
  uint32_t cpu_count = 4;
  uint64_t max_instructions = 0;  // 0 = unlimited
  bool trace_instructions = false;
  std::function<void(const char*, size_t)> console_sink;
};

struct ThreadReport {
  uint32_t tid = 0;
  uint64_t instructions = 0;
  int exit_code = 0;
  bool faulted = false;
  Fault fault;
};

struct RunResult {
  bool ok = false;
  int exit_code = 0;
  Fault fault;
  ElfImage elf;
  uint64_t instructions = 0;
  uint64_t wall_ns = 0;
  uint64_t guest_ram_bytes = 0;
  uint64_t host_peak_rss = 0;
  uint64_t host_current_rss = 0;
  uint32_t threads_created = 0;
  std::vector<ThreadReport> threads;
  std::string console;
  std::string message;
};

// Full Phase 1 machine: guest RAM, one interpreter per guest thread, and the
// hypercall surface that replaces the Linux syscall ABI for test payloads.
//
// Deliberately absent: MMU, exception vectors, device emulation. Those arrive
// with the QEMU TCG engine in Phase 2. Keeping them out here is what makes the
// Phase 1 result a measurement rather than a claim.
class Machine final : public CpuEnvironment {
 public:
  explicit Machine(MachineOptions options);
  ~Machine() override;

  Machine(const Machine&) = delete;
  Machine& operator=(const Machine&) = delete;

  bool init(std::string* error);
  bool loadElf(const uint8_t* data, size_t size, std::string* error);
  RunResult run();

  const std::string& console() const { return console_; }
  void clearConsole();

  GuestMemory& memory() { return memory_; }
  const ElfImage& elf() const { return elf_; }

  // CpuEnvironment.
  bool hyperWrite(int fd, uint64_t ptr, size_t len, uint64_t* written,
                  Fault* fault) override;
  bool hyperRead(int fd, uint64_t ptr, size_t len, uint64_t* read,
                 Fault* fault) override;
  void hyperExit(int code) override;
  uint64_t hyperTicks() override;
  uint32_t hyperSpawn(uint64_t entry, uint64_t arg, uint64_t stack_top,
                      Fault* fault) override;
  void hyperYield() override;
  void hyperSleep(uint64_t ns) override;
  void hyperDebug(const char* data, size_t len) override;
  uint32_t hyperCpuCount() override;

  struct GuestThread {
    uint32_t tid = 0;
    CPU cpu;
    std::thread thread;
    std::atomic<bool> finished{false};
    std::atomic<bool> started{false};
    int exit_code = 0;
    Fault fault;
  };

 private:
  void threadLoop(GuestThread* thread);
  void appendConsole(const char* data, size_t len);

  MachineOptions options_;
  GuestMemory memory_;
  ElfImage elf_;
  std::vector<std::unique_ptr<GuestThread>> threads_;
  std::mutex threads_mutex_;
  std::mutex console_mutex_;
  std::string console_;
  std::atomic<uint32_t> next_tid_{1};
  std::atomic<uint32_t> threads_created_{0};
  std::atomic<uint64_t> total_instructions_{0};
  std::atomic<bool> stop_{false};
  std::atomic<bool> running_{false};
  uint64_t start_ns_ = 0;
  uint64_t stack_top_ = 0;
};

// Host process memory, measured rather than inferred.
struct HostMemory {
  uint64_t resident_bytes = 0;
  uint64_t peak_resident_bytes = 0;
};
HostMemory hostMemoryUsage();

}  // namespace aster

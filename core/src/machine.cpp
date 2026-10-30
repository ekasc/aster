#include "aster/machine.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <thread>

#include "aster/clock.h"
#include "aster/hypercall.h"

namespace aster {
namespace {

thread_local Machine::GuestThread* g_current = nullptr;

constexpr size_t kConsoleCap = 4u * 1024 * 1024;

}  // namespace

Machine::Machine(MachineOptions options) : options_(std::move(options)) {}

Machine::~Machine() {
  stop_.store(true);
  std::vector<std::unique_ptr<GuestThread>> threads;
  {
    std::lock_guard<std::mutex> lock(threads_mutex_);
    threads.swap(threads_);
  }
  for (auto& thread : threads) {
    if (thread->thread.joinable()) thread->thread.join();
  }
}

bool Machine::init(std::string* error) {
  if (!memory_.init(options_.ram_size, kRamBase)) {
    if (error != nullptr) *error = memory_.error();
    return false;
  }
  stack_top_ = memory_.end_addr() - 64;
  return true;
}

bool Machine::loadElf(const uint8_t* data, size_t size, std::string* error) {
  elf_ = loadElf64AArch64(memory_, data, size);
  if (!elf_.ok) {
    if (error != nullptr) *error = elf_.error;
    return false;
  }
  // Phase 1 images carry no PT_INTERP; a dynamic loader would be a different
  // (and much larger) execution model.
  if (!elf_.interpreter.empty()) {
    if (error != nullptr) *error = "dynamic ELF images are not supported";
    return false;
  }
  return true;
}

void Machine::appendConsole(const char* data, size_t len) {
  if (len == 0) return;
  bool truncated = false;
  {
    std::lock_guard<std::mutex> lock(console_mutex_);
    const size_t room = kConsoleCap > console_.size() ? kConsoleCap - console_.size() : 0;
    if (len > room) {
      truncated = true;
      len = room;
    }
    console_.append(data, len);
  }
  if (options_.console_sink) options_.console_sink(data, len);
  if (truncated) {
    static const char kMsg[] = "\n[aster] console limit reached\n";
    std::lock_guard<std::mutex> lock(console_mutex_);
    console_.append(kMsg, sizeof(kMsg) - 1);
  }
}

void Machine::clearConsole() {
  std::lock_guard<std::mutex> lock(console_mutex_);
  console_.clear();
}

bool Machine::hyperWrite(int fd, uint64_t ptr, size_t len, uint64_t* written,
                         Fault* fault) {
  if (fd != kFdStdout && fd != kFdStderr && fd != kFdDebug) {
    if (fault != nullptr) {
      fault->kind = FaultKind::Hypercall;
      fault->detail = "write to unsupported guest fd " + std::to_string(fd);
    }
    return false;
  }
  if (len == 0) {
    if (written != nullptr) *written = 0;
    return true;
  }
  if (!memory_.rangeOk(ptr, len)) {
    if (fault != nullptr) {
      fault->kind = FaultKind::Unmapped;
      fault->address = ptr;
      fault->length = len;
      fault->access = Access::Read;
    }
    return false;
  }
  appendConsole(reinterpret_cast<const char*>(memory_.host(ptr)), len);
  if (written != nullptr) *written = len;
  return true;
}

bool Machine::hyperRead(int, uint64_t, size_t, uint64_t* read, Fault*) {
  // Phase 1 has no console input device; consumers use the host test interface.
  if (read != nullptr) *read = 0;
  return true;
}

void Machine::hyperExit(int code) {
  if (g_current != nullptr) g_current->exit_code = code;
}

uint64_t Machine::hyperTicks() {
  if (start_ns_ == 0) return 0;
  return nowNanos() - start_ns_;
}

uint32_t Machine::hyperSpawn(uint64_t entry, uint64_t arg, uint64_t stack_top,
                             Fault* fault) {
  if (stop_.load()) {
    if (fault != nullptr) {
      fault->kind = FaultKind::HostAbort;
      fault->detail = "machine is shutting down";
    }
    return 0;
  }
  if (!memory_.rangeOk(entry, 4) || !memory_.rangeOk(stack_top - 16, 16)) {
    if (fault != nullptr) {
      fault->kind = FaultKind::Unmapped;
      fault->address = entry;
      fault->detail = "thread entry or stack outside guest RAM";
    }
    return 0;
  }
  if ((stack_top & 0xF) != 0) {
    if (fault != nullptr) {
      fault->kind = FaultKind::HostAbort;
      fault->detail = "guest stack must be 16-byte aligned";
    }
    return 0;
  }

  auto thread = std::make_unique<GuestThread>();
  thread->tid = next_tid_.fetch_add(1);
  thread->cpu.attach(&memory_, this);
  thread->cpu.reset(entry, stack_top);
  thread->cpu.setArg(arg);
  GuestThread* raw = thread.get();

  {
    std::lock_guard<std::mutex> lock(threads_mutex_);
    threads_.push_back(std::move(thread));
  }
  threads_created_.fetch_add(1);
  raw->started.store(true);
  raw->thread = std::thread(&Machine::threadLoop, this, raw);
  return raw->tid;
}

void Machine::hyperYield() {
  std::this_thread::yield();
}

void Machine::hyperSleep(uint64_t ns) {
  const uint64_t deadline = nowNanos() + ns;
  while (!stop_.load()) {
    const uint64_t now = nowNanos();
    if (now >= deadline) break;
    const uint64_t left = deadline - now;
    std::this_thread::sleep_for(
        std::chrono::nanoseconds(std::min<uint64_t>(left, 1000000)));
  }
}

void Machine::hyperDebug(const char* data, size_t len) { appendConsole(data, len); }

uint32_t Machine::hyperCpuCount() { return options_.cpu_count; }

void Machine::threadLoop(GuestThread* thread) {
  g_current = thread;
  uint64_t local = 0;
  for (;;) {
    if (stop_.load()) {
      thread->exit_code = -1;
      break;
    }
    Fault fault;
    const StepResult result = thread->cpu.step(&fault);
    ++local;
    if ((local & 0x3FF) == 0) {
      if (options_.max_instructions != 0 &&
          thread->cpu.instructions() >= options_.max_instructions) {
        thread->fault.kind = FaultKind::HostAbort;
        thread->fault.pc = thread->cpu.state().pc;
        thread->fault.detail = "instruction budget exhausted";
        break;
      }
    }
    if (result == StepResult::Fault) {
      thread->fault = fault;
      thread->exit_code = -1;
      break;
    }
    if (result == StepResult::Halt) break;
  }
  thread->finished.store(true);
  g_current = nullptr;
}

RunResult Machine::run() {
  RunResult result;
  result.elf = elf_;
  result.guest_ram_bytes = options_.ram_size;
  if (start_ns_ == 0) start_ns_ = nowNanos();
  const uint64_t started = nowNanos();

  GuestThread main_thread;
  main_thread.tid = 1;
  main_thread.cpu.attach(&memory_, this);
  main_thread.cpu.reset(elf_.entry, stack_top_);
  g_current = &main_thread;
  running_.store(true);

  Fault fault;
  for (;;) {
    if (options_.trace_instructions) {
      char line[64];
      std::snprintf(line, sizeof(line), "[trace] pc=%llx insn=%08x\n",
                    static_cast<unsigned long long>(main_thread.cpu.state().pc),
                    main_thread.cpu.lastInstruction());
      appendConsole(line, std::strlen(line));
    }
    const StepResult step = main_thread.cpu.step(&fault);
    if ((main_thread.cpu.instructions() & 0x3FF) == 0 &&
        options_.max_instructions != 0 &&
        main_thread.cpu.instructions() >= options_.max_instructions) {
      fault.kind = FaultKind::HostAbort;
      fault.pc = main_thread.cpu.state().pc;
      fault.detail = "instruction budget exhausted";
      result.fault = fault;
      break;
    }
    if (step == StepResult::Fault) {
      result.fault = fault;
      break;
    }
    if (step == StepResult::Halt) break;
  }

  stop_.store(true);
  running_.store(false);
  g_current = nullptr;

  std::vector<std::unique_ptr<GuestThread>> threads;
  {
    std::lock_guard<std::mutex> lock(threads_mutex_);
    threads.swap(threads_);
  }
  for (auto& thread : threads) {
    if (thread->thread.joinable()) thread->thread.join();
    total_instructions_.fetch_add(thread->cpu.instructions());
    ThreadReport report;
    report.tid = thread->tid;
    report.instructions = thread->cpu.instructions();
    report.exit_code = thread->exit_code;
    report.faulted = !thread->fault.ok();
    report.fault = thread->fault;
    result.threads.push_back(report);
  }

  result.instructions = total_instructions_.load() + main_thread.cpu.instructions();
  result.exit_code = main_thread.fault.ok() ? main_thread.exit_code : -1;
  result.threads_created = threads_created_.load();
  result.wall_ns = nowNanos() - started;

  // Main thread's own report is first.
  ThreadReport main_report;
  main_report.tid = 1;
  main_report.instructions = main_thread.cpu.instructions();
  main_report.exit_code = main_thread.exit_code;
  main_report.faulted = !main_thread.fault.ok();
  main_report.fault = main_thread.fault;
  result.threads.insert(result.threads.begin(), main_report);

  const HostMemory mem = hostMemoryUsage();
  result.host_peak_rss = mem.peak_resident_bytes;
  result.host_current_rss = mem.resident_bytes;
  result.ok = result.fault.ok();
  {
    std::lock_guard<std::mutex> lock(console_mutex_);
    result.console = console_;
  }
  if (result.ok) {
    result.message = "guest exited with code " + std::to_string(result.exit_code);
  } else {
    result.message = std::string("guest fault: ") +
                     faultKindName(result.fault.kind) + " at pc 0x";
    char pc[24];
    std::snprintf(pc, sizeof(pc), "%llx",
                  static_cast<unsigned long long>(result.fault.pc));
    result.message += pc;
    if (!result.fault.detail.empty()) result.message += " (" + result.fault.detail + ")";
  }
  return result;
}

}  // namespace aster

// AArch64 interpreter used by Aster's Phase 1 execution gate.
//
// This is intentionally a *correctness* component, not the final performance
// engine. It exists to prove, on a stock iPhone, that signed application code
// can decode and execute AArch64 guest instructions against guest memory
// without JIT, W^X circumvention, or private entitlements. Phase 2 embeds QEMU
// TCG (TCI interpreter backend) for full-system Android; the same host layer,
// lifecycle and telemetry wrap both engines.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "aster/memory.h"
#include "aster/types.h"

namespace aster {

class CpuEnvironment {
 public:
  virtual ~CpuEnvironment() = default;

  virtual bool hyperWrite(int fd, uint64_t ptr, size_t len, uint64_t* written,
                          Fault* fault) = 0;
  virtual bool hyperRead(int fd, uint64_t ptr, size_t len, uint64_t* read,
                         Fault* fault) = 0;
  virtual void hyperExit(int code) = 0;
  virtual uint64_t hyperTicks() = 0;
  virtual uint32_t hyperSpawn(uint64_t entry, uint64_t arg, uint64_t stack_top,
                              Fault* fault) = 0;
  virtual void hyperYield() = 0;
  virtual void hyperSleep(uint64_t ns) = 0;
  virtual void hyperDebug(const char* data, size_t len) = 0;
  virtual uint32_t hyperCpuCount() = 0;
};

struct CpuState {
  uint64_t x[31] = {};
  uint64_t sp = 0;
  uint64_t pc = 0;
  uint32_t nzcv = 0;  // N=31 Z=30 C=29 V=28
  uint32_t fpcr = 0;
  uint32_t fpsr = 0;
  uint64_t tpidr_el0 = 0;
  uint8_t v[32][16] = {};

  uint64_t pstate() const { return static_cast<uint64_t>(nzcv) << 28; }
  void setPstate(uint64_t p) { nzcv = static_cast<uint32_t>((p >> 28) & 0xF); }
  bool flagN() const { return (nzcv >> 3) & 1; }
  bool flagZ() const { return (nzcv >> 2) & 1; }
  bool flagC() const { return (nzcv >> 1) & 1; }
  bool flagV() const { return nzcv & 1; }
  void setFlags(bool n, bool z, bool c, bool v) {
    nzcv = (n ? 8u : 0u) | (z ? 4u : 0u) | (c ? 2u : 0u) | (v ? 1u : 0u);
  }
};

enum class StepResult : uint8_t { Continue, Halt, Fault };

class CPU {
 public:
  CPU() = default;

  void attach(GuestMemory* memory, CpuEnvironment* env) {
    memory_ = memory;
    env_ = env;
  }

  void reset(uint64_t entry, uint64_t stack_top);
  void setPc(uint64_t pc) { state_.pc = pc; }
  void setArg(uint64_t value) { state_.x[0] = value; }

  StepResult step(Fault* fault);
  StepResult run(Fault* fault, uint64_t max_instructions = 0);

  CpuState& state() { return state_; }
  const CpuState& state() const { return state_; }

  uint64_t instructions() const { return instructions_; }
  uint64_t lastPc() const { return last_pc_; }
  uint32_t lastInstruction() const { return last_instruction_; }

  // Guest-visible register snapshot for tests and crash reports.
  std::string registerDump() const;

  // Decode a single instruction's effect, public for unit tests.
  static bool decodeBitMasks(bool sf, uint32_t n, uint32_t imms, uint32_t immr,
                             bool immediate, uint64_t* wmask, uint64_t* tmask);

 private:
  uint64_t reg(uint32_t index, bool use_sp) const;
  void setReg(uint32_t index, uint64_t value, bool use_sp);
  void setRegWidth(uint32_t index, uint64_t value, bool sf, bool use_sp);

  bool evalCondition(uint32_t cond) const;

  StepResult undefined(Fault* fault, uint32_t insn, const char* what);
  StepResult takeBranch(uint64_t target, bool link);
  StepResult hypercall(uint32_t insn, Fault* fault);
  bool loadStoreAddress(uint32_t insn, uint64_t* addr, bool* writeback,
                        uint64_t* wb_value, Fault* fault);

  GuestMemory* memory_ = nullptr;
  CpuEnvironment* env_ = nullptr;
  CpuState state_{};
  uint64_t instructions_ = 0;
  uint64_t last_pc_ = 0;
  uint32_t last_instruction_ = 0;
  bool halted_ = false;

  // Exclusive monitor: last exclusive load, used by LDXR/STXR.
  bool exclusive_valid_ = false;
  uint64_t exclusive_addr_ = 0;
  uint64_t exclusive_value_ = 0;
  uint32_t exclusive_size_ = 0;
};

}  // namespace aster

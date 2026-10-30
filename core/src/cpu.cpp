#include "aster/cpu.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <mutex>

#include "aster/hypercall.h"

namespace aster {
namespace {

inline uint64_t bits(uint64_t v, unsigned hi, unsigned lo) {
  return (v >> lo) & ((hi - lo == 63) ? ~0ULL : ((1ULL << (hi - lo + 1)) - 1));
}

inline uint64_t signExtend(uint64_t value, unsigned sign_bit) {
  const unsigned shift = 63 - sign_bit;
  return static_cast<uint64_t>(static_cast<int64_t>(value << shift) >> shift);
}

inline uint64_t maskForWidth(bool sf) {
  return sf ? ~0ULL : 0xFFFFFFFFULL;
}

inline uint64_t asr(uint64_t value, unsigned amount, bool sf) {
  if (sf) return static_cast<uint64_t>(static_cast<int64_t>(value) >> amount);
  return static_cast<uint32_t>(static_cast<int32_t>(static_cast<uint32_t>(value)) >> amount);
}

inline uint64_t lsl(uint64_t value, unsigned amount, bool sf) {
  return (value << amount) & maskForWidth(sf);
}

inline uint64_t lsr(uint64_t value, unsigned amount, bool sf) {
  return (value & maskForWidth(sf)) >> amount;
}

inline uint64_t rorWidth(uint64_t value, unsigned amount, unsigned width) {
  amount %= width;
  const uint64_t mask = (width == 64) ? ~0ULL : ((1ULL << width) - 1);
  value &= mask;
  if (amount == 0) return value;
  return ((value >> amount) | (value << (width - amount))) & mask;
}

inline uint64_t shiftedReg(uint64_t value, unsigned type, unsigned amount,
                           bool sf) {
  switch (type) {
    case 0: return lsl(value, amount, sf);
    case 1: return lsr(value, amount, sf);
    case 2: return asr(value, amount, sf);
    case 3: return rorWidth(value, amount, sf ? 64 : 32);
  }
  return value;
}

// ARM ARM DecodeBitMasks, including the separate tmask. The tmask is what
// truncates a bitfield result to the extracted field width; deriving it from
// S alone (a common shortcut) breaks UBFX/SBFX when the field ends at bit 63.
bool decodeBitMasksImpl(bool sf, uint32_t n, uint32_t imms, uint32_t immr,
                        bool immediate, uint64_t* wmask, uint64_t* tmask) {
  const uint32_t combined = (n << 6) | (~imms & 0x3F);
  int len = 0;
  for (int i = 6; i >= 0; --i) {
    if (combined & (1u << i)) {
      len = i;
      break;
    }
  }
  if (len < 1) return false;
  const unsigned esize = 1u << len;
  if (!sf && esize > 32) return false;
  const uint32_t levels = esize - 1;
  const uint32_t s = imms & levels;
  const uint32_t r = immr & levels;
  if (immediate && s == levels) return false;

  const uint32_t diff = (s - r) & levels;
  const uint64_t welem = rorWidth(
      (s + 1 >= 64) ? ~0ULL : ((1ULL << (s + 1)) - 1), r, esize);
  const uint64_t telem = (diff + 1 >= 64) ? ~0ULL : ((1ULL << (diff + 1)) - 1);
  uint64_t w = 0;
  uint64_t t = 0;
  for (unsigned pos = 0; pos < 64; pos += esize) {
    w |= (welem << pos);
    t |= (telem << pos);
  }
  if (!sf) {
    w &= 0xFFFFFFFFULL;
    t &= 0xFFFFFFFFULL;
  }
  *wmask = w;
  *tmask = t;
  return true;
}

// Exclusive monitor serialisation. Guest threads share guest memory; the
// compare-on-store form below is the classic emulator implementation of an
// exclusive monitor and is sufficient for lock-free guest sequences.
std::mutex& exclusiveMutex() {
  static std::mutex m;
  return m;
}

}  // namespace

void CPU::reset(uint64_t entry, uint64_t stack_top) {
  state_ = CpuState{};
  state_.pc = entry;
  state_.sp = stack_top;
  state_.tpidr_el0 = 0;
  instructions_ = 0;
  last_pc_ = entry;
  last_instruction_ = 0;
  halted_ = false;
  exclusive_valid_ = false;
}

uint64_t CPU::reg(uint32_t index, bool use_sp) const {
  if (index == 31) return use_sp ? state_.sp : 0;
  return state_.x[index];
}

void CPU::setReg(uint32_t index, uint64_t value, bool use_sp) {
  if (index == 31) {
    if (use_sp) state_.sp = value;
    return;
  }
  state_.x[index] = value;
}

void CPU::setRegWidth(uint32_t index, uint64_t value, bool sf, bool use_sp) {
  if (!sf) value &= 0xFFFFFFFFULL;
  setReg(index, value, use_sp);
}

bool CPU::evalCondition(uint32_t cond) const {
  const bool n = state_.flagN(), z = state_.flagZ(), c = state_.flagC(),
             v = state_.flagV();
  bool result;
  switch (cond >> 1) {
    case 0b000: result = z; break;                    // EQ
    case 0b001: result = c; break;                    // CS/HS
    case 0b010: result = n; break;                    // MI
    case 0b011: result = v; break;                    // VS
    case 0b100: result = c && !z; break;              // HI
    case 0b101: result = (n == v); break;             // GE
    case 0b110: result = !z && (n == v); break;       // GT
    default:    result = true; break;                 // AL / NV
  }
  // Bit 0 of the condition inverts the base condition. Collapsing on
  // cond >> 1 without applying this inversion makes NE behave like EQ (and
  // every other inverted condition behave as its pair), which silently
  // changes control flow in guest loops.
  return (cond & 1) ? !result : result;
}

StepResult CPU::undefined(Fault* fault, uint32_t insn, const char* what) {
  fault->kind = FaultKind::UndefinedInstruction;
  fault->pc = state_.pc;
  fault->instruction = insn;
  fault->detail = what;
  return StepResult::Fault;
}

bool CPU::decodeBitMasks(bool sf, uint32_t n, uint32_t imms, uint32_t immr,
                         bool immediate, uint64_t* wmask, uint64_t* tmask) {
  return decodeBitMasksImpl(sf, n, imms, immr, immediate, wmask, tmask);
}

std::string CPU::registerDump() const {
  char buf[128];
  std::string out;
  for (int i = 0; i < 31; i += 2) {
    std::snprintf(buf, sizeof(buf), "x%-2d=%016llx x%-2d=%016llx\n", i,
                  static_cast<unsigned long long>(state_.x[i]), i + 1,
                  static_cast<unsigned long long>(state_.x[i + 1]));
    out += buf;
  }
  std::snprintf(buf, sizeof(buf), "sp=%016llx pc=%016llx nzcv=%c%c%c%c\n",
                static_cast<unsigned long long>(state_.sp),
                static_cast<unsigned long long>(state_.pc),
                state_.flagN() ? 'N' : '-', state_.flagZ() ? 'Z' : '-',
                state_.flagC() ? 'C' : '-', state_.flagV() ? 'V' : '-');
  out += buf;
  return out;
}

StepResult CPU::hypercall(uint32_t insn, Fault* fault) {
  const uint32_t id = static_cast<uint32_t>(state_.x[0]);
  switch (id) {
    case kHyperPing:
      state_.x[0] = kHyperMagic;
      state_.x[1] = 0;
      return StepResult::Continue;

    case kHyperWrite:
    case kHyperRead: {
      const int fd = static_cast<int>(state_.x[1]);
      const uint64_t ptr = state_.x[2];
      const uint64_t len = state_.x[3];
      uint64_t done = 0;
      Fault f;
      bool ok = (id == kHyperWrite)
                    ? env_->hyperWrite(fd, ptr, static_cast<size_t>(len), &done, &f)
                    : env_->hyperRead(fd, ptr, static_cast<size_t>(len), &done, &f);
      if (!ok) {
        f.pc = state_.pc;
        f.instruction = insn;
        f.detail = "hypercall buffer fault";
        *fault = f;
        return StepResult::Fault;
      }
      state_.x[0] = done;
      return StepResult::Continue;
    }

    case kHyperExit:
      env_->hyperExit(static_cast<int>(state_.x[1]));
      halted_ = true;
      return StepResult::Halt;

    case kHyperTicks:
      state_.x[0] = env_->hyperTicks();
      return StepResult::Continue;

    case kHyperSpawn: {
      Fault f;
      const uint32_t tid = env_->hyperSpawn(state_.x[1], state_.x[2],
                                           state_.x[3], &f);
      if (tid == 0 && !f.ok()) {
        f.pc = state_.pc;
        f.instruction = insn;
        *fault = f;
        return StepResult::Fault;
      }
      state_.x[0] = tid;
      return StepResult::Continue;
    }

    case kHyperYield:
      env_->hyperYield();
      state_.x[0] = 0;
      return StepResult::Continue;

    case kHyperThreadExit:
      env_->hyperExit(static_cast<int>(state_.x[1]));
      halted_ = true;
      return StepResult::Halt;

    case kHyperSleepNs:
      env_->hyperSleep(state_.x[1]);
      state_.x[0] = 0;
      return StepResult::Continue;

    case kHyperDebug: {
      const uint64_t ptr = state_.x[1];
      const uint64_t len = state_.x[2];
      Fault f;
      bool ok = env_->hyperWrite(kFdDebug, ptr, static_cast<size_t>(len), nullptr, &f);
      if (!ok) {
        f.pc = state_.pc;
        f.instruction = insn;
        *fault = f;
        return StepResult::Fault;
      }
      state_.x[0] = 0;
      return StepResult::Continue;
    }

    case kHyperCpuCount:
      state_.x[0] = env_->hyperCpuCount();
      return StepResult::Continue;

    default:
      fault->kind = FaultKind::Hypercall;
      fault->pc = state_.pc;
      fault->instruction = insn;
      fault->detail = "unknown hypercall id " + std::to_string(id);
      return StepResult::Fault;
  }
}

StepResult CPU::step(Fault* fault) {
  *fault = Fault{};
  if (halted_) return StepResult::Halt;

  uint32_t insn = 0;
  Fault memFault;
  if (!memory_->fetch(state_.pc, &insn, &memFault)) {
    memFault.pc = state_.pc;
    memFault.instruction = 0;
    if (memFault.detail.empty()) memFault.detail = "instruction fetch";
    *fault = memFault;
    return StepResult::Fault;
  }

  last_pc_ = state_.pc;
  last_instruction_ = insn;
  ++instructions_;

  const uint64_t pc = state_.pc;
  uint64_t next = pc + 4;

  const uint32_t rd = insn & 0x1F;
  const uint32_t rn = (insn >> 5) & 0x1F;
  const uint32_t rm = (insn >> 16) & 0x1F;
  const uint32_t rt = insn & 0x1F;
  const uint32_t rt2 = (insn >> 10) & 0x1F;
  const bool sf = (insn >> 31) & 1;
  const unsigned width = sf ? 64 : 32;

  auto readMem = [&](uint64_t addr, void* out, size_t len) -> bool {
    Fault f;
    if (!memory_->read(addr, out, len, &f)) {
      f.pc = pc;
      f.instruction = insn;
      *fault = f;
      return false;
    }
    return true;
  };
  auto writeMem = [&](uint64_t addr, const void* in, size_t len) -> bool {
    Fault f;
    if (!memory_->write(addr, in, len, &f)) {
      f.pc = pc;
      f.instruction = insn;
      *fault = f;
      return false;
    }
    return true;
  };
  auto finish = [&]() {
    state_.pc = next;
    return StepResult::Continue;
  };

  // ---------------------------------------------------------------------
  // Branch, exception and system groups. These are matched before the wide
  // data-processing groups because their top-level patterns overlap.
  // ---------------------------------------------------------------------

  // SVC / HVC / BRK: 1101 0100 opc imm16 op2 LL
  if ((insn & 0xFFE0001F) == 0xD4000001) {  // SVC
    fault->kind = FaultKind::HostAbort;
    fault->pc = pc;
    fault->instruction = insn;
    fault->detail = "guest executed SVC; Aster Phase 1 has no exception vectors";
    return StepResult::Fault;
  }
  if ((insn & 0xFFE0001F) == 0xD4000002) {  // HVC #0
    const StepResult handled = hypercall(insn, fault);
    // Advancing the PC is easy to forget here: without it the HVC re-executes
    // and the hypercall's return value becomes the next call id.
    return handled == StepResult::Continue ? finish() : handled;
  }
  if ((insn & 0xFFE0001F) == 0xD4200000) {  // BRK
    fault->kind = FaultKind::HostAbort;
    fault->pc = pc;
    fault->instruction = insn;
    fault->detail = "guest BRK #" + std::to_string((insn >> 5) & 0xFFFF);
    return StepResult::Fault;
  }

  // Unconditional branch (immediate): B / BL
  if ((insn & 0x7C000000) == 0x14000000) {
    const int64_t imm = static_cast<int64_t>(signExtend(bits(insn, 25, 0), 25)) << 2;
    next = pc + static_cast<uint64_t>(imm);
    if (insn & 0x80000000u) state_.x[30] = pc + 4;
    return finish();
  }

  // Compare and branch (immediate): CBZ / CBNZ
  if ((insn & 0x7E000000) == 0x34000000) {
    const uint64_t value = reg(rt, false) & maskForWidth(sf);
    const bool nonzero = (insn >> 24) & 1;
    if ((value != 0) == nonzero) {
      const int64_t imm =
          static_cast<int64_t>(signExtend(bits(insn, 23, 5), 18)) << 2;
      next = pc + static_cast<uint64_t>(imm);
    }
    return finish();
  }

  // Test and branch (immediate): TBZ / TBNZ
  if ((insn & 0x7E000000) == 0x36000000) {
    const unsigned bit = ((insn >> 26) & 0x20) | ((insn >> 19) & 0x1F);
    const bool nonzero = (insn >> 24) & 1;
    const bool set = ((reg(rt, false) >> bit) & 1) != 0;
    if (set == nonzero) {
      const int64_t imm =
          static_cast<int64_t>(signExtend(bits(insn, 18, 5), 13)) << 2;
      next = pc + static_cast<uint64_t>(imm);
    }
    return finish();
  }

  // Conditional branch (immediate): B.cond
  if ((insn & 0xFF000010) == 0x54000000) {
    if (evalCondition(insn & 0xF)) {
      const int64_t imm =
          static_cast<int64_t>(signExtend(bits(insn, 23, 5), 18)) << 2;
      next = pc + static_cast<uint64_t>(imm);
    }
    return finish();
  }

  // Unconditional branch (register): BR / BLR / RET / ERET
  if ((insn & 0xFFFFFC1F) == 0xD61F0000) {  // BR
    next = reg(rn, false);
    return finish();
  }
  if ((insn & 0xFFFFFC1F) == 0xD63F0000) {  // BLR
    const uint64_t target = reg(rn, false);
    state_.x[30] = pc + 4;
    next = target;
    return finish();
  }
  if ((insn & 0xFFFFFC1F) == 0xD65F0000) {  // RET
    next = reg(rn, false);
    return finish();
  }
  if (insn == 0xD69F03E0) {
    fault->kind = FaultKind::HostAbort;
    fault->pc = pc;
    fault->instruction = insn;
    fault->detail = "ERET requires an exception model";
    return StepResult::Fault;
  }

  // System instructions: 1101 0101 00 ...
  if ((insn & 0xFFC00000) == 0xD5000000) {
    const bool is_read = (insn >> 21) & 1;
    const uint32_t op0 = (insn >> 19) & 0x3;
    const uint32_t op1 = (insn >> 16) & 0x7;
    const uint32_t crn = (insn >> 12) & 0xF;
    const uint32_t crm = (insn >> 8) & 0xF;
    const uint32_t op2 = (insn >> 5) & 0x7;

    // Hints and barriers use the MSR-immediate encoding space.
    if (!is_read && op0 == 0 && op1 == 3 && crn == 2) {
      // NOP / YIELD / WFE / WFI / SEV / SEVL
      if (op2 == 1) env_->hyperYield();
      if (op2 == 2 || op2 == 3) env_->hyperYield();
      return finish();
    }
    if (!is_read && op0 == 0 && op1 == 3 && crn == 3) {
      // CLREX / DSB / DMB / ISB / SB. The interpreter executes one instruction
      // at a time per guest thread, so ordering instructions need no action;
      // CLREX drops the exclusive monitor.
      if (op2 == 2) exclusive_valid_ = false;
      return finish();
    }

    uint64_t value = 0;
    bool known = true;
    if (op0 == 3 && op1 == 3 && crn == 4 && crm == 2 && op2 == 0) {
      value = state_.pstate();
      if (!is_read) state_.setPstate(state_.x[rt]);
    } else if (op0 == 3 && op1 == 3 && crn == 4 && crm == 2 && op2 == 1) {
      value = state_.x[rt] & 0;  // DAIF is not modelled
    } else if (op0 == 3 && op1 == 3 && crn == 4 && crm == 4 && op2 == 0) {
      value = state_.fpcr;
      if (!is_read) state_.fpcr = static_cast<uint32_t>(state_.x[rt]);
    } else if (op0 == 3 && op1 == 3 && crn == 4 && crm == 4 && op2 == 1) {
      value = state_.fpsr;
      if (!is_read) state_.fpsr = static_cast<uint32_t>(state_.x[rt]);
    } else if (op0 == 3 && op1 == 3 && crn == 13 && crm == 0 && op2 == 2) {
      value = state_.tpidr_el0;
      if (!is_read) state_.tpidr_el0 = state_.x[rt];
    } else if (op0 == 3 && op1 == 3 && crn == 14 && crm == 0 && op2 == 0) {
      value = 1000000000ULL;  // CNTFRQ_EL0: 1 GHz, matching our tick unit.
    } else if (op0 == 3 && op1 == 3 && crn == 14 && crm == 0 && op2 == 2) {
      value = env_->hyperTicks();  // CNTVCT_EL0
    } else if (op0 == 3 && op1 == 0 && crn == 0 && crm == 0 && op2 == 0) {
      value = 0x410FD4A0ULL;  // MIDR_EL1: "Aster virtual core"
    } else if (op0 == 3 && op1 == 0 && crn == 0 && crm == 0 && op2 == 5) {
      value = 0x80000000ULL;  // MPIDR_EL1
    } else if (op0 == 3 && op1 == 3 && crn == 0 && crm == 0 && op2 == 1) {
      value = 0x8444C004ULL;  // CTR_EL0
    } else if (op0 == 3 && op1 == 0 && crn == 4 && crm == 2 && op2 == 2) {
      value = 0;  // CurrentEL: guest code runs with EL0 semantics
    } else {
      known = false;
    }

    if (is_read) {
      // Unknown read-only ID registers return zero rather than trapping: real
      // compiled code probes them opportunistically.
      setRegWidth(rt, known ? value : 0, sf, false);
    }
    return finish();
  }

  // ---------------------------------------------------------------------
  // Loads and stores.
  // ---------------------------------------------------------------------

  // Load/store register (register offset)
  if ((insn & 0x3B200C00) == 0x38200800) {
    const unsigned size = (insn >> 30) & 0x3;
    const unsigned opc = (insn >> 22) & 0x3;
    const unsigned option = (insn >> 13) & 0x7;
    const bool scaled = (insn >> 12) & 1;
    const uint64_t base = reg(rn, true);
    uint64_t offset = reg(rm, false);
    switch (option) {
      case 0b010: offset &= 0xFFFFFFFFULL; break;                    // UXTW
      case 0b011: break;                                             // LSL
      case 0b110: offset = signExtend(offset, 31); break;            // SXTW
      case 0b111: offset = signExtend(offset, 63); break;            // SXTX
      default: return undefined(fault, insn, "invalid load/store extend");
    }
    if (scaled) offset <<= size;
    const uint64_t addr = base + offset;
    const unsigned bytes = 1u << size;

    if (opc == 0) {  // store
      const uint64_t value = reg(rt, false);
      if (!writeMem(addr, &value, bytes)) return StepResult::Fault;
    } else if (opc == 1) {  // load, zero extend
      uint64_t value = 0;
      if (!readMem(addr, &value, bytes)) return StepResult::Fault;
      setRegWidth(rt, value, sf, false);
    } else if (opc == 2 && size < 3) {  // load, sign extend to 64
      uint64_t value = 0;
      if (!readMem(addr, &value, bytes)) return StepResult::Fault;
      setReg(rt, signExtend(value, bytes * 8 - 1), false);
    } else if (opc == 3 && size < 3) {  // load, sign extend to 32
      uint64_t value = 0;
      if (!readMem(addr, &value, bytes)) return StepResult::Fault;
      setRegWidth(rt, signExtend(value, bytes * 8 - 1), false, false);
    } else {
      return undefined(fault, insn, "invalid load/store opc");
    }
    return finish();
  }

  // Load/store register (unsigned immediate)
  if ((insn & 0x3B000000) == 0x39000000) {
    const unsigned size = (insn >> 30) & 0x3;
    const unsigned opc = (insn >> 22) & 0x3;
    const bool vector = (insn >> 26) & 1;
    const uint64_t base = reg(rn, true);
    const uint64_t addr = base + (static_cast<uint64_t>(bits(insn, 21, 10)) << size);
    const unsigned bytes = 1u << size;

    if (vector) {
      // SIMD&FP: opc 00/01 = STR/LDR B,H,S,D; opc 10/11 = STR/LDR Q.
      const bool load = (opc & 1) != 0;
      const unsigned count = (opc >= 2) ? 16u : bytes;
      if (load) {
        std::memset(state_.v[rt], 0, 16);
        if (!readMem(addr, state_.v[rt], count)) return StepResult::Fault;
      } else {
        if (!writeMem(addr, state_.v[rt], count)) return StepResult::Fault;
      }
      return finish();
    }

    if (opc == 3 && size == 3) return finish();  // PRFM

    if (opc == 0) {
      const uint64_t value = reg(rt, false);
      if (!writeMem(addr, &value, bytes)) return StepResult::Fault;
    } else if (opc == 1) {
      uint64_t value = 0;
      if (!readMem(addr, &value, bytes)) return StepResult::Fault;
      setRegWidth(rt, value, sf, false);
    } else if (opc == 2) {
      uint64_t value = 0;
      if (!readMem(addr, &value, bytes)) return StepResult::Fault;
      setReg(rt, signExtend(value, bytes * 8 - 1), false);
    } else {  // opc == 3: sign extend to 32
      if (size == 3) return finish();
      uint64_t value = 0;
      if (!readMem(addr, &value, bytes)) return StepResult::Fault;
      setRegWidth(rt, signExtend(value, bytes * 8 - 1), false, false);
    }
    return finish();
  }

  // Load/store register (unscaled / pre-index / post-index)
  if ((insn & 0x3B200000) == 0x38000000 && ((insn >> 10) & 0x3) != 0x2) {
    const unsigned size = (insn >> 30) & 0x3;
    const unsigned opc = (insn >> 22) & 0x3;
    const unsigned mode = (insn >> 10) & 0x3;  // 00 unscaled, 01 post, 11 pre
    const int64_t imm =
        static_cast<int64_t>(signExtend(bits(insn, 20, 12), 8));
    const uint64_t base = reg(rn, true);
    // Unscaled and pre-index both use base+imm; only post-index uses the
    // unmodified base and writes back afterwards.
    const uint64_t addr = (mode == 1) ? base : base + static_cast<uint64_t>(imm);
    const unsigned bytes = 1u << size;
    const bool vector = (insn >> 26) & 1;

    if (vector) {
      const bool load = (opc & 1) != 0;
      const unsigned count = (opc >= 2) ? 16u : bytes;
      if (load) {
        std::memset(state_.v[rt], 0, 16);
        if (!readMem(addr, state_.v[rt], count)) return StepResult::Fault;
      } else {
        if (!writeMem(addr, state_.v[rt], count)) return StepResult::Fault;
      }
      if (mode == 1) setReg(rn, base + static_cast<uint64_t>(imm), true);
      else if (mode == 3) setReg(rn, addr, true);
      return finish();
    }

    if (opc == 0) {
      const uint64_t value = reg(rt, false);
      if (!writeMem(addr, &value, bytes)) return StepResult::Fault;
    } else if (opc == 1) {
      uint64_t value = 0;
      if (!readMem(addr, &value, bytes)) return StepResult::Fault;
      setRegWidth(rt, value, sf, false);
    } else if (opc == 2 && size < 3) {
      uint64_t value = 0;
      if (!readMem(addr, &value, bytes)) return StepResult::Fault;
      setReg(rt, signExtend(value, bytes * 8 - 1), false);
    } else if (opc == 3 && size < 3) {
      uint64_t value = 0;
      if (!readMem(addr, &value, bytes)) return StepResult::Fault;
      setRegWidth(rt, signExtend(value, bytes * 8 - 1), false, false);
    } else {
      return undefined(fault, insn, "invalid unscaled load/store");
    }

    if (mode == 1) {  // post-index
      setReg(rn, base + static_cast<uint64_t>(imm), true);
    } else if (mode == 3) {  // pre-index
      setReg(rn, addr, true);
    }
    return finish();
  }

  // Load register (literal)
  if ((insn & 0x3B000000) == 0x18000000) {
    const unsigned opc = (insn >> 30) & 0x3;
    const int64_t imm =
        static_cast<int64_t>(signExtend(bits(insn, 23, 5), 18)) << 2;
    const uint64_t addr = pc + static_cast<uint64_t>(imm);
    if (opc == 0) {
      uint32_t value = 0;
      if (!readMem(addr, &value, 4)) return StepResult::Fault;
      setRegWidth(rt, value, false, false);
    } else if (opc == 1) {
      uint64_t value = 0;
      if (!readMem(addr, &value, 8)) return StepResult::Fault;
      setReg(rt, value, false);
    } else if (opc == 2) {
      uint32_t value = 0;
      if (!readMem(addr, &value, 4)) return StepResult::Fault;
      setReg(rt, signExtend(value, 31), false);
    }
    return finish();  // opc == 3 is PRFM
  }

  // Load/store pair (signed offset / pre-index / post-index)
  if ((insn & 0x3A000000) == 0x28000000) {
    const unsigned opc = (insn >> 30) & 0x3;
    const unsigned mode = (insn >> 23) & 0x3;  // 01 post, 10 signed, 11 pre
    const bool load = (insn >> 22) & 1;
    const bool is64 = (opc & 0x2) != 0;
    const unsigned bytes = is64 ? 8 : 4;
    const int64_t imm = static_cast<int64_t>(signExtend(bits(insn, 21, 15), 6))
                        << (is64 ? 3 : 2);
    const uint64_t base = reg(rn, true);
    const uint64_t addr = (mode == 1) ? base : base + static_cast<uint64_t>(imm);
    const bool vector = (insn >> 26) & 1;

    if (vector) {
      // SIMD&FP pair: opc 00 = S, 01 = D, 10 = Q.
      if (opc == 3) return undefined(fault, insn, "invalid SIMD load/store pair");
      const unsigned bytes = (opc == 0) ? 4u : (opc == 1) ? 8u : 16u;
      if (load) {
        std::memset(state_.v[rt], 0, 16);
        std::memset(state_.v[rt2], 0, 16);
        if (!readMem(addr, state_.v[rt], bytes) ||
            !readMem(addr + bytes, state_.v[rt2], bytes))
          return StepResult::Fault;
      } else {
        if (!writeMem(addr, state_.v[rt], bytes) ||
            !writeMem(addr + bytes, state_.v[rt2], bytes))
          return StepResult::Fault;
      }
      if (mode == 1) setReg(rn, base + static_cast<uint64_t>(imm), true);
      else if (mode == 3) setReg(rn, addr, true);
      return finish();
    }

    if (opc == 3 || (opc == 1 && !load))
      return undefined(fault, insn, "invalid load/store pair opc");

    if (opc == 1) {  // LDPSW
      uint64_t v1 = 0, v2 = 0;
      if (!readMem(addr, &v1, 4) || !readMem(addr + 4, &v2, 4))
        return StepResult::Fault;
      setReg(rt, signExtend(v1, 31), false);
      setReg(rt2, signExtend(v2, 31), false);
      if (mode == 1) setReg(rn, base + static_cast<uint64_t>(imm), true);
      else if (mode == 3) setReg(rn, addr, true);
      return finish();
    }

    if (load) {
      uint64_t v1 = 0, v2 = 0;
      if (!readMem(addr, &v1, bytes) || !readMem(addr + bytes, &v2, bytes))
        return StepResult::Fault;
      if (is64) {
        setReg(rt, v1, false);
        setReg(rt2, v2, false);
      } else {
        setRegWidth(rt, v1, false, false);
        setRegWidth(rt2, v2, false, false);
      }
    } else {
      const uint64_t v1 = reg(rt, false);
      const uint64_t v2 = reg(rt2, false);
      if (!writeMem(addr, &v1, bytes) || !writeMem(addr + bytes, &v2, bytes))
        return StepResult::Fault;
    }

    if (mode == 1) {
      setReg(rn, base + static_cast<uint64_t>(imm), true);
    } else if (mode == 3) {
      setReg(rn, addr, true);
    }
    return finish();
  }

  // Load/store exclusive and acquire/release.
  {
    // Keep the opcode bits and the acquire/release bit, discard Rs, Rt2, Rn
    // and Rt, which are all register fields.
    const uint32_t masked = insn & 0x3FE08000;
    const bool is64 = ((insn >> 30) & 0x3) == 0x3;
    const unsigned bytes = is64 ? 8 : 4;
    if (masked == 0x08400000 || masked == 0x08408000) {  // LDXR / LDAXR
      const uint64_t addr = reg(rn, true);
      uint64_t value = 0;
      if (!readMem(addr, &value, bytes)) return StepResult::Fault;
      const uint64_t masked_addr = addr & ~static_cast<uint64_t>(bytes - 1);
      {
        std::lock_guard<std::mutex> lock(exclusiveMutex());
        exclusive_valid_ = true;
        exclusive_addr_ = masked_addr;
        exclusive_value_ = value;
        exclusive_size_ = bytes;
      }
      setRegWidth(rt, value, is64, false);
      return finish();
    }
    if (masked == 0x08000000 || masked == 0x08008000) {  // STXR / STLXR
      const uint32_t rs = (insn >> 16) & 0x1F;
      const uint64_t addr = reg(rn, true);
      const uint64_t value = reg(rt, false);
      const uint64_t masked_addr = addr & ~static_cast<uint64_t>(bytes - 1);
      bool success = false;
      {
        std::lock_guard<std::mutex> lock(exclusiveMutex());
        if (exclusive_valid_ && exclusive_addr_ == masked_addr &&
            exclusive_size_ == bytes) {
          uint64_t current = 0;
          Fault f;
          if (!memory_->read(addr, &current, bytes, &f)) {
            f.pc = pc;
            f.instruction = insn;
            *fault = f;
            return StepResult::Fault;
          }
          const uint64_t current64 = current;
          if (current64 == exclusive_value_) {
            Fault wf;
            if (!memory_->write(addr, &value, bytes, &wf)) {
              wf.pc = pc;
              wf.instruction = insn;
              *fault = wf;
              return StepResult::Fault;
            }
            success = true;
          }
        }
        exclusive_valid_ = false;
      }
      setRegWidth(rs, success ? 0 : 1, false, false);
      return finish();
    }
    if (masked == 0x08C08000) {  // LDAR
      uint64_t value = 0;
      if (!readMem(reg(rn, true), &value, bytes)) return StepResult::Fault;
      setRegWidth(rt, value, is64, false);
      return finish();
    }
    if (masked == 0x08808000) {  // STLR
      const uint64_t value = reg(rt, false);
      if (!writeMem(reg(rn, true), &value, bytes)) return StepResult::Fault;
      return finish();
    }
  }

  // ---------------------------------------------------------------------
  // Data processing — immediate.
  // ---------------------------------------------------------------------

  // ADR / ADRP
  if ((insn & 0x1F000000) == 0x10000000) {
    const uint64_t immlo = bits(insn, 30, 29);
    const uint64_t immhi = bits(insn, 23, 5);
    const int64_t imm = static_cast<int64_t>(signExtend((immhi << 2) | immlo, 20));
    uint64_t value;
    if (insn & 0x80000000u) {
      value = (pc & ~0xFFFULL) + (static_cast<uint64_t>(imm) << 12);
    } else {
      value = pc + static_cast<uint64_t>(imm);
    }
    setReg(rd, value, false);
    return finish();
  }

  // Add/subtract (immediate)
  if ((insn & 0x1F000000) == 0x11000000) {
    const bool subtract = (insn >> 30) & 1;
    const bool set_flags = (insn >> 29) & 1;
    uint64_t imm = bits(insn, 21, 10);
    if ((insn >> 22) & 1) imm <<= 12;  // sh
    const uint64_t lhs = reg(rn, true) & maskForWidth(sf);
    const uint64_t rhs = imm & maskForWidth(sf);
    const uint64_t result = subtract ? (lhs - rhs) : (lhs + rhs);
    const uint64_t truncated = result & maskForWidth(sf);
    if (set_flags) {
      const bool n = (truncated >> (width - 1)) & 1;
      const bool z = truncated == 0;
      const bool c = subtract ? (lhs >= rhs) : (truncated < lhs);
      const bool v = subtract ? (((lhs ^ rhs) & (lhs ^ truncated)) >> (width - 1)) & 1
                              : (((lhs ^ ~rhs) & (lhs ^ truncated)) >> (width - 1)) & 1;
      state_.setFlags(n, z, c, v);
    }
    // With S=0, Rd names SP; with S=1 it names the zero register.
    setRegWidth(rd, truncated, sf, !set_flags);
    return finish();
  }

  // Logical (immediate)
  if ((insn & 0x1F800000) == 0x12000000) {
    const unsigned opc = (insn >> 29) & 0x3;
    const bool n = (insn >> 22) & 1;
    const uint32_t immr = static_cast<uint32_t>(bits(insn, 21, 16));
    const uint32_t imms = static_cast<uint32_t>(bits(insn, 15, 10));
    uint64_t wmask = 0, tmask = 0;
    if (!decodeBitMasksImpl(sf, n, imms, immr, true, &wmask, &tmask))
      return undefined(fault, insn, "invalid logical immediate");
    const uint64_t src = reg(rn, false) & maskForWidth(sf);
    uint64_t result = 0;
    switch (opc) {
      case 0: result = src & wmask; break;
      case 1: result = src | wmask; break;
      case 2: result = src ^ wmask; break;
      case 3:
        result = src & wmask;
        state_.setFlags((result >> (width - 1)) & 1, result == 0, false, false);
        break;
    }
    setRegWidth(rd, result, sf, false);
    return finish();
  }

  // Move wide (immediate): MOVN / MOVZ / MOVK
  if ((insn & 0x1F800000) == 0x12800000) {
    const unsigned opc = (insn >> 29) & 0x3;
    const unsigned hw = (insn >> 21) & 0x3;
    const uint64_t imm16 = bits(insn, 20, 5);
    if (opc == 1) return undefined(fault, insn, "unallocated move-wide opc");
    if (!sf && hw > 1) return undefined(fault, insn, "move-wide hw out of range");
    const unsigned shift = hw * 16;
    uint64_t value;
    if (opc == 0) {  // MOVN
      value = ~(imm16 << shift);
    } else if (opc == 2) {  // MOVZ
      value = imm16 << shift;
    } else {  // MOVK
      const uint64_t mask = ~(0xFFFFULL << shift);
      value = (reg(rd, false) & mask) | (imm16 << shift);
    }
    setRegWidth(rd, value, sf, false);
    return finish();
  }

  // Bitfield: SBFM / BFM / UBFM
  if ((insn & 0x1F800000) == 0x13000000) {
    const unsigned opc = (insn >> 29) & 0x3;
    const bool n = (insn >> 22) & 1;
    const uint32_t immr = static_cast<uint32_t>(bits(insn, 21, 16));
    const uint32_t imms = static_cast<uint32_t>(bits(insn, 15, 10));
    if (n != sf) return undefined(fault, insn, "bitfield N/sf mismatch");
    uint64_t wmask = 0, tmask = 0;
    if (!decodeBitMasksImpl(sf, n, imms, immr, false, &wmask, &tmask))
      return undefined(fault, insn, "invalid bitfield");
    const uint64_t src = reg(rn, false) & maskForWidth(sf);
    const uint64_t bot = rorWidth(src, immr, width) & wmask;
    const unsigned diff = (imms - immr) & (width - 1);
    uint64_t result;
    if (opc == 2) {  // UBFM
      result = bot & tmask;
    } else if (opc == 0) {  // SBFM
      const uint64_t field = bot & tmask;
      result = signExtend(field, diff) & maskForWidth(sf);
    } else if (opc == 1) {  // BFM
      const uint64_t dst = reg(rd, false) & maskForWidth(sf);
      result = (dst & ~tmask) | (bot & tmask);
    } else {
      return undefined(fault, insn, "unallocated bitfield opc");
    }
    setRegWidth(rd, result, sf, false);
    return finish();
  }

  // Extract: EXTR
  if ((insn & 0x1F800000) == 0x13800000) {
    const bool n = (insn >> 22) & 1;
    const uint32_t lsb = static_cast<uint32_t>(bits(insn, 15, 10));
    if (n != sf || lsb >= width) return undefined(fault, insn, "invalid EXTR");
    const uint64_t hi = reg(rn, false) & maskForWidth(sf);
    const uint64_t lo = reg(rm, false) & maskForWidth(sf);
    uint64_t result;
    if (lsb == 0) {
      result = lo;
    } else {
      result = (lo >> lsb) | (hi << (width - lsb));
    }
    setRegWidth(rd, result, sf, false);
    return finish();
  }

  // ---------------------------------------------------------------------
  // Data processing — register.
  // ---------------------------------------------------------------------

  // Add/subtract (shifted register)
  if ((insn & 0x1F200000) == 0x0B000000) {
    const bool subtract = (insn >> 30) & 1;
    const bool set_flags = (insn >> 29) & 1;
    const unsigned shift_type = (insn >> 22) & 0x3;
    const unsigned amount = static_cast<unsigned>(bits(insn, 15, 10));
    if (amount >= width) return undefined(fault, insn, "shift amount out of range");
    const uint64_t lhs = reg(rn, false) & maskForWidth(sf);
    const uint64_t rhs = shiftedReg(reg(rm, false), shift_type, amount, sf);
    const uint64_t result = subtract ? (lhs - rhs) : (lhs + rhs);
    const uint64_t truncated = result & maskForWidth(sf);
    if (set_flags) {
      const bool n = (truncated >> (width - 1)) & 1;
      const bool z = truncated == 0;
      const bool c = subtract ? (lhs >= rhs) : (result < lhs);
      const bool v = subtract ? (((lhs ^ rhs) & (lhs ^ truncated)) >> (width - 1)) & 1
                              : (((lhs ^ ~rhs) & (lhs ^ truncated)) >> (width - 1)) & 1;
      state_.setFlags(n, z, c, v);
    }
    setRegWidth(rd, truncated, sf, false);
    return finish();
  }

  // Add/subtract (extended register)
  if ((insn & 0x1F200000) == 0x0B200000) {
    const bool subtract = (insn >> 30) & 1;
    const bool set_flags = (insn >> 29) & 1;
    const unsigned option = (insn >> 13) & 0x7;
    const unsigned amount = (insn >> 10) & 0x7;
    uint64_t rhs = reg(rm, false);
    switch (option) {
      case 0b000: rhs &= 0xFF; break;
      case 0b001: rhs &= 0xFFFF; break;
      case 0b010: rhs &= 0xFFFFFFFFULL; break;
      case 0b011: break;
      case 0b100: rhs = signExtend(rhs, 7); break;
      case 0b101: rhs = signExtend(rhs, 15); break;
      case 0b110: rhs = signExtend(rhs, 31); break;
      case 0b111: rhs = signExtend(rhs, 63); break;
    }
    rhs <<= amount;
    rhs &= maskForWidth(sf);
    const uint64_t lhs = reg(rn, true) & maskForWidth(sf);
    const uint64_t result = subtract ? (lhs - rhs) : (lhs + rhs);
    const uint64_t truncated = result & maskForWidth(sf);
    if (set_flags) {
      const bool n = (truncated >> (width - 1)) & 1;
      const bool z = truncated == 0;
      const bool c = subtract ? (lhs >= rhs) : (truncated < lhs);
      const bool v = subtract ? (((lhs ^ rhs) & (lhs ^ truncated)) >> (width - 1)) & 1
                              : (((lhs ^ ~rhs) & (lhs ^ truncated)) >> (width - 1)) & 1;
      state_.setFlags(n, z, c, v);
    }
    setRegWidth(rd, truncated, sf, !set_flags);
    return finish();
  }

  // Logical (shifted register)
  if ((insn & 0x1F000000) == 0x0A000000) {
    const unsigned opc = (insn >> 29) & 0x3;
    const unsigned shift_type = (insn >> 22) & 0x3;
    const bool n = (insn >> 21) & 1;
    const unsigned amount = static_cast<unsigned>(bits(insn, 15, 10));
    if (amount >= width) return undefined(fault, insn, "shift amount out of range");
    uint64_t operand = shiftedReg(reg(rm, false), shift_type, amount, sf);
    if (n) operand = ~operand & maskForWidth(sf);
    const uint64_t lhs = reg(rn, false) & maskForWidth(sf);
    uint64_t result = 0;
    switch (opc) {
      case 0: result = lhs & operand; break;
      case 1: result = lhs | operand; break;
      case 2: result = lhs ^ operand; break;
      case 3:
        result = lhs & operand;
        state_.setFlags((result >> (width - 1)) & 1, result == 0, false, false);
        break;
    }
    setRegWidth(rd, result, sf, false);
    return finish();
  }

  // Add/subtract with carry
  if ((insn & 0x1FE00000) == 0x1A000000) {
    const bool subtract = (insn >> 30) & 1;
    const bool set_flags = (insn >> 29) & 1;
    const uint64_t lhs = reg(rn, false) & maskForWidth(sf);
    const uint64_t rhs = reg(rm, false) & maskForWidth(sf);
    const uint64_t carry = state_.flagC() ? 1 : 0;
    const uint64_t truncated = (subtract ? (lhs - rhs - (1 - carry))
                                        : (lhs + rhs + carry)) & maskForWidth(sf);
    if (set_flags) {
      const bool n = (truncated >> (width - 1)) & 1;
      const bool z = truncated == 0;
      // Compute the carry with a wider intermediate so 32-bit and 64-bit
      // wrap-around behave exactly like the architectural pseudocode.
      unsigned __int128 wide = subtract
          ? (static_cast<unsigned __int128>(lhs) - rhs - (1 - carry))
          : (static_cast<unsigned __int128>(lhs) + rhs + carry);
      const bool c = subtract ? (wide <= static_cast<unsigned __int128>(maskForWidth(sf)))
                              : (wide > static_cast<unsigned __int128>(maskForWidth(sf)));
      const bool v = subtract ? (((lhs ^ rhs) & (lhs ^ truncated)) >> (width - 1)) & 1
                              : (((lhs ^ ~rhs) & (lhs ^ truncated)) >> (width - 1)) & 1;
      state_.setFlags(n, z, c, v);
    }
    setRegWidth(rd, truncated, sf, false);
    return finish();
  }

  // Conditional compare (immediate/register): CCMP / CCMN. Clang emits these
  // for short-circuit boolean chains.
  if ((insn & 0x1FE00000) == 0x1A400000) {
    const bool compare = (insn >> 30) & 1;   // 0 = CCMN, 1 = CCMP
    const bool immediate = (insn >> 11) & 1;
    const uint32_t cond = static_cast<uint32_t>(bits(insn, 15, 12));
    const uint64_t lhs = reg(rn, false) & maskForWidth(sf);
    const uint64_t rhs = immediate ? bits(insn, 20, 16) : (reg(rm, false) & maskForWidth(sf));
    if (evalCondition(cond)) {
      const uint64_t result = compare ? (lhs - rhs) : (lhs + rhs);
      const uint64_t truncated = result & maskForWidth(sf);
      const bool n = (truncated >> (width - 1)) & 1;
      const bool z = truncated == 0;
      const bool c = compare ? (lhs >= rhs) : (truncated < lhs);
      const bool v = compare ? (((lhs ^ rhs) & (lhs ^ truncated)) >> (width - 1)) & 1
                             : (((lhs ^ ~rhs) & (lhs ^ truncated)) >> (width - 1)) & 1;
      state_.setFlags(n, z, c, v);
    } else {
      const uint32_t nzcv = static_cast<uint32_t>(bits(insn, 3, 0));
      state_.setFlags(nzcv & 8, nzcv & 4, nzcv & 2, nzcv & 1);
    }
    return finish();
  }

  // Conditional select
  if ((insn & 0x1FE00000) == 0x1A800000) {
    const unsigned op2 = (insn >> 10) & 0x3;
    const uint32_t cond = static_cast<uint32_t>(bits(insn, 15, 12));
    uint64_t result;
    if (evalCondition(cond)) {
      result = reg(rn, false);
    } else {
      const uint64_t other = reg(rm, false);
      switch (op2) {
        case 0: result = other; break;
        case 1: result = other + 1; break;
        case 2: result = ~other; break;
        default: result = static_cast<uint64_t>(-static_cast<int64_t>(other)); break;
      }
    }
    setRegWidth(rd, result, sf, false);
    return finish();
  }

  // Data-processing (2 source)
  if ((insn & 0x1FE00000) == 0x1AC00000) {
    const unsigned opcode = static_cast<unsigned>(bits(insn, 15, 10));
    const uint64_t lhs = reg(rn, false);
    const uint64_t rhs = reg(rm, false);
    uint64_t result = 0;
    switch (opcode) {
      case 0x02:  // UDIV
        result = (rhs == 0) ? 0 : ((lhs & maskForWidth(sf)) / (rhs & maskForWidth(sf)));
        break;
      case 0x03: {  // SDIV
        const uint64_t l = sf ? lhs : static_cast<uint64_t>(static_cast<int64_t>(static_cast<int32_t>(lhs)));
        const uint64_t r = sf ? rhs : static_cast<uint64_t>(static_cast<int64_t>(static_cast<int32_t>(rhs)));
        result = (r == 0) ? 0
                          : static_cast<uint64_t>(static_cast<int64_t>(l) /
                                                  static_cast<int64_t>(r));
        break;
      }
      case 0x08: result = lsl(lhs, rhs & (width - 1), sf); break;
      case 0x09: result = lsr(lhs, rhs & (width - 1), sf); break;
      case 0x0A: result = asr(lhs, rhs & (width - 1), sf); break;
      case 0x0B: result = rorWidth(lhs, rhs & (width - 1), width); break;
      default: return undefined(fault, insn, "unsupported 2-source opcode");
    }
    setRegWidth(rd, result, sf, false);
    return finish();
  }

  // Widening multiply (3 source): SMADDL / SMSUBL / SMULH / UMADDL /
  // UMSUBL / UMULH. These appear in normal clang output for 32x32->64 math.
  {
    const uint32_t w = insn & 0xFFE08000;
    if (w == 0x9B200000 || w == 0x9BA00000) {  // SMADDL / UMADDL
      const bool signed_mult = ((insn >> 23) & 1) == 0;  // SMADDL vs UMADDL
      const bool subtract = (insn & 0x00008000) != 0;
      const uint64_t lhs = state_.x[rn] & 0xFFFFFFFFULL;
      const uint64_t rhs = state_.x[rm] & 0xFFFFFFFFULL;
      uint64_t product;
      if (signed_mult) {
        product = static_cast<uint64_t>(static_cast<int64_t>(static_cast<int32_t>(lhs)) *
                                        static_cast<int64_t>(static_cast<int32_t>(rhs)));
      } else {
        product = lhs * rhs;
      }
      const uint64_t addend = state_.x[(insn >> 10) & 0x1F];
      setReg(rd, subtract ? (addend - product) : (addend + product), false);
      return finish();
    }
    // Ra occupies bits 15:10 and is 11111 for SMULH/UMULH, so it must be
    // masked out of the comparison.
    const uint32_t h = insn & 0xFFE08000;
    if (h == 0x9B400000 || h == 0x9BC00000) {  // SMULH / UMULH
      const uint64_t lhs = state_.x[rn];
      const uint64_t rhs = state_.x[rm];
      unsigned __int128 product;
      if (h == 0x9B400000) {
        product = static_cast<unsigned __int128>(
            static_cast<__int128>(static_cast<int64_t>(lhs)) *
            static_cast<__int128>(static_cast<int64_t>(rhs)));
      } else {
        product = static_cast<unsigned __int128>(lhs) * rhs;
      }
      setReg(rd, static_cast<uint64_t>(product >> 64), false);
      return finish();
    }
  }

  // Data-processing (3 source)
  if ((insn & 0x1FE00000) == 0x1B000000) {
    const bool subtract = (insn >> 15) & 1;
    const bool is64 = (insn >> 31) & 1;
    const uint64_t lhs = reg(rn, false) & maskForWidth(is64);
    const uint64_t rhs = reg(rm, false) & maskForWidth(is64);
    const uint64_t addend = reg((insn >> 10) & 0x1F, false) & maskForWidth(is64);
    const uint64_t product = lhs * rhs;
    const uint64_t result = subtract ? (addend - product) : (addend + product);
    setRegWidth(rd, result, is64, false);
    return finish();
  }

  // Data-processing (1 source)
  if ((insn & 0x7FE00000) == 0x5AC00000 && ((insn >> 16) & 0x1F) == 0) {
    const unsigned opcode = static_cast<unsigned>(bits(insn, 15, 10));
    const uint64_t src = reg(rn, false) & maskForWidth(sf);
    uint64_t result = 0;
    switch (opcode) {
      case 0: {  // RBIT
        uint64_t v = src;
        result = 0;
        for (unsigned i = 0; i < width; ++i) {
          result = (result << 1) | (v & 1);
          v >>= 1;
        }
        break;
      }
      case 1: {  // REV16
        result = 0;
        for (unsigned i = 0; i < width; i += 16) {
          const uint64_t half = (src >> i) & 0xFFFF;
          result |= ((half & 0xFF) << 8 | ((half >> 8) & 0xFF)) << i;
        }
        break;
      }
      case 2: {  // REV32
        result = 0;
        for (unsigned i = 0; i < width; i += 32) {
          const uint32_t word = static_cast<uint32_t>((src >> i) & 0xFFFFFFFF);
          result |= static_cast<uint64_t>(
                        ((word & 0xFF) << 24) | ((word & 0xFF00) << 8) |
                        ((word >> 8) & 0xFF00) | ((word >> 24) & 0xFF))
                    << i;
        }
        break;
      }
      case 3: {  // REV
        uint64_t v = src;
        result = 0;
        for (unsigned i = 0; i < width; i += 8) {
          result = (result << 8) | (v & 0xFF);
          v >>= 8;
        }
        break;
      }
      case 4:  // CLZ
        result = width;
        for (unsigned i = width; i > 0; --i) {
          if ((src >> (i - 1)) & 1) {
            result = width - i;
            break;
          }
        }
        break;
      case 5: {  // CLS
        result = width;
        for (unsigned i = width; i > 0; --i) {
          const unsigned bit = (src >> (i - 1)) & 1;
          const unsigned sign = (src >> (width - 1)) & 1;
          if (bit != sign) {
            result = width - i;
            break;
          }
        }
        break;
      }
      default: return undefined(fault, insn, "unsupported 1-source opcode");
    }
    setRegWidth(rd, result, sf, false);
    return finish();
  }

  // ---------------------------------------------------------------------
  // Scalar floating point.
  //
  // Storage is 16 bytes per V register; scalar S/D accesses reinterpret the
  // low bytes. Bit-copy operations (FMOV) must not go through a float cast,
  // and arithmetic must not go through an integer cast: both mistakes produce
  // plausible-looking but wrong results, so the two accessor families are kept
  // explicitly separate.
  // ---------------------------------------------------------------------
  {
    auto f32 = [&](uint32_t i) -> float& {
      return *reinterpret_cast<float*>(state_.v[i]);
    };
    auto f64 = [&](uint32_t i) -> double& {
      return *reinterpret_cast<double*>(state_.v[i]);
    };
    auto bits32 = [&](uint32_t i) -> uint32_t& {
      return *reinterpret_cast<uint32_t*>(state_.v[i]);
    };
    auto bits64 = [&](uint32_t i) -> uint64_t& {
      return *reinterpret_cast<uint64_t*>(state_.v[i]);
    };

    const uint32_t m = insn & 0xFFE0FC00;
    const bool dbl = (insn & 0x00400000) != 0;

    // Fused multiply-add family.
    {
      const uint32_t fused = insn & 0xFFE08000;
      const uint32_t ra = (insn >> 10) & 0x1F;
      if (fused == 0x1F000000 || fused == 0x1F008000 ||
          fused == 0x1F200000 || fused == 0x1F208000 ||
          fused == 0x1F400000 || fused == 0x1F408000 ||
          fused == 0x1F600000 || fused == 0x1F608000) {
        const bool negate_product = (fused & 0x00200000) != 0;
        const bool subtract = (fused & 0x00008000) != 0;
        const double product = negate_product
            ? -(static_cast<double>(dbl ? f64(rn) : f32(rn)) *
                static_cast<double>(dbl ? f64(rm) : f32(rm)))
            : (static_cast<double>(dbl ? f64(rn) : f32(rn)) *
               static_cast<double>(dbl ? f64(rm) : f32(rm)));
        const double addend = dbl ? f64(ra) : static_cast<double>(f32(ra));
        const double result = subtract ? (addend - product) : (addend + product);
        if (dbl) f64(rd) = result;
        else f32(rd) = static_cast<float>(result);
        return finish();
      }
    }

    // FMOV (scalar, immediate).
    if ((insn & 0xFFE01FE0) == 0x1E201000 ||
        (insn & 0xFFE01FE0) == 0x1E601000) {
      const bool is_double = (insn & 0x00400000) != 0;
      const uint8_t imm8 = static_cast<uint8_t>((insn >> 13) & 0xFF);
      // Single precision packs the 6 immediate bits at bits 22:17 of the
      // float (<<3 here, then <<16); double precision packs them at bits 51:48.
      const uint64_t mantissa = is_double ? (imm8 & 0x3F) : ((imm8 & 0x3F) << 3);
      const uint64_t field = (imm8 & 0x80 ? 0x8000ULL : 0) |
                             (imm8 & 0x40 ? (is_double ? 0x3FC0ULL : 0x3E00ULL)
                                          : 0x4000ULL) |
                             mantissa;
      if (is_double) {
        const uint64_t pattern = field << 48;
        std::memcpy(state_.v[rd], &pattern, 8);
      } else {
        const uint32_t pattern = static_cast<uint32_t>(field << 16);
        std::memcpy(state_.v[rd], &pattern, 4);
      }
      return finish();
    }

    // FMOV between general-purpose and scalar FP registers (bit copy).
    // Direction matters: Rd names the destination register file.
    if ((insn & 0xFFFFFC00) == 0x1E260000) { setRegWidth(rd, bits32(rn), false, false); return finish(); }  // Wd <- Sn
    if ((insn & 0xFFFFFC00) == 0x1E270000) { bits32(rd) = static_cast<uint32_t>(state_.x[rn]); return finish(); }  // Sd <- Wn
    if ((insn & 0xFFFFFC00) == 0x9E660000) { setReg(rd, bits64(rn), false); return finish(); }  // Xd <- Dn
    if ((insn & 0xFFFFFC00) == 0x9E670000) { bits64(rd) = state_.x[rn]; return finish(); }  // Dd <- Xn

    // Two-operand FP: FNEG / FABS / FSQRT / FNMUL.
    if (m == 0x1E214000 || m == 0x1E614000) {
      if (dbl) f64(rd) = -f64(rn); else f32(rd) = -f32(rn);
      return finish();
    }
    if (m == 0x1E20C000 || m == 0x1E60C000) {
      if (dbl) f64(rd) = std::fabs(f64(rn)); else f32(rd) = std::fabs(f32(rn));
      return finish();
    }
    if (m == 0x1E21C000 || m == 0x1E61C000) {
      if (dbl) f64(rd) = std::sqrt(f64(rn)); else f32(rd) = std::sqrt(f32(rn));
      return finish();
    }
    if (m == 0x1E208800 || m == 0x1E608800) {
      if (dbl) f64(rd) = -(f64(rn) * f64(rm));
      else f32(rd) = -(f32(rn) * f32(rm));
      return finish();
    }

    if (m == 0x1E202800 || m == 0x1E602800) {  // FADD
      if (dbl) f64(rd) = f64(rn) + f64(rm);
      else f32(rd) = f32(rn) + f32(rm);
      return finish();
    }
    if (m == 0x1E203800 || m == 0x1E603800) {  // FSUB
      if (dbl) f64(rd) = f64(rn) - f64(rm);
      else f32(rd) = f32(rn) - f32(rm);
      return finish();
    }
    if (m == 0x1E200800 || m == 0x1E600800) {  // FMUL
      if (dbl) f64(rd) = f64(rn) * f64(rm);
      else f32(rd) = f32(rn) * f32(rm);
      return finish();
    }
    if (m == 0x1E201800 || m == 0x1E601800) {  // FDIV
      if (dbl) f64(rd) = f64(rn) / f64(rm);
      else f32(rd) = f32(rn) / f32(rm);
      return finish();
    }
    if ((insn & 0xFFE0FC1F) == 0x1E202008 ||
        (insn & 0xFFE0FC1F) == 0x1E602008) {  // FCMP with #0.0
      const double a = dbl ? f64(rn) : static_cast<double>(f32(rn));
      const bool nan = std::isnan(a);
      state_.setFlags(!nan && a < 0.0, !nan && a == 0.0, nan || a >= 0.0, nan);
      return finish();
    }
    if (m == 0x1E202000 || m == 0x1E602000) {  // FCMP with register
      const double a = dbl ? f64(rn) : static_cast<double>(f32(rn));
      const double b = dbl ? f64(rm) : static_cast<double>(f32(rm));
      const bool nan = std::isnan(a) || std::isnan(b);
      state_.setFlags(!nan && a < b, !nan && a == b, nan || a >= b, nan);
      return finish();
    }

    // Integer <-> floating point conversions. The opcode keeps bits 20:10
    // fixed, so the mask covers them rather than treating them as registers.
    {
      const uint32_t conv = insn & 0xFFFFFC00;
      const uint64_t src_int = state_.x[rn];
      switch (conv) {
        case 0x1E220000: f32(rd) = static_cast<float>(static_cast<int32_t>(src_int)); return finish();
        case 0x9E220000: f32(rd) = static_cast<float>(static_cast<int64_t>(src_int)); return finish();
        case 0x1E620000: f64(rd) = static_cast<double>(static_cast<int32_t>(src_int)); return finish();
        case 0x9E620000: f64(rd) = static_cast<double>(static_cast<int64_t>(src_int)); return finish();
        case 0x1E230000: f32(rd) = static_cast<float>(static_cast<uint32_t>(src_int)); return finish();
        case 0x9E230000: f32(rd) = static_cast<float>(src_int); return finish();
        case 0x1E630000: f64(rd) = static_cast<double>(static_cast<uint32_t>(src_int)); return finish();
        case 0x9E630000: f64(rd) = static_cast<double>(src_int); return finish();
        case 0x1E380000: setRegWidth(rd, static_cast<uint64_t>(static_cast<int64_t>(f32(rn))), false, false); return finish();
        case 0x9E380000: setReg(rd, static_cast<uint64_t>(static_cast<int64_t>(f32(rn))), false); return finish();
        case 0x1E780000: setRegWidth(rd, static_cast<uint64_t>(static_cast<int64_t>(f64(rn))), false, false); return finish();
        case 0x9E780000: setReg(rd, static_cast<uint64_t>(static_cast<int64_t>(f64(rn))), false); return finish();
        case 0x1E390000: setRegWidth(rd, static_cast<uint64_t>(f32(rn)), false, false); return finish();
        case 0x9E390000: setReg(rd, static_cast<uint64_t>(f32(rn)), false); return finish();
        case 0x1E790000: setRegWidth(rd, static_cast<uint64_t>(f64(rn)), false, false); return finish();
        case 0x9E790000: setReg(rd, static_cast<uint64_t>(f64(rn)), false); return finish();
        default: break;
      }
    }
  }

  // NOP and unallocated encodings that are harmless in test payloads.
  if (insn == 0xD503201F) return finish();  // NOP

  return undefined(fault, insn, "unsupported AArch64 encoding");
}

StepResult CPU::run(Fault* fault, uint64_t max_instructions) {
  uint64_t executed = 0;
  for (;;) {
    const StepResult r = step(fault);
    if (r != StepResult::Continue) return r;
    if (max_instructions != 0 && ++executed >= max_instructions) {
      *fault = Fault{};
      fault->kind = FaultKind::HostAbort;
      fault->pc = state_.pc;
      fault->detail = "instruction budget exhausted";
      return StepResult::Fault;
    }
  }
}

}  // namespace aster

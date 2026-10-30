// Host-side tests for the Aster execution core. These run the same engine and
// the same guest images that the iOS app runs; only the host process differs.
#include <cinttypes>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "aster/cpu.h"
#include "aster/elf.h"
#include "aster/machine.h"
#include "aster/memory.h"

namespace {

int g_failures = 0;
int g_checks = 0;

#define CHECK(cond)                                                       \
  do {                                                                    \
    ++g_checks;                                                           \
    if (!(cond)) {                                                        \
      ++g_failures;                                                       \
      std::printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);       \
    }                                                                     \
  } while (0)

#define CHECK_STR(a, b)                                                   \
  do {                                                                    \
    ++g_checks;                                                           \
    const std::string va = (a);                                           \
    const std::string vb = (b);                                           \
    if (va != vb) {                                                       \
      ++g_failures;                                                       \
      std::printf("  FAIL %s:%d: %s == %s (\"%s\" vs \"%s\")\n",         \
                  __FILE__, __LINE__, #a, #b, va.c_str(), vb.c_str());    \
    }                                                                     \
  } while (0)

#define CHECK_EQ(a, b)                                                    \
  do {                                                                    \
    ++g_checks;                                                           \
    auto va = (a);                                                        \
    auto vb = (b);                                                        \
    if (!(va == vb)) {                                                    \
      ++g_failures;                                                       \
      std::printf("  FAIL %s:%d: %s == %s (%llu vs %llu)\n", __FILE__,    \
                  __LINE__, #a, #b, (unsigned long long)va,               \
                  (unsigned long long)vb);                                \
    }                                                                     \
  } while (0)

std::vector<uint8_t> readFile(const std::string& path) {
  std::vector<uint8_t> data;
  FILE* f = std::fopen(path.c_str(), "rb");
  if (f == nullptr) return data;
  std::fseek(f, 0, SEEK_END);
  const long size = std::ftell(f);
  std::fseek(f, 0, SEEK_SET);
  if (size > 0) {
    data.resize(static_cast<size_t>(size));
    if (std::fread(data.data(), 1, data.size(), f) != data.size()) data.clear();
  }
  std::fclose(f);
  return data;
}

std::string valueOf(const std::string& console, const std::string& key) {
  const std::string needle = key + "=";
  const size_t pos = console.find(needle);
  if (pos == std::string::npos) return "";
  const size_t start = pos + needle.size();
  const size_t end = console.find('\n', start);
  return console.substr(start, end == std::string::npos ? std::string::npos
                                                        : end - start);
}

uint64_t hexOf(const std::string& value) {
  return std::strtoull(value.c_str(), nullptr, 16);
}

uint64_t referenceChecksum() {
  uint64_t sum = 0;
  for (uint64_t i = 0; i < 200000ULL; ++i) {
    sum = (sum ^ i) * 6364136223846793005ULL + 1442695040888963407ULL;
  }
  return sum;
}

uint32_t referenceCrc32(const uint8_t* data, size_t len) {
  uint32_t crc = 0xFFFFFFFFu;
  for (size_t i = 0; i < len; ++i) {
    crc ^= data[i];
    for (int bit = 0; bit < 8; ++bit) {
      const uint32_t mask = 0u - (crc & 1u);
      crc = (crc >> 1) ^ (0xEDB88320u & mask);
    }
  }
  return ~crc;
}

uint32_t referenceDemoCrc32() {
  uint64_t state = 0x123456789ABCDEF0ULL;
  uint8_t buffer[4096];
  for (size_t i = 0; i < sizeof(buffer); ++i) {
    state = state * 6364136223846793005ULL + 1442695040888963407ULL;
    buffer[i] = static_cast<uint8_t>(state >> 33);
  }
  return referenceCrc32(buffer, sizeof(buffer));
}

uint64_t referenceSortSum() {
  uint32_t state = 0x12345678u;
  (void)state;
  uint64_t sum = 0;
  for (int i = 0; i < 256; ++i) {
    sum += static_cast<uint16_t>((i * 40503 + 17) & 0xFFFF);
  }
  return sum;
}

uint64_t referenceCopySum() {
  uint64_t state = 0x123456789ABCDEF0ULL;
  uint8_t buffer[4096];
  for (size_t i = 0; i < sizeof(buffer); ++i) {
    state = state * 6364136223846793005ULL + 1442695040888963407ULL;
    buffer[i] = static_cast<uint8_t>(state >> 33);
  }
  uint64_t sum = 0;
  for (int i = 0; i < 512; ++i) sum += static_cast<uint8_t>(buffer[i] ^ 0xA5);
  return sum;
}

std::string payloadDir;

aster::RunResult runPayload(const std::string& name, size_t ram = 64u << 20,
                            uint64_t budget = 0) {
  aster::MachineOptions options;
  options.ram_size = ram;
  options.max_instructions = budget;
  aster::Machine machine(options);
  std::string error;
  aster::RunResult result;
  if (!machine.init(&error)) {
    result.message = error;
    return result;
  }
  const auto image = readFile(payloadDir + "/" + name + ".elf");
  if (!machine.loadElf(image.data(), image.size(), &error)) {
    result.message = error;
    return result;
  }
  aster::RunResult out = machine.run();
  if (!out.ok) {
    std::printf("  debug: %s\n", out.message.c_str());
    if (!out.fault.detail.empty()) {
      std::printf("  debug: fault detail: %s pc=0x%llx insn=0x%08x\n",
                  out.fault.detail.c_str(),
                  static_cast<unsigned long long>(out.fault.pc),
                  out.fault.instruction);
    }
  }
  return out;
}

void testBitMasks() {
  uint64_t w = 0, t = 0;
  CHECK(aster::CPU::decodeBitMasks(true, 1, 7, 0, true, &w, &t));
  CHECK_EQ(w, 0xFFULL);
  CHECK(aster::CPU::decodeBitMasks(false, 0, 7, 0, true, &w, &t));
  CHECK_EQ(w, 0xFFULL);
  // 32-bit logical mask with rotation: imms=1, immr=8 -> 0xC0000000
  CHECK(aster::CPU::decodeBitMasks(false, 0, 1, 8, true, &w, &t));
  CHECK_EQ(w, 0x03000000ULL);
  // Immediate form rejects S == levels.
  CHECK(!aster::CPU::decodeBitMasks(true, 1, 63, 0, true, &w, &t));
}

void testMemory() {
  aster::GuestMemory mem;
  std::string error;
  CHECK(mem.init(1u << 20, aster::kRamBase));
  CHECK_EQ(mem.base_addr(), aster::kRamBase);

  uint32_t value = 0x11223344;
  aster::Fault fault;
  CHECK(mem.write(aster::kRamBase, &value, 4, &fault));
  uint32_t readback = 0;
  CHECK(mem.read(aster::kRamBase, &readback, 4, &fault));
  CHECK_EQ(readback, value);

  CHECK(mem.protect(aster::kRamBase + 4096, 4096, aster::kProtRead));
  CHECK(mem.write(aster::kRamBase + 8192, &value, 4, &fault));
  aster::Fault denied;
  CHECK(!mem.write(aster::kRamBase + 4096, &value, 4, &denied));
  CHECK(denied.kind == aster::FaultKind::Permissions);

  aster::Fault unmapped;
  CHECK(!mem.read(0x1000, &readback, 4, &unmapped));
  CHECK(unmapped.kind == aster::FaultKind::Unmapped);

  // 4 KiB guest granularity on a 16 KiB host page: the neighbouring page must
  // remain writable when only one is protected.
  CHECK(mem.write(aster::kRamBase + 4096 - 4, &value, 4, &fault));
  CHECK(mem.write(aster::kRamBase + 4096 + 4096, &value, 4, &fault));
}

void testElfRejectsGarbage() {
  aster::GuestMemory mem;
  CHECK(mem.init(1u << 20, aster::kRamBase));
  const uint8_t garbage[64] = {0};
  const aster::ElfImage image = aster::loadElf64AArch64(mem, garbage, sizeof(garbage));
  CHECK(!image.ok);
}

void testHello() {
  const aster::RunResult r = runPayload("hello");
  CHECK(r.ok);
  CHECK_EQ(r.exit_code, 0);
  CHECK(r.console.find("ASTER-PHASE1 OK") != std::string::npos);
  CHECK_EQ(hexOf(valueOf(r.console, "checksum")), referenceChecksum());
  CHECK_STR(valueOf(r.console, "data_ok"), "1");
  CHECK_STR(valueOf(r.console, "bss_ok"), "1");
  CHECK_STR(valueOf(r.console, "unaligned_ok"), "1");
  CHECK(r.instructions > 1000000ULL);
}

void testDemo() {
  const aster::RunResult r = runPayload("demo");
  CHECK(r.ok);
  CHECK_EQ(r.exit_code, 0);
  CHECK(r.console.find("ASTER-DEMO OK") != std::string::npos);
  CHECK_EQ(hexOf(valueOf(r.console, "crc32")), referenceDemoCrc32());
  CHECK_STR(valueOf(r.console, "fib20"), "6765");
  CHECK_STR(valueOf(r.console, "sorted"), "1");
  CHECK_EQ(std::strtoull(valueOf(r.console, "sort_sum").c_str(), nullptr, 10),
           referenceSortSum());
  CHECK_EQ(std::strtoull(valueOf(r.console, "copy_sum").c_str(), nullptr, 10),
           referenceCopySum());
}

void testThreads() {
  const aster::RunResult r = runPayload("threads", 128u << 20);
  CHECK(r.ok);
  CHECK_EQ(r.exit_code, 0);
  CHECK(r.console.find("ASTER-THREADS OK") != std::string::npos);
  CHECK_STR(valueOf(r.console, "spawned"), "4");
  CHECK_EQ(std::strtoull(valueOf(r.console, "counter").c_str(), nullptr, 10),
           100000ULL);
  CHECK_EQ(r.threads_created, 4u);
  CHECK_EQ(r.threads.size(), size_t{5});
}

void testFloatingPoint() {
  const aster::RunResult r = runPayload("fp");
  CHECK(r.ok);
  CHECK_EQ(r.exit_code, 0);
  CHECK(r.console.find("ASTER-FP OK") != std::string::npos);
  CHECK_STR(valueOf(r.console, "double_milli"), "2833");
  CHECK_STR(valueOf(r.console, "float_tenths"), "35");
}

void testFaults() {
  const aster::RunResult unmapped = runPayload("fault_unmapped");
  CHECK(!unmapped.ok);
  CHECK(unmapped.fault.kind == aster::FaultKind::Unmapped);
  CHECK_EQ(unmapped.fault.address, 0x1000ULL);

  const aster::RunResult rodata = runPayload("fault_rodata");
  CHECK(!rodata.ok);
  CHECK(rodata.fault.kind == aster::FaultKind::Permissions);
}

void testInstructionBudget() {
  const aster::RunResult r = runPayload("hello", 64u << 20, 5000);
  CHECK(!r.ok);
  CHECK(r.fault.kind == aster::FaultKind::HostAbort);
  CHECK(r.fault.detail.find("budget") != std::string::npos);
}

void testTelemetry() {
  const aster::RunResult r = runPayload("hello");
  CHECK(r.ok);
  CHECK(r.host_peak_rss > 0);
  CHECK(r.guest_ram_bytes == (64u << 20));
  CHECK(r.wall_ns > 0);
  const double mips = r.instructions / (static_cast<double>(r.wall_ns) / 1e3);
  std::printf("  telemetry: %llu instructions in %.3f ms = %.1f MIPS,"
              " peak RSS %.1f MiB\n",
              static_cast<unsigned long long>(r.instructions),
              r.wall_ns / 1e6, mips,
              static_cast<double>(r.host_peak_rss) / (1024.0 * 1024.0));
}

struct Test {
  const char* name;
  void (*fn)();
};

}  // namespace

int main(int argc, char** argv) {
  payloadDir = argc > 1 ? argv[1] : "build/guest";
  const Test tests[] = {
      {"bitmask-immediate", testBitMasks},
      {"guest-memory", testMemory},
      {"elf-rejects-garbage", testElfRejectsGarbage},
      {"hello", testHello},
      {"demo", testDemo},
      {"threads", testThreads},
      {"floating-point", testFloatingPoint},
      {"faults", testFaults},
      {"instruction-budget", testInstructionBudget},
      {"telemetry", testTelemetry},
  };

  for (const auto& test : tests) {
    std::printf("[ RUN ] %s\n", test.name);
    const int before = g_failures;
    test.fn();
    std::printf("[%s] %s\n", g_failures == before ? "  OK  " : " FAIL ",
                test.name);
  }
  std::printf("\n%d checks, %d failures\n", g_checks, g_failures);
  return g_failures == 0 ? 0 : 1;
}

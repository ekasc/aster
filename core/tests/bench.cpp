// Measurement harness: runs each guest payload and prints one JSON line per
// run. This is the artifact behind the numbers in docs/phase1.md.
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "aster/machine.h"

namespace {

std::vector<unsigned char> readFile(const std::string& path) {
  std::vector<unsigned char> data;
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

}  // namespace

int main(int argc, char** argv) {
  const std::string dir = argc > 1 ? argv[1] : "build/guest";
  const char* payloads[] = {"hello", "demo", "threads", "fp"};
  for (const char* name : payloads) {
    aster::MachineOptions options;
    options.ram_size = 256ull * 1024 * 1024;
    options.cpu_count = 4;
    aster::Machine machine(options);
    std::string error;
    if (!machine.init(&error)) {
      std::printf("{\"payload\":\"%s\",\"error\":\"%s\"}\n", name, error.c_str());
      continue;
    }
    const auto image = readFile(dir + "/" + name + ".elf");
    if (!machine.loadElf(image.data(), image.size(), &error)) {
      std::printf("{\"payload\":\"%s\",\"error\":\"%s\"}\n", name, error.c_str());
      continue;
    }
    const aster::RunResult r = machine.run();
    const double mips = r.wall_ns ? r.instructions / (static_cast<double>(r.wall_ns) / 1e3) : 0;
    std::printf(
        "{\"payload\":\"%s\",\"ok\":%s,\"exit\":%d,\"fault\":\"%s\","
        "\"instructions\":%llu,\"wall_ms\":%.3f,\"mips\":%.1f,"
        "\"guest_ram_mb\":%llu,\"peak_rss_mb\":%.2f,\"threads\":%u}\n",
        name, r.ok ? "true" : "false", r.exit_code,
        aster::faultKindName(r.fault.kind),
        static_cast<unsigned long long>(r.instructions),
        r.wall_ns / 1e6, mips,
        static_cast<unsigned long long>(r.guest_ram_bytes / (1024 * 1024)),
        static_cast<double>(r.host_peak_rss) / (1024.0 * 1024.0),
        r.threads_created);
  }
  return 0;
}

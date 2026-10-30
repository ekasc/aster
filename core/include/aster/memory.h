// Guest physical memory for Aster.
//
// The Phase 1 engine runs with the guest MMU off, so guest virtual addresses
// are guest physical addresses. That is deliberate: it makes the execution
// gate independent of MMU emulation, and Phase 2 moves the MMU into QEMU TCG.
//
// Host pages are 16 KiB on Apple silicon while the AArch64 guest contract we
// test against is 4 KiB. The backing store is therefore allocated with mmap and
// managed in 4 KiB guest pages, with every access bounds- and permission-checked
// before it touches host memory.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#include "aster/types.h"

namespace aster {

class GuestMemory {
 public:
  GuestMemory() = default;
  ~GuestMemory();

  GuestMemory(const GuestMemory&) = delete;
  GuestMemory& operator=(const GuestMemory&) = delete;

  // size must be a multiple of kGuestPageSize. Returns false and sets error().
  bool init(size_t size, uint64_t base = kRamBase);

  // Convenience for tests: map a fixed image at its link address.
  bool map_image(uint64_t guest_addr, const void* data, size_t size,
                 uint32_t prot);

  bool protect(uint64_t guest_addr, size_t size, uint32_t prot);

  // Checked accessors. A false return leaves `fault` populated.
  bool read(uint64_t addr, void* out, size_t len, Fault* fault) const;
  bool write(uint64_t addr, const void* in, size_t len, Fault* fault);
  bool fetch(uint64_t addr, uint32_t* out, Fault* fault) const;

  // Unchecked host pointer, only valid after rangeOk() succeeded.
  uint8_t* host(uint64_t addr) { return base_ + (addr - base_addr_); }
  const uint8_t* host(uint64_t addr) const {
    return base_ + (addr - base_addr_);
  }

  bool rangeOk(uint64_t addr, size_t len) const;

  uint64_t base_addr() const { return base_addr_; }
  size_t size() const { return size_; }
  uint64_t end_addr() const { return base_addr_ + size_; }
  bool contains(uint64_t addr, size_t len) const;

  const std::string& error() const { return error_; }

  // Page state is exposed for diagnostics and tests.
  uint32_t pageProt(uint64_t addr) const;
  size_t mappedPages() const;
  size_t residentBytes() const;

 private:
  bool fail(const std::string& msg);
  bool check(uint64_t addr, size_t len, Access access, Fault* fault) const;

  uint8_t* base_ = nullptr;
  size_t size_ = 0;
  uint64_t base_addr_ = kRamBase;
  std::vector<uint8_t> prot_;  // one byte per 4 KiB guest page
  std::string error_;
};

// Permission bits mirror the Linux PROT_* values the guest image carries.
constexpr uint32_t kProtRead = 1u << 0;
constexpr uint32_t kProtWrite = 1u << 1;
constexpr uint32_t kProtExec = 1u << 2;

}  // namespace aster

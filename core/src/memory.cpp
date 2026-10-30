#include "aster/memory.h"

#include <sys/mman.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>

namespace aster {
namespace {

size_t hostPageSize() {
  long ps = sysconf(_SC_PAGESIZE);
  return ps > 0 ? static_cast<size_t>(ps) : 16384;
}

size_t roundUp(size_t value, size_t align) {
  return (value + align - 1) & ~(align - 1);
}

}  // namespace

GuestMemory::~GuestMemory() {
  if (base_ != nullptr) {
    munmap(base_, roundUp(size_, hostPageSize()));
  }
}

bool GuestMemory::fail(const std::string& msg) {
  error_ = msg;
  return false;
}

bool GuestMemory::init(size_t size, uint64_t base) {
  if (base_ != nullptr) return fail("memory already initialised");
  if (size == 0 || (size % kGuestPageSize) != 0) {
    return fail("guest RAM size must be a non-zero multiple of 4 KiB");
  }
  if ((base % kGuestPageSize) != 0) return fail("guest RAM base must be 4 KiB aligned");
  if (base + size < base) return fail("guest RAM range overflows");

  const size_t host = hostPageSize();
  const size_t mapSize = roundUp(size, host);
  void* p = mmap(nullptr, mapSize, PROT_READ | PROT_WRITE,
                 MAP_PRIVATE | MAP_ANON, -1, 0);
  if (p == MAP_FAILED) {
    return fail(std::string("mmap failed: ") + std::strerror(errno));
  }
  base_ = static_cast<uint8_t*>(p);
  size_ = size;
  base_addr_ = base;
  // Fault everything in lazily; the guest touches only what it uses and iOS
  // charges resident pages against the app's footprint.
  madvise(base_, mapSize, MADV_NORMAL);
  // Flat RAM starts fully readable and writable, like the physical pages a
  // bootloader hands to a kernel. The ELF loader then narrows protection:
  // executable segments become RX, read-only data becomes R.
  prot_.assign(size / kGuestPageSize,
               static_cast<uint8_t>(kProtRead | kProtWrite));
  return true;
}

bool GuestMemory::contains(uint64_t addr, size_t len) const {
  if (base_ == nullptr) return false;
  if (addr < base_addr_) return false;
  uint64_t offset = addr - base_addr_;
  if (offset > size_) return false;
  return len <= size_ - static_cast<size_t>(offset);
}

bool GuestMemory::rangeOk(uint64_t addr, size_t len) const {
  return contains(addr, len);
}

bool GuestMemory::check(uint64_t addr, size_t len, Access access,
                        Fault* fault) const {
  if (len == 0) return true;
  if (!contains(addr, len)) {
    if (fault != nullptr) {
      fault->kind = FaultKind::Unmapped;
      fault->address = addr;
      fault->length = len;
      fault->access = access;
    }
    return false;
  }
  const uint32_t need = access == Access::Read      ? kProtRead
                        : access == Access::Write   ? kProtWrite
                                                    : kProtExec;
  const size_t first = static_cast<size_t>((addr - base_addr_) / kGuestPageSize);
  const size_t last =
      static_cast<size_t>((addr + len - 1 - base_addr_) / kGuestPageSize);
  for (size_t page = first; page <= last; ++page) {
    if ((prot_[page] & need) == 0) {
      if (fault != nullptr) {
        fault->kind = FaultKind::Permissions;
        fault->address = addr;
        fault->length = len;
        fault->access = access;
      }
      return false;
    }
  }
  return true;
}

bool GuestMemory::map_image(uint64_t addr, const void* data, size_t size,
                            uint32_t prot) {
  if (!rangeOk(addr, size)) return fail("image does not fit in guest RAM");
  if (!protect(addr, size, kProtRead | kProtWrite)) return false;
  std::memcpy(host(addr), data, size);
  return protect(addr, size, prot);
}

bool GuestMemory::protect(uint64_t addr, size_t size, uint32_t prot) {
  if (size == 0) return true;
  if (!rangeOk(addr, size)) return fail("protect range outside guest RAM");
  const size_t first = static_cast<size_t>((addr - base_addr_) / kGuestPageSize);
  const size_t last =
      static_cast<size_t>((addr + size - 1 - base_addr_) / kGuestPageSize);
  for (size_t page = first; page <= last; ++page) prot_[page] = static_cast<uint8_t>(prot);
  return true;
}

bool GuestMemory::read(uint64_t addr, void* out, size_t len, Fault* fault) const {
  if (!check(addr, len, Access::Read, fault)) return false;
  std::memcpy(out, host(addr), len);
  return true;
}

bool GuestMemory::write(uint64_t addr, const void* in, size_t len, Fault* fault) {
  if (!check(addr, len, Access::Write, fault)) return false;
  std::memcpy(host(addr), in, len);
  return true;
}

bool GuestMemory::fetch(uint64_t addr, uint32_t* out, Fault* fault) const {
  if (!check(addr, sizeof(uint32_t), Access::Execute, fault)) return false;
  std::memcpy(out, host(addr), sizeof(uint32_t));
  return true;
}

uint32_t GuestMemory::pageProt(uint64_t addr) const {
  if (!contains(addr, 1)) return 0;
  return prot_[static_cast<size_t>((addr - base_addr_) / kGuestPageSize)];
}

size_t GuestMemory::mappedPages() const {
  size_t n = 0;
  for (uint8_t p : prot_) {
    if (p != 0) ++n;
  }
  return n;
}

size_t GuestMemory::residentBytes() const {
  // The backing store is anonymous private memory. Resident bytes are measured
  // by the host layer (task_info) rather than guessed here.
  return size_;
}

}  // namespace aster

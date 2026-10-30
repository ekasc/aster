#include "aster/elf.h"

#include <algorithm>
#include <cstdio>
#include <cstring>

namespace aster {
namespace {

struct Elf64Ehdr {
  uint8_t e_ident[16];
  uint16_t e_type;
  uint16_t e_machine;
  uint32_t e_version;
  uint64_t e_entry;
  uint64_t e_phoff;
  uint64_t e_shoff;
  uint32_t e_flags;
  uint16_t e_ehsize;
  uint16_t e_phentsize;
  uint16_t e_phnum;
  uint16_t e_shentsize;
  uint16_t e_shnum;
  uint16_t e_shstrndx;
};

struct Elf64Phdr {
  uint32_t p_type;
  uint32_t p_flags;
  uint64_t p_offset;
  uint64_t p_vaddr;
  uint64_t p_paddr;
  uint64_t p_filesz;
  uint64_t p_memsz;
  uint64_t p_align;
};

constexpr uint16_t kEtExec = 2;
constexpr uint16_t kEtDyn = 3;
constexpr uint16_t kEmAArch64 = 183;
constexpr uint32_t kPtLoad = 1;
constexpr uint32_t kPtInterp = 3;

ElfImage bad(const std::string& message) {
  ElfImage image;
  image.error = message;
  return image;
}

}  // namespace

ElfImage loadElf64AArch64(GuestMemory& memory, const uint8_t* data, size_t size) {
  if (data == nullptr || size < sizeof(Elf64Ehdr)) return bad("image too small");
  Elf64Ehdr eh;
  std::memcpy(&eh, data, sizeof(eh));
  if (eh.e_ident[0] != 0x7F || eh.e_ident[1] != 'E' ||
      eh.e_ident[2] != 'L' || eh.e_ident[3] != 'F') {
    return bad("not an ELF file");
  }
  if (eh.e_ident[4] != 2) return bad("not ELFCLASS64");
  if (eh.e_ident[5] != 1) return bad("not little-endian");
  if (eh.e_machine != kEmAArch64) return bad("not EM_AARCH64");
  if (eh.e_type != kEtExec && eh.e_type != kEtDyn) {
    return bad("unsupported ELF type " + std::to_string(eh.e_type));
  }
  if (eh.e_phentsize != sizeof(Elf64Phdr) || eh.e_phnum == 0) {
    return bad("invalid program header table");
  }
  if (eh.e_phoff > size ||
      eh.e_phoff + static_cast<uint64_t>(eh.e_phnum) * sizeof(Elf64Phdr) > size) {
    return bad("program headers outside file");
  }

  ElfImage image;
  image.entry = eh.e_entry;
  image.load_low = ~0ULL;
  image.load_high = 0;

  bool entry_covered = false;
  for (uint16_t i = 0; i < eh.e_phnum; ++i) {
    Elf64Phdr ph;
    std::memcpy(&ph, data + eh.e_phoff + i * sizeof(Elf64Phdr), sizeof(ph));
    if (ph.p_type == kPtInterp) {
      const char* start = reinterpret_cast<const char*>(data + ph.p_offset);
      if (ph.p_offset < size) {
        image.interpreter.assign(start, strnlen(start, size - ph.p_offset));
      }
      continue;
    }
    if (ph.p_type != kPtLoad) continue;
    if (ph.p_memsz == 0) continue;
    if (ph.p_filesz > ph.p_memsz) return bad("PT_LOAD filesz > memsz");
    if (ph.p_offset + ph.p_filesz > size) return bad("PT_LOAD outside file");
    if (ph.p_vaddr < memory.base_addr() ||
        ph.p_vaddr + ph.p_memsz > memory.end_addr()) {
      char buf[160];
      std::snprintf(buf, sizeof(buf),
                    "PT_LOAD %llx..%llx outside guest RAM %llx..%llx",
                    static_cast<unsigned long long>(ph.p_vaddr),
                    static_cast<unsigned long long>(ph.p_vaddr + ph.p_memsz),
                    static_cast<unsigned long long>(memory.base_addr()),
                    static_cast<unsigned long long>(memory.end_addr()));
      return bad(buf);
    }

    const uint32_t prot = (ph.p_flags & 0x1 ? kProtExec : 0) |
                          (ph.p_flags & 0x2 ? kProtWrite : 0) |
                          (ph.p_flags & 0x4 ? kProtRead : 0);
    // Map writable while copying, then apply final protection.
    if (!memory.protect(ph.p_vaddr, ph.p_memsz, kProtRead | kProtWrite)) {
      return bad(memory.error());
    }
    if (ph.p_filesz != 0) {
      std::memcpy(memory.host(ph.p_vaddr), data + ph.p_offset, ph.p_filesz);
    }
    if (ph.p_memsz > ph.p_filesz) {
      std::memset(memory.host(ph.p_vaddr + ph.p_filesz), 0,
                  ph.p_memsz - ph.p_filesz);
    }
    if (!memory.protect(ph.p_vaddr, ph.p_memsz, prot)) {
      return bad(memory.error());
    }

    image.load_low = std::min(image.load_low, ph.p_vaddr);
    image.load_high = std::max(image.load_high, ph.p_vaddr + ph.p_memsz);
    if (eh.e_entry >= ph.p_vaddr && eh.e_entry < ph.p_vaddr + ph.p_memsz) {
      entry_covered = true;
    }
  }

  if (image.load_high == 0) return bad("no PT_LOAD segments");
  if (!entry_covered) return bad("entry point is not inside a PT_LOAD segment");
  image.ok = true;
  return image;
}

}  // namespace aster

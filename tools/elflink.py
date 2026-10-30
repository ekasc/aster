#!/usr/bin/env python3
"""Minimal ELF64/AArch64 static linker for Aster guest images.

The host has no working ELF linker (Xcode's ld is Mach-O only and the Rust
toolchain's lld is missing its libLLVM). This tool links the small, fixed-address
freestanding payloads that Phase 1 needs: one or more ET_REL objects, no shared
libraries, no TLS, no dynamic relocations.

Supported relocations are the ones clang emits for -fno-pic -mcmodel=small
-mgeneral-regs-only code. Anything else fails loudly rather than producing a
silently broken image.
"""

from __future__ import annotations

import argparse
import struct
import sys

ET_REL = 1
ET_EXEC = 2
EM_AARCH64 = 183
SHT_RELA = 4
SHT_NOBITS = 8
SHT_SYMTAB = 2
SHF_ALLOC = 0x2
SHF_WRITE = 0x1
SHF_EXECINSTR = 0x4
SHN_ABS = 0xFFF1

PT_LOAD = 1
PT_GNU_STACK = 0x6474E551


class LinkError(Exception):
    pass


class Section:
    def __init__(self, index, sh):
        (self.name_off, self.type, self.flags, self.addr, self.offset,
         self.size, self.link, self.info, self.align, self.entsize) = sh
        self.index = index
        self.name = ""
        self.data = b""
        self.out_addr = 0
        self.out_offset = 0

    @property
    def alloc(self):
        return bool(self.flags & SHF_ALLOC)

    @property
    def executable(self):
        return bool(self.flags & SHF_EXECINSTR)

    @property
    def writable(self):
        return bool(self.flags & SHF_WRITE)


def parse_elf(data):
    if data[:4] != b"\x7fELF":
        raise LinkError("input is not an ELF file")
    if data[4] != 2 or data[5] != 1:
        raise LinkError("input must be ELFCLASS64 little-endian")
    (e_type, e_machine, _, e_entry, e_phoff, e_shoff, _, _, e_phentsize,
     e_phnum, e_shentsize, e_shnum, e_shstrndx) = struct.unpack_from(
        "<HHIQQQIHHHHHH", data, 16)
    if e_type != ET_REL:
        raise LinkError(f"input must be ET_REL, got {e_type}")
    if e_machine != EM_AARCH64:
        raise LinkError(f"input must be EM_AARCH64, got {e_machine}")
    if e_shoff == 0 or e_shnum == 0:
        raise LinkError("input has no section headers")

    sections = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        sh = struct.unpack_from("<IIQQQQIIQQ", data, off)
        sections.append(Section(i, sh))

    shstr = sections[e_shstrndx]
    strtab = data[shstr.offset:shstr.offset + shstr.size]
    for s in sections:
        end = strtab.find(b"\0", s.name_off)
        s.name = strtab[s.name_off:end].decode("utf-8", "replace")

    for s in sections:
        if s.type == SHT_NOBITS:
            s.data = b"\0" * s.size
        elif s.type != 8:
            s.data = data[s.offset:s.offset + s.size]

    return sections


def parse_symbols(sections, data):
    symtab = next((s for s in sections if s.type == SHT_SYMTAB), None)
    if symtab is None:
        raise LinkError("input has no .symtab")
    strsec = sections[symtab.link]
    strings = strsec.data
    count = symtab.size // 24
    symbols = []
    for i in range(count):
        name_off, info, other, shndx, value, size = struct.unpack_from(
            "<IBBHQQ", data, symtab.offset + i * 24)
        end = strings.find(b"\0", name_off)
        name = strings[name_off:end].decode("utf-8", "replace")
        symbols.append({"name": name, "info": info, "shndx": shndx,
                        "value": value, "size": size, "other": other})
    return symbols


def align_up(value, alignment):
    if alignment <= 1:
        return value
    return (value + alignment - 1) & ~(alignment - 1)


def apply_relocation(kind, insn, s, a, p):
    """Return the patched 32-bit word for the given relocation."""
    if kind == 0:  # R_AARCH64_NONE
        return insn
    if kind == 257:  # ABS64 handled by caller
        return (s + a) & 0xFFFFFFFFFFFFFFFF
    if kind == 258:  # ABS32
        return (s + a) & 0xFFFFFFFF
    if kind == 261:  # PREL32
        return (s + a - p) & 0xFFFFFFFF
    if kind in (275, 276):  # ADR_PREL_PG_HI21(_NC)
        imm = ((s + a) & ~0xFFF) - (p & ~0xFFF)
        if imm < -(1 << 32) or imm >= (1 << 32):
            raise LinkError("ADR_PREL_PG_HI21 out of range")
        imm >>= 12
        immlo = imm & 0x3
        immhi = (imm >> 2) & 0x7FFFF
        return (insn & ~0x60FFFFE0) | (immlo << 29) | (immhi << 5)
    if kind == 277:  # ADD_ABS_LO12_NC
        return (insn & ~0x003FFC00) | (((s + a) & 0xFFF) << 10)
    if kind == 278:  # LDST8_ABS_LO12_NC
        return (insn & ~0x003FFC00) | (((s + a) & 0xFFF) << 10)
    # The mask is 12 bits (the low page), not 16: keeping bit 12 would spill
    # into the opcode bits above imm12.
    if kind == 284:  # LDST16_ABS_LO12_NC
        return (insn & ~0x003FFC00) | (((s + a) & 0xFFE) << 9)
    if kind == 285:  # LDST32_ABS_LO12_NC
        return (insn & ~0x003FFC00) | (((s + a) & 0xFFC) << 8)
    if kind == 286:  # LDST64_ABS_LO12_NC
        return (insn & ~0x003FFC00) | (((s + a) & 0xFF8) << 7)
    if kind == 299:  # LDST128_ABS_LO12_NC
        return (insn & ~0x003FFC00) | (((s + a) & 0xFF0) << 6)
    if kind in (282, 283):  # JUMP26 / CALL26
        delta = s + a - p
        if delta & 0x3:
            raise LinkError("branch target is not 4-byte aligned")
        if not -(1 << 27) <= delta < (1 << 27):
            raise LinkError("branch target out of range for B/BL")
        imm26 = (delta >> 2) & 0x3FFFFFF
        return (insn & 0xFC000000) | imm26
    if kind == 280:  # CONDBR19
        delta = s + a - p
        if delta & 0x3:
            raise LinkError("conditional branch target is not 4-byte aligned")
        imm19 = (delta >> 2) & 0x7FFFF
        return (insn & ~0x00FFFFE0) | (imm19 << 5)
    if kind == 279:  # TSTBR14
        delta = s + a - p
        if delta & 0x3:
            raise LinkError("test branch target is not 4-byte aligned")
        imm14 = (delta >> 2) & 0x3FFF
        return (insn & ~0x0007FFE0) | (imm14 << 5)
    if kind in (263, 264, 265, 266, 267, 268, 269):  # MOVW_UABS_G0..G3
        shift = {263: 0, 264: 0, 265: 16, 266: 16, 267: 32, 268: 32, 269: 48}[kind]
        imm16 = ((s + a) >> shift) & 0xFFFF
        return (insn & ~0x001FFFE0) | (imm16 << 5)
    if kind == 273:  # LD_PREL_LO19
        delta = s + a - p
        imm19 = (delta >> 2) & 0x7FFFF
        return (insn & ~0x00FFFFE0) | (imm19 << 5)
    raise LinkError(f"unsupported relocation type {kind}")


def link(objects, entry_name, base):
    parsed = []
    all_sections = []
    for data in objects:
        obj_sections = parse_elf(data)
        obj_symbols = parse_symbols(obj_sections, data)
        parsed.append((obj_sections, obj_symbols))
        all_sections.extend(obj_sections)

    alloc = [s for s in all_sections if s.alloc and s.size > 0]
    if not alloc:
        raise LinkError("no allocatable sections")

    rx = [s for s in alloc if s.executable or not s.writable]
    rw = [s for s in alloc if s.writable]

    cursor = base
    for s in rx:
        cursor = align_up(cursor, max(s.align, 1))
        s.out_addr = cursor
        cursor += s.size
    rx_end = cursor

    cursor = align_up(rx_end, 4096)
    rw_start = cursor
    for s in rw:
        cursor = align_up(cursor, max(s.align, 1))
        s.out_addr = cursor
        cursor += s.size
    rw_end = cursor
    rw_mem_end = align_up(rw_end, 4096)

    sym_addr = {}
    for obj_sections, obj_symbols in parsed:
        for sym in obj_symbols:
            if sym["shndx"] == 0 or not sym["name"]:
                continue
            if sym["name"].startswith("$"):
                continue
            if sym["shndx"] == SHN_ABS:
                addr = sym["value"]
            elif sym["shndx"] < len(obj_sections):
                sec = obj_sections[sym["shndx"]]
                if not sec.alloc or sec.out_addr == 0:
                    continue
                addr = sec.out_addr + sym["value"]
            else:
                continue
            is_global = (sym["info"] >> 4) == 1
            if (is_global and sym["name"] in sym_addr and
                    sym_addr[sym["name"]] != addr):
                raise LinkError(f"duplicate symbol {sym['name']!r}")
            sym_addr[sym["name"]] = addr

    entry = sym_addr.get(entry_name)
    if entry is None:
        raise LinkError(f"entry symbol {entry_name!r} not found")

    for obj_sections, obj_symbols in parsed:
        for sec in obj_sections:
            if sec.type != SHT_RELA:
                continue
            target = obj_sections[sec.info]
            if not target.alloc or target.out_addr == 0:
                continue
            blob = bytearray(target.data)
            count = sec.size // 24
            for i in range(count):
                r_offset, r_info, r_addend = struct.unpack_from(
                    "<QQq", target_rela_bytes(obj_sections, sec, i))
                r_sym = r_info >> 32
                r_type = r_info & 0xFFFFFFFF
                sym = obj_symbols[r_sym]
                if sym["shndx"] == 0:
                    if sym["name"] not in sym_addr:
                        raise LinkError(f"undefined symbol {sym['name']!r}")
                    s = sym_addr[sym["name"]]
                elif sym["shndx"] == SHN_ABS:
                    s = sym["value"]
                else:
                    s = obj_sections[sym["shndx"]].out_addr + sym["value"]
                p = target.out_addr + r_offset
                if r_type == 257:
                    blob = _ensure(blob, r_offset + 8)
                    struct.pack_into("<Q", blob, r_offset,
                                     (s + r_addend) & 0xFFFFFFFFFFFFFFFF)
                    continue
                blob = _ensure(blob, r_offset + 4)
                insn = struct.unpack_from("<I", blob, r_offset)[0]
                patched = apply_relocation(r_type, insn, s, r_addend, p)
                struct.pack_into("<I", blob, r_offset, patched & 0xFFFFFFFF)
            target.data = bytes(blob)

    file_base = 0x1000
    for s in alloc:
        if s.type == SHT_NOBITS:
            continue
        s.out_offset = file_base + (s.out_addr - base)

    rx_size = rx_end - base
    rw_file_size = rw_end - rw_start
    # The RW segment's file offset must be congruent with its vaddr modulo the
    # page size, so leave the alignment gap between RX and RW in the file.
    rw_offset = file_base + (rw_start - base)
    total = rw_offset + rw_file_size
    image = bytearray(b"\0" * total)

    ehdr = struct.pack(
        "<16sHHIQQQIHHHHHH", b"\x7fELF\x02\x01\x01" + b"\0" * 9,
        ET_EXEC, EM_AARCH64, 1, entry, 64, 0, 0, 64, 56, 2, 64, 0, 0)
    image[0:64] = ehdr

    def phdr(flags, offset, vaddr, filesz, memsz, align):
        return struct.pack("<IIQQQQQQ", PT_LOAD, flags, offset, vaddr, vaddr,
                           filesz, memsz, align)

    image[64:64 + 56] = phdr(5, file_base, base, rx_size, rx_size, 4096)
    image[120:120 + 56] = phdr(6, rw_offset, rw_start,
                               rw_file_size, rw_mem_end - rw_start, 4096)

    for s in alloc:
        if s.type == SHT_NOBITS or s.size == 0:
            continue
        end = s.out_offset + s.size
        if s.out_offset < file_base:
            raise LinkError("section overlaps ELF header")
        image[s.out_offset:end] = s.data

    return bytes(image), sym_addr, (base, rx_end), entry


def target_rela_bytes(sections, rela, index):
    """Read one 24-byte Rela entry.

    The Rela payload lives in the section object, so keep it addressable after
    parsing instead of holding the whole original file.
    """
    off = index * 24
    return rela.data[off:off + 24]


def _ensure(blob, size):
    if len(blob) < size:
        blob.extend(b"\0" * (size - len(blob)))
    return blob


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("objects", nargs="+")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--entry", default="_start")
    ap.add_argument("--base", default="0x40000000")
    ap.add_argument("--map", default=None, help="write a symbol map")
    args = ap.parse_args()

    base = int(args.base, 0)
    objects = [open(p, "rb").read() for p in args.objects]
    try:
        image, sym_addr, _, entry = link(objects, args.entry, base)
    except LinkError as exc:
        print(f"elflink: {exc}", file=sys.stderr)
        return 1

    with open(args.output, "wb") as fh:
        fh.write(image)
    if args.map:
        with open(args.map, "w") as fh:
            for name, addr in sorted(sym_addr.items(), key=lambda kv: kv[1]):
                fh.write(f"0x{addr:016x} {name}\n")
    print(f"elflink: {args.output} entry=0x{entry:x} size={len(image)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

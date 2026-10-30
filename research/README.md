# Research trail

Source files were read from upstream rather than vendored here. The ones that
changed a decision:

- XNU `bsd/kern/kern_mman.c` and `osfmk/vm/vm_map.c` — `MAP_JIT` is gated by the
  code-signing monitor (`vm_map_csm_allow_jit` → `csm_allow_jit_region`), which
  is why a JIT-less backend was chosen.
  https://github.com/apple-oss-distributions/xnu
- QEMU `meson_options.txt` (`tcg_interpreter`), `tcg/tci.c`, and
  `target/arm/cpu-param.h` (`TARGET_PAGE_BITS 12`) — TCI needs no executable
  memory and keeps 4 KiB guest pages.
  https://github.com/qemu/qemu
- QEMU `target/arm/tcg/translate-vfp.c` (`vfp_expand_imm`) — the exact
  `FMOV` immediate bit layout used by the interpreter.
- AOSP emulator and system-image documentation, plus the UTM, iSH, Android
  Translation Layer and Project Sandcastle trees, for what each project relies
  on and what it does not.
  https://source.android.com

The measurements that matter are in `docs/phase1.md`; they come from the device
and the host suite, not from these documents.

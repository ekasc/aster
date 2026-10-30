# Phase 1: execution feasibility

Gate: an ARM64 guest runs and produces verified output through the iOS host
application.

Status: **passed on the physical iPhone 17 (iOS 27.0), 6/6.**

## What executed

The host suite (`tools/build_host.sh`) ran the same engine and the same guest
images the iOS app embeds. 62 checks, 0 failures.

| Test | What it proves | Result |
|---|---|---|
| `bitmask-immediate` | `DecodeBitMasks` including `tmask` | pass |
| `guest-memory` | 4 KiB protection inside a 16 KiB host page, unmapped fault | pass |
| `elf-rejects-garbage` | loader refuses non-ELF input | pass |
| `hello` | integer execution, call/return, loops, .data, .bss, unaligned load | pass |
| `demo` | recursion, sorting, CRC32, byte/half/word memory traffic | pass |
| `threads` | 4 guest threads, LDXR/STXR atomic counter, join-by-counter | pass |
| `floating-point` | FMOV immediate, FMADD, FDIV, FMUL, FCVTZS, FCMP | pass |
| `faults` | write to `0x1000` → unmapped; write to .rodata → permissions | pass |
| `instruction-budget` | runaway guest is stopped with a typed fault | pass |
| `telemetry` | instructions, wall time, RSS, guest RAM are measured | pass |

The host reference values are computed independently in C++ from the same
algorithms: the guest's CRC32, sort sum, copy sum and LCG checksum are compared
against a native implementation, not against a recorded string.

## Measurements

MacBook Air M2 (8-core), macOS 27.2, `clang -O2`, 256 MiB guest RAM:

| Payload | Instructions | Wall time | Speed | Peak host RSS |
|---|---:|---:|---:|---:|
| hello | 1,002,695 | 11.7 ms | 85.7 MIPS | 1.84 MB |
| demo | 474,313 | 6.5 ms | 73.0 MIPS | 1.91 MB |
| threads (5 threads) | 883,245 | 30.6 ms | 28.9 MIPS | 2.12 MB |
| fp | 697 | 0.013 ms | 52.6 MIPS | 2.12 MB |

The threads number is lower because four worker threads plus the main thread
contend on one atomic counter through the exclusive monitor; the interpreter
itself is not slower.

Peak RSS stays near 2 MB despite 256 MiB of configured guest RAM: the backing
mapping is lazy and only touched pages count. That matters on a device where
jetsam, not the CPU, is the usual reason a process disappears.

These are host measurements. They are not presented as iPhone numbers. The
device gate prints the same fields so the two can be compared directly.

## Defects found and fixed during Phase 1

Every one of these was found by a failing guest, not by inspection:

1. **Condition inversion.** `evalCondition` switched on `cond >> 1` and ignored
   bit 0, so `NE`, `CC`, `PL`, `VC`, `LS`, `LT`, `LE` evaluated as their pairs.
   Every guest loop exited after one iteration. Fixed by applying the inversion
   bit after evaluating the base condition.
2. **Unscaled addressing ignored the offset.** `LDUR`/`STUR` and the pre-index
   forms used `base` instead of `base + imm`. Stack spills silently wrote to the
   wrong slot.
3. **Hypercall did not advance the PC.** The `HVC` handler returned `Continue`
   without `state_.pc = next`, so each hypercall executed twice and the first
   result became the second call id.
4. **ELF linker `LDST*_ABS_LO12_NC` mask.** The linker masked 16 bits instead
   of 12, so any unsigned-offset access whose address had bit 12 set was off by
   `0x1000`. Symptom: the thread counter printed 0 while the workers reported
   success.
5. **Guest HVC operand binding.** `x2`/`x3` were generic `"r"` operands in the
   inline assembly, so the compiler could place them in registers the ABI does
   not read. Small console writes produced empty strings; the arguments must be
   pinned to `x2` and `x3`.
6. **FP register accessor types.** Arithmetic used `uint32_t`/`uint64_t`
   lvalues, so `FADD` added bit patterns and `FCVTZS` returned the float's bits.
   Bit copies and arithmetic now use separate `float&`/`double&` and
   `uint32_t&`/`uint64_t&` accessors.
7. **`FMOV` direction inverted.** All four general-register/scalar transfers
   wrote the source register file. `FMOV Dd, Xn` zeroed `d3` and the double
   result became 0.
8. **`FMOV` immediate mantissa placement.** Single precision needs the 6-bit
   immediate at bits 22:17; it was placed at 25:20. `fmov s2, #10.0` became
   2.5625.

## Device results (iPhone 17, iPhone18,3, iOS 27.0 build 24A435)

`tools/run_on_device.sh`, Release build with the interpreter at `-O3`,
256 MiB guest RAM, 4 guest CPUs. Every result below came from the phone; none
of it is simulated.

| Payload | Instructions | Wall time | Speed | Peak RSS |
|---|---:|---:|---:|---:|
| hello | 1,002,695 | 10.6–18.3 ms | 55–95 MIPS | 38.5 MB |
| demo | 474,313 | 7.7–12.3 ms | 38–62 MIPS | 43.2 MB |
| threads (5 threads) | 773k–790k | 16.0–25.5 ms | 31–57 MIPS | 44.0 MB |
| fp | 697 | ≤0.02 ms | too short to measure | 44.0 MB |
| fault_unmapped | 113 | ≤0.11 ms | — | 44.4 MB |
| fault_rodata | 362 | ≤0.03 ms | — | 44.4 MB |

Instruction counts are stable across runs; wall time is not. The phone is
hand-held and thermally dynamic, so the speed column is a range over clean
runs, not a best-of.

`[gate] summary 6/6`.

The instruction count is identical to the host run (1,002,695 for hello),
which is the meaningful correctness signal. Peak RSS is the whole app process
and stays under 45 MB because guest RAM is touched lazily.

Screenshots: `docs/evidence/device-gate.png` (runtime, measured policy, first
three gate rows) and `docs/evidence/device-gate-rows.png`. Crash excerpt:
`docs/evidence/codesigning-crash.txt`.

Measured execution policy on the same device:

```
MAP_JIT   denied (errno 1 = EPERM)
mmap RWX  allowed
RW → RX   allowed
```

The two "allowed" results are syscall-level answers, not permission to execute.
The definitive experiment ran one unsigned page containing `mov w0,#1; ret`
after `mmap(RW)` and `mprotect(RX)`:

```
exception:   EXC_BAD_ACCESS, SIGKILL,
             KERN_PROTECTION_FAILURE at the mapped page
termination: namespace CODESIGNING, indicator "Invalid Page"
thread:      com.apple.main-thread,
             RuntimeController.probeUnsignedExecution()
```

The call did not return. iOS kills the process in the code-signing fault path
even though both mmap and mprotect succeeded. That is the measured basis for the
"no JIT" design decision; the crash report is in the device's system crash logs
(`Aster-*.ips`). The probe is reproducible with:

```
xcrun devicectl device process launch --device <udid> --console \
  --terminate-existing dev.ekas.aster --probe-exec
```

## Next step

1. Re-run `tools/run_on_device.sh` after any engine change; it refuses to pass
   on anything other than `[gate] summary 6/6`.
2. Cross-compile QEMU TCI for iOS arm64 using the recipe in
   `docs/phase2-plan.md`, then boot the same Alpine kernel from inside the app.
3. Once a Linux userspace boots on the device, record its time-to-shell next to
   the host TCI/JIT numbers (34.46 s vs 8.31 s) and decide whether the Android
   step is practical on this engine.

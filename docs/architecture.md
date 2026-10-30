# Aster architecture

Aster runs an ARM64 guest inside a signed iOS application. The app owns the
display, lifecycle and developer interface; the engine owns guest memory, CPU
state, traps and telemetry.

```
┌──────────────────────────────────────────────────────────────┐
│ iOS app (SwiftUI)                                            │
│  runtime card · measured execution policy · gate · console   │
├──────────────────────────────────────────────────────────────┤
│ C ABI  core/include/aster/aster.h                            │
│  aster_run_elf(image, options) -> result + streamed console  │
├──────────────────────────────────────────────────────────────┤
│ Machine  core/src/machine.cpp                                │
│  guest threads · hypercalls · console · fault capture        │
│  instruction and RSS accounting                              │
├───────────────────────────┬──────────────────────────────────┤
│ Compatible backend        │ Accelerated backend (not built)   │
│  Phase 1: AArch64         │  offline AOT into signed code,   │
│  interpreter (this repo)  │  isolated, never the baseline    │
│  Phase 2: QEMU TCG/TCI    │                                  │
├───────────────────────────┴──────────────────────────────────┤
│ GuestMemory  core/src/memory.cpp                             │
│  one anonymous mapping · 4 KiB guest pages · checked access  │
├──────────────────────────────────────────────────────────────┤
│ Guest image  ELF64/EM_AARCH64, fixed link address            │
│  freestanding payloads built by tools/elflink.py             │
└──────────────────────────────────────────────────────────────┘
```

## Why the Phase 1 engine is an interpreter

Stock iOS does not grant `MAP_JIT` to ordinary apps, so a TCG code cache is not
available. An interpreter executes the guest without ever asking the kernel for
executable anonymous memory, and it produces the property the project needs
first: evidence that guest instructions, memory, threads and traps work inside
the sandbox. See `docs/constrained-execution.md`.

The Phase 1 interpreter is deliberately narrow. It has **no MMU, no exception
vectors, and no devices**. Those omissions are what make its result a
measurement rather than a claim: every instruction it executes is one it
decoded, and every byte it touches is one `GuestMemory` allowed.

Phase 2 does not extend this interpreter into a Linux-capable system. It embeds
QEMU's TCI backend behind the same `Machine` and C ABI. QEMU already has the
MMU, GIC, timers, virtio and machine models; reimplementing them would be a
worse use of the same effort.

## Guest ABI

Guest code is a fixed-address ELF64 image loaded at `0x4000_0000`. It runs with
the MMU off, so guest virtual addresses are guest physical addresses. There is
no libc and no Linux syscall ABI in Phase 1. The only host interface is:

```
hvc #0
  x0     call id
  x1..x5 arguments
  x0     result, x1 auxiliary result
```

| id | call | behaviour |
|---|---|---|
| 0 | ping | returns `0x41535452` ("ASTR") |
| 1 | write | copies guest bytes to the console (fd 1/2/3) |
| 2 | exit | ends the calling thread's run |
| 3 | ticks | nanoseconds since machine start |
| 4 | spawn | creates a guest thread at `entry` with `arg` and `stack_top` |
| 5 | yield | host `sched_yield` |
| 6 | thread exit | ends a worker thread |
| 7 | read | returns 0; no console input in Phase 1 |
| 8 | sleep | bounded sleep that aborts when the machine stops |
| 9 | debug | writes to the debug console |
| 10 | cpu count | guest-visible CPU count |

The ABI is intentionally small. Android's real ABI arrives with the Linux
kernel in Phase 2; these calls only exist to make Phase 1 payloads observable
and testable.

## Threads and synchronisation

Each guest thread has its own `CPU` (registers, PC, exclusive monitor) and
shares one `GuestMemory`. `spawn` creates a host `std::thread` and returns a
guest tid; when the main thread halts, the machine sets a stop flag and joins
every worker.

`LDXR`/`STXR` are implemented with compare-on-store semantics: `STXR` succeeds
only if memory still holds the value the same thread loaded. That is stronger
than the architectural monitor and is sufficient for lock-free guest sequences.
It does **not** model a store from another CPU invalidating the monitor between
the load and the store, so a guest algorithm that depends on that invalidation
would need the Phase 2 engine. This limitation is stated here on purpose.

## Fault model

Every memory access is bounds- and permission-checked before it touches host
memory. A guest cannot fault the host process with a wild pointer. Faults are
typed (`unmapped`, `permissions`, `undefined-instruction`, `bad-hypercall`,
`host-abort`) and carry PC, instruction word and address. The app shows the
fault; the host suite asserts it.

The two fault payloads are inverted tests: `fault_unmapped` writes to `0x1000`
and must produce `unmapped`; `fault_rodata` writes to a read-only segment and
must produce `permissions`.

## Telemetry

`RunResult` reports guest RAM, instructions retired, wall time, peak and current
host RSS, threads created, per-thread instruction counts and the console. The
app prints one `[gate]` line per payload and a summary; the host harness prints
the same numbers as JSON lines.

RSS is measured with `task_info(MACH_TASK_BASIC_INFO)`. Guest RAM is allocated
with `mmap` and only touched pages become resident, so a 256 MiB guest RAM
configuration does not cost 256 MiB at boot.

## Repository map

```
core/include/aster/   types, memory, CPU, machine, ELF, hypercalls, C ABI
core/src/             interpreter, memory, loader, machine, platform, API
core/tests/           host correctness suite and measurement harness
guest/                freestanding AArch64 payloads and their runtime
tools/                guest build, ELF linker, Xcode project generator,
                      host test runner, device runner
app/Aster/            SwiftUI application
docs/                 constraints, architecture, Phase 1 evidence, Phase 2 plan
```

## Accelerated backend (experimental, gated)

The only acceleration that can work on stock iOS is code that is signed before
it runs. Two variants are worth investigating; neither is implemented, and
neither may become the correctness baseline:

1. **Offline AOT.** Translate a curated guest binary to ARM64 at build time,
   link it into the app, and route guest syscalls through traps. Correctness is
   checked by running the same payload through the interpreter and comparing
   state and output.
2. **Selective native userspace execution.** Run a guest image as native code
   only when it is AOT-translated and its external calls are replaced with
   host shims. It is not a route to unmodified APKs; Android native code assumes
   a Linux process ABI that iOS does not provide.

The gate for either variant is state-for-state equality on the interpreter test
suite, and isolation in a separate backend so a failure cannot be mistaken for
baseline behaviour.

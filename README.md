# Aster

An Android testing runtime that runs on an iPhone. Guest ARM64 code executes
locally, in the app process, on the phone's CPU. A Mac builds, installs, and
collects results; it does not run the guest.

Aster is built around one constraint: stock iOS does not let a third-party app
create executable memory. There is no JIT, no `MAP_JIT`, and no public
hypervisor access. The compatible backend is therefore an interpreter, and the
full-system backend is QEMU's TCI interpreter rather than its JIT.

## Status

| Phase | State |
|---|---|
| 1. ARM64 guest execution on iOS | **Passed.** Host suite 62/62; device gate 6/6 on iPhone 17 (iOS 27.0), 65–95 MIPS on the integer payload. |
| 2. Boot Android | Alpine Linux boots inside the physical iPhone app through QEMU TCI. Android kernel/device probe passes on the host; Android userspace boot remains blocked. See `docs/phase2.md`. |
| 3. Run APKs | Not started. |
| 4. Remote testing | Not started. |
| 5. Expo build | Not started. |

`tools/run_on_device.sh` prints one line per payload and passes only on
`[gate] summary 6/6` from a physical iPhone. It has been run; see
`docs/phase1.md` for the numbers, the execution-policy probe, and the
code-signing crash evidence.

## What runs today

The app embeds a purpose-built AArch64 interpreter and six freestanding guest
payloads:

- `hello` — integer execution, calls, loops, `.data`, `.bss`, unaligned load
- `demo` — recursion, insertion sort, CRC32, byte/half/word memory traffic
- `threads` — four guest threads incrementing a shared counter with `LDXR`/`STXR`
- `fp` — `FMOV` immediate, `FMADD`, `FDIV`, `FMUL`, `FCVTZS`, `FCMP`
- `fault_unmapped`, `fault_rodata` — inverted tests: these must fault, with the
  right kind and address

Each run reports exit code, fault kind, instructions retired, wall time, MIPS,
threads created and peak RSS. The app also probes `MAP_JIT`, `RWX` and `RW→RX`
and shows the kernel's answers — measured, not assumed.

## Build and test

```
tools/build_host.sh          # guest payloads + 62-check host suite + benchmark
tools/gen_xcodeproj.py       # regenerate the Xcode project
tools/run_on_device.sh       # build, install, launch with --autorun, check 6/6
```

Device prerequisites: paired iPhone, Developer Mode, **phone unlocked**. iOS
refuses to launch an app on a locked device (`BSErrorCodeDescription = Locked`).
The app is signed with a free personal-team profile and expires after seven
days.

QEMU TCI host reproduction (Phase 2 step 1):

```
tools/build_qemu_host_tci.sh
```

## Layout

```
core/      portable guest engine, C ABI, host tests
guest/     freestanding AArch64 payloads and their HVC runtime
app/       SwiftUI application and generated Xcode project
tools/     build, linker, project generator, device runner, QEMU recipe
docs/      constraints, architecture, Phase 1 evidence, Phase 2 plan
```

## Hard limits, stated up front

- **No JIT.** `MAP_JIT` is gated by the code-signing monitor; ordinary apps get
  `EPERM`. See `docs/constrained-execution.md`.
- **No hardware virtualization.** No public entitlement reaches EL2.
- **No `fork`/`exec`.** The guest is a memory image, never a child process.
- **The Phase 1 engine has no MMU or devices.** That is deliberate. Phase 2 uses
  QEMU TCI for full-system guests.
- **`LDXR`/`STXR` use compare-on-store semantics.** Correct for lock-free guest
  counters; it does not model cross-CPU monitor invalidation.
- **No GPU passthrough.** Metal composites a guest framebuffer; it does not
  virtualise the GPU.

## Next step

Phase 3 host bring-up uses a pinned complete AOSP Cuttlefish bundle. First-stage
mounts, second-stage init, and zygote launch have executed under TCI, but Android
framework completion is not yet verified. See `docs/phase3-build.md` for the
reproducible configuration and `docs/phase3-work.md` for current evidence.

Complete the Android host framework gate before attempting the same environment
on the phone. The earlier Linux device reproduction remains in
`docs/phase2-build.md`; the Android kernel-only probe is in `docs/android-host.md`.

# Constraints on a stock iOS 27 process

What the iPhone hardware can do is not what a signed third-party app may do.
This note records the boundary Aster is built inside, with the primary sources
that were actually checked.

## Hardware vs. process capability

| Capability | A19 hardware | Ordinary iOS app process | Consequence for Aster |
|---|---|---|---|
| ARM64 integer/FP execution | yes | yes | Guest instructions run through a software interpreter. |
| Writable + executable memory | yes (as separate mappings) | `MAP_JIT` requires the code-signing monitor to allow the process | No JIT. No TCG-style code cache. |
| EL2 / hardware virtualization | yes | no public API or entitlement | KVM is not an option; no accelerated full-system backend. |
| `fork` / `exec` of guest binaries | — | sandbox forbids | The guest is a memory image, never a child process. |
| 16 KiB host pages | yes | yes | A 4 KiB-granule guest must be managed page by page. |
| Threads, mutexes, atomics | yes | yes | Guest threads map onto host threads with an explicit monitor. |
| Metal GPU access | yes | yes, through Metal | A future display path; not a GPU passthrough path. |
| Network sockets | yes | yes, under sandbox rules | The Phase 4 test interface can bind a local listener. |

## JIT: the decisive constraint

XNU gates `MAP_JIT` on the code-signing monitor:

```c
/* bsd/kern/kern_mman.c, mmap() */
if ((flags & MAP_JIT) && (vm_map_csm_allow_jit(user_map) != KERN_SUCCESS)) {
    printf("[%d] code signing monitor denies JIT mapping\n", proc_pid(p));
    return EPERM;
}
```

and `vm_map_csm_allow_jit` is a thin wrapper over `csm_allow_jit_region` when
`CODE_SIGNING_MONITOR` is enabled (`osfmk/vm/vm_map.c`). The monitor consults the
process's entitlements. Apple documents the same requirement from the other
side: a JIT on Apple silicon must use `MAP_JIT` and carry
`com.apple.security.cs.allow-jit`, and must toggle write protection with
`pthread_jit_write_protect_np`. That entitlement is not issued to ordinary App
Store or free-provisioned iOS apps. WebKit and a small set of system processes
are the exception, not the model.

Aster therefore treats these as closed:

- `mmap(..., PROT_WRITE | PROT_EXEC)` for an anonymous guest code cache.
- `mmap(MAP_JIT)` plus `pthread_jit_write_protect_np`.
- `mprotect` of anonymous memory to `PROT_EXEC` **followed by execution**. The
  syscall may succeed; the page still has no code signature, so the fault path
  is not a supported JIT path. The app probes the syscall and reports the
  result rather than assuming either answer.

This is why the compatible backend is an interpreter. It is also why the
Phase 1 engine deliberately has two independent layers: a correct interpreter
that needs no executable memory, and a runtime that is honest about which
properties it measured.

### Measured on the iPhone 17

The app probes the three requests and reports the kernel's answers:

| Request | Result |
|---|---|
| `mmap(MAP_JIT)` | `EPERM` (errno 1) |
| `mmap(PROT_READ \| PROT_WRITE \| PROT_EXEC)` | allowed |
| `mprotect(RW → RX)` | allowed |

The last two are syscall-level successes and nothing more. Executing one
unsigned page (`mov w0,#1; ret`) after `mmap(RW)` + `mprotect(RX)` terminated
the process:

```
EXC_BAD_ACCESS, SIGKILL, KERN_PROTECTION_FAILURE at the mapped page
termination namespace CODESIGNING, indicator "Invalid Page"
```

The call never returned. `MAP_JIT` was refused before execution; execution of an
unsigned page was refused after it. Both paths are closed, and the interpreter
needs neither.

## Memory and page size

Apple silicon iOS uses 16 KiB host pages. The AArch64 guest contract Aster
tests is 4 KiB, and QEMU's ARM target keeps `TARGET_PAGE_BITS 12`
(`target/arm/cpu-param.h`), so the two layers agree on the guest granule.

The Phase 1 `GuestMemory` allocates one anonymous mapping for RAM and tracks
protection per 4 KiB guest page. A single-page `mprotect` is therefore
observable at 4 KiB while the kernel still maps whole 16 KiB host pages. The
host test asserts that protecting one guest page leaves its neighbour writable.

## Process lifecycle

- iOS can terminate an app for memory pressure (jetsam) without a crash report
  the app can intercept. Guest RAM is therefore allocated lazily, never touched
  unless used, and every run reports peak RSS.
- Background execution is not guaranteed. A long guest run belongs in the
  foreground with the screen awake; the Phase 1 payloads are milliseconds.
- The app has no `fork`, no `ptrace` of other processes, and cannot spawn a
  helper daemon. Everything the runtime does happens in one process.

## Code signing and installation

Free provisioning (personal team) installs a development-signed app for seven
days. Aster uses no private entitlements. The only entitlement in the built app
is the standard `get-task-allow` from the development profile, which grants
debugging, not JIT or virtualization.

## What this leaves open

1. **Interpreter speed.** TCG's JIT is unavailable, so the ceiling is the
   interpreter. Phase 1 measures it directly.
2. **Full-system boot.** An interpreter with no MMU cannot boot Linux. Phase 2
   uses QEMU's TCI backend, which interprets TCG ops and also needs no
   executable memory.
3. **Display.** Metal is available, so a framebuffer can be uploaded as a
   texture. There is no GPU passthrough: the guest renders in software or with
   a virtual GPU, and the host only composites the result.
4. **Remote access.** Local network sockets are allowed. An ADB-compatible
   surface must be authenticated and must not be exposed publicly.

## Sources checked

- `apple-oss-distributions/xnu`, `bsd/kern/kern_mman.c`, `osfmk/vm/vm_map.c`
  (`main` branch, read directly).
- Apple, *Porting your just-in-time compiler to Apple silicon* (JIT
  entitlement, `MAP_JIT`, `pthread_jit_write_protect_np`).
- QEMU `meson_options.txt` (`tcg_interpreter`), `tcg/tci.c`,
  `target/arm/cpu-param.h` (`TARGET_PAGE_BITS 12`).
- AOSP documentation for the emulator's virtual board and system images.
- UTM, UTM SE, iSH, Android Translation Layer, and Project Sandcastle source
  trees for what each project does and does not rely on.

The numeric claims that matter (page size, `MAP_JIT` denial, `RWX` denial,
instructions per second) are measured by the app and the host suite, not
inferred from these documents.

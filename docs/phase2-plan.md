# Phase 2 plan and gate status

The primary gate passes. The physical iPhone app boots the existing Alpine
ARM64 kernel and initramfs through QEMU 11.1.3 TCI, executes a shell command,
and stops its vCPU. The Android host gate remains incomplete.

Measured results and limitations are in [phase2.md](phase2.md). Build and device
commands are in [phase2-build.md](phase2-build.md). Phase 1 remains a separate
engine with its existing regression suite.

## QEMU embedding contract

Upstream QEMU 11.1.3 is an executable project, not a supported embedding SDK.
Its `meson.build` creates an internal `libqemu-aarch64-softmmu.a` target, but
the executable uses `extract_all_objects(recursive: true)` and additional
archives. That archive alone is not the engine's complete link contract.

`tools/qemu/package_ios.py` reads the actual Ninja executable link command.
It packages selected object files without `system/main.c`, then records the
remaining dependency archives separately. Force-loading the dependency
archives also loads conflicting stubs. The first integration attempt produced
220 duplicate symbols for that reason.

Upstream `system/main.c` calls `qemu_init`, transfers the BQL and replay mutex,
runs `qemu_main_loop`, calls `qemu_cleanup`, and exits the process. The default
Darwin entry point also reserves the main thread for a Core Foundation run loop.
The app does not use that entry point. Its dedicated engine thread retains the
locks acquired by `qemu_init` through the main loop and cleanup.

These functions are internal APIs tied to this exact source version. Cleanup
does not reset QEMU's global state, and the round-robin vCPU thread normally
does not exit. The repository patch adds a stop path for that thread. The
engine joins the vCPU and serial reader before returning. A second start in
the same app process is rejected. Guest RAM and QEMU global allocations can
remain until app termination. This is not a reusable multi-VM library.

Initialization still contains QEMU process-wide signal handling, `atexit`
handlers, assertions, and fatal `exit` paths. The pinned, validated boot inputs
work on the phone. General error recovery and process isolation are not solved.

## iOS dependency and execution requirements

The serial-only build uses static GLib 2.84.4, PCRE2 10.45, libffi 3.4.8,
GLib's pinned proxy-libintl fallback, and QEMU's bundled libfdt. It links iOS
SDK zlib, iconv, and CoreFoundation. It does not require pixman for this gate.
Display, networking, HVF, native TCG, dynamic modules, and host device access
are not part of the tested configuration.

Meson uses a real cross file with `needs_exe_wrapper = true`. No wrapper
pretends to execute target binaries. Only GLib and GModule library targets
and their headers are staged, so target GIO tools do not run on the Mac.
Host code generators continue to run with the existing host Python.

TCI interprets bytecode, but upstream Darwin code still requests `MAP_JIT`
for its translation buffer. The first physical launch failed with `EPERM`.
The patch removes that request for TCI and removes unavailable Darwin JIT
write-protection calls for TCI. It does not enable executable guest memory.

## What an Android boot actually requires

An AOSP GSI supplies Android system partitions, not a complete boot environment.
It does not supply the board's kernel, boot ramdisk, vendor implementation, or
graphics stack. A generic system image alone cannot establish Android or APK
compatibility.

A reproducible QEMU `virt` configuration needs all of these matched components:

- An ARM64 kernel with GIC, PSCI, timers, PL011, and the actual virtio transport.
  Android additionally requires Binder, Binderfs, SELinux, and the release's
  shared-memory interfaces. GKI can leave board drivers in external modules.
- A first-stage initramfs containing Android `/init`, required modules and their
  load order, device-node rules, fstab, and the configuration required to locate
  and mount Android partitions. Android releases can split these files across
  `boot`, `init_boot`, and `vendor_boot` images.
- A coherent partition set, including system and its release's product and
  system_ext content, vendor and optional odm content, writable userdata and
  metadata, and any dynamic-partition `super` metadata. Disk labels, partition
  paths, AVB metadata, and the verification policy must match fstab.
- Device properties, init scripts, ueventd rules, SELinux policy, VINTF
  manifests, and the vendor HAL services expected by that system image.
- Compatible graphics components. A kernel virtio-GPU driver is insufficient.
  SurfaceFlinger needs guest gralloc, a composer HAL, and a usable EGL/GLES
  renderer. Software rendering through SwiftShader or a software Mesa driver
  must match that guest configuration. The current iOS QEMU build has no
  gfxstream or OpenGL integration. Metal composition is not implemented.

The [Android host probe](android-host.md) establishes a prebuilt AOSP kernel,
matching virtio modules, Binderfs, SELinuxfs, block reads, entropy, and input
enumeration on `virt,gic-version=3`. Its diagnostic init is Alpine BusyBox.
It does not validate Android first-stage mounts, vendor services, Binder
transactions, graphics, zygote, or `sys.boot_completed`.

## Next gates

1. Select a legally distributable, matched ARM64 virtual-device image bundle.
   Inspect its partition layout and vendor dependencies before downloading a
   large artifact. Cuttlefish and Android Emulator image names do not prove
   compatibility with upstream QEMU `virt`.
2. Boot that complete environment on the host. Record each stage, complete
   serial output, CPU use, RSS, and failure evidence. Keep networking disabled
   until an authenticated debug transport is explicitly configured.
3. Attempt the same Android configuration on the phone only after the host
   gate passes. Do not extrapolate Android latency from the Alpine shell gate.
4. Compare TCI against viable alternatives if the measured Android cost is
   unacceptable. There is no measured stock-iOS JIT or public EL2 option.
   A purpose-built interpreter needs an MMU and device model before it can
   replace QEMU. A Linux guest hosting an Android container still needs Binder,
   vendor integration, and graphics. A remote Android guest changes the local
   execution requirement and needs a product decision.

No full AOSP build, additional host software installation, or secondary machine
is required by the work completed here. No Android userspace or APK execution
has been claimed.

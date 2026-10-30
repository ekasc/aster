# Android ARM64 host boot status

The prebuilt Android kernel boots on the existing QEMU `virt` board under TCI.
Android userspace does not boot in this experiment. The initramfs contains an
Alpine diagnostic program, not Android init, ART, or the framework.

## Reproducible kernel configuration

`tools/android/probe.py` uses Python 3, the existing `lz4` executable, the existing
ARM64 Alpine initramfs, and the existing host QEMU. It installs no software.
The default executable is `build/qemu/build-tci5/qemu-system-aarch64`.
`tools/build_qemu_host_tci.sh` instead names `build/qemu/build-host-tci` as its
output directory. `--qemu` selects another existing binary.

The following command fetches two pinned AOSP artifacts if absent, verifies
their SHA-256 hashes, prepares a diagnostic initramfs, and runs a bounded boot:

```sh
python3 tools/android/probe.py --fetch --timeout 120
```

Without `--fetch`, the command uses verified local artifacts only.
`--prepare-only` prepares inputs without launching QEMU. The timeout range is
1 to 600 seconds. Each boot records a complete `serial.log`, `command.json`, and
`result.json` in a unique `build/android/run-*` directory. The command record
includes the QEMU version, executable hash, adjacent interpreter build-header
check, and all input hashes. A copied executable without its build header has
unknown interpreter provenance in that record.

The tested machine configuration is:

```text
-M virt,gic-version=3 -cpu max -accel tcg,thread=single -smp 1 -m 768
-display none -monitor none -serial stdio -nic none -no-reboot
-kernel build/android/Image -initrd build/android/probe.cpio.gz
-append "console=ttyAMA0 earlycon=pl011,0x09000000 rdinit=/init panic=-1"
-global virtio-mmio.force-legacy=false
-drive if=none,id=probe,format=raw,readonly=on,file=build/android/probe.raw
-device virtio-blk-device,drive=probe
-device virtio-rng-device -device virtio-keyboard-device
```

The launcher passes absolute paths. The disk is a generated 1 MiB sentinel
image, attached read-only. No user disk is attached. Networking and the monitor
are disabled. There is no ADB listener. Timeout cleanup terminates only the
QEMU process started by the launcher, escalates after five seconds if needed,
and waits for it to exit.

## Pinned artifacts and source attribution

Both AOSP repositories identify Android build `12996908`. The running kernel
reports `6.6.66-android15-8-g807ce3b4f02f-ab12996908-4k`.

| Artifact | Pinned repository revision | SHA-256 of downloaded binary |
|---|---|---|
| `kernel-6.6-gz`, saved as `Image.gz` | [kernel prebuilt 6104d37087c872b8183f8483ba32e3bc165d3517](https://android.googlesource.com/kernel/prebuilts/6.6/arm64/+/6104d37087c872b8183f8483ba32e3bc165d3517/) | `994b3aeb16185a45d59219eae4e7de945d420f788882b98ff77ea1ac2e731d71` |
| `initramfs.img`, saved as `modules-initramfs.img` | [virtual-device prebuilt 4ac926c8fe96fd75b5c036e9a7c517cb01151f81](https://android.googlesource.com/kernel/prebuilts/common-modules/virtual-device/6.6/arm64/+/4ac926c8fe96fd75b5c036e9a7c517cb01151f81/) | `77fb5081ba4a94d0cbc206c041eb195ecaf4654bc6f32d50c7b265defad27713` |

The kernel's corresponding [common-kernel source revision](https://android.googlesource.com/kernel/common/+/807ce3b4f02f37ed863053f05604ba866e8847a9/)
is publicly available. These are AOSP kernel prebuilts, not proprietary phone
firmware or Google Play system images. No binary is added to the source tree.
Redistribution still requires the relevant kernel, module, BusyBox, and musl
license obligations. This experiment does not package binaries for distribution.

The probe reuses only `bin/busybox` and `lib/ld-musl-aarch64.so.1` from
`build/qemu/images/initramfs-virt`. Its tested archive SHA-256 is
`90f5159c2cbf81b5d6ad7b14a314815589a7077d0e8fa4e8aa0403c3fef458ae`.
That archive already existed from the Alpine netboot experiment. The launcher
checks its two executables are ARM64 ELF and records its hash on each boot.
It does not automatically download a replacement from a mutable Alpine URL.

The two AOSP downloads total about 23 MiB decoded and 30 MiB in Gitiles base64
transport. Each encoded response is capped at 64 MiB. Scratch artifacts,
including an independent fetch verification, occupied 116 MiB after testing.
No Android system-image bundle or toolchain was downloaded.

## Dependency checks and observed boot stages

The launcher extracts the actual kernel's embedded configuration to
`build/android/kernel.config`. A source defconfig alone is not the evidence.

| Dependency or stage | Actual configuration or observation |
|---|---|
| ARM64 kernel, GICv3, timer, PL011, PSCI | Kernel reaches `/init` on `linux,dummy-virt` |
| Initramfs | `CONFIG_BLK_DEV_INITRD=y`, `CONFIG_RD_GZIP=y`; generated newc archive executes ARM64 BusyBox |
| Binder | `CONFIG_ANDROID_BINDER_IPC=y`, `CONFIG_ANDROID_BINDERFS=y`; Binderfs mounts and exposes character devices `binder-control`, `binder`, `hwbinder`, and `vndbinder` |
| SELinux | `CONFIG_SECURITY_SELINUX=y`; SELinuxfs mounts and exposes `enforce` |
| Android shared memory | `CONFIG_ASHMEM=y`, `CONFIG_MEMFD_CREATE=y`; ashmem initialization appears in the boot log |
| Virtio transport | `CONFIG_VIRTIO_MMIO` is disabled in GKI; matching external `virtio_mmio.ko` loads successfully |
| Block device | `CONFIG_VIRTIO_BLK=m`; matching `virtio_blk.ko` loads, enumerates `vda`, and reads the sentinel |
| Entropy and keyboard | Matching `virtio-rng.ko` and `virtio_input.ko` load; sysfs identifies `virtio_rng` and `QEMU Virtio Keyboard` |
| Filesystems | ext4, F2FS, and EROFS are built in; Android partition mounts were not tested |
| Device nodes | `CONFIG_DEVTMPFS` is disabled; the diagnostic init explicitly creates nodes and uses sysfs for the disk major number |
| Display | Not attached or tested. Matching archive includes `virtio-gpu.ko`, but loading it and presenting an Android framebuffer remain untested |
| Android init and framework | Not present or executed |

The final passing TCI run is `build/android/run-klctp53t/`. It completed all
assertions and powered off in 7.767 host wall-clock seconds with 768 MiB
configured guest RAM and one vCPU. Its result records `android_booted: false`.
This is not Android boot latency or a memory measurement.

The initial runs `run-wkboc0vi` and `run-uyhjt1c1` failed an incorrect probe
assumption that Binder devices must appear in `/proc/misc`. The actual nodes
are under Binderfs. Their complete failure logs remain available.
`run-bdt1vap7` passed the earlier probe before the final explicit Binder-device
assertions were added. The final probe requires every stage marker on its own
line and rejects timeout, nonzero exit, or an explicit failure marker.

`run-ru99yb4b` exercised the one-second timeout. It records a timeout, a failed
probe, and a reaped child process. Host process checks found neither final-run
PID still alive after completion.

## Why this is not yet an Android boot recipe

A GSI supplies a system image. It does not supply this board's complete vendor
configuration or a matching first-stage ramdisk. The downloaded module archive
contains modules, not an Android `/init` executable. The generated probe fills
that gap only with Linux diagnostic userspace.

[AOSP init's early boot sequence](https://android.googlesource.com/platform/system/core/+/refs/heads/main/init/README.md#early-init-boot-sequence)
requires first-stage partition mounts, SELinux setup, and second-stage init.
The [Cuttlefish fstab](https://android.googlesource.com/device/google/cuttlefish/+/refs/heads/main/shared/config/fstab.in)
also expects named partitions, logical partitions, AVB metadata, vendor modules,
and writable userdata and metadata. Attaching an arbitrary `system.img` as
`vda` does not satisfy those requirements.

The next full-system attempt needs a matched redistributable ARM64 virtual-device
bundle with Android init, fstab and ueventd rules, vendor userspace and policy,
system partitions, and partition metadata. Its device expectations must be
checked against upstream QEMU rather than assumed from the name `ranchu` or
`cuttlefish`. Boot properties alone cannot supply missing vendor HALs.

Graphics also needs a compatible guest gralloc, composer, and software renderer.
The current TCI build has OpenGL disabled and no gfxstream integration. A kernel
virtio-GPU module alone does not prove SurfaceFlinger can present a frame.

No Binder IPC transaction, SELinux policy load, Android first-stage mount,
zygote, SurfaceFlinger, `sys.boot_completed`, APK, or ADB test has run. No
container fallback has run. Selecting and budgeting that matched userspace
bundle is the remaining decision before any large download.

## Checks run

```sh
sh -n tools/android/probe-init.sh
PYTHONPYCACHEPREFIX="$PWD/build/android/pycache" python3 -m py_compile tools/android/probe.py tools/android/test_probe.py
PYTHONPYCACHEPREFIX="$PWD/build/android/pycache" python3 -m unittest discover -s tools/android -p 'test_*.py' -v
python3 tools/android/probe.py --timeout 120
python3 tools/android/probe.py --timeout 1
```

Three unit tests passed. The final 120-second-budget boot passed. The one-second
boot returned failure as expected. A separate invocation of the artifact-fetch
function downloaded both pins into `build/android/fetch-check` and verified
their hashes. Syntax checks do not establish Android compatibility. Only the
boot log establishes the kernel and device stages listed above.

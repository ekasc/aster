# Phase 2 plan: boot Android on the iOS host

Phase 2 does not extend the Phase 1 interpreter. It embeds QEMU's TCI backend,
which interprets TCG operations and therefore needs no executable memory — the
one property that makes it viable on stock iOS.

## Step 1 (done, host): prove the engine and the board

QEMU 11.1.3 was built for the host with `-Dtcg_interpreter=true` and
`--target-list=aarch64-softmmu` (`tools/build_qemu_host_tci.sh`), then used to
boot Alpine's Linux 6.6.134 `vmlinuz-virt` with `-M virt -cpu max -m 512`.

Result: the kernel initialises the MMU, GIC, PL011 UART and virtio-mmio, mounts
its initramfs and reaches a userspace shell.

| Backend | Time to userspace | Relative |
|---|---:|---:|
| TCG with JIT | 8.31 s | 1.00× |
| TCI interpreter | 34.46 s | 4.15× |

The same run with a virtio-blk device attached enumerated `vda` and its
partitions under TCI. This is the first hard number for the iOS feasibility
question: full-system ARM64 at roughly a quarter of JIT speed, before any iOS
overhead.

The Alpine initramfs stops at an emergency shell because the netboot image has
no root media; that is expected and is not a TCI failure. A full userspace needs
a root filesystem, which is the next rung.

## Step 2: cross-compile QEMU TCI for iOS arm64

Dependencies, all cross-compiled as static iOS libraries:

| Library | Why |
|---|---|
| glib 2.80+ | QEMU's object, main-loop and string layer |
| pixman | console surfaces and software blit |
| libffi | TCI calls helpers through libffi |
| pcre2 | glib |
| zlib | QEMU image and device code |

The awkward part is not QEMU; it is glib. Its build runs freshly compiled
binaries (`glib-genmarshal`, `glib-compile-resources`) which cannot execute on
macOS when built for iOS. The established approach, used by UTM and iSH, is:

1. Build glib for the host first and install it to a prefix.
2. Cross-build glib with a Meson cross file whose `exe_wrapper` is a script that
   maps a requested build tool to the host binary of the same name.
3. Point QEMU at both: host tools through `native` file, target libraries
   through the cross file.

Cross file shape (one wrapper script per tool family):

```ini
[binaries]
c = 'tools/qemu/xcrun-ios-clang'
cpp = 'tools/qemu/xcrun-ios-clang++'
ar = 'xcrun'
strip = 'xcrun'
pkgconfig = 'pkg-config'
exe_wrapper = 'tools/qemu/ios-exe-wrapper'

[host_machine]
system = 'darwin'
cpu_family = 'aarch64'
cpu = 'arm64'
endian = 'little'

[properties]
needs_exe_wrapper = true
```

`xcrun-ios-clang` must pass `-isysroot $(xcrun --sdk iphoneos --show-sdk-path)`,
`-arch arm64`, `-miphoneos-version-min=18.0`, and
`-fapplication-extension` only if the code is linked into an extension.

QEMU configuration:

```
--target-list=aarch64-softmmu
--enable-tcg-interpreter
--disable-tools --disable-docs --disable-guest-agent --disable-plugins
--disable-capstone --disable-gtk --disable-sdl --disable-vnc
--disable-opengl -Dpvg=disabled -Dlibssh=disabled
--disable-slirp        # only if user-mode networking is deferred
```

Two build details that already bit the host build: Homebrew keeps `libffi` out
of the default include path, and macOS 27 marks `PGTask_t` unavailable, so
`-Dpvg=disabled` is required. Neither applies to the iOS SDK, but the flags are
harmless there.

### Integration shape

Do not port UTM's application. Use QEMU as a library:

- Build the static archives (`libqemu-aarch64-softmmu.a` and its dependencies).
- Call `qemu_init(argc, argv)` and `qemu_main_loop()` from a dedicated pthread.
- Register a QEMU chardev backed by a `socketpair`; the app reads serial output
  from the other end, so no stdio redirection is needed.
- Register a `DisplayChangeListener` that copies dirty rectangles into a
  shared framebuffer; the app uploads it as a Metal texture.
- Stop with `qemu_system_shutdown_request()` and join the thread.

The iOS app then has two engines behind one interface: the Phase 1 interpreter
for fast, deterministic payload tests, and QEMU for full-system guests.

## Step 3: a root filesystem and a Linux userspace

Before Android, boot a small Buildroot or Alpine rootfs on virtio-blk and get a
login prompt. This proves block I/O, an init system, and a writable disk on the
iOS host. Measure boot time and memory on the device; the 4.15× host factor is
a floor, not a promise.

## Step 4: Android

Two routes, in order of fidelity:

1. **AOSP GSI (primary).** An arm64 generic system image keeps the real Android
   kernel, ART, Binder and framework. It needs a kernel with the virt board's
   drivers plus Android's binder, ashmem/memfd and SELinux policy, and it needs
   a graphics stack that SurfaceFlinger accepts (virtio-gpu with a software
   renderer or SwiftShader). This is the route that satisfies "unmodified APK"
   with the least substitution, and the route most likely to expose
   device-specific blockers.
2. **Waydroid-style container (fallback).** A Linux guest with binder and an
   Android userspace runs unmodified APKs without a full GSI. It shares the
   guest Linux kernel rather than running an Android kernel. It is cheaper to
   bring up and a legitimate test target, but it must be labelled as a
   container, not as full-system Android.

The disk and time budget on this machine (17 GiB free) rules out an AOSP build
here; that is a hardware constraint, not a design choice. The GSI route starts
from a prebuilt image and a matching kernel.

## Step 5: display, input, APK, ADB

- Display: virtio-gpu 2D commands into a shared buffer, composited by Metal.
  No GPU passthrough; the guest's rendering is whatever its software stack
  produces.
- Input: translate UIKit touches into virtio input events or an ADB-level
  injector, with the guest coordinate space derived from the scanout size.
- APK install: `adb install` against the guest's adbd, or a host-side
  installer that pushes and invokes `pm` over the same channel.
- Remote testing: authenticated TCP on the local network. No unauthenticated
  ADB listener, ever.

## Stop conditions

Phase 2 is not "done" until the device shows a booted guest and the numbers are
in `docs/phase1.md`-style tables. If TCI cannot reach a usable Android state in
a bounded time, the correct output is the boot log, the failing stage, and the
measured cost — not a claim.

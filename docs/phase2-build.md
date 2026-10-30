# Boot Alpine inside the physical iPhone app

## Prerequisites

Use the existing Xcode iPhoneOS SDK, signing configuration, Python 3, Ninja,
pkg-config, make, and a host Meson installation. The tested Meson executable
is `build/qemu/build-tci5/pyvenv/bin/meson`, retained from Phase 1's host build.
Set `ASTER_MESON` to another existing Meson executable if necessary. The scripts
do not install host packages. Obtain approval before adding missing tools.

Connect and unlock the paired physical phone with Developer Mode enabled.
Set `ASTER_DEVICE_UDID` for a different paired device. No secondary machine is
used. Keep the application in the foreground throughout the boot.

## Build and run

Run these commands from the repository root:

```sh
bash tools/build_qemu_ios_deps.sh
python3 tools/run_linux_device.py
```

The dependency build downloads checksum-pinned sources into `build/qemu` and
stages static iOS ARM64 libraries under `build/qemu/ios-prefix`. Meson uses
the generated `ios-deps/cross.ini`. This is a target-library build, not a host
package installation. GLib's unused GIO tools are not built or executed.

The device runner builds QEMU, generates the opt-in Xcode project, builds and
installs the signed app, and launches `--linux`. It restarts only Aster on the
selected phone. It captures the launch console, then retrieves the complete
serial log and measurement JSON from the app's Documents directory. A launch
UUID prevents a stale result file from passing a later failed boot.

Each invocation creates a new `build/qemu/device-<test>-<timestamp>` directory.
The runner stops and reaps its host console collector after the engine summary.
`devicectl` forwards that catchable termination signal to Aster. The app can
therefore terminate after its vCPU and serial reader have joined. The console
and `console.collector.json` distinguish this requested cleanup from a guest
failure or an iOS memory-pressure termination.

The same board and CPU configuration are used for Alpine and the Android kernel
probe. The memory allocations differ:

```text
-M virt,gic-version=3 -cpu max -accel tcg,thread=single,tb-size=32
-smp 1 -m 512 -display none -monitor none -nic none -no-reboot
-kernel vmlinuz-virt -initrd initramfs-virt -append console=ttyAMA0
```

Serial uses a connected socketpair, not redirected application stdio. The app
copies guest bytes to a file and the launch console. After the emergency shell
prompt, the serial reader sends a command that produces
`ASTER-LINUX-USERSPACE` and runs `uname -a`. The engine requires the command's
output, not its echoed input, before recording time to userspace. It requests
shutdown one second later and joins the vCPU and serial reader.

To exercise cancellation and the deadline on the installed system-engine build:

```sh
python3 tools/run_linux_device.py --skip-build --test cancel
python3 tools/run_linux_device.py --skip-build --test timeout
```

These tests expect an unsuccessful boot result with the corresponding flag and
a clean engine return. They are not successful Linux boot measurements.

## Verify Phase 1

```sh
tools/build_host.sh
python3 -m unittest discover -s tools -p 'test_*device*.py' -v
python3 tools/qemu/test_patches.py
bash tools/run_on_device.sh
```

The device gate accepts only a complete `[gate] summary 6/6` line. A console
timeout without that summary fails. The timeout escalates after five seconds
so an unresponsive console collector cannot wait forever.

The tracked default Xcode project remains Phase 1 only. Regenerate it after a
system-engine build with `python3 tools/gen_xcodeproj.py`. To generate the
system-engine project without building, set `ASTER_SYSTEM_ENGINE=1` when
running that generator. The opt-in project links `ios-link.rsp` and bundles
the pinned Alpine images. It does not replace the Phase 1 C ABI.

## Source and redistribution requirements

`tools/qemu/prepare_sources.sh` verifies QEMU and both Alpine image hashes.
The Alpine URLs are mutable upstream netboot locations. If they change, the
script fails its checksum rather than silently using a different kernel.
The expected inputs are the existing Linux 6.6.134 experiment's images.

`tools/qemu/patch_ios.py` applies guarded, version-specific source changes.
`tools/qemu/test_patches.py` verifies them against the unmodified release
archive and checks idempotence. `package_ios.py` derives link inputs from the
actual generated executable command rather than guessing an archive list.

No downloaded binaries or dependency sources are committed. Before distributing
the linked app, satisfy QEMU's GPL and all linked library, kernel, module,
BusyBox, and musl source and license obligations. A signed development build
does not resolve App Store policy or distribution-license questions.

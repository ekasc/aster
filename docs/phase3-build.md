# Reproduce the Android ARM64 host boot attempt

The complete Android framework gate is not yet verified. The commands below
prepare the real AOSP image set and run a bounded, instrumented guest. A
successful preparation command does not establish a successful Android boot.

## Prepare the pinned bundle

Use the existing Python 3, lz4, and host QEMU TCI tools. Install nothing without
approval. Check available disk space before starting. The manifest requires at
least 3 GiB remaining, limits compressed downloads to 2 GiB, and limits new
artifact allocation to 6 GiB. Sparse disk logical size is not allocated size.

```sh
df -h .
export PYTHONPYCACHEPREFIX="$PWD/build/android/pycache"
python3 tools/android/images.py list
python3 tools/android/images.py fetch --bundle cf-arm64-only-phone-16644040
python3 tools/android/images.py inspect --bundle cf-arm64-only-phone-16644040
python3 tools/android/images.py prepare --bundle cf-arm64-only-phone-16644040 --memory-mb 1536
python3 tools/android/images.py verify --bundle cf-arm64-only-phone-16644040 --prepared
```

`tools/android/artifacts-v1.json` pins the 1,101,174,958-byte AOSP Cuttlefish
archive to build 16644040 and its SHA-256. The artifact-viewer URL returns HTML.
The tool resolves its signed download URL, verifies the bytes, and streams the
sparse super and F2FS userdata images into a GPT disk. The prepared disk has
18 labelled partitions, including A/B boot and vbmeta partitions, super,
userdata, metadata, and misc. It remains local under `build/android/images`.

The bundle contains Android 17, matching vendor modules, and the 6.12.74 GKI
kernel. The older 6.6.66 Android 15 kernel/module probe remains untouched.
Combining those older modules with the new kernel would violate their ABI.

The archive and derived files are not committed. This is an AOSP userdebug
bundle, not a Google Play image. Source and redistribution obligations remain
documented in the manifest. No full AOSP source checkout or toolchain is used.

## Generate a host configuration

Choose a new short output directory for each attempt. macOS UNIX socket paths
must be shorter than 104 bytes. Do not reuse an earlier console socket.

```sh
python3 tools/android/boot_config.py \
  --kernel build/android/images/prepared/cf-arm64-only-phone-16644040/kernel \
  --ramdisk build/android/images/prepared/cf-arm64-only-phone-16644040/initrd.img \
  --disk build/android/images/prepared/cf-arm64-only-phone-16644040/android.img \
  --base-bootconfig build/android/images/prepared/cf-arm64-only-phone-16644040/bootconfig.txt \
  --vendor-cmdline build/android/images/prepared/cf-arm64-only-phone-16644040/cmdline.txt \
  --output build/android/integration/myrun --ram-mib 1536 --timeout 1800
python3 tools/android/serial_control.py \
  --config build/android/integration/myrun/config.json \
  --console build/android/integration/myrun/console.sock \
  --output build/android/integration/myrun/control --timeout 1830
```

The configuration uses upstream QEMU `virt,gic-version=3`, one `max` CPU,
TCI, 1536 MiB guest RAM, and a 32 MiB translation buffer. It uses snapshot
writes so the verified base disk does not change. No network interface,
host-forwarded ADB port, OpenGL renderer, or host GPU acceleration is enabled.
Guest ANGLE/SwiftShader uses a 480 by 800 2D virtio-GPU display. That selection
is a configuration to test, not proof that SurfaceFlinger works.

The generated bootconfig preserves the matched fstab suffix and nonsecure
KeyMint and Gatekeeper APEX selections. It selects the emulated camera APEX
explicitly. Omitting that camera selection caused the first real boot to abort
at APEX bootstrap. SELinux is permissive during these bring-up attempts.
The configuration also supplies light-service vsock port7000. The stock HAL
runs as nobody and its unconfigured port-zero bind failed the kernel capability
check. This candidate correction is undergoing a real boot test.

The configuration preserves Cuttlefish's numbered virtio console ports.
`hvc1` is a private UNIX-socket userdebug console and `hvc2` captures logcat.
Kernel UART output goes to the boot runner's complete `serial.log`.

The controller submits a quoted `su 0 /system/bin/sh -c` command. It does not
use a heredoc, which failed before the early Android shell had a temporary
directory. `guest_gate.sh` tails real logcat, waits for `sys.boot_completed=1`,
checks the activity Binder service and live system_server PID, and performs
`cmd activity get-current-user`. It reports facts through the UART under the
fresh boot nonce. The submitted command and raw control stream are retained.

The runner refuses missing or non-TCI provenance by default. It records
per-child CPU time, process RSS high-water mark, wall time, and teardown from
`wait4`. Deadline, clean exit without completion, failed framework checks, and
stale nonce evidence all fail. Host and guest execution remain bounded even
when the framework never finishes.

## Inspect the evidence

Each actual boot creates a new `runs/run-<UTC>-<nonce>` directory. Preserve
`command.json`, `serial.log`, `android.log`, and `result.json`, plus the sibling
controller transcript. Do not count a zero QEMU exit code as a pass.

```sh
python3 tools/android/report.py build/android/integration/myrun/runs/run-<UTC>-<nonce>
```

The report reanalyses the preserved bytes with the current parser and does not
alter the original result. Its stage times are guest kernel uptime where the
kernel printed a timestamp. They are not host boot-latency estimates. A report
command's exit status is not the framework acceptance gate.

## Run regression checks

```sh
python3 -m unittest discover -s tools/android -p 'test_*.py'
python3 -m unittest discover -s tools -p 'test_*device*.py'
./build/host/aster_tests build/guest
```

The existing Phase 1 binary can be exercised without rebuilding its engine.
Physical-device regression uses the existing installed app and `--autorun`.
Android device integration and APK work remain gated on a completed host boot.

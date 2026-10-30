# Waydroid early-init bring-up

## Verified result

The pinned Waydroid ARM64 userspace executes Android init as guest PID 1.
The 120-second run, `docs/evidence/phase3/waydroid/boot6/`, observed:

```text
[   19.929086][    T1] init: init first stage started!
[   20.512807][    T1] init: init second stage started!
ASTER-WAYDROID|prop=ro.build.version.sdk=33
ASTER-WAYDROID|binder_services=Found 5 services: 0 android.system.suspend.ISystemSuspend/default: ...
```

The final line above abbreviates the recorded service list. The bootstrap
limits each diagnostic to 110 characters, so only the count and first service
are identified. Live `service check activity` and `service check package`
returned `not found`. `sys.boot_completed` remained empty. Init attempted to
start zygote, but that message alone does not establish a running Java VM.
No framework boot or phone acceptance is claimed.

That run reached its 120-second host deadline and reaped QEMU after
cooperative termination. Wall time was 120.027459 seconds, CPU time 118.408027
seconds, and peak QEMU RSS 283,082,752 bytes. These are host-process figures,
not physical iPhone measurements.

The final 90-second run, `docs/evidence/phase3/waydroid/boot7/`, additionally
verified `setprop debug.aster.bootstrap ready` returned 0 and a subsequent
`getprop` returned `ready`. This exercises the property service, rather than
only reading properties from shared memory. Its full Binder list contained:

```text
android.system.suspend.ISystemSuspend/default
manager
suspend_control
suspend_control_internal
vold
```

It still reported ActivityManager absent and an empty boot-completed property.
QEMU was cooperatively stopped and reaped at 90.028174 seconds, with 87.624215
CPU seconds and peak RSS 229,916,672 bytes. The final bootstrap was used by
this run. No processes remain from these experiments.

## Failure sequence

All earlier evidence and commits remain unchanged.

| Attempt | Change and observed result |
| --- | --- |
| Existing boot1 | `unshare(0x20020000)` returned `EINVAL`; guest PID 1 exited and the kernel panicked. Android init was never executed. |
| boot2 | Direct `exec chroot /android /init` reached `FirstStageMain`. Precreated kmsg and urandom nodes caused fatal `EEXIST`; init logged its abort and rebooted after 23 seconds. |
| boot3 | Removed precreated Android device nodes. First and second stages ran. Servicemanager processes aborted because Binder nodes were absent. Deadline 120 seconds. |
| boot4 | Mounted binderfs and linked its real nodes. Servicemanager could find Binder but aborted with `Permission denied`. Deadline 120 seconds. |
| boot5 | Set Binder device permissions to 0666. Binder-open aborts stopped; live service checks returned `not found` instead. Deadline 150 seconds. |
| boot6 | Added live getprop and service-list probes. SDK property 33 and five registered Binder services observed. Deadline 120 seconds. |
| boot7 | Added property write/read verification and untruncated Binder list. Property service and five registrations verified. Deadline 90 seconds. |

Successful first/second-stage log messages from kernel task T1 establish that
the ELF interpreter and enough dependencies loaded to execute init. No syscall
tracer was available, so no raw `execve` return or exhaustive dependency audit
is claimed. A successful exec does not return to its caller. The first-stage
abort is identified by its own log and stack, not by QEMU's exit status.

## Bootstrap layout and isolation

- `system.img` mounts read-only at `/mnt/system-ro`.
- A separate tmpfs contains overlay upper/work directories. `/android` is the
  writable overlay root, not the Android `/system` directory.
- `vendor.img` mounts read-only at `/android/vendor`.
- `/android/data` is a dedicated ephemeral tmpfs, not a bind of Linux `/data`.
- `/proc`, read-only `/sys`, tmpfs `/dev`, and guest-local Binder nodes are
  prepared before handing over PID 1. Android init creates kmsg and urandom.
- `/init` is the image symlink to `/system/bin/init`. The image executable is
  AArch64 PIE with interpreter `/system/bin/bootstrap/linker64`.
- The guest has no PID namespace support. Android replaces the bootstrap in
  the guest's root PID namespace. This is VM isolation, not an LXC container
  with a supervising Linux PID 1. No host filesystem/device passthrough occurs.
- Capabilities are recorded, not copied from the upstream LXC capability list.
  No user namespace or host network is introduced.

Overlayfs accepted the tmpfs upper despite xattr warnings. This does not prove
all Android SELinux labels or persistent userdata semantics work. The log
shows loaded file contexts, but no separate SELinux-policy transition was
observed between first and second stage. Do not equate file contexts with an
enforcing policy. The earlier prediction of a missing-fstab fatal did not
occur in the shipped Waydroid init.

## Upstream comparison

Read-only source downloads are pinned in
`build/android/waydroid/upstream-bootstrap/REVISION`.

- Waydroid revision `db7294c4c2bd43157bab94cf8a3015e22d9f1ad8`.
  `tools/helpers/images.py:162-185` mounts system at rootfs, vendor under it,
  with optional overlays. `tools/helpers/mount.py:154-180` supplies upper/work
  directories. `tools/helpers/lxc.py:40-127` generates selected device mounts,
  including Binder. `data/configs/config_base` supplies proc/sys/cgroups and
  disables automatic device population. `lxc.py:421-422` launches `/init`.
- Lineage system/core reference revision
  `c0500ffe2cc283d61f1e3db1b2c979bfbd9beb8c` explains init dispatch and logging.
  It is a reference source, not proof that every branch matches the shipped
  Waydroid binary. Runtime evidence takes precedence.

The bootstrap copies the rootfs/device requirements, not the host graphics,
audio, seccomp, AppArmor, or capability policy. No replacement Android service
or modified init binary is used.

## Reproduce and next gate

Generate a new directory with `tools/android/linux_guest.py` using
`--init tools/android/waydroid_boot_init.sh`, the existing pinned kernel and
vendor ramdisk, and `--disk` for the existing system and vendor images.
Use `--memory-mb 1536 --timeout 120`. Run:

```sh
python3 tools/android/waydroid_validate.py \
  --config build/android/waydroid/boot6/config.json \
  --output build/android/waydroid/boot6/validation --timeout 120
```

The validator creates a fresh run directory, verifies input hashes, preserves
the command and combined serial log, and reaps its owned process. Exit 1 is
expected while the framework remains unverified. Archived results retain the
parser version used at capture; regression tests apply the current parser
without rewriting historical reports.

Next work is to capture zygote/logcat failures and resolve the observed
APEX/linkerconfig and task-profile failures. Longer
deadlines alone are not evidence of progress. ActivityManager plus a fresh
`sys.boot_completed=1` remain mandatory before physical iPhone work.

## Checks for this change

- Six serialized real VM runs, each capped at 90 to 150 seconds.
- `python3 -m unittest discover -s tools/android -p 'test_*.py'`: 170 passed.
  Existing duplicate-ZIP and unclosed-file fixture warnings remain.
- `sh -n tools/android/waydroid_boot_init.sh` and `git diff --check`: passed.
- Final boot7 input hashes and all six archived checksum sets: verified.
- Process sweep for QEMU and test sleep processes: no matches.

No iOS build, physical phone run, APK execution, raw syscall trace, or full
framework acceptance test passed in this work. Both requested implementation
delegates used T3Code-managed `opencode-go/deepseek-v4.1-flash`; bootstrap and
validation files had separate owners. Integration, real guest runs, evidence
archiving and final regression checks were performed by the lead.

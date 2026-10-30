# Phase 3 Android boot status

The phase is incomplete. Real AOSP Android init, partition mounts, userdata,
zygote, SurfaceFlinger startup, and a system_server Binder caller have executed
under host QEMU TCI. No run has yet verified `sys.boot_completed=1` together
with a working ActivityManager service. No Android iPhone or APK test has run.

The fourth host attempt passed the light HAL's earlier bind failure, but still
timed out before the framework gate. Further guest thread and service diagnostics
are being added before another attempt.
The existing iOS engine remains unchanged until the host framework gate passes.

## Reproduction and artifacts

[phase3-build.md](phase3-build.md) contains the preparation, boot, evidence,
and regression commands. `tools/android/artifacts-v1.json` pins the complete
AOSP Cuttlefish ARM64-only phone bundle, build 16644040, Android 17, and matching
GKI 6.12.74 kernel and modules. The archive is 1101174958 bytes and its SHA-256 is
`a9314183f3cf4d15a0a2121a85f8300ca7990788c52474d322c6fe9320b40802`.

The previous Android 15 GKI 6.6.66 probe remains intact. Its modules do not match
the selected bundle's kernel ABI. No full AOSP checkout, new host package,
secondary machine, or host OpenCode CLI session was used.

## Executed host attempts

The preserved [evidence](evidence/phase3/) includes complete compressed serial
and Android logs, checksums, commands, original measurements, current-parser
stage reports, and guest-console submissions. Every archived framework verdict
is false. A zero QEMU exit code is not accepted as completion.

The attempts used one guest CPU, TCI, 1536 MiB guest RAM, a 32 MiB translation
buffer, snapshot writes to a verified GPT disk, guest SwiftShader, and 2D
virtio-GPU. SELinux was permissive. Networking and unauthenticated TCP ADB were
disabled. No guest GPU acceleration or Metal integration is claimed.

- `h1` failed after 59.971495 wall seconds at APEX bootstrap. The bundle needed
  an explicit selection between its two camera-provider APEX variants.
- `h2` fixed that selection and timed out after 600.187618 seconds. It also
  exposed a console-probe heredoc failure before the early shell had a temporary
  directory. The corrected probe uses a quoted in-memory local su command.
- `h3` ran the real probe, reached system_server, and timed out after 1800.077487
  seconds. Its peak host RSS was 312180736 bytes. Measured child CPU time was
  1792.178080 seconds, or 99.561% of one core over the run. It was not an OOM
  termination. system_server waited for the repeatedly crashing light HAL.
- `h4` supplied the unprivileged light-service port and progressed past that
  bind. It still timed out after 1800.085110 seconds, with 352960512 bytes peak
  host RSS and 98.499% of one core. ActivityManager was not available.

RSS is the exact host QEMU process high-water mark from wait4, not configured
guest RAM, iPhone memory use, or an Android completion-time estimate. The guest
kernel timestamps in stage reports are labelled separately from host wall time.

## Status of the Cuttlefish path

Four bounded host boots reached Android second-stage init and system_server but
never ActivityManager. The missing piece is not tuning: this QEMU build has
`CONFIG_AF_VSOCK` and every `CONFIG_VHOST*` undefined, and `-device help` lists
no vsock device. Android's sensors HAL (`android.hardware.sensors-service.multihal`)
aborts with `Could not connect to sensors control: No such device`, and
`system_server` stops issuing HAL lookups immediately after the light service.
`tools/android/preflight.py` now answers this in two seconds instead of one
30-minute boot.

The fifth run measured the framework directly at guest uptime 1628.41 s:

```text
system_server state=R threads=56 rss=193132 kB
service check activity        rc=124 (timed out in servicemanager)
service check package         rc=0   Service package: not found
service check window          rc=0   Service window: not found
service check SurfaceFlinger  rc=124 (timed out)
```

`system_server` is alive and running, not dead and not out of memory, yet
ActivityManager never registers. Wall time was 1800.08 s, CPU time 1789.77 s
(99.427% of one core) and peak host RSS was 330596352 bytes. Two HAL aborts
repeat throughout: the sensors control transport (`No such device`) and
`node_id > 0`. This is a platform capability failure, not a timeout budget.

## Verified Linux foundation for the fallback path

Because the Cuttlefish HAL contract cannot be satisfied here, the supported
fallback from `docs/phase2-plan.md` now has a working base: an Alpine 3.20.3
userspace on the bundle's Android GKI 6.12.74 kernel, prepared by
`tools/android/linux_guest.py` from a hash-pinned minirootfs plus the bundle's
own virtio modules.

Observed on the host, QEMU exit 0:

```text
ASTER-LINUX-GUEST|uname=6.12.74-android16-6-g3ec022196c4e-ab15076761-4k
ASTER-LINUX-GUEST|binder=kernel_support
ASTER-LINUX-GUEST|binderfs_mount=ok
ASTER-LINUX-GUEST|binderfs_nodes=binder binder-control features hwbinder vndbinder
ASTER-LINUX-GUEST|release=3.20.3
ASTER-LINUX-GUEST|apk=/sbin/apk
ASTER-LINUX-GUEST|uid=0
```

That is Binder plus a package manager inside a guest we control, without any
host daemon. It is not an Android boot: no Android init, zygote or framework
ran. Evidence is in `docs/evidence/phase3/linux-gki/`.

## Current blocker and tested reasoning

The tested QEMU build exposes no vsock device:

```text
$ build/qemu/build-tci5/qemu-system-aarch64 -device help | grep -i vsock
(no output)
```

`config-host.h` also has no vsock backend. Every guest `AF_VSOCK` socket
therefore fails with `ENODEV`, so Cuttlefish's host-service HAL model cannot
work on this host through vsock. The light HAL's port-zero bind failure was
real and is fixed, but supplying port 7000 cannot make a missing transport
appear. Cuttlefish normally launches host-side lights and sensors services and
connects over vsock; this host runs only QEMU.

`h5` therefore collects guest-side evidence rather than assuming a fix. Every
300 seconds the guest reports uptime, `sys.boot_completed`, bound services, and
the dominant kernel wait channels of every `system_server` thread. That
distinguishes a blocked framework thread from slow dexopt work or a crashed
process, which the previous runs could not.

The matching [light HAL source](https://android.googlesource.com/device/google/cuttlefish/+/283645aacf6cdb56cc31a9362f54d412dbc132a1/guest/hals/light/lights.rs)
reads `ro.boot.vsock_lights_port` and defaults to zero. Its
[init service](https://android.googlesource.com/device/google/cuttlefish/+/283645aacf6cdb56cc31a9362f54d412dbc132a1/guest/hals/light/lights-cuttlefish.rc)
runs as nobody. The matching
[kernel bind implementation](https://android.googlesource.com/kernel/common/+/3ec022196c4e/net/vmw_vsock/af_vsock.c)
requires CAP_NET_BIND_SERVICE for reserved vsock ports and returns EACCES
otherwise. The actual guest reported that Permission denied abort repeatedly.

`boot_config.py` now supplies `androidboot.vsock_lights_port=7000`. No HAL or
Android API implementation was replaced. Attempt `h4` reached its Binder
registration path but did not complete Android startup. Further Cuttlefish
host-service dependencies remain possible and are not assumed solved.

## Delegation and verification

Both implementation workers were created with T3Code `delegate_task`, selecting
provider `opencode` and model `opencode-go/deepseek-v4.1-flash` explicitly:

- Artifact preparation implemented `images.py`, `artifacts-v1.json`, and 32
  preparation tests. It downloaded and prepared the coherent image set without
  running QEMU or touching the phone.
- Validation implemented `run.py`, `boot_evidence.py`, and 66 initial tests for
  nonce-bound boot evidence, provenance, measurement, deadlines, and teardown.
  It used dummy child processes, not a real Android boot.

The lead inspected their output, fixed issues exposed by real boots, implemented
the host configuration and console probe, and ran all real guest and device
tests. The integrated Android tooling suite currently passes 117 tests. Four
existing device-runner tests also pass. The unchanged Phase 1 host binary passes
62 checks, and the installed physical iPhone app passes its 6/6 regression gate.

Next, verify that the real light HAL registers and that Android's framework
gate completes. Only then integrate this configuration into the existing
single-shot iPhone engine and begin unmodified APK installation and Activity
execution. The Alpine device baseline is not an Android performance prediction.

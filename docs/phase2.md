# Phase 2 evidence

The primary gate passes on the physical iPhone 17, `iPhone18,3`, iOS 27.0.
The app embeds QEMU 11.1.3's AArch64 system emulator with TCI. It boots Alpine
Linux 6.6.134 and its existing initramfs, reaches an emergency shell, executes
a command through the PL011 serial channel, and returns from the engine.

The second gate does not pass. An AOSP kernel boots on the host with matching
virtio modules and diagnostic Linux userspace. No complete Android userspace
configuration has booted. No Android framework, APK, graphics, or ADB support
is claimed.

## Physical boot measurements

Final foreground run `device-boot-20261029-222349`, Release app, one guest vCPU,
512 MiB configured guest RAM, 32 MiB TCI translation buffer:

| Measurement | Observed value |
|---|---:|
| Time to verified shell command output | 25.195212 seconds |
| Engine wall time, including shutdown and joins | 26.201622 seconds |
| Peak whole-app RSS | 326,254,592 bytes |
| Average whole-app CPU over engine wall time | 78.120175% |
| Engine status | 0 |
| Deadline exceeded or cancellation requested | neither |
| Second start in the same app process | rejected with -3 |

The byte count is 311.14 MiB. RSS comes from Darwin `getrusage(RUSAGE_SELF)`
and is the app process's lifetime high-water mark, not guest RAM usage or
iOS physical footprint. CPU is the change in process user and system CPU
time divided by monotonic wall time. 100% means one fully occupied core.
Neither value is an estimate or a host-emulator measurement.

The stopwatch begins before `qemu_init`. The serial reader first sees the
shell prompt, then sends `echo ASTER-LINUX-$(echo USERSPACE); uname -a;`.
It requires a complete `ASTER-LINUX-USERSPACE` output line before recording
userspace time. The echoed command cannot satisfy that marker.

The decisive serial output is:

```text
initramfs emergency recovery shell launched. Type 'exit' to continue boot
sh: can't access tty; job control turned off
~ # echo ASTER-LINUX-$(echo USERSPACE); uname -a;
ASTER-LINUX-USERSPACE
Linux (none) 6.6.134-0-virt #1-Alpine SMP PREEMPT_DYNAMIC 2026-04-11 17:03:51 aarch64 Linux
```

This proves execution of initramfs userspace, not a mounted Alpine root disk,
login service, or Android. The emergency shell is expected because the
netboot initramfs has no root medium. The final run contains no concurrent
Phase 1 gate output.

## Lifecycle and resource gates

The C system-engine interface is separate from `aster_run_elf`. It accepts
128 through 768 MiB guest RAM and a 1 through 600 second cooperative deadline.
Four invalid RAM/deadline combinations were rejected on the phone before
QEMU initialization. The configured RAM limit is not a hard process RSS cap.

Cancellation uses an atomic request checked by a realtime QEMU timer.
Requests made during initialization take effect after initialization returns.
This is not a hard watchdog for a hung `qemu_init`, a suspended application,
or a stuck QEMU helper. The host runner has a separate 210-second capture
deadline and reaps its console process with a bounded escalation.

The final one-second cancellation test returned status 0 after 1.089 seconds
with `cancelled=1`, no userspace marker, and `passed=false`. The final
one-second deadline test returned status 0 after 1.073 seconds with
`timed_out=1`, no userspace marker, and `passed=false`. The committed JSON
records the exact values. Neither test is counted as a successful boot.

Each completed test logs `[linux] vcpu joined`. The serial reader drains its
socket before joining. Upstream round-robin TCG normally keeps a paused
thread alive, so the embedding patch provides an explicit stop path.
Restart remains unsupported. QEMU globals, guest RAM mappings, helper state,
signal handlers, and `atexit` callbacks are not a resettable library lifecycle.

The app disables automatic screen sleep only while its engine thread runs and
restores the prior default behavior afterward. Manual locking and background
execution remain outside the tested contract. One earlier repeat stopped
producing serial output after the RTC message. The outer command ended before
the runner's capture deadline, leaving a collector that was explicitly removed.
That partial run has no summary or performance result and is not a pass. Its
cause was not established. The successful final run used the foreground
idle-timer guard.

## Failures and iOS termination evidence

The first device launch failed before kernel execution:

```text
aster-qemu: -accel tcg,thread=single,tb-size=32: allocate 33554432 bytes for jit buffer: Operation not permitted
```

QEMU's Darwin bytecode-buffer path still requested `MAP_JIT` under TCI. The
version-specific patch removes that request and unavailable JIT protection
calls only when TCI is selected. TCI's buffer remains data, not executable code.
The launcher reported exit code 0 despite this failure. No system-engine
summary or matching measurement UUID means the new runner cannot pass it.

`devicectl --console` forwards catchable host signals to the app. The runner's
post-summary collector cleanup can therefore request app termination after
measurements finish. QEMU's installed signal handler can instead consume the
signal, requiring a host-collector kill. `console.collector.json` records the
request and any escalation. This is requested test cleanup, not jetsam evidence.
The Phase 1 rerun explicitly reported remote signal 15 after its 6/6 summary.

Before and after the tests, the exposed system-crash inventory contained only
the earlier Aster reports from 21:06 and 21:07. No new Aster crash or current-day
JetsamEvent appeared. Crash-log exposure can be delayed. This observation does
not establish a jetsam threshold or guarantee that no report exists elsewhere.

## Android host gate

`tools/android/probe.py` boots a pinned Android 15 GKI 6.6.66 kernel on
`virt,gic-version=3` with matching external virtio modules. The probe verifies
Binderfs devices, SELinuxfs, a read-only virtio-block sentinel, entropy, and
keyboard enumeration. Build ran the probe independently after the delegated
implementation. That run passed in 11.428 host wall-clock seconds and reaped
QEMU. Its result explicitly records `android_booted=false`.

The probe uses an Alpine diagnostic `/init`, not Android `/init`. Android
partition mounts, vendor HALs, SELinux policy loading, Binder transactions,
zygote, SurfaceFlinger, and `sys.boot_completed` remain unexecuted. See
[android-host.md](android-host.md) for artifact hashes and dependencies, and
[phase2-plan.md](phase2-plan.md) for the complete boot-environment requirements.

## Regression checks and next decision

The Phase 1 host suite passed all 62 checks. The physical Phase 1 rerun produced
`[gate] summary 6/6`, and the corrected device script returned success. Its
mock tests reject an absent summary, a partial summary, and a `6/60` prefix
match, including timeout cases. They accept the real console's CRLF line end.

The added tests exercise console EOF, capture deadlines, and collector reaping.
QEMU patch tests apply to the unmodified 11.1.3 archive and verify idempotence.
The iOS engine object records `LC_BUILD_VERSION` with platform `IOS`, minimum
iOS 18.0, and SDK 27.0. Xcode built and signed the physical-device Release app.

The next engineering decision is the matched Android virtual-device image
bundle, not display UI or APK installation. Inspect its ramdisk, partitions,
vendor dependencies, and software graphics path before a large download.
The Linux measurements do not yet establish an Android performance limit or
justify replacing TCI. No new host software or secondary machine was used.

Complete transcripts, measurements, commands, termination observations, and
build identity are under [evidence/phase2](evidence/phase2/). Text copies
normalize CRLF and trailing whitespace for review. Their `.gz` companions
retain the original bytes.

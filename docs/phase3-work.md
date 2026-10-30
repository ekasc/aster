# Phase 3 execution state

Starting commit is `e8de5b6` on `main`. The working tree was clean before this
task. Phase 1 and the existing single-shot QEMU TCI engine remain the baseline.

## Acceptance gates

1. Host Android reaches `sys.boot_completed=1` with verified framework activity.
2. The same environment reaches that state inside the physical iPhone app.
3. An unmodified ARM64 APK installs, launches, and performs a verified operation.

A kernel, init shell, or diagnostic Linux init does not pass the first gate.
The Prove It Works principle requires guest property and framework evidence,
not an inference from Android-looking serial output.

## Ownership and execution limits

The lead owns host boot configuration, shared QEMU code and builds, iOS
integration, device testing, documentation integration, Git, and final review.

Two T3Code implementation workers use the explicitly discovered route
`opencode` with model `opencode-go/deepseek-v4.1-flash`:

- Artifact preparation owns `tools/android/images.py`,
  `tools/android/test_images.py`, `tools/android/artifacts-v1.json`, and
  `build/android/images/`.
- Boot validation owns `tools/android/boot_evidence.py`,
  `tools/android/run.py`, `tools/android/test_boot_evidence.py`, and
  `tools/android/test_run.py`.

Workers do not build QEMU, run real guests, touch the phone, install software,
manage sessions through a CLI, commit, or delegate. The shared checkout avoids
duplicate build trees. Real QEMU runs and device tests are serialized by the
lead.

Initial available space is 9.6 GiB. Artifact preparation must check actual
archive sizes, extraction needs, provenance, licenses, and pins. Its maximum
new compressed download budget is 2 GiB and total additional disk budget is
6 GiB, with at least 3 GiB remaining. No existing artifacts are deleted.
If a coherent bundle cannot fit, preserve the exact size and source evidence.

## Current state

The required Phase 2 documents, host probe, C ABI, Swift caller, embedding
patches, QEMU build script, and archive packaging implementation have been read.
T3Code capability discovery confirms the requested model route is available.
Both implementation workers were launched through T3Code with the specified
provider and model. No OpenCode CLI session was created.

The validation worker completed its four owned files. The lead inspected the
runner's wait4-based measurement and teardown implementation and independently
ran its 66 new tests successfully. Its gate needs fresh nonce-bound property,
activity-service, and system_server evidence. The guest commands remain the
lead's responsibility, so synthetic parser tests are not Android boot evidence.

The lead added `boot_config.py`, `serial_control.py`, `guest_gate.sh`, and their
tests. Nine tests pass for bootconfig serialization, hvc port mapping, private
console submission, incomplete startup, and process teardown. The control
transport uses a private UNIX socket and sends real getprop, service, pidof,
logcat, and ActivityManager commands. It creates no TCP ADB listener.

The matched bundle's BUILD_INFO pins Cuttlefish source revision
`283645aacf6cdb56cc31a9362f54d412dbc132a1` and system/core revision
`545d2487e38192a2ce25040897ced877cf6b4f53`. The lead read the actual qemu_cli
manager, fstab, first-stage init, and property initialization at those revisions.
Bootconfig properties take precedence over kernel-command-line properties.
The direct-kernel configuration therefore merges board properties into an
initramfs bootconfig trailer and passes only the fresh nonce on the command line.

The artifact worker completed the pinned manifest, preparation tool, and 32
tests. The lead reran those tests and verified the prepared files independently.
The downloaded Cuttlefish build is Android 17 with a 6.12 kernel, so reusing
the previous Android 15 GKI modules would not be a compatible module pairing.
Real guest runs remain lead-owned and serialized.

Host attempt `h1` used 1536 MiB and a 600-second deadline. Android first-stage
mounts and second-stage init ran. APEX bootstrap then aborted because both
camera-provider APEX variants were present without a selection property.
The guest requested reboot and QEMU exited. Measured wall time was 59.971495
seconds, CPU time 58.553290 seconds, and peak host RSS 222052352 bytes.
This is an incomplete Android boot, not a framework pass. Full evidence is in
`build/android/integration/h1/runs/run-20261030-064217-18d6b5db/`.

The lead added the explicit emulated-camera APEX selection and launched `h2`
with the same memory bound. That run passed bootstrap, set up userdata, and
started vendor services. Its early serial console exposed a separate probe
bug: mksh could not create a heredoc temporary file before userdata was ready.
The next controller uses `su 0 /system/bin/sh -c` with a quoted in-memory script
instead. This needs no temporary file and uses only the local userdebug console.
No Android completion or iPhone Android boot has yet been verified.

Host attempt `h2` reached its 600-second deadline. It started the actual zygote
at guest kernel uptime 281.741958 seconds and SurfaceFlinger at 370.048427
seconds. Neither launch establishes a functional framework. The activity service
was still unavailable in the captured service-manager output at the deadline.
The run measured 600.187618 wall seconds, 592.549899 CPU seconds, and 245514240
bytes peak host RSS. All QEMU resources for that run were reaped.

The real transcript exposed an over-broad zygote log matcher. A failed
`ro.zygote.disable_gl_preload` property-setting action was incorrectly treated
as execution. The matcher now requires a runtime tag or an actual service
launch. A regression test preserves that distinction. Original run results
are retained unchanged; `report.py` reanalyses their raw logs with the current
parser and labels stage timestamps as guest uptime.

Attempt `h3` used the corrected local su command, live logcat capture,
the same 1536 MiB RAM bound, and an 1800-second deadline. Its guest emitted a
fresh `framework_probe_started` marker, proving that the corrected probe did
execute through the real console. This is instrumentation evidence only.

That attempt timed out after 1800.077487 host wall seconds, with 1792.178080
CPU seconds and 312180736 bytes peak host RSS. A real system_server Binder
caller was observed, but it waited for `android.hardware.light.ILights/default`.
The stock light HAL repeatedly aborted with Permission denied. The matching
Cuttlefish source reads `ro.boot.vsock_lights_port`, defaults to port zero,
and binds a vsock listener while running as nobody. The matching kernel's
`net/vmw_vsock/af_vsock.c` rejects reserved ports without CAP_NET_BIND_SERVICE.
SELinux permissive mode does not bypass that capability check.

The lead configured `androidboot.vsock_lights_port=7000`, retained the stock HAL,
and started attempt `h4` with the same 1536 MiB and 1800-second limits. This is
a candidate configuration correction, not yet a verified fix. No system image,
Android API, or HAL implementation was replaced.

The unchanged, installed Phase 1 app was launched with `--autorun` on the
connected physical iPhone17. The gate passed 6/6. No Phase 1 rebuild was needed,
and no Android device test has been attempted before the host framework gate.

All 117 Android tooling tests pass, alongside four existing device-runner tests.
The complete failed runs and reanalysis are preserved under
`docs/evidence/phase3`. The interpreter, C ABI, iOS system engine, and Xcode
project have not been changed in this phase.

The fourth run's stock light HAL reached the service-manager registration path
at guest uptime 416.677973 seconds instead of aborting at the bind. That
establishes progress past the diagnosed capability check, not a completed
framework gate. The run subsequently timed out at 1800.085110 seconds, with
1773.065561 CPU seconds and 352960512 bytes peak host RSS. Its full evidence is
archived under `docs/evidence/phase3/h4`. ActivityManager was still unavailable.
The zygote timing log records 402178 ms in preload, including 308658 ms in class
preload. Later system_server startup remains incomplete, so increasing the
deadline without further diagnostics is not the next action.

The lead added an explicit kernel-audit toggle for a controlled audit-overhead
experiment and a reproducible completed-run archiver. The next attempt will
retain the same RAM bound and stock Android code while collecting guest thread
and service diagnostics. No fifth guest is running yet.

The existing prebuilt Phase 1 host test binary was executed without rebuilding
the interpreter. All 62 checks passed. The existing three Android kernel-probe
tests and the lead's nine integration-tool tests also passed. The probe tests
require `tools/android` on PYTHONPATH when invoked by module name; an initial
invocation without that path failed to import `probe`, then passed with the
documented test directory on PYTHONPATH. No pre-existing test was modified.

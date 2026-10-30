import sys
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

import boot_evidence as be

NONCE = "0123456789abcdef0123456789abcdef"
OLD_NONCE = "f" * 32

KERNEL = (
    "[    0.000000] Booting Linux on physical CPU 0x0000000000 [0x410fd034]",
    "[    0.000000] Linux version 6.6.66-android15-8-g807ce3b4f02f-ab12996908-4k "
    "(build@host) (aarch64-linux-gnu-gcc) #1 SMP PREEMPT",
    "[    0.000000] Kernel command line: console=ttyAMA0 "
    f"androidboot.aster_nonce={NONCE}",
)
INIT = (
    "init: init first stage started!",
    "init: Loading SELinux policy",
    "[    1.100000] init: init first stage done",
    "init: init second stage started!",
    "init: starting service 'ueventd'",
    "init: starting service 'logd'",
    "init: starting service 'zygote'",
    "SystemServer: Entered the Android system server!",
)


def line(nonce, seq, kind, value):
    return f"ASTER-BOOT|{nonce}|{seq}|{kind}|{value}"


def completion(nonce=NONCE, start=1):
    return (
        line(nonce, start, "STAGE", "first_stage_init"),
        line(nonce, start + 1, "STAGE", "zygote"),
        line(nonce, start + 2, "PROP", "sys.boot_completed=1"),
        line(nonce, start + 3, "FRAMEWORK", "service_activity=found"),
        line(nonce, start + 4, "FRAMEWORK", "system_server_pid=1234"),
    )


def transcript(*blocks, trailing=()):
    lines = [item for block in blocks for item in block] + list(trailing)
    return "\n".join(lines) + "\n"


class GateTests(unittest.TestCase):
    def test_live_system_server_binder_caller_is_not_boot_completion(self):
        output = transcript(
            KERNEL,
            INIT[:4],
            (
                "[ 1334.393238][ T375] servicemanager: Caller(pid=1990,uid=1000,sid=u:r:system_server:s0) Since android.hardware.light.ILights/default could not be found",
            ),
        )
        analysis = be.analyze(output, NONCE)
        self.assertEqual(analysis.furthest_stage, "system_server")
        self.assertFalse(analysis.ok)

    def test_zygote_property_configuration_is_not_execution(self):
        output = transcript(
            KERNEL,
            INIT[:4],
            (
                "[ 107.719087][ T1] init: Command 'setprop ro.zygote.disable_gl_preload' failed",
            ),
        )
        analysis = be.analyze(output, NONCE)
        self.assertNotIn("zygote", analysis.stages)

    def test_real_duplicate_apex_bootstrap_failure_is_reported(self):
        output = transcript(
            KERNEL,
            INIT,
            (
                "[   58.128597][   T90] apexd-bootstrap: Found two APEX packages for module com.google.emulated.camera.provider.hal",
            ),
        )
        analysis = be.analyze(output, NONCE)
        self.assertFalse(analysis.ok)
        self.assertTrue(
            any("apex_bootstrap_failure" in item for item in analysis.failures),
            analysis.failures,
        )

    def test_valid_fresh_completion_is_accepted(self):
        analysis = be.analyze(transcript(KERNEL, INIT, completion()), NONCE)
        self.assertTrue(analysis.ok, analysis.failures)
        self.assertEqual(analysis.failures, [])
        self.assertEqual(analysis.furthest_stage, "boot_completed")
        self.assertEqual(
            analysis.stages,
            [
                "kernel",
                "first_stage_init",
                "second_stage_init",
                "zygote",
                "system_server",
                "boot_completed",
            ],
        )
        self.assertEqual(analysis.completion["system_server_pid"]["pid"], 1234)
        self.assertEqual(analysis.completion["boot_completed"]["line"], 14)

    def test_android_looking_kernel_and_init_alone_rejected(self):
        analysis = be.analyze(transcript(KERNEL, INIT), NONCE)
        self.assertFalse(analysis.ok)
        self.assertEqual(analysis.furthest_stage, "system_server")
        self.assertTrue(
            any("sys.boot_completed=1" in item for item in analysis.failures),
            analysis.failures,
        )
        self.assertTrue(any("service_activity" in item for item in analysis.failures))

    def test_echoed_completion_rejected(self):
        echoed = (
            f"~ # echo ASTER-BOOT|{NONCE}|1|PROP|sys.boot_completed=1",
            f"+ echo ASTER-BOOT|{NONCE}|2|FRAMEWORK|service_activity=found",
            f"ASTER-BOOT|{NONCE}|3|FRAMEWORK|system_server_pid=1234'",
        )
        analysis = be.analyze(transcript(KERNEL, INIT, echoed), NONCE)
        self.assertFalse(analysis.ok)
        self.assertIsNone(analysis.completion["boot_completed"])
        self.assertEqual(len(analysis.protocol["rejected"]), 3)
        reasons = [entry["reason"] for entry in analysis.protocol["rejected"]]
        self.assertEqual(
            sum("does not match" in reason for reason in reasons), 2, reasons
        )
        self.assertEqual(
            sum("shell quoting" in reason for reason in reasons), 1, reasons
        )
        self.assertTrue(
            any("PROP|sys.boot_completed=1" in item for item in analysis.failures)
        )

    def test_partial_completion_rejected(self):
        full = completion()
        for index, missing in enumerate(full[2:], start=2):
            with self.subTest(missing=missing):
                analysis = be.analyze(
                    transcript(KERNEL, INIT, full[:index] + full[index + 1 :]), NONCE
                )
                self.assertFalse(analysis.ok)
                self.assertEqual(len(analysis.failures), 1, analysis.failures)

    def test_old_nonce_completion_rejected(self):
        analysis = be.analyze(transcript(KERNEL, INIT, completion(OLD_NONCE)), NONCE)
        self.assertFalse(analysis.ok)
        self.assertTrue(
            any("stale evidence" in item for item in analysis.failures),
            analysis.failures,
        )
        self.assertIn(OLD_NONCE, analysis.protocol["foreign_nonces"])

    def test_completion_before_kernel_evidence_rejected(self):
        analysis = be.analyze(transcript(completion(), KERNEL, INIT), NONCE)
        self.assertFalse(analysis.ok)
        self.assertTrue(
            any("precedes kernel boot evidence" in item for item in analysis.failures),
            analysis.failures,
        )

    def test_completion_before_first_stage_init_rejected(self):
        protocol_only = (
            line(NONCE, 1, "PROP", "sys.boot_completed=1"),
            line(NONCE, 2, "FRAMEWORK", "service_activity=found"),
            line(NONCE, 3, "FRAMEWORK", "system_server_pid=1234"),
        )
        analysis = be.analyze(transcript(KERNEL, protocol_only, INIT), NONCE)
        self.assertFalse(analysis.ok)
        self.assertTrue(
            any(
                "precedes first-stage init evidence" in item
                for item in analysis.failures
            ),
            analysis.failures,
        )

    def test_timeout_rejected_even_with_full_evidence(self):
        analysis = be.analyze(
            transcript(KERNEL, INIT, completion()), NONCE, timed_out=True
        )
        self.assertFalse(analysis.ok)
        self.assertTrue(any("timeout" in item for item in analysis.failures))

    def test_nonzero_exit_rejected(self):
        analysis = be.analyze(
            transcript(KERNEL, INIT, completion()), NONCE, exit_code=1
        )
        self.assertFalse(analysis.ok)
        self.assertTrue(any("status 1" in item for item in analysis.failures))

    def test_unrequested_signal_rejected_but_requested_stop_accepted(self):
        killed = be.analyze(
            transcript(KERNEL, INIT, completion()),
            NONCE,
            exit_code=None,
            term_signal=15,
        )
        self.assertFalse(killed.ok)
        requested = be.analyze(
            transcript(KERNEL, INIT, completion()),
            NONCE,
            exit_code=None,
            term_signal=15,
            runner_stop_requested=True,
        )
        self.assertTrue(requested.ok, requested.failures)

    def test_guest_fail_line_rejected(self):
        analysis = be.analyze(
            transcript(
                KERNEL, INIT, completion(), (line(NONCE, 9, "FAIL", "zygote died"),)
            ),
            NONCE,
        )
        self.assertFalse(analysis.ok)
        self.assertTrue(
            any("guest reported failure" in item for item in analysis.failures)
        )

    def test_sequence_regression_rejected(self):
        regressed = (
            line(NONCE, 1, "STAGE", "first_stage_init"),
            line(NONCE, 7, "PROP", "sys.boot_completed=1"),
            line(NONCE, 3, "FRAMEWORK", "service_activity=found"),
            line(NONCE, 9, "FRAMEWORK", "system_server_pid=1234"),
        )
        analysis = be.analyze(transcript(KERNEL, INIT, regressed), NONCE)
        self.assertFalse(analysis.ok)
        self.assertIsNone(analysis.completion["service_activity"])
        reasons = [entry["reason"] for entry in analysis.protocol["rejected"]]
        self.assertTrue(
            any("does not increase" in reason for reason in reasons), reasons
        )

    def test_zero_system_server_pid_rejected(self):
        analysis = be.analyze(
            transcript(
                KERNEL,
                INIT,
                completion()[:4]
                + (line(NONCE, 5, "FRAMEWORK", "system_server_pid=0"),),
            ),
            NONCE,
        )
        self.assertFalse(analysis.ok)
        self.assertIsNone(analysis.completion["system_server_pid"])

    def test_alpine_probe_marker_rejected(self):
        alpine = (
            "initramfs emergency recovery shell launched. Type 'exit' to continue boot",
            "sh: can't access tty; job control turned off",
            "~ # echo ASTER-LINUX-$(echo USERSPACE); uname -a;",
            "ASTER-LINUX-USERSPACE",
            "ASTER_KERNEL_PROBE_PASS",
        )
        analysis = be.analyze(transcript(KERNEL, INIT, alpine, completion()), NONCE)
        self.assertFalse(analysis.ok)
        self.assertTrue(
            any("alpine_userspace" in item for item in analysis.failures),
            analysis.failures,
        )

    def test_alpine_marker_without_completion_rejected(self):
        analysis = be.analyze(transcript(KERNEL, ("ASTER_KERNEL_PROBE_PASS",)), NONCE)
        self.assertFalse(analysis.ok)
        self.assertEqual(analysis.furthest_stage, "kernel")

    def test_failed_mounts_rejected_and_recorded(self):
        failing = (
            "[    0.900000] init: Failed to mount /dev/block/by-name/system",
            "fs_mgr: Failed to mount /dev/block/by-name/vendor",
            "init: starting service 'zygote'",
        )
        analysis = be.analyze(transcript(KERNEL, INIT[:1], failing), NONCE)
        self.assertFalse(analysis.ok)
        self.assertEqual(analysis.counts["mount_failure"], 2)
        self.assertIn("mount_failure", analysis.blocking_observations)
        self.assertTrue(
            any("mount_failure" in item for item in analysis.diagnostics),
            analysis.diagnostics,
        )

    def test_transient_mount_failure_does_not_block_real_completion(self):
        mixed = (
            "[    0.900000] init: Failed to mount /dev/block/by-name/metadata",
            "init: Retrying mount of /dev/block/by-name/metadata",
        )
        analysis = be.analyze(
            transcript(KERNEL, INIT[:1], mixed, INIT[1:], completion()), NONCE
        )
        self.assertTrue(analysis.ok, analysis.failures)
        self.assertEqual(analysis.counts["mount_failure"], 1)
        self.assertEqual(analysis.blocking_observations, [])
        self.assertTrue(any("mount_failure" in item for item in analysis.diagnostics))

    def test_selinux_denials_are_diagnostics_not_automatic_failure(self):
        denials = (
            'avc: denied { read } for pid=1 comm="init" scontext=u:r:init:s0',
            'avc: denied { write } for pid=300 comm="system_server"',
        )
        analysis = be.analyze(transcript(KERNEL, INIT, denials, completion()), NONCE)
        self.assertTrue(analysis.ok, analysis.failures)
        self.assertEqual(analysis.counts["selinux_denial"], 2)

    def test_hard_failures_block_even_with_completion(self):
        for marker in (
            "Kernel panic - not syncing: Attempted to kill init!",
            "init: Failed to load SELinux policy",
            "FATAL EXCEPTION IN SYSTEM PROCESS: main",
        ):
            with self.subTest(marker=marker):
                analysis = be.analyze(
                    transcript(KERNEL, INIT, ("ASTER-X " + marker,), completion()),
                    NONCE,
                )
                self.assertFalse(analysis.ok)

    def test_malformed_protocol_lines_are_reported(self):
        broken = (
            f"ASTER-BOOT|{NONCE}|1|PROP",
            f"ASTER-BOOT|{NONCE}|2|BOGUS|value",
            f"ASTER-BOOT|{NONCE}|3|STAGE|framework",
            f"ASTER-BOOT|{NONCE}|4|MARKER|",
        )
        analysis = be.analyze(transcript(KERNEL, INIT, broken), NONCE)
        self.assertFalse(analysis.ok)
        reasons = " ".join(entry["reason"] for entry in analysis.protocol["rejected"])
        self.assertIn("does not match", reasons)
        self.assertIn("unknown kind", reasons)
        self.assertIn("unknown stage", reasons)
        self.assertIn("needs a value", reasons)

    def test_invalid_nonce_rejected(self):
        for value in ("", "not-a-nonce", NONCE.upper(), NONCE + "0"):
            with self.subTest(nonce=value):
                with self.assertRaises(ValueError):
                    be.analyze("", value)

    def test_bytes_transcript_accepted(self):
        analysis = be.analyze(transcript(KERNEL, INIT, completion()).encode(), NONCE)
        self.assertTrue(analysis.ok, analysis.failures)

    def test_as_dict_is_json_serialisable(self):
        import json

        analysis = be.analyze(transcript(KERNEL, INIT, completion()), NONCE)
        payload = json.loads(json.dumps(analysis.as_dict()))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["protocol"]["last_seq"], 5)


class StageTrackerTests(unittest.TestCase):
    def test_forward_progress_and_regression(self):
        tracker = be.StageTracker()
        tracker.observe("kernel", 1, "log")
        tracker.observe("second_stage_init", 4, "log")
        self.assertEqual(tracker.furthest(), "second_stage_init")
        tracker.observe("first_stage_init", 5, "log")
        self.assertEqual(tracker.furthest(), "second_stage_init")
        self.assertEqual(len(tracker.regressions), 1)
        self.assertEqual(tracker.regressions[0]["after"], "second_stage_init")
        self.assertEqual(
            tracker.verified(), ["kernel", "first_stage_init", "second_stage_init"]
        )

    def test_repeat_observation_keeps_first_evidence(self):
        tracker = be.StageTracker()
        tracker.observe("zygote", 3, "log")
        tracker.observe("zygote", 9, "protocol")
        self.assertEqual(tracker.evidence["zygote"], {"line": 3, "source": "log"})

    def test_unknown_stage_rejected(self):
        with self.assertRaises(ValueError):
            be.StageTracker().observe("framework", 1, "log")


class ClassificationTests(unittest.TestCase):
    def test_stage_lines(self):
        cases = {
            "[    0.000000] Booting Linux on physical CPU 0x0": "kernel",
            "[    0.000000] Linux version 6.6.66-android15": "kernel",
            "[    0.000000][    T0] Booting Linux on physical CPU 0x0000000000": "kernel",
            "[    0.000000][    T1] Linux version 6.6.66-android15-8": "kernel",
            "[    1.200000][   T42] init: init second stage started!": "second_stage_init",
            "init: init first stage started!": "first_stage_init",
            "init: Loading SELinux policy": "first_stage_init",
            "init: init second stage started!": "second_stage_init",
            "init: starting service 'servicemanager'": "second_stage_init",
            "init: starting service 'zygote'": "zygote",
            "SystemServer: Entered the Android system server!": "system_server",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(be.classify_log_line(text), ("stage", expected))

    def test_failure_and_diagnostic_lines(self):
        cases = {
            "Kernel panic - not syncing: fatal": ("failure", "kernel_panic"),
            "init: Failed to load SELinux policy": ("failure", "selinux_policy_load"),
            "FATAL EXCEPTION IN SYSTEM PROCESS: boom": (
                "failure",
                "system_server_crash",
            ),
            "init: Failed to mount /dev/block/by-name/system": (
                "diagnostic",
                "mount_failure",
            ),
            "init: cannot find partition 'vendor'": ("diagnostic", "partition_failure"),
            "avc: denied { read } for pid=1": ("diagnostic", "selinux_denial"),
            "vintf: incompatible manifest for vendor": ("diagnostic", "vintf_error"),
            "hwservicemanager: cannot find service": ("diagnostic", "vendor_error"),
            "~ # ls": ("diagnostic", "root_shell_prompt"),
            "ASTER-LINUX-USERSPACE": ("diagnostic", "alpine_userspace"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(be.classify_log_line(text), expected)

    def test_unrelated_line_is_not_classified(self):
        self.assertIsNone(be.classify_log_line("random guest noise"))
        self.assertIsNone(be.classify_log_line("   "))
        self.assertIsNone(be.classify_log_line(f"ASTER-BOOT|{NONCE}|1|MARKER|hi"))


if __name__ == "__main__":
    unittest.main()

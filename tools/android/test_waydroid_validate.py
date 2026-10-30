"""Tests for the bounded Waydroid host validation runner.

Every fixture is an inline transcript or a tiny local shell script; no VM, no
image and no network is touched. Run directories live in unique temporary
directories and every spawned child is reaped by the runner under test.
"""

import contextlib
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest

from tools.android import waydroid_validate as wv

BASE_ARGS = [
    "-M",
    "virt",
    "-serial",
    "stdio",
    "-monitor",
    "none",
    "-display",
    "none",
]

KERNEL = "[   12.964801][    T1] Run /init as init process"

COMPLETE_TRANSCRIPT = "\n".join(
    [
        "[    0.000000][    T0] Booting Linux on physical CPU 0x0",
        KERNEL,
        "ASTER-WAYDROID|system_mount=ok",
        "ASTER-WAYDROID|init_stage=first_stage_init",
        "[   13.100000][    T1] init: init first stage started!",
        "[   13.150000][    T1] init: Loading SELinux policy",
        "[   13.200000][    T1] init: init second stage started!",
        "[   13.300000][    T1] init: starting service 'zygote'",
        "[   13.400000][    T1] init: starting service 'system_server'",
        "[   13.500000][    T1] Binder: 1:1 transaction",
        "ASTER-WAYDROID|system_server_pid=412",
        "ASTER-WAYDROID|prop=sys.boot_completed=1",
        "ASTER-WAYDROID|service_activity=rc=0 detail=Service activity: found",
    ]
)

ALPINE_TRANSCRIPT = "\n".join(
    [
        KERNEL,
        "ASTER-LINUX-GUEST|binderfs_nodes=binder binder-control features hwbinder vndbinder",
        "ASTER-LINUX-GUEST|release=3.20.3",
        "ASTER-LINUX-GUEST|apk=/sbin/apk",
        "ASTER-LINUX-GUEST|uid=0",
    ]
)

RECORDED_ATTEMPT1 = (
    Path(__file__).resolve().parents[2]
    / "docs/evidence/phase3/waydroid/boot-attempt1-serial.log"
)


class ParserTests(unittest.TestCase):
    def test_activity_is_required_even_with_boot_property(self):
        text = COMPLETE_TRANSCRIPT.replace(
            "ASTER-WAYDROID|service_activity=rc=0 detail=Service activity: found",
            "ASTER-WAYDROID|service_activity=rc=0 detail=Service activity: not found",
        )
        self.assertFalse(wv.analyze(text)["android_boot_verified"])

    def test_recorded_first_stage_abort_is_failure(self):
        path = RECORDED_ATTEMPT1.parent / "boot2/serial.log.gz"
        result = wv.analyze(gzip.decompress(path.read_bytes()).decode())
        self.assertEqual(result["verdict"], "failed")
        self.assertIn("init_first_stage_abort", {f["kind"] for f in result["failures"]})

    def test_recorded_second_stage_and_properties_are_not_framework_boot(self):
        path = RECORDED_ATTEMPT1.parent / "boot6/serial.log.gz"
        result = wv.analyze(gzip.decompress(path.read_bytes()).decode())
        self.assertTrue(result["facts"]["first_stage_init"])
        self.assertTrue(result["facts"]["second_stage_init"])
        self.assertEqual(result["guest_properties"]["ro.build.version.sdk"], "33")
        self.assertFalse(result["facts"]["activity"])
        self.assertFalse(result["android_boot_verified"])

    def test_recorded_property_service_roundtrip_is_not_framework_boot(self):
        path = RECORDED_ATTEMPT1.parent / "boot7/serial.log.gz"
        result = wv.analyze(gzip.decompress(path.read_bytes()).decode())
        self.assertEqual(result["guest_properties"]["debug.aster.bootstrap"], "ready")
        self.assertTrue(result["facts"]["second_stage_init"])
        self.assertFalse(result["facts"]["activity"])
        self.assertFalse(result["android_boot_verified"])

    def test_alpine_markers_never_verify_an_android_boot(self):
        result = wv.analyze(ALPINE_TRANSCRIPT, exit_code=0)
        self.assertFalse(result["android_boot_verified"])
        self.assertEqual(result["verdict"], "no_android_init")
        self.assertFalse(result["android_init_observed"])
        self.assertFalse(result["facts"]["binder"])
        self.assertFalse(result["facts"]["boot_completed"])
        self.assertEqual(result["alpine_guest_marker_lines"], 4)
        self.assertIn("no_android_init_marker_observed", result["unknowns"])
        self.assertIn("transcript_includes_alpine_guest_markers", result["unknowns"])

    def test_clean_exit_alone_is_never_a_pass(self):
        result = wv.analyze("", exit_code=0)
        self.assertFalse(result["android_boot_verified"])
        self.assertEqual(result["verdict"], "no_android_init")
        self.assertIn("boot_completed_not_observed", result["unknowns"])

    def test_complete_evidence_reaches_framework_booted(self):
        result = wv.analyze(COMPLETE_TRANSCRIPT, exit_code=1)
        self.assertTrue(result["android_boot_verified"])
        self.assertEqual(result["verdict"], "framework_booted")
        self.assertEqual(result["furthest_stage"], "boot_completed")
        self.assertEqual(
            [item["stage"] for item in result["observed_stages"]],
            [
                "kernel",
                "first_stage_init",
                "second_stage_init",
                "zygote",
                "system_server",
                "boot_completed",
            ],
        )
        self.assertTrue(all(result["facts"].values()))
        self.assertEqual(result["guest_properties"], {"sys.boot_completed": "1"})

    def test_partial_init_stages_are_reported_as_unknowns(self):
        text = "\n".join(
            [
                KERNEL,
                "[   13.100000][    T1] init: init first stage started!",
                "[   13.200000][    T1] init: init second stage started!",
            ]
        )
        result = wv.analyze(text, exit_code=0)
        self.assertFalse(result["android_boot_verified"])
        self.assertEqual(result["verdict"], "android_init_started")
        self.assertEqual(result["furthest_stage"], "second_stage_init")
        for missing in (
            "system_server_not_observed",
            "boot_completed_not_observed",
            "binder_activity_not_observed",
        ):
            self.assertIn(missing, result["unknowns"])

    def test_prefixed_and_embedded_markers_are_ignored(self):
        text = "\n".join(
            [
                "note: ASTER-WAYDROID|prop=sys.boot_completed=1",
                "[   13.600000][    T1] ASTER-WAYDROID|system_server_pid=9",
                "ASTER-WAYDROID|note=prop=sys.boot_completed=1",
                "ASTER-LINUX-GUEST|prop=sys.boot_completed=1",
            ]
        )
        result = wv.analyze(text)
        self.assertFalse(result["facts"]["boot_completed"])
        self.assertFalse(result["facts"]["system_server"])
        self.assertIn("note", result["unrecognised_guest_keys"])
        self.assertIn("guest_markers_without_known_semantics", result["unknowns"])

    def test_failure_after_complete_evidence_still_fails(self):
        text = COMPLETE_TRANSCRIPT + (
            "\n[   14.200796][    T1] Kernel panic - not syncing: Attempted to kill init!"
        )
        result = wv.analyze(text)
        self.assertFalse(result["android_boot_verified"])
        self.assertEqual(result["verdict"], "failed")
        self.assertEqual(
            [item["kind"] for item in result["failures"]], ["kernel_panic"]
        )

    def test_blocking_guest_mount_failure_prevents_a_pass(self):
        text = COMPLETE_TRANSCRIPT + "\nASTER-WAYDROID|system_mount=failed"
        result = wv.analyze(text)
        self.assertEqual(result["verdict"], "failed")
        self.assertFalse(result["android_boot_verified"])
        self.assertTrue(result["diagnostics"][0]["blocking"])

    def test_nonzero_android_init_exit_is_a_failure(self):
        result = wv.analyze("ASTER-WAYDROID|android_init_exit=1")
        self.assertEqual(result["verdict"], "failed")
        self.assertEqual(result["failures"][0]["kind"], "android_init_exit_nonzero")

    def test_timeout_and_signal_are_retained_as_unknowns(self):
        result = wv.analyze(COMPLETE_TRANSCRIPT, timed_out=True, term_signal=15)
        self.assertTrue(result["android_boot_verified"])
        self.assertIn("process_still_running_at_deadline", result["unknowns"])
        self.assertIn("process_terminated_by_signal", result["unknowns"])

    def test_recorded_attempt1_transcript_is_not_a_boot(self):
        if not RECORDED_ATTEMPT1.is_file():
            self.skipTest(f"recorded transcript unavailable: {RECORDED_ATTEMPT1}")
        text = RECORDED_ATTEMPT1.read_text(errors="replace")
        result = wv.analyze(text, exit_code=0)
        self.assertFalse(result["android_boot_verified"])
        self.assertEqual(result["verdict"], "failed")
        kinds = {item["kind"] for item in result["failures"]}
        self.assertIn("kernel_panic", kinds)
        self.assertIn("guest_namespace_failure", kinds)
        self.assertFalse(result["facts"]["system_server"])
        self.assertFalse(result["facts"]["boot_completed"])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self._scratch = tempfile.TemporaryDirectory(prefix="waydroid-validate-")
        self.addCleanup(self._scratch.cleanup)
        self.root = Path(self._scratch.name)

    def _script(self, name, body):
        path = self.root / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
        return path

    def _config(
        self,
        qemu,
        *,
        args=None,
        timeout=5,
        input_paths=None,
        hashes=None,
        extra_disks=None,
    ):
        data = {
            "schema_version": 1,
            "qemu": str(qemu),
            "args": list(BASE_ARGS if args is None else args),
            "timeout_seconds": timeout,
            "extra_disks": list(extra_disks or []),
            "stop_on_completion": False,
            "input_paths": list(input_paths or []),
            "hashes": dict(hashes or {}),
            "evidence_root": str(self.root / "runs"),
        }
        path = self.root / "config.json"
        path.write_text(json.dumps(data, indent=2) + "\n")
        return path

    def _run_main(self, argv):
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(io.StringIO()):
                return wv.main(argv)

    def test_run_records_immutable_command_and_combined_serial(self):
        script = self._script(
            "qemu-ok",
            'echo "[    0.0][    T0] Run /init as init process"\n'
            'echo "ASTER-LINUX-GUEST|release=3.20.3" >&2',
        )
        config = wv.load_config(self._config(script))
        result = wv.run_config(config, output_root=self.root / "out")

        self.assertFalse(result["verified"])
        self.assertEqual(result["verdict"], "no_android_init")
        self.assertTrue(result["process"]["reaped"])
        self.assertEqual(result["process"]["exit_code"], 0)
        self.assertIn("wait4", result["process"]["rusage_scope"])

        evidence = Path(result["evidence_dir"])
        self.assertTrue(str(evidence).startswith(str((self.root / "out").resolve())))
        command = evidence / "command.json"
        self.assertEqual(command.stat().st_mode & 0o222, 0)
        payload = json.loads(command.read_text())
        self.assertEqual(payload["command"]["argv"][0], str(script.resolve()))
        self.assertEqual(payload["config"], json.loads(config.path.read_text()))
        self.assertEqual(
            payload["config_sha256"],
            hashlib.sha256(config.path.read_bytes()).hexdigest(),
        )
        self.assertEqual(result["config_sha256"], payload["config_sha256"])
        self.assertEqual(
            result["command_sha256"], hashlib.sha256(command.read_bytes()).hexdigest()
        )

        serial = (evidence / "serial.log").read_text()
        self.assertIn("Run /init as init process", serial)
        self.assertIn("ASTER-LINUX-GUEST|release=3.20.3", serial)
        self.assertEqual(
            result["serial_sha256"], hashlib.sha256(serial.encode()).hexdigest()
        )
        written = json.loads((evidence / "result.json").read_text())
        self.assertFalse(written["verified"])
        self.assertEqual(written["analysis"]["verdict"], "no_android_init")

    def test_timeout_stops_and_reaps_only_the_owned_child(self):
        script = self._script("qemu-slow", "exec sleep 30")
        config = wv.load_config(self._config(script, timeout=1))
        started = time.monotonic()
        result = wv.run_config(config, output_root=self.root / "out")
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 20.0)
        process = result["process"]
        self.assertTrue(process["timed_out"])
        self.assertTrue(process["reaped"])
        self.assertFalse(result["verified"])
        self.assertIn(process["term_signal"], (signal.SIGTERM, signal.SIGKILL))
        with self.assertRaises(ProcessLookupError):
            os.kill(process["pid"], 0)

    def test_exec_failure_is_recorded_instead_of_raised(self):
        bad = self.root / "qemu-broken"
        bad.write_text("this is not an executable format\n")
        bad.chmod(0o755)
        result = wv.run_config(
            wv.load_config(self._config(bad)), output_root=self.root / "out"
        )
        self.assertFalse(result["verified"])
        self.assertTrue(result["process"]["reaped"])
        self.assertEqual(result["process"]["exit_code"], 127)
        self.assertIn("process_exit_nonzero", result["analysis"]["unknowns"])

    def test_two_runs_get_distinct_evidence_directories(self):
        script = self._script("qemu-ok", "echo kernel")
        config = wv.load_config(self._config(script))
        first = wv.run_config(config, output_root=self.root / "out")
        second = wv.run_config(config, output_root=self.root / "out")
        self.assertNotEqual(first["evidence_dir"], second["evidence_dir"])

    def test_hash_mismatch_aborts_before_any_run_directory(self):
        script = self._script("qemu-ok", "echo kernel")
        payload = self.root / "kernel.bin"
        payload.write_bytes(b"kernel")
        config = self._config(
            script,
            input_paths=[str(payload)],
            hashes={str(payload): "0" * 64},
        )
        loaded = wv.load_config(config)
        with self.assertRaises(wv.ContractError) as caught:
            wv.run_config(loaded, output_root=self.root / "out")
        self.assertIn("hash mismatch", str(caught.exception))
        self.assertFalse((self.root / "out").exists())

    def test_contract_refusals_happen_before_boot(self):
        script = self._script("qemu-ok", "echo kernel")
        cases = {
            "no serial": BASE_ARGS[:2] + ["-serial", "none", "-monitor", "none"],
            "daemonize": BASE_ARGS + ["-daemonize"],
            "no serial at all": ["-monitor", "none"],
        }
        for label, args in cases.items():
            with self.subTest(label):
                with self.assertRaises(wv.ContractError):
                    wv.load_config(self._config(script, args=args))

    def test_unknown_key_and_stop_on_completion_are_refused(self):
        script = self._script("qemu-ok", "echo kernel")
        for label, mutate in {
            "unknown key": lambda data: data.update({"surprise": 1}),
            "stop on completion": lambda data: data.update(
                {"stop_on_completion": True}
            ),
            "bad timeout": lambda data: data.update({"timeout_seconds": 0}),
            "missing disk": lambda data: data.update({"extra_disks": ["nope.img"]}),
        }.items():
            with self.subTest(label):
                path = self._config(script)
                data = json.loads(path.read_text())
                mutate(data)
                path.write_text(json.dumps(data))
                with self.assertRaises(wv.ContractError):
                    wv.load_config(path)

    def test_cli_rejects_timeout_override_without_starting_anything(self):
        script = self._script("qemu-ok", "echo kernel")
        config = self._config(script)
        self.assertEqual(
            self._run_main(
                [
                    "--config",
                    str(config),
                    "--output",
                    str(self.root / "out"),
                    "--timeout",
                    "0",
                ]
            ),
            2,
        )
        self.assertFalse((self.root / "out").exists())

    def test_cli_exit_code_follows_evidence_not_process_success(self):
        script = self._script(
            "qemu-android",
            'echo "[   12.9][    T1] Run /init as init process"\n'
            "echo 'ASTER-WAYDROID|init_stage=first_stage_init'\n"
            '[ -n "$ASTER_NEVER_SET" ] && exit 1\n'
            "exit 0",
        )
        config = self._config(script)
        self.assertEqual(
            self._run_main(
                ["--config", str(config), "--output", str(self.root / "out")]
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()

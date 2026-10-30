import contextlib
import io
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

import boot_evidence as be
import run as boot_run

ROOT = MODULE_DIR.parents[1]
VALIDATION = ROOT / "build/android/validation"

FAKE_QEMU = '''#!/usr/bin/env python3
"""Stand-in for qemu-system-aarch64: emits a controlled serial transcript."""

import sys
import time

MODE = "__MODE__"
EXIT_CODE = __EXIT__

args = sys.argv[1:]
if "--version" in args:
    print("QEMU emulator version 11.1.3 (fake stand-in)")
    raise SystemExit(0)

nonce = ""
for arg in args:
    if "aster_nonce=" in arg:
        nonce = arg.split("aster_nonce=", 1)[1].split()[0]


def emit(value):
    print(value, flush=True)


for item in (
    "[    0.000000] Booting Linux on physical CPU 0x0000000000",
    "[    0.000000] Linux version 6.6.66-android15-8-test",
    "init: init first stage started!",
    "init: init second stage started!",
    "init: starting service 'zygote'",
    "SystemServer: Entered the Android system server!",
):
    emit(item)

if MODE == "hang":
    time.sleep(60.0)
elif MODE == "incomplete":
    pass
elif MODE == "stale":
    other = "f" * 32
    emit("ASTER-BOOT|%s|1|PROP|sys.boot_completed=1" % other)
    emit("ASTER-BOOT|%s|2|FRAMEWORK|service_activity=found" % other)
    emit("ASTER-BOOT|%s|3|FRAMEWORK|system_server_pid=4242" % other)
else:
    for seq, pair in enumerate(
        (
            ("STAGE", "first_stage_init"),
            ("STAGE", "zygote"),
            ("PROP", "sys.boot_completed=1"),
            ("FRAMEWORK", "service_activity=found"),
            ("FRAMEWORK", "system_server_pid=4242"),
        ),
        1,
    ):
        emit("ASTER-BOOT|%s|%d|%s|%s" % (nonce, seq, pair[0], pair[1]))
    if MODE == "complete-then-sleep":
        time.sleep(60.0)

raise SystemExit(EXIT_CODE)
'''

SERIAL_ARGS = ["-serial", "stdio"]


def scratch(testcase, name="scratch"):
    VALIDATION.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=VALIDATION))
    testcase.addCleanup(shutil.rmtree, directory, ignore_errors=True)
    return directory


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def make_fake_qemu(root, *, header="tci", mode="pass", exit_code=0):
    qemu = root / "qemu-system-aarch64"
    script = FAKE_QEMU.replace("__MODE__", mode).replace("__EXIT__", str(exit_code))
    qemu.write_text(script)
    qemu.chmod(0o755)
    if header == "tci":
        (root / "config-host.h").write_text(
            "#define CONFIG_TCG 1\n#define CONFIG_TCG_INTERPRETER\n"
        )
    elif header == "notci":
        (root / "config-host.h").write_text(
            "#define CONFIG_TCG 1\n#undef CONFIG_TCG_INTERPRETER\n"
        )
    return qemu


def base_config(executable, **overrides):
    data = {
        "schema_version": 1,
        "qemu": str(executable),
        "args": [
            "-M",
            "virt,gic-version=3",
            "-accel",
            "tcg,thread=single",
            "-display",
            "none",
            "-monitor",
            "none",
            *SERIAL_ARGS,
            "-nic",
            "none",
            "-append",
            "console=ttyAMA0 androidboot.aster_nonce={nonce}",
        ],
        "timeout_seconds": 5,
    }
    data.update(overrides)
    return data


def write_config(root, data):
    path = root / "boot.json"
    path.write_text(json.dumps(data))
    return path


class SuperviseTests(unittest.TestCase):
    def test_normal_exit_records_child_usage_and_reaps(self):
        root = scratch(self, "normal")
        log = root / "serial.log"
        code = "import os; print(os.getpid(), flush=True)"
        outcome = boot_run.supervise([sys.executable, "-c", code], log, 10)
        self.assertEqual(outcome.exit_code, 0)
        self.assertIsNone(outcome.term_signal)
        self.assertFalse(outcome.timed_out)
        self.assertTrue(outcome.reaped)
        self.assertGreater(outcome.wall_seconds, 0)
        self.assertIsNotNone(outcome.cpu_seconds)
        self.assertGreaterEqual(outcome.cpu_seconds, 0)
        self.assertIsNotNone(outcome.peak_rss_bytes)
        self.assertGreater(outcome.peak_rss_bytes, 0)
        self.assertEqual(int(log.read_text().strip()), outcome.pid)
        self.assertFalse(alive(outcome.pid))

    def test_deadline_stops_cooperatively_then_reaps(self):
        root = scratch(self, "timeout")
        log = root / "serial.log"
        code = "import time; time.sleep(60)"
        outcome = boot_run.supervise([sys.executable, "-c", code], log, 0.3, grace=2.0)
        self.assertTrue(outcome.timed_out)
        self.assertTrue(outcome.reaped)
        self.assertTrue(outcome.cooperative_stop)
        self.assertFalse(outcome.escalated)
        self.assertEqual(outcome.term_signal, signal.SIGTERM)
        self.assertIsNone(outcome.exit_code)
        self.assertGreaterEqual(outcome.wall_seconds, 0.3)
        self.assertFalse(alive(outcome.pid))

    def test_nonzero_exit_is_reported_and_reaped(self):
        root = scratch(self, "nonzero")
        log = root / "serial.log"
        code = "import sys; sys.exit(7)"
        outcome = boot_run.supervise([sys.executable, "-c", code], log, 10)
        self.assertEqual(outcome.exit_code, 7)
        self.assertFalse(outcome.timed_out)
        self.assertFalse(alive(outcome.pid))

    def test_escalates_to_kill_when_child_ignores_sigterm(self):
        root = scratch(self, "escalate")
        log = root / "serial.log"
        code = (
            "import signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); "
            "time.sleep(60)"
        )

        def ready():
            return "ready" in log.read_text()

        outcome = boot_run.supervise(
            [sys.executable, "-c", code], log, 30, should_stop=ready, grace=0.5
        )
        self.assertTrue(outcome.runner_stop_requested)
        self.assertTrue(outcome.escalated)
        self.assertEqual(outcome.term_signal, signal.SIGKILL)
        self.assertTrue(outcome.reaped)
        self.assertFalse(alive(outcome.pid))

    def test_requested_stop_is_cooperative(self):
        root = scratch(self, "stop")
        log = root / "serial.log"
        code = "print('ready', flush=True); import time; time.sleep(60)"

        def ready():
            return "ready" in log.read_text()

        outcome = boot_run.supervise(
            [sys.executable, "-c", code], log, 30, should_stop=ready
        )
        self.assertTrue(outcome.runner_stop_requested)
        self.assertTrue(outcome.cooperative_stop)
        self.assertFalse(outcome.escalated)
        self.assertEqual(outcome.term_signal, signal.SIGTERM)
        self.assertFalse(alive(outcome.pid))

    def test_exception_in_should_stop_still_reaps_child(self):
        root = scratch(self, "exception")
        log = root / "serial.log"
        code = "import os, time; print(os.getpid(), flush=True); time.sleep(60)"

        def boom():
            if not log.read_text().strip():
                return False
            raise RuntimeError("watcher failed")

        with self.assertRaises(RuntimeError):
            boot_run.supervise([sys.executable, "-c", code], log, 30, should_stop=boom)
        pid = int(log.read_text().strip())
        self.assertFalse(alive(pid))

    def test_measurement_record_labels_rusage_scope(self):
        root = scratch(self, "measurement")
        log = root / "serial.log"
        outcome = boot_run.supervise([sys.executable, "-c", "print('done')"], log, 10)
        record = boot_run.measurement_record(outcome)
        self.assertIn("wait4", record["rusage_scope"])
        self.assertIn("cumulative", record["rusage_scope"])
        self.assertIn("true high-water", record["peak_rss_source"])
        self.assertTrue(record["sampling"].startswith("none;"))


class ConfigTests(unittest.TestCase):
    def test_accepts_documented_contract(self):
        root = scratch(self, "config-ok")
        qemu = make_fake_qemu(root)
        image = root / "Image"
        image.write_bytes(b"kernel")
        data = base_config(
            qemu, input_paths=[str(image)], evidence_root=str(root / "runs")
        )
        config = boot_run.load_config(write_config(root, data))
        self.assertEqual(config.timeout_seconds, 5)
        self.assertEqual(config.input_paths, (image,))
        self.assertTrue(config.evidence_root.is_absolute())

    def test_rejects_unknown_key(self):
        root = scratch(self, "config-unknown")
        qemu = make_fake_qemu(root)
        path = write_config(root, base_config(qemu, kernel="typo"))
        with self.assertRaisesRegex(boot_run.ContractError, "unknown config key"):
            boot_run.load_config(path)

    def test_rejects_bad_schema_version(self):
        root = scratch(self, "config-schema")
        qemu = make_fake_qemu(root)
        for version in (2, "1", True, None):
            with self.subTest(version=version):
                path = write_config(root, base_config(qemu, schema_version=version))
                with self.assertRaisesRegex(boot_run.ContractError, "schema_version"):
                    boot_run.load_config(path)

    def test_rejects_relative_missing_or_non_executable_qemu(self):
        root = scratch(self, "config-qemu")
        qemu = make_fake_qemu(root)

        relative = write_config(root, base_config(qemu, qemu="./qemu-system-aarch64"))
        with self.assertRaisesRegex(boot_run.ContractError, "absolute"):
            boot_run.load_config(relative)

        missing = write_config(root, base_config(qemu, qemu=str(root / "absent")))
        with self.assertRaisesRegex(boot_run.ContractError, "not an existing file"):
            boot_run.load_config(missing)

        plain = root / "plain"
        plain.write_text("")
        plain.chmod(0o644)
        not_executable = write_config(root, base_config(qemu, qemu=str(plain)))
        with self.assertRaisesRegex(boot_run.ContractError, "not executable"):
            boot_run.load_config(not_executable)

    def test_rejects_bad_timeout(self):
        root = scratch(self, "config-timeout")
        qemu = make_fake_qemu(root)
        for value in (0, 1801, -1, "300", True, 3.5):
            with self.subTest(value=value):
                path = write_config(root, base_config(qemu, timeout_seconds=value))
                with self.assertRaisesRegex(boot_run.ContractError, "timeout_seconds"):
                    boot_run.load_config(path)

    def test_requires_nonce_placeholder(self):
        root = scratch(self, "config-nonce")
        qemu = make_fake_qemu(root)
        data = base_config(qemu)
        data["args"] = [
            item.replace(" androidboot.aster_nonce={nonce}", "")
            for item in data["args"]
        ]
        path = write_config(root, data)
        with self.assertRaisesRegex(boot_run.ContractError, "nonce"):
            boot_run.load_config(path)

    def test_rejects_unknown_placeholder(self):
        root = scratch(self, "config-placeholder")
        qemu = make_fake_qemu(root)
        data = base_config(qemu)
        data["args"] = [item.replace("{nonce}", "{kernel}") for item in data["args"]]
        path = write_config(root, data)
        with self.assertRaisesRegex(boot_run.ContractError, "unknown placeholder"):
            boot_run.load_config(path)

    def guard_case(self, root, qemu, args):
        data = base_config(qemu)
        data["args"] = [
            *args,
            "-append",
            "console=ttyAMA0 androidboot.aster_nonce={nonce}",
        ]
        return write_config(root, data)

    def test_rejects_serial_null(self):
        root = scratch(self, "config-serial-null")
        qemu = make_fake_qemu(root)
        path = self.guard_case(root, qemu, ["-serial", "null", "-monitor", "none"])
        with self.assertRaisesRegex(boot_run.ContractError, "serial console"):
            boot_run.load_config(path)

    def test_rejects_serial_file(self):
        root = scratch(self, "config-serial-file")
        qemu = make_fake_qemu(root)
        path = self.guard_case(
            root, qemu, ["-serial", "file:boot.log", "-monitor", "none"]
        )
        with self.assertRaisesRegex(boot_run.ContractError, "serial console"):
            boot_run.load_config(path)

    def test_rejects_monitor_on_stdio(self):
        root = scratch(self, "config-monitor")
        qemu = make_fake_qemu(root)
        path = self.guard_case(root, qemu, ["-serial", "stdio", "-monitor", "stdio"])
        with self.assertRaisesRegex(boot_run.ContractError, "monitor"):
            boot_run.load_config(path)

    def test_rejects_qmp_over_tcp(self):
        root = scratch(self, "config-qmp")
        qemu = make_fake_qemu(root)
        path = self.guard_case(
            root,
            qemu,
            [
                "-serial",
                "stdio",
                "-monitor",
                "none",
                "-qmp",
                "tcp:127.0.0.1:4444,server,nowait",
            ],
        )
        with self.assertRaisesRegex(boot_run.ContractError, "QMP"):
            boot_run.load_config(path)

    def test_rejects_missing_console(self):
        root = scratch(self, "config-no-console")
        qemu = make_fake_qemu(root)
        path = self.guard_case(root, qemu, ["-display", "none"])
        with self.assertRaisesRegex(boot_run.ContractError, "no serial console"):
            boot_run.load_config(path)

    def test_rejects_nographic_without_monitor_none(self):
        root = scratch(self, "config-nographic")
        qemu = make_fake_qemu(root)
        path = self.guard_case(root, qemu, ["-nographic"])
        with self.assertRaisesRegex(boot_run.ContractError, "nographic"):
            boot_run.load_config(path)

    def test_accepts_nographic_with_monitor_none(self):
        root = scratch(self, "config-nographic-ok")
        qemu = make_fake_qemu(root)
        data = base_config(qemu)
        data["args"] = [
            "-nographic",
            "-monitor",
            "none",
            "-append",
            "console=ttyAMA0 aster_nonce={nonce}",
        ]
        config = boot_run.load_config(write_config(root, data))
        self.assertIn("-nographic", config.args)

    def test_rejects_daemonize(self):
        root = scratch(self, "config-daemonize")
        qemu = make_fake_qemu(root)
        data = base_config(qemu)
        data["args"].append("-daemonize")
        path = write_config(root, data)
        with self.assertRaisesRegex(boot_run.ContractError, "daemonize"):
            boot_run.load_config(path)

    def test_rejects_adb_port_forward(self):
        root = scratch(self, "config-adb")
        qemu = make_fake_qemu(root)
        path = self.guard_case(
            root,
            qemu,
            [
                "-serial",
                "stdio",
                "-monitor",
                "none",
                "-netdev",
                "user,id=net0,hostfwd=tcp::5555-:5555",
                "-append",
                "console=ttyAMA0 aster_nonce={nonce}",
            ],
        )
        with self.assertRaisesRegex(boot_run.ContractError, "5555"):
            boot_run.load_config(path)

    def test_rejects_hash_mismatch_before_boot(self):
        root = scratch(self, "config-hash")
        qemu = make_fake_qemu(root)
        image = root / "Image"
        image.write_bytes(b"kernel")
        config = boot_run.load_config(
            write_config(root, base_config(qemu, hashes={str(image): "0" * 64}))
        )
        with self.assertRaisesRegex(boot_run.ContractError, "hash mismatch"):
            boot_run.verify_inputs(config)

    def test_build_argv_substitutes_placeholders(self):
        root = scratch(self, "config-argv")
        qemu = make_fake_qemu(root)
        config = boot_run.load_config(write_config(root, base_config(qemu)))
        argv = boot_run.build_argv(config, "a" * 32, Path("/tmp/evidence"))
        self.assertEqual(argv[0], str(qemu))
        self.assertTrue(any("a" * 32 in item for item in argv))
        self.assertFalse(any("{nonce}" in item for item in argv))
        self.assertFalse(any("{evidence_dir}" in item for item in argv))

    def test_new_evidence_dir_is_unique(self):
        root = scratch(self, "config-evidence")
        first = boot_run.new_evidence_dir(root, "a" * 32)
        second = boot_run.new_evidence_dir(root, "b" * 32)
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir() and second.is_dir())
        with self.assertRaises(FileExistsError):
            boot_run.new_evidence_dir(root, "a" * 32)


class ProvenanceTests(unittest.TestCase):
    def test_tci_header_accepted(self):
        root = scratch(self, "provenance-tci")
        record = boot_run.inspect_qemu(make_fake_qemu(root))
        self.assertTrue(record["config_host_h_present"])
        self.assertTrue(record["tcg"])
        self.assertTrue(record["tcg_interpreter"])
        boot_run.require_tci_provenance(record)

    def test_non_tci_header_refused_by_default(self):
        root = scratch(self, "provenance-notci")
        record = boot_run.inspect_qemu(make_fake_qemu(root, header="notci"))
        with self.assertRaisesRegex(boot_run.ContractError, "not a TCI build"):
            boot_run.require_tci_provenance(record)
        boot_run.require_tci_provenance(record, allow_non_tci=True)

    def test_missing_header_is_unknown_provenance(self):
        root = scratch(self, "provenance-unknown")
        record = boot_run.inspect_qemu(make_fake_qemu(root, header=None))
        self.assertFalse(record["config_host_h_present"])
        self.assertIsNone(record["tcg_interpreter"])
        with self.assertRaisesRegex(boot_run.ContractError, "provenance unknown"):
            boot_run.require_tci_provenance(record)
        boot_run.require_tci_provenance(record, allow_unknown=True)


class EndToEndTests(unittest.TestCase):
    def prepare(self, name, *, mode="pass", exit_code=0, **overrides):
        root = scratch(self, name)
        qemu = make_fake_qemu(root, mode=mode, exit_code=exit_code)
        data = base_config(qemu, **overrides)
        data.setdefault("evidence_root", str(root / "runs"))
        config = write_config(root, data)
        return root, config

    def call(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = boot_run.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def result_of(self, config):
        data = json.loads(config.read_text())
        runs = sorted(Path(data["evidence_root"]).glob("run-*"))
        self.assertEqual(len(runs), 1, runs)
        return runs[0], json.loads((runs[0] / "result.json").read_text())

    def test_passing_boot(self):
        root, config = self.prepare("e2e-pass")
        image = root / "Image"
        image.write_bytes(b"kernel-image")
        data = json.loads(config.read_text())
        data["input_paths"] = [str(image)]
        config.write_text(json.dumps(data))

        code, stdout, stderr = self.call(["--config", str(config)])
        self.assertEqual(code, 0, stderr + stdout)
        run_dir, result = self.result_of(config)
        self.assertTrue(result["passed"])
        self.assertTrue(result["android_booted"])
        self.assertEqual(len(result["nonce"]), 32)
        self.assertIsNotNone(be.NONCE_PATTERN.match(result["nonce"]))
        self.assertEqual(result["measurement"]["exit_code"], 0)
        self.assertTrue(result["measurement"]["reaped"])
        self.assertGreater(result["measurement"]["peak_rss_bytes"], 0)
        self.assertGreater(result["measurement"]["wall_seconds"], 0)
        self.assertEqual(result["evidence"]["furthest_stage"], "boot_completed")
        self.assertEqual(
            result["evidence"]["completion"]["system_server_pid"]["pid"], 4242
        )

        command = json.loads((run_dir / "command.json").read_text())
        self.assertIn(result["nonce"], " ".join(command["command"]))
        self.assertFalse(any("{nonce}" in item for item in command["command"]))
        self.assertTrue(command["qemu"]["tcg_interpreter"])
        self.assertIn(str(image), command["inputs"])
        transcript = (run_dir / "serial.log").read_text()
        self.assertIn(
            f"ASTER-BOOT|{result['nonce']}|3|PROP|sys.boot_completed=1", transcript
        )
        self.assertEqual(
            json.loads((run_dir / "command.json").read_text())["nonce"],
            result["nonce"],
        )

    def test_hung_boot_times_out_and_is_reaped(self):
        root, config = self.prepare("e2e-timeout", mode="hang", timeout_seconds=1)
        code, stdout, stderr = self.call(["--config", str(config)])
        self.assertEqual(code, 1, stderr + stdout)
        run_dir, result = self.result_of(config)
        self.assertFalse(result["passed"])
        self.assertTrue(result["measurement"]["timed_out"])
        self.assertTrue(result["measurement"]["reaped"])
        self.assertFalse(alive(result["measurement"]["pid"]))
        self.assertTrue(
            any("timeout" in item for item in result["evidence"]["failures"])
        )
        self.assertEqual(result["evidence"]["furthest_stage"], "system_server")

    def test_eof_without_completion_fails(self):
        root, config = self.prepare("e2e-incomplete", mode="incomplete")
        code, stdout, stderr = self.call(["--config", str(config)])
        self.assertEqual(code, 1, stderr + stdout)
        run_dir, result = self.result_of(config)
        self.assertFalse(result["passed"])
        self.assertEqual(result["measurement"]["exit_code"], 0)
        self.assertTrue(
            any(
                "serial stream ended" in item
                for item in result["evidence"]["failures"]
            ),
            result["evidence"]["failures"],
        )

    def test_stale_nonce_evidence_fails(self):
        root, config = self.prepare("e2e-stale", mode="stale")
        code, stdout, stderr = self.call(["--config", str(config)])
        self.assertEqual(code, 1, stderr + stdout)
        run_dir, result = self.result_of(config)
        self.assertFalse(result["passed"])
        self.assertTrue(
            any("stale evidence" in item for item in result["evidence"]["failures"])
        )

    def test_nonzero_exit_fails(self):
        root, config = self.prepare("e2e-exit", exit_code=3)
        code, stdout, stderr = self.call(["--config", str(config)])
        self.assertEqual(code, 1, stderr + stdout)
        run_dir, result = self.result_of(config)
        self.assertFalse(result["passed"])
        self.assertEqual(result["measurement"]["exit_code"], 3)
        self.assertTrue(
            any("status 3" in item for item in result["evidence"]["failures"])
        )

    def test_stop_on_completion_stops_early_and_passes(self):
        root, config = self.prepare(
            "e2e-stop",
            mode="complete-then-sleep",
            timeout_seconds=60,
            stop_on_completion=True,
        )
        started = time.monotonic()
        code, stdout, stderr = self.call(["--config", str(config)])
        elapsed = time.monotonic() - started
        self.assertEqual(code, 0, stderr + stdout)
        self.assertLess(elapsed, 20.0)
        run_dir, result = self.result_of(config)
        self.assertTrue(result["passed"])
        self.assertTrue(result["measurement"]["runner_stop_requested"])
        self.assertTrue(result["measurement"]["reaped"])
        self.assertFalse(alive(result["measurement"]["pid"]))

    def test_bad_config_returns_two(self):
        root = scratch(self, "e2e-bad")
        config = root / "boot.json"
        config.write_text(json.dumps({"schema_version": 1}))
        code, stdout, stderr = self.call(["--config", str(config)])
        self.assertEqual(code, 2)
        self.assertIn("error:", stderr)

    def test_unknown_provenance_requires_explicit_flag(self):
        root, config = self.prepare("e2e-provenance")
        (root / "config-host.h").unlink()
        code, stdout, stderr = self.call(["--config", str(config)])
        self.assertEqual(code, 2)
        self.assertIn("provenance unknown", stderr)
        code, stdout, stderr = self.call(
            ["--config", str(config), "--allow-unknown-qemu-provenance"]
        )
        self.assertEqual(code, 0, stderr + stdout)


if __name__ == "__main__":
    unittest.main()

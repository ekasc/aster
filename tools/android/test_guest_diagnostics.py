"""Fixture tests for the bounded guest diagnostics snapshot.

Every Android command is a stub resolved through a sandbox PATH. The process
tree is a temporary fixture, so no host process is inspected, signalled, or
passed to a real debuggerd.
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/android/guest_diagnostics.sh"
NONCE = "0123456789abcdef0123456789abcdef"
SYSTEM_SERVER_PID = 4242
PROCESS_PIDS = {
    "system_server": SYSTEM_SERVER_PID,
    "zygote": 300,
    "zygote_secondary": 301,
    "surfaceflinger": 500,
}


class Fixture:
    def __init__(self, root):
        self.root = root
        self.bin = root / "bin"
        self.proc = root / "proc"
        self.bin.mkdir()
        self.proc.mkdir()
        self.log = root / "calls.log"
        self.log.write_text("")
        for utility in ("tr", "cut"):
            target = shutil.which(utility)
            if target is None:
                raise unittest.SkipTest(f"{utility} is required to run the helper")
            (self.bin / utility).symlink_to(target)

        (self.proc / "uptime").write_text("1234.56 999.00\n")
        for name, pid in PROCESS_PIDS.items():
            self.add_process(pid, name)

        self.stub_getprop(
            {
                "ro.boot.aster_nonce": NONCE,
                "sys.boot_completed": "0",
                "init.svc.zygote": "running",
                "init.svc.zygote_secondary": "stopped",
                "init.svc.surfaceflinger": "running",
                "init.svc.servicemanager": "running",
                "init.svc.logd": "running",
                "init.svc.bootanim": "stopped",
            }
        )
        self.stub_pidof({name: str(pid) for name, pid in PROCESS_PIDS.items()})
        self.stub_service()
        self.stub_ps()
        self.stub_timeout()
        self.stub_debuggerd()

    def add_process(self, pid, name, rss="352960"):
        directory = self.proc / str(pid)
        directory.mkdir()
        (directory / "status").write_text(
            "Name:\t{name}\n"
            "State:\tS (sleeping)\n"
            "Tgid:\t{pid}\n"
            "Pid:\t{pid}\n"
            "PPid:\t1\n"
            "Threads:\t250\n"
            "VmPeak:\t 4000000 kB\n"
            "VmSize:\t 3900000 kB\n"
            "VmRSS:\t {rss} kB\n"
            "VmSwap:\t 0 kB\n".format(name=name, pid=pid, rss=rss)
        )
        (directory / "wchan").write_text("binder_wait_for_work\n")

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def remove(self, name):
        (self.bin / name).unlink()

    def stub_getprop(self, values):
        lines = ['case "$1" in']
        for key, value in values.items():
            lines.append(
                "    {key}) printf '%s\\n' '{value}' ;;".format(key=key, value=value)
            )
        lines.append("    *) printf '%s\\n' '' ;;")
        lines.append("esac")
        self.stub("getprop", "\n".join(lines) + "\n")

    def stub_pidof(self, mapping):
        lines = ['case "$1" in']
        for name, pid in mapping.items():
            if pid is None:
                lines.append("    {name}) exit 1 ;;".format(name=name))
            else:
                lines.append(
                    "    {name}) printf '%s\\n' '{pid}' ;;".format(name=name, pid=pid)
                )
        lines.append("    *) exit 1 ;;")
        lines.append("esac")
        self.stub("pidof", "\n".join(lines) + "\n")

    def stub_service(self):
        self.stub(
            "service",
            'printf \'service %s\\n\' "$*" >> "$ASTER_CALL_LOG"\n'
            'case "$2" in\n'
            "    activity) printf 'Service activity: found\\n' ;;\n"
            "    *) printf 'Service %s: not found\\n' \"$2\" ;;\n"
            "esac\n",
        )

    def stub_ps(self):
        self.stub(
            "ps",
            'printf \'ps %s\\n\' "$*" >> "$ASTER_CALL_LOG"\n'
            "printf 'PID PPID STAT WCHAN VSZ RSS NAME\\n'\n"
            'printf \'%s 1 S binder_wait_for_work 3900000 352960 proc-%s\\n\' "$2" "$2"\n',
        )

    def stub_timeout(self):
        self.stub(
            "timeout",
            'printf \'timeout %s\\n\' "$*" >> "$ASTER_CALL_LOG"\nshift\nexec "$@"\n',
        )

    def stub_debuggerd(self, line_count=3):
        self.stub(
            "debuggerd",
            'printf \'debuggerd %s\\n\' "$*" >> "$ASTER_CALL_LOG"\n'
            "i=0\n"
            'while [ "$i" -lt {count} ]; do\n'.format(count=line_count)
            + "    printf '#%02d pc 0000000000000000 /system/lib64/libc.so\\n' \"$i\"\n"
            "    i=$((i + 1))\n"
            "done\n",
        )

    def calls(self):
        return self.log.read_text()


class GuestDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.fixture = Fixture(Path(self._temporary.name))

    def tearDown(self):
        self._temporary.cleanup()

    def run_script(self, shell="/bin/sh", **overrides):
        env = {
            "PATH": str(self.fixture.bin),
            "ASTER_DIAG_PROC_ROOT": str(self.fixture.proc),
            "ASTER_CALL_LOG": str(self.fixture.log),
        }
        env.update(overrides)
        return subprocess.run(
            [shell, str(SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def diagnostics(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ASTER-BOOT", result.stdout)
        lines = [
            line
            for line in result.stdout.splitlines()
            if line.startswith("ASTER-DIAG|")
        ]
        self.assertTrue(lines)
        return lines

    def test_script_parses_with_posix_sh(self):
        result = subprocess.run(
            ["/bin/sh", "-n", str(SCRIPT)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_snapshot_under_available_posix_shells(self):
        shells = ["/bin/sh"]
        if Path("/bin/dash").exists():
            shells.append("/bin/dash")
        for shell in shells:
            with self.subTest(shell=shell):
                stdout = "\n".join(self.diagnostics(self.run_script(shell=shell)))
                self.assertIn("|UPTIME_SECONDS|1234.56", stdout)
                self.assertIn("VmRSS=352960 kB", stdout)
                self.assertIn("|BACKTRACE|system_server pid=4242 rc=0 lines=3", stdout)

    def test_script_has_no_gate_marker_and_no_signal(self):
        text = SCRIPT.read_text()
        self.assertNotIn("ASTER-BOOT", text)
        self.assertIsNone(re.search(r"\bkill\b", text))

    def test_true_measured_values_are_preserved(self):
        result = self.run_script()
        lines = self.diagnostics(result)
        stdout = "\n".join(lines)
        self.assertIn("|UPTIME_SECONDS|1234.56", stdout)
        self.assertIn("|BOOT_COMPLETED|0", stdout)
        self.assertIn("|PROP|init.svc.zygote=running", stdout)
        self.assertIn("|PROP|init.svc.zygote_secondary=stopped", stdout)
        self.assertIn("|PROCESS|system_server pid=4242", stdout)
        self.assertIn("Threads=250", stdout)
        self.assertIn("VmRSS=352960 kB", stdout)
        self.assertIn("|PROCESS_WCHAN|system_server=binder_wait_for_work", stdout)
        self.assertIn("PS_ROW|system_server", stdout)
        self.assertIn(
            "|BINDER_SERVICE|activity rc=0 bound=timeout Service activity: found",
            stdout,
        )
        self.assertIn("|BINDER_SERVICE|package rc=0 bound=timeout", stdout)
        self.assertIn("|BACKTRACE|system_server pid=4242 rc=0 lines=3", stdout)
        self.assertIn("|SNAPSHOT_END|guest_diagnostics_v1", stdout)

    def test_output_lines_are_finite_and_carry_the_fresh_nonce(self):
        lines = self.diagnostics(self.run_script())
        sequences = []
        for line in lines:
            fields = line.split("|", 4)
            self.assertEqual(len(fields), 5, line)
            self.assertEqual(fields[0], "ASTER-DIAG")
            self.assertEqual(fields[1], NONCE)
            self.assertTrue(fields[3])
            sequences.append(int(fields[2]))
        self.assertEqual(sequences, list(range(1, len(sequences) + 1)))

    def test_absent_system_server_skips_backtrace(self):
        self.fixture.stub_pidof(
            {
                "system_server": None,
                "zygote": "300",
                "zygote_secondary": "301",
                "surfaceflinger": "500",
            }
        )
        result = self.run_script()
        lines = self.diagnostics(result)
        stdout = "\n".join(lines)
        self.assertIn("|PROCESS|system_server absent", stdout)
        self.assertIn("|BACKTRACE|system_server skipped absent", stdout)
        self.assertNotIn("BACKTRACE_LINE", stdout)
        self.assertNotIn("debuggerd ", self.fixture.calls())

    def test_invalid_pid_is_reported_without_a_backtrace(self):
        for raw_pid in ("not-a-pid", "0", "4242x"):
            with self.subTest(raw_pid=raw_pid):
                self.fixture.stub_pidof(
                    {
                        "system_server": raw_pid,
                        "zygote": "300",
                        "zygote_secondary": "301",
                        "surfaceflinger": "500",
                    }
                )
                self.fixture.log.write_text("")
                result = self.run_script()
                lines = self.diagnostics(result)
                self.assertIn(
                    "|PROCESS|system_server invalid_pid={0}".format(raw_pid),
                    "\n".join(lines),
                )
                self.assertNotIn("debuggerd ", self.fixture.calls())

    def test_missing_optional_commands_are_reported_not_fatal(self):
        self.fixture.remove("pidof")
        self.fixture.remove("ps")
        self.fixture.remove("service")
        result = self.run_script()
        lines = self.diagnostics(result)
        stdout = "\n".join(lines)
        self.assertIn("|TOOL|pidof=unavailable", stdout)
        self.assertIn("|TOOL|ps=unavailable", stdout)
        self.assertIn("|TOOL|service=unavailable", stdout)
        self.assertIn("|PROCESS|system_server tool=pidof_unavailable", stdout)
        self.assertIn("|BINDER_SERVICE|activity tool=service_unavailable", stdout)
        self.assertIn("|BACKTRACE|system_server skipped absent", stdout)

    def test_backtrace_needs_both_timeout_and_debuggerd(self):
        self.fixture.remove("timeout")
        result = self.run_script()
        stdout = "\n".join(self.diagnostics(result))
        self.assertIn(
            "|BACKTRACE|system_server pid=4242 skipped timeout_unavailable", stdout
        )
        self.assertNotIn("debuggerd ", self.fixture.calls())

        self.fixture.stub_timeout()
        self.fixture.remove("debuggerd")
        result = self.run_script()
        stdout = "\n".join(self.diagnostics(result))
        self.assertIn(
            "|BACKTRACE|system_server pid=4242 skipped debuggerd_unavailable", stdout
        )

    def test_debuggerd_runs_once_under_the_android_timeout(self):
        self.fixture.log.write_text("")
        self.diagnostics(self.run_script())
        calls = self.fixture.calls()
        self.assertIn("timeout 20 debuggerd -b 4242", calls)
        self.assertEqual(
            sum(1 for line in calls.splitlines() if line.startswith("debuggerd ")),
            1,
        )
        self.assertIn("timeout 5 service check activity", calls)

    def test_backtrace_output_is_bounded(self):
        self.fixture.stub_debuggerd(line_count=500)
        result = self.run_script()
        lines = self.diagnostics(result)
        backtrace_lines = [line for line in lines if "|BACKTRACE_LINE|" in line]
        self.assertEqual(len(backtrace_lines), 200)
        self.assertIn(
            "|BACKTRACE|system_server pid=4242 rc=0 lines=200 limit=200 truncated=1",
            "\n".join(lines),
        )

    def test_timeout_expiry_is_recorded_as_an_observation(self):
        self.fixture.stub(
            "timeout",
            'printf \'timeout %s\\n\' "$*" >> "$ASTER_CALL_LOG"\nexit 124\n',
        )
        result = self.run_script()
        stdout = "\n".join(self.diagnostics(result))
        self.assertIn("|BACKTRACE|system_server pid=4242 rc=124 lines=0", stdout)
        self.assertIn("|BINDER_SERVICE|activity rc=124", stdout)

    def test_invalid_nonce_is_refused(self):
        self.fixture.stub_getprop({"ro.boot.aster_nonce": "NOTHEX"})
        result = self.run_script()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("invalid ro.boot.aster_nonce", result.stderr)

        self.fixture.stub_getprop({"ro.boot.aster_nonce": "deadbeef"})
        result = self.run_script()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")

    def test_missing_getprop_is_refused(self):
        self.fixture.remove("getprop")
        result = self.run_script()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("getprop unavailable", result.stderr)

    def test_proc_root_override_is_the_only_process_source(self):
        self.fixture.add_process(7777, "system_server", rss="101010")
        self.fixture.stub_pidof({"system_server": "7777"})
        result = self.run_script()
        stdout = "\n".join(self.diagnostics(result))
        self.assertIn("|PROCESS|system_server pid=7777", stdout)
        self.assertIn("VmRSS=101010 kB", stdout)
        self.assertNotIn("VmRSS=352960 kB", stdout)


if __name__ == "__main__":
    unittest.main()

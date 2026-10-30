import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "runner", Path(__file__).with_name("run_linux_device.py")
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ConsoleCollectorTests(unittest.TestCase):
    def test_summary_and_reaped_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = 'import os,signal; print(os.getpid(), flush=True); print("[linux] summary PASS", flush=True); signal.pause()'
            output = runner.collect([sys.executable, "-c", code], root / "console", 3)
            self.assertIn(b"[linux] summary PASS", output)
            pid = int(output.splitlines()[0])
            with self.assertRaises(ProcessLookupError):
                import os

                os.kill(pid, 0)

    def test_timeout_fails_and_reaps_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = "import os,signal; print(os.getpid(), flush=True); signal.pause()"
            with self.assertRaises(TimeoutError):
                runner.collect([sys.executable, "-c", code], root / "console", 0.5)
            pid = int((root / "console").read_text().strip())
            with self.assertRaises(ProcessLookupError):
                import os

                os.kill(pid, 0)

    def test_closed_console_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                runner.collect(
                    [sys.executable, "-c", 'print("no summary")'],
                    Path(directory) / "console",
                    3,
                )


if __name__ == "__main__":
    unittest.main()

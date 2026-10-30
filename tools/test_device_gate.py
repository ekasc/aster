#!/usr/bin/env python3
"""Exercise the device runner without building or contacting a phone."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class DeviceGateTests(unittest.TestCase):
    def test_launch_results(self):
        runner = Path(__file__).with_name("run_on_device.sh")
        with tempfile.TemporaryDirectory(prefix="aster-gate-") as directory:
            root = Path(directory)
            tools = root / "tools"
            tools.mkdir()
            shutil.copyfile(runner, tools / runner.name)
            commands = {
                "build_guest.sh": "exit 0",
                "xcodebuild": "exit 0",
                "xcrun": "exit 0",
                "timeout": 'printf "%s\\n" "$TEST_OUTPUT"; exit "$TEST_STATUS"',
            }
            for name, body in commands.items():
                command = tools / name
                command.write_text("#!/bin/bash\n" + body + "\n")
                command.chmod(0o755)
            cases = [
                ("", 124, 1),
                ("[gate] summary 5/6", 124, 1),
                ("[gate] summary 6/60", 0, 1),
                ("[gate] summary 6/6", 124, 0),
                ("[gate] summary 6/6", 0, 0),
                ("[gate] summary 6/6\r", 124, 0),
                ("launch failed", 1, 1),
                ("BSErrorCodeDescription = Locked", 1, 2),
            ]
            for output, status, expected in cases:
                with self.subTest(output=output, status=status):
                    env = dict(
                        os.environ,
                        PATH=f"{tools}:{os.environ['PATH']}",
                        TEST_OUTPUT=output,
                        TEST_STATUS=str(status),
                    )
                    result = subprocess.run(
                        ["bash", str(tools / runner.name)],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    self.assertEqual(
                        result.returncode, expected, result.stdout + result.stderr
                    )


if __name__ == "__main__":
    unittest.main()

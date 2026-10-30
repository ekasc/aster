import os
import json
import shlex
from pathlib import Path
import sys
import tempfile
import unittest

from tools.android import serial_control


class SerialControlTests(unittest.TestCase):
    def test_command_uses_a_quoted_script_without_a_tempfile(self):
        script = b"echo '$not_expanded'\n"
        command = serial_control.guest_command(script)
        self.assertEqual(
            shlex.split(command.decode()),
            ["su", "0", "/system/bin/sh", "-c", script.decode()],
        )
        self.assertNotIn(b"<<", command)

    def test_only_console_prompt_triggers_script(self):
        self.assertTrue(serial_control.PROMPT.search(b"console:/ # "))
        self.assertFalse(
            serial_control.PROMPT.search(b"[ 1.25] init: starting console")
        )

    def test_exit_reaps_runner_without_fabricating_a_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = serial_control.drive(
                root / "config",
                root / "absent.sock",
                root / "out",
                timeout=2,
                runner=[sys.executable, "-c", 'print("incomplete")'],
            )
            self.assertEqual(result, 1)
            self.assertNotIn("ASTER-BOOT", (root / "out/runner.log").read_text())
            self.assertIn(
                '"script_submitted": false',
                (root / "out/control-result.json").read_text(),
            )

    def test_deadline_reaps_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = serial_control.drive(
                root / "config",
                root / "absent.sock",
                root / "out",
                timeout=0.2,
                runner=[sys.executable, "-c", "import signal; signal.pause()"],
            )
            self.assertEqual(result, 1)
            self.assertIn(
                '"runner_reaped": true', (root / "out/control-result.json").read_text()
            )
            record = json.loads((root / "out/control-result.json").read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(record["runner_pid"], 0)

    def test_script_reaches_a_private_console(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            socket_path = root / "console.sock"
            received = root / "received"
            code = """import socket,sys,re
from pathlib import Path
server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
server.bind(sys.argv[1]); server.listen(1)
connection,_=server.accept()
connection.sendall(b'console:/ # ')
data=b''
while True:
    block=connection.recv(4096)
    if not block: break
    data+=block
    if len(data) >= int(sys.argv[3]): break
Path(sys.argv[2]).write_bytes(data)
connection.close(); server.close()
"""
            result = serial_control.drive(
                root / "config",
                socket_path,
                root / "out",
                timeout=3,
                runner=[
                    sys.executable,
                    "-c",
                    code,
                    str(socket_path),
                    str(received),
                    str(
                        len(
                            serial_control.guest_command(
                                (
                                    serial_control.ROOT / "tools/android/guest_gate.sh"
                                ).read_bytes()
                            )
                        )
                    ),
                ],
            )
            self.assertEqual(result, 0)
            self.assertFalse(socket_path.exists())
            self.assertIn(b"getprop sys.boot_completed", received.read_bytes())


if __name__ == "__main__":
    unittest.main()

import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.android import archive_run


class ArchiveTests(unittest.TestCase):
    def test_completed_failure_is_preserved_without_turning_it_into_a_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attempt = root / "attempt"
            run = attempt / "runs/test"
            run.mkdir(parents=True)
            nonce = "a" * 32
            result = {
                "nonce": nonce,
                "measurement": {
                    "timed_out": True,
                    "exit_code": 0,
                    "term_signal": None,
                    "runner_stop_requested": False,
                },
            }
            (run / "result.json").write_text(json.dumps(result))
            (run / "command.json").write_text(json.dumps({"nonce": nonce}))
            raw = b"[ 0.0] Booting Linux on physical CPU\r\n"
            (run / "serial.log").write_bytes(raw)
            output = root / "evidence"
            analysis = archive_run.archive(attempt, output)
            self.assertFalse(analysis["framework_verified"])
            self.assertEqual(
                gzip.decompress((output / "serial.log.gz").read_bytes()), raw
            )
            hashes = json.loads((output / "raw-log-sha256.json").read_text())
            self.assertEqual(hashes["serial.log"], hashlib.sha256(raw).hexdigest())
            with self.assertRaises(FileExistsError):
                archive_run.archive(attempt, output)

    def test_incomplete_attempt_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                archive_run.archive(root, root / "evidence")
            self.assertFalse((root / "evidence").exists())


if __name__ == "__main__":
    unittest.main()

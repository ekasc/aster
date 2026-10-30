import json
import gzip
from pathlib import Path
import tempfile
import unittest

from tools.android import report


class ReportTests(unittest.TestCase):
    def test_partial_stages_have_only_observed_guest_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nonce = "a" * 32
            measurement = {
                "timed_out": True,
                "exit_code": 0,
                "term_signal": None,
                "runner_stop_requested": False,
                "wall_seconds": 20.5,
            }
            (root / "result.json").write_text(
                json.dumps({"nonce": nonce, "measurement": measurement})
            )
            (root / "command.json").write_text(json.dumps({"nonce": nonce}))
            (root / "serial.log").write_text(
                "[ 0.000000] Booting Linux on physical CPU\n[ 5.25][ T1] init: init first stage started!\n"
            )
            result = report.report(root)
            self.assertFalse(result["framework_verified"])
            self.assertEqual(
                result["stages"]["first_stage_init"]["guest_kernel_uptime_seconds"],
                5.25,
            )
            self.assertEqual(result["measurement"]["wall_seconds"], 20.5)
            (root / "serial.log.gz").write_bytes(
                gzip.compress((root / "serial.log").read_bytes(), mtime=0)
            )
            (root / "serial.log").unlink()
            self.assertEqual(report.report(root), result)
            (root / "command.json").write_text(json.dumps({"nonce": "b" * 32}))
            with self.assertRaises(ValueError):
                report.report(root)


if __name__ == "__main__":
    unittest.main()

import gzip
import json
from pathlib import Path
import tempfile
import unittest

from tools.android import triage

NONCE = "a" * 32


class TriageTests(unittest.TestCase):
    def test_reports_last_lookup_and_dominant_wait_channel(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            serial = "\n".join(
                [
                    f"[ 1.0][ T1] servicemanager: Caller(pid=1874,uid=1000,sid=u:r:system_server:s0) Found android.hardware.power.IPower/default in device VINTF manifest.",
                    f"[ 2.0][ T1] servicemanager: Caller(pid=1874,uid=1000,sid=u:r:system_server:s0) Found android.hardware.light.ILights/default in device VINTF manifest.",
                    f"ASTER-DIAG|{NONCE}|1|t150|uptime=1500.0 boot_completed= system_server=1874",
                    f"ASTER-DIAG|{NONCE}|1|t150|service=activity rc=1 not found",
                    f"ASTER-DIAG|{NONCE}|1|t150|wchan count=40 where=binder_ioctl",
                    f"ASTER-DIAG|{NONCE}|1|t150|wchan count=3 where=run",
                    f"ASTER-DIAG|{NONCE}|1|t150|snapshot_end",
                ]
            )
            (root / "serial.log").write_text(serial)
            (root / "android.log.gz").write_bytes(
                gzip.compress(
                    b"F DEBUG   : Abort message: 'Could not connect to sensors control: No such device'\n",
                    mtime=0,
                )
            )
            result = triage.triage(root)
            self.assertEqual(result["system_server_pid"], "1874")
            self.assertEqual(
                result["last_binder_lookup"],
                "Found android.hardware.light.ILights/default in device VINTF manifest.",
            )
            self.assertEqual(len(result["guest_snapshots"]), 1)
            self.assertEqual(
                result["guest_snapshots"][0]["wchan"][0], (40, "binder_ioctl")
            )
            self.assertIn("sensors", json.dumps(result["process_aborts"]))

    def test_missing_logs_produce_empty_observations(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = triage.triage(Path(temporary))
            self.assertIsNone(result["system_server_pid"])
            self.assertEqual(result["guest_snapshots"], [])


if __name__ == "__main__":
    unittest.main()

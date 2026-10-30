import json
from pathlib import Path
import tempfile
import unittest

from tools.android import preflight


class PreflightTests(unittest.TestCase):
    def fake_qemu(self, root, device_text, header=""):
        qemu = root / "qemu-system-aarch64"
        qemu.write_text("#!/bin/sh\ncat <<'EOF'\n" + device_text + "\nEOF\n")
        qemu.chmod(0o755)
        (root / "config-host.h").write_text(header)
        return qemu

    def test_missing_vsock_marks_bundle_hals_unserviceable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            devices = "\n".join(
                f'name "{name}", bus pci' for name in preflight.REQUIRED_DEVICES
            )
            qemu = self.fake_qemu(
                root, devices, "#undef CONFIG_VHOST\n#undef CONFIG_AF_VSOCK\n"
            )
            findings = preflight.check(qemu)
            self.assertFalse(findings["ok"])
            self.assertIn("android.hardware.sensors", findings["unserviceable_hals"])
            self.assertEqual(findings["missing_devices"], [])

    def test_vsock_device_makes_the_configuration_serviceable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            devices = "\n".join(
                f'name "{name}", bus pci' for name in preflight.REQUIRED_DEVICES
            )
            devices += '\nname "vhost-vsock-pci", bus PCI'
            qemu = self.fake_qemu(root, devices, "#define CONFIG_VHOST_VSOCK 1\n")
            findings = preflight.check(qemu)
            self.assertTrue(findings["ok"])
            self.assertTrue(findings["vsock_device_present"])

    def test_missing_required_device_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            qemu = self.fake_qemu(root, 'name "virtio-gpu-pci", bus PCI')
            findings = preflight.check(qemu, need_host_hal_transport=False)
            self.assertFalse(findings["ok"])
            self.assertIn("virtconsole", findings["missing_devices"])

    def test_unexecutable_qemu_is_an_error_not_a_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            findings = preflight.check(Path(temporary) / "absent")
            self.assertFalse(findings["ok"])
            self.assertIn("cannot execute QEMU", findings["error"])


if __name__ == "__main__":
    unittest.main()

import unittest

import probe


class ProbeTests(unittest.TestCase):
    def test_newc_roundtrip_and_padding(self):
        entries = [
            ("bin", 0o40755, b"", 0, 0),
            ("bin/tool", 0o100755, b"abcde", 0, 0),
            ("dev/console", 0o20600, b"", 5, 1),
            ("TRAILER!!!", 0, b"", 0, 0),
        ]
        data = probe.newc(entries)
        self.assertEqual(
            dict(probe.read_cpio(data)),
            {"bin": b"", "bin/tool": b"abcde", "dev/console": b""},
        )
        self.assertEqual(data, probe.newc(entries))

    def test_reject_other_archive_format(self):
        with self.assertRaises(ValueError):
            list(probe.read_cpio(b"not cpio" * 20))

    def test_pass_requires_every_stage_and_clean_exit(self):
        output = "\n".join(
            (
                "ASTER_BINDERFS_DEVICES_OK",
                "ASTER_SELINUXFS_OK",
                "ASTER_VIRTIO_BLOCK_READ_OK",
                "ASTER_VIRTIO_RNG_AND_INPUT_ENUMERATED",
                "ASTER_KERNEL_PROBE_PASS",
            )
        )
        self.assertTrue(probe.probe_passed(output, 0, False))
        self.assertFalse(probe.probe_passed(output, 0, True))
        self.assertFalse(probe.probe_passed(output, 1, False))
        self.assertFalse(probe.probe_passed("+ echo ASTER_KERNEL_PROBE_PASS", 0, False))
        self.assertFalse(
            probe.probe_passed(output.replace("ASTER_SELINUXFS_OK", ""), 0, False)
        )
        self.assertFalse(
            probe.probe_passed(output + "\nASTER_KERNEL_PROBE_FAIL", 0, False)
        )


if __name__ == "__main__":
    unittest.main()

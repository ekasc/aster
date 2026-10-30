import struct
import contextlib
import io
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tools.android import boot_config


class BootConfigurationTests(unittest.TestCase):
    def test_generated_config_preserves_the_expected_disk_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ["kernel", "ramdisk", "disk"]:
                (root / name).write_bytes(b"fixture")
            (root / "bootconfig").write_text("androidboot.hardware=cutf_cvm\n")
            expected = "a" * 64
            (root / "prepared.json").write_text(
                json.dumps(
                    {
                        "artifacts": {
                            "android.img": {
                                "size": 7,
                                "sha256": expected,
                            }
                        }
                    }
                )
            )
            argv = [
                "boot_config.py",
                "--kernel",
                str(root / "kernel"),
                "--ramdisk",
                str(root / "ramdisk"),
                "--disk",
                str(root / "disk"),
                "--base-bootconfig",
                str(root / "bootconfig"),
                "--output",
                str(root / "out"),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                boot_config.main()
            config = json.loads((root / "out/config.json").read_text())
            self.assertEqual(config["hashes"][str((root / "disk").resolve())], expected)
            self.assertIn(str((root / "disk").resolve()), config["input_paths"])
            properties = json.loads((root / "out/boot-properties.json").read_text())
            self.assertEqual(properties["androidboot.vsock_lights_port"], "7000")

    def test_bootconfig_trailer_size_and_checksum(self):
        data = boot_config.append_bootconfig(
            b"ramdisk", {"androidboot.hardware": "cutf_cvm"}
        )
        self.assertTrue(data.endswith(boot_config.MAGIC))
        size, checksum = struct.unpack("<II", data[-20:-12])
        payload = data[-20 - size : -20]
        self.assertEqual(len(payload), size)
        self.assertEqual(sum(payload), checksum)
        self.assertTrue(payload.endswith(b"\x00"))

    def test_reject_nested_trailer_and_invalid_values(self):
        with self.assertRaises(ValueError):
            boot_config.append_bootconfig(b"old" + boot_config.MAGIC, {})
        with self.assertRaises(ValueError):
            boot_config.append_bootconfig(b"ramdisk", {"androidboot.bad": "one\ntwo"})

    def test_parse_exact_properties_and_reject_duplicate(self):
        self.assertEqual(
            boot_config.parse_properties('androidboot.hardware = "cutf_cvm"\n'),
            {"androidboot.hardware": "cutf_cvm"},
        )
        with self.assertRaises(ValueError):
            boot_config.parse_properties('a = "b"\na = "c"')

    def test_bare_properties_and_replacing_a_verified_trailer(self):
        properties = boot_config.parse_properties("androidboot.hardware=cutf_cvm\n")
        first = boot_config.append_bootconfig(b"ramdisk", properties)
        self.assertEqual(boot_config.strip_bootconfig(first), b"ramdisk")
        damaged = bytearray(first)
        damaged[-21] ^= 1
        with self.assertRaises(ValueError):
            boot_config.strip_bootconfig(bytes(damaged))

    def test_board_and_port_mapping_are_explicit(self):
        args = boot_config.arguments(
            Path("/kernel"), Path("/initrd"), Path("/disk"), Path("/console"), 1536
        )
        self.assertIn("virt,gic-version=3", args)
        self.assertIn("tcg,thread=single,tb-size=32", args)
        self.assertEqual(sum(x.startswith("virtconsole,") for x in args), 17)
        self.assertIn("socket,id=hvc1,path=/console,server=on,wait=off", args)
        self.assertIn("file,id=hvc2,path={evidence_dir}/android.log", args)
        self.assertNotIn("hostfwd", " ".join(args))


if __name__ == "__main__":
    unittest.main()

import gzip
import io
import tarfile
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.android import linux_guest


def make_tar(path, entries):
    with tarfile.open(path, "w") as archive:
        for name, data, mode in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            archive.addfile(info, io.BytesIO(data))


class LinuxGuestTests(unittest.TestCase):
    def test_newc_roundtrip_preserves_mode_and_bytes(self):
        entries = [
            ("a", 0o040755, b""),
            ("a/b", 0o100755, b"hello"),
            ("TRAILER!!!", 0, b""),
        ]
        parsed = list(linux_guest.read_cpio(linux_guest.newc(entries)))
        self.assertEqual(parsed, [("a", 0o040755, b""), ("a/b", 0o100755, b"hello")])

    def test_unsafe_archive_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.tar"
            make_tar(path, [("../escape", b"x", 0o644)])
            with tarfile.open(path) as archive:
                with self.assertRaises(ValueError):
                    list(linux_guest.safe_members(archive))

    def test_initramfs_roots_minirootfs_at_slash_with_exec_bits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tar = root / "mini.tar"
            make_tar(
                tar,
                [
                    ("etc/alpine-release", b"3.20.3\n", 0o644),
                    ("bin/busybox", b"\x7fELF", 0o755),
                ],
            )
            init = root / "init.sh"
            init.write_text("#!/bin/sh\necho hi\n")
            data = gzip.decompress(linux_guest.build_initramfs(tar, {}, init))
            entries = {
                name: (mode, body) for name, mode, body in linux_guest.read_cpio(data)
            }
            self.assertEqual(entries["etc/alpine-release"][1], b"3.20.3\n")
            self.assertEqual(entries["bin/busybox"][0], 0o100755)
            self.assertEqual(entries["init"][0], 0o100755)

    def test_module_archive_selection_reads_lz4_cpio(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cpio = linux_guest.newc(
                [
                    ("lib/modules/virtio_blk.ko", 0o100644, b"BLK"),
                ("lib/modules/virtio_pci.ko", 0o100644, b"PCI"),
                    ("lib/modules/virtio_mmio.ko", 0o100644, b"MMIO"),
                    ("lib/modules/virtio_rng.ko", 0o100644, b"RNG"),
                    ("lib/modules/virtio_input.ko", 0o100644, b"IN"),
                    ("lib/modules/virtio_console.ko", 0o100644, b"CON"),
                    ("lib/modules/virtio-gpu.ko", 0o100644, b"GPU"),
                    ("TRAILER!!!", 0, b""),
                ]
            )
            packed = root / "vendor_ramdisk.lz4"
            subprocess.run(
                ["lz4", "-l", "-c"], input=cpio, stdout=packed.open("wb"), check=True
            )
            modules = linux_guest.module_archive_entries(packed)
            self.assertEqual(modules["lib/modules/virtio_blk.ko"], b"BLK")
            self.assertEqual(modules["lib/modules/virtio_pci.ko"], b"PCI")

    def test_initramfs_creates_module_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tar = root / "mini.tar"
            make_tar(tar, [("etc/alpine-release", b"3.20.3\n", 0o644)])
            init = root / "init.sh"
            init.write_text("#!/bin/sh\n")
            data = gzip.decompress(linux_guest.build_initramfs(
                tar, {"lib/modules/virtio_blk.ko": b"BLK"}, init))
            names = {name for name, _mode, _body in linux_guest.read_cpio(data)}
            self.assertIn("lib/modules", names)
            self.assertIn("lib/modules/virtio_blk.ko", names)

    def test_module_archive_reports_what_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packed = root / "vendor_ramdisk.lz4"
            cpio = linux_guest.newc(
                [
                    ("lib/modules/virtio_blk.ko", 0o100644, b"BLK"),
                    ("TRAILER!!!", 0, b""),
                ]
            )
            with packed.open("wb") as sink:
                subprocess.run(["lz4", "-l", "-c"], input=cpio, stdout=sink, check=True)
            with self.assertRaises(ValueError) as caught:
                linux_guest.module_archive_entries(packed)
            self.assertIn("virtio_pci.ko", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

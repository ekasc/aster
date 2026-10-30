import hashlib
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class QemuPatchTests(unittest.TestCase):
    def test_upstream_patches_apply_and_are_idempotent(self):
        files = [
            "VERSION",
            "include/qemu/osdep.h",
            "block/file-posix.c",
            "tcg/region.c",
            "accel/tcg/tcg-accel-ops-rr.c",
            "system/main.c",
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            with tarfile.open(ROOT / "build/qemu/qemu-11.1.3.tar.xz") as archive:
                for name in files:
                    target = source / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(
                        archive.extractfile("qemu-11.1.3/" + name).read()
                    )
            command = [
                sys.executable,
                str(ROOT / "tools/qemu/patch_ios.py"),
                str(source),
            ]
            subprocess.run(command, check=True)
            hashes = {
                name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                for name in files
            }
            subprocess.run(command, check=True)
            self.assertEqual(
                hashes,
                {
                    name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                    for name in files
                },
            )
            (source / "VERSION").write_text("unknown")
            self.assertNotEqual(
                subprocess.run(command, capture_output=True).returncode, 0
            )


if __name__ == "__main__":
    unittest.main()

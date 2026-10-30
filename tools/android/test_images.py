#!/usr/bin/env python3
"""Unit tests for the artifact manifest and preparation primitives."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import images


class UserdataFilesystemTests(unittest.TestCase):
    def test_fstab_and_userdata_filesystem_must_agree(self):
        prefix = bytearray(4096)
        with self.assertRaises(images.ImageError):
            images.check_userdata(prefix)
        struct.pack_into("<I", prefix, 1024, 0xF2F52010)
        self.assertEqual(images.check_userdata(prefix)["filesystem"], "f2fs")


def make_zip(path: Path, entries: list[tuple[str, bytes, int]]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            archive.writestr(info, data)


def bundle_for_zip(path: Path, required: list[str] | None = None) -> dict:
    members = []
    names = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            members.append(
                {
                    "name": info.filename,
                    "size": info.file_size,
                    "crc32": f"{info.CRC:08x}",
                }
            )
            names.append(info.filename)
    return {
        "id": "test-bundle",
        "members": members,
        "required_members": required if required is not None else names,
    }


def mini_bundle() -> dict:
    return {
        "id": "test-bundle",
        "branch": "test",
        "target": "test",
        "build_id": "1",
        "build_fingerprint": "test",
        "android_release": "17",
        "android_sdk": 37,
        "kernel": {"version": "test"},
        "archive": {
            "artifact": "test.zip",
            "page_url": "https://ci.android.com/test",
            "download_resolver": "ci_artifact_viewer_jsvariables_v1",
            "storage_path": "/android-build/test",
            "size": 1,
            "etag": "0" * 32,
            "last_modified": "test",
            "md5_base64": "AAAAAAAAAAAAAAAAAAAAAA==",
            "crc32c_base64": "AAAAAA==",
            "sha256": "0" * 64,
        },
        "members": [
            {
                "name": "boot.img",
                "size": 4096,
                "crc32": "00000000",
            }
        ],
        "required_members": ["boot.img"],
        "partition_plan": [{"label": "boot", "member": "boot.img"}],
        "bootconfig": {
            "fstab_suffix": "cf.f2fs.hctr2",
            "hardware": "cutf_cvm",
            "slot_suffix": "_a",
            "boot_devices": "4010000000.pcie",
            "settings": {"androidboot.force_normal_boot": "1"},
        },
        "qemu": {"args_template": ["-append", "{cmdline}"]},
        "license": {},
        "budget": {"estimated_prepared_bytes": 1, "estimated_peak_bytes": 1},
        "blockers": [],
    }


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = images.load_manifest()

    def test_pinned_bundle_is_coherent(self) -> None:
        bundle = images.bundle_by_id(self.manifest, "cf-arm64-only-phone-16644040")
        self.assertEqual(bundle["build_id"], "16644040")
        self.assertEqual(bundle["archive"]["size"], 1101174958)
        self.assertTrue(images.is_hex_sha256(bundle["archive"]["sha256"]))
        self.assertIn("cf.f2fs.hctr2", bundle["bootconfig"]["fstab_suffix"])
        self.assertIn("{cmdline}", " ".join(bundle["qemu"]["args_template"]))

    def test_duplicate_id_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["bundles"].append(copy.deepcopy(manifest["bundles"][0]))
        with self.assertRaises(images.ImageError):
            images.validate_manifest(manifest)

    def test_bad_sha256_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["bundles"][0]["archive"]["sha256"] = "z" * 64
        with self.assertRaises(images.ImageError):
            images.validate_manifest(manifest)

    def test_bad_md5_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["bundles"][0]["archive"]["md5_base64"] = "AAAA"
        with self.assertRaises(images.ImageError):
            images.validate_manifest(manifest)

    def test_missing_required_member_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["bundles"][0]["members"] = [
            item
            for item in manifest["bundles"][0]["members"]
            if item["name"] != "super.img"
        ]
        with self.assertRaises(images.ImageError):
            images.validate_manifest(manifest)

    def test_args_without_nonce_are_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["bundles"][0]["qemu"]["args_template"] = ["-M", "virt"]
        with self.assertRaises(images.ImageError):
            images.validate_manifest(manifest)

    def test_free_floor_below_three_gib_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["budgets"]["free_floor_bytes"] = 1
        with self.assertRaises(images.ImageError):
            images.validate_manifest(manifest)


class SafeArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="images-test-", dir=images.WORK.parent
        )
        self.root = Path(self.tmp.name)
        self.zip = self.root / "test.zip"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_archive_hash_mismatch_is_detected(self) -> None:
        make_zip(self.zip, [("boot.img", b"boot" * 1024, 0o100644)])
        data = self.zip.read_bytes()
        bundle = bundle_for_zip(self.zip)
        bundle["archive"] = {
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "md5_base64": images.base64.b64encode(
                images.hashlib.md5(data).digest()
            ).decode(),
        }
        self.assertTrue(images.archive_sha256_matches(self.zip, bundle))
        self.zip.write_bytes(data + b"x")
        self.assertFalse(images.archive_sha256_matches(self.zip, bundle))

    def test_valid_member_extracts_and_checks_crc(self) -> None:
        make_zip(self.zip, [("boot.img", b"boot" * 1024, 0o100644)])
        bundle = bundle_for_zip(self.zip)
        members = images.inspect_archive(self.zip, bundle)
        images.extract_members(self.zip, members, ["boot.img"], self.root / "out")
        self.assertEqual((self.root / "out" / "boot.img").read_bytes(), b"boot" * 1024)

    def test_parent_traversal_is_rejected(self) -> None:
        make_zip(self.zip, [("../evil", b"x", 0o100644)])
        with self.assertRaises(images.ImageError):
            images.inspect_archive(self.zip, bundle_for_zip(self.zip))

    def test_absolute_member_is_rejected(self) -> None:
        make_zip(self.zip, [("/etc/passwd", b"x", 0o100644)])
        with self.assertRaises(images.ImageError):
            images.inspect_archive(self.zip, bundle_for_zip(self.zip))

    def test_backslash_member_is_rejected(self) -> None:
        make_zip(self.zip, [("a\\b", b"x", 0o100644)])
        with self.assertRaises(images.ImageError):
            images.inspect_archive(self.zip, bundle_for_zip(self.zip))

    def test_symlink_member_is_rejected(self) -> None:
        make_zip(self.zip, [("link", b"/etc/passwd", 0o120777)])
        with self.assertRaises(images.ImageError):
            images.inspect_archive(self.zip, bundle_for_zip(self.zip))

    def test_duplicate_member_is_rejected(self) -> None:
        with zipfile.ZipFile(self.zip, "w") as archive:
            archive.writestr("boot.img", b"a")
            archive.writestr("boot.img", b"b")
        with self.assertRaises(images.ImageError):
            images.inspect_archive(self.zip, bundle_for_zip(self.zip))

    def test_crc_mismatch_is_rejected(self) -> None:
        make_zip(self.zip, [("boot.img", b"boot", 0o100644)])
        bundle = bundle_for_zip(self.zip)
        bundle["members"][0]["crc32"] = "00000000"
        with self.assertRaises(images.ImageError):
            images.inspect_archive(self.zip, bundle)

    def test_size_mismatch_is_rejected(self) -> None:
        make_zip(self.zip, [("boot.img", b"boot", 0o100644)])
        bundle = bundle_for_zip(self.zip)
        bundle["members"][0]["size"] = 999
        with self.assertRaises(images.ImageError):
            images.inspect_archive(self.zip, bundle)

    def test_undeclared_member_is_rejected(self) -> None:
        make_zip(self.zip, [("boot.img", b"a", 0o100644), ("extra", b"b", 0o100644)])
        bundle = bundle_for_zip(self.zip)
        bundle["members"] = bundle["members"][:1]
        with self.assertRaises(images.ImageError):
            images.inspect_archive(self.zip, bundle)

    def test_incomplete_required_set_is_rejected(self) -> None:
        make_zip(self.zip, [("boot.img", b"a", 0o100644)])
        bundle = bundle_for_zip(self.zip, required=["boot.img", "vendor_boot.img"])
        with self.assertRaises(images.ImageError) as raised:
            images.inspect_archive(self.zip, bundle)
        self.assertIn("incomplete", str(raised.exception))

    def test_refuses_to_overwrite_existing_target(self) -> None:
        make_zip(self.zip, [("boot.img", b"boot", 0o100644)])
        bundle = bundle_for_zip(self.zip)
        members = images.inspect_archive(self.zip, bundle)
        out = self.root / "out"
        out.mkdir()
        (out / "boot.img").write_bytes(b"keep")
        with self.assertRaises(images.ImageError):
            images.extract_members(self.zip, members, ["boot.img"], out)
        self.assertEqual((out / "boot.img").read_bytes(), b"keep")


class SparseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="images-sparse-", dir=images.WORK.parent
        )
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def sparse_bytes(
        chunks: list[tuple[int, int, bytes]], block_size: int = 4096
    ) -> bytes:
        total_blocks = sum(blocks for _kind, blocks, _payload in chunks)
        header = struct.pack(
            "<IHHHHIIII",
            images.SPARSE_MAGIC,
            1,
            0,
            28,
            12,
            block_size,
            total_blocks,
            len(chunks),
            0,
        )
        output = bytearray(header)
        for kind, blocks, payload in chunks:
            output.extend(struct.pack("<HHII", kind, 0, blocks, 12 + len(payload)))
            output.extend(payload)
        return bytes(output)

    def test_expand_raw_fill_skip_and_crc(self) -> None:
        raw = bytes((index * 7) & 0xFF for index in range(4096))
        fill = struct.pack("<I", 0x11223344)
        sparse = self.sparse_bytes(
            [
                (images.SPARSE_RAW, 1, raw),
                (images.SPARSE_FILL, 1, fill),
                (images.SPARSE_SKIP, 1, b""),
                (images.SPARSE_CRC32, 0, b"\0\0\0\0"),
            ]
        )
        source = self.root / "sparse.img"
        source.write_bytes(sparse)
        target = self.root / "raw.img"
        with target.open("wb") as handle:
            handle.truncate(3 * 4096)
        with source.open("rb") as stream:
            images.expand_sparse(stream, target, 0, 3 * 4096, 0, self.root)
        data = target.read_bytes()
        self.assertEqual(data[:4096], raw)
        self.assertEqual(data[4096:8192], fill * 1024)
        self.assertEqual(data[8192:], b"\0" * 4096)

    def test_expanded_size_mismatch_is_rejected(self) -> None:
        sparse = self.sparse_bytes([(images.SPARSE_RAW, 1, b"x" * 4096)])
        source = self.root / "sparse.img"
        source.write_bytes(sparse)
        target = self.root / "raw.img"
        with target.open("wb") as handle:
            handle.truncate(4096)
        with source.open("rb") as stream:
            with self.assertRaises(images.ImageError):
                images.expand_sparse(stream, target, 0, 8192, 0, self.root)

    def test_unknown_chunk_is_rejected(self) -> None:
        sparse = self.sparse_bytes([(0xCAFE, 1, b"x" * 4096)])
        source = self.root / "sparse.img"
        source.write_bytes(sparse)
        target = self.root / "raw.img"
        with target.open("wb") as handle:
            handle.truncate(4096)
        with source.open("rb") as stream:
            with self.assertRaises(images.ImageError):
                images.expand_sparse(stream, target, 0, 4096, 0, self.root)

    def test_prefix_expansion_stops_at_limit(self) -> None:
        sparse = self.sparse_bytes(
            [(images.SPARSE_RAW, 1, b"a" * 4096), (images.SPARSE_SKIP, 1, b"")]
        )
        source = self.root / "sparse.img"
        source.write_bytes(sparse)
        with source.open("rb") as stream:
            prefix = images.sparse_prefix(stream, 5000)
        self.assertEqual(len(prefix), 5000)
        self.assertEqual(prefix[:4096], b"a" * 4096)
        self.assertEqual(prefix[4096:], b"\0" * 904)


class FormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="images-format-", dir=images.WORK.parent
        )
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_boot_image_and_init_boot(self) -> None:
        header = bytearray(4096)
        header[:8] = images.BOOT_MAGIC
        struct.pack_into("<II", header, 8, 4096, 0)
        struct.pack_into("<I", header, 20, 1584)
        struct.pack_into("<I", header, 40, 4)
        kernel = bytearray(4096)
        kernel[56:60] = images.ARM64_IMAGE_MAGIC
        boot = self.root / "boot.img"
        boot.write_bytes(bytes(header) + bytes(kernel))
        parsed = images.parse_boot_image(boot)
        self.assertEqual((parsed.kernel_size, parsed.ramdisk_size), (4096, 0))

        init_header = bytearray(4096)
        init_header[:8] = images.BOOT_MAGIC
        struct.pack_into("<II", init_header, 8, 0, 4096)
        struct.pack_into("<I", init_header, 20, 1584)
        struct.pack_into("<I", init_header, 40, 4)
        init = self.root / "init_boot.img"
        init.write_bytes(bytes(init_header) + b"\x02\x21\x4c\x18" + b"\0" * 4092)
        parsed_init = images.parse_boot_image(init, require_kernel=False)
        self.assertEqual((parsed_init.kernel_size, parsed_init.ramdisk_size), (0, 4096))

    def test_bad_boot_magic_is_rejected(self) -> None:
        path = self.root / "bad.img"
        path.write_bytes(b"NOTBOOT!" + b"\0" * 4088)
        with self.assertRaises(images.ImageError):
            images.parse_boot_image(path)

    def test_vendor_boot_v4_table_and_bootconfig(self) -> None:
        page = 2048
        ramdisk = b"vendor-ramdisk" * 8
        entry = bytearray(108)
        struct.pack_into("<III", entry, 0, len(ramdisk), 0, 1)
        entry[12:20] = b"platform"
        table = bytes(entry)
        bootconfig = b"androidboot.hardware=cutf_cvm\n"
        header = bytearray(2128)
        header[:8] = images.VENDOR_BOOT_MAGIC
        struct.pack_into("<IIIII", header, 8, 4, page, 0, 0, len(ramdisk))
        struct.pack_into("<I", header, 2096, 2128)
        struct.pack_into("<I", header, 2100, 0)
        struct.pack_into("<IIII", header, 2112, len(table), 1, 108, len(bootconfig))
        data = bytearray(header)
        data += b"\0" * (images._align(len(data), page) - len(data))
        data += ramdisk
        data += b"\0" * (images._align(len(data), page) - len(data))
        data += table
        data += b"\0" * (images._align(len(data), page) - len(data))
        data += bootconfig
        path = self.root / "vendor_boot.img"
        path.write_bytes(bytes(data))
        parsed = images.parse_vendor_boot_image(path)
        self.assertEqual(parsed.ramdisks[0].name, "platform")
        self.assertEqual(parsed.bootconfig, bootconfig)
        self.assertEqual(parsed.page_size, page)

    def test_bootconfig_trailer_roundtrip_and_tamper(self) -> None:
        config = b"androidboot.hardware=cutf_cvm\nandroidboot.selinux=permissive\n"
        initrd = images.build_initrd(b"V" * 20, b"G" * 20, config)
        images.verify_bootconfig_trailer(initrd, config)
        tampered = bytearray(initrd)
        tampered[-21] ^= 0x01
        with self.assertRaises(images.ImageError):
            images.verify_bootconfig_trailer(bytes(tampered), config)

    def test_lp_metadata_lists_partitions(self) -> None:
        prefix = bytearray(12288 + 256 + 52)
        geometry = (
            struct.pack("<II", images.LP_GEOMETRY_MAGIC, 52)
            + b"\0" * 32
            + struct.pack("<III", 65536, 2, 4096)
        )
        prefix[4096 : 4096 + len(geometry)] = geometry
        header = bytearray(256)
        struct.pack_into("<IHHI", header, 0, images.LP_HEADER_MAGIC, 10, 2, 256)
        struct.pack_into("<I", header, 44, 52)
        struct.pack_into("<III", header, 80, 0, 1, 52)
        prefix[12288 : 12288 + 256] = header
        entry = bytearray(52)
        entry[:8] = b"system_a"
        prefix[12288 + 256 : 12288 + 256 + 52] = entry
        info = images.check_lp_metadata(bytes(prefix))
        self.assertEqual(info["partitions"], ["system_a"])

    def test_avb_magic(self) -> None:
        path = self.root / "vbmeta.img"
        path.write_bytes(images.AVB_MAGIC + b"\0" * 60)
        images.check_avb(path)
        path.write_bytes(b"NOPE" + b"\0" * 60)
        with self.assertRaises(images.ImageError):
            images.check_avb(path)

    def test_bootconfig_contains_f2fs_suffix_and_nonce_is_not_embedded(self) -> None:
        bundle = mini_bundle()
        config = images.bootconfig_bytes(bundle, b"", "permissive", 2048)
        self.assertIn(b"androidboot.fstab_suffix=cf.f2fs.hctr2\n", config)
        self.assertIn(b"androidboot.ddr_size=2048MB\n", config)
        self.assertNotIn(b"{nonce}", config)
        cmdline = images.build_cmdline(
            bundle, "init=/init bootconfig", "permissive", 2048
        )
        self.assertIn("androidboot.aster_nonce={nonce}", cmdline)
        self.assertEqual(cmdline.count("androidboot.ddr_size="), 1)


class GptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="images-gpt-", dir=images.WORK.parent
        )
        self.root = Path(self.tmp.name)
        self.old_work = images.WORK
        images.WORK = self.root

    def tearDown(self) -> None:
        images.WORK = self.old_work
        self.tmp.cleanup()

    def test_gpt_roundtrip_with_raw_and_blank_partitions(self) -> None:
        raw = bytes((index * 3) & 0xFF for index in range(4096))
        archive_path = self.root / "raw.zip"
        make_zip(archive_path, [("raw.img", raw, 0o100644)])
        bundle = bundle_for_zip(archive_path)
        members = images.inspect_archive(archive_path, bundle)
        bundle["id"] = "gpt-test"
        partitions = [
            images.Partition("raw", 4096, {"member": "raw.img"}),
            images.Partition("blank", 8192, {"generated": "blank", "size": 8192}),
        ]
        disk = self.root / "disk.img"
        images.write_gpt(disk, bundle, partitions, archive_path, members, 0)

        data = disk.read_bytes()
        self.assertEqual(data[510:512], b"\x55\xaa")
        self.assertEqual(data[446 + 4], 0xEE)
        header = data[512 : 512 + 92]
        self.assertEqual(header[:8], b"EFI PART")
        self.assertEqual(struct.unpack_from("<I", header, 8)[0], 0x00010000)
        crc = struct.unpack_from("<I", header, 16)[0]
        self.assertEqual(
            zlib.crc32(header[:16] + b"\0\0\0\0" + header[20:]) & 0xFFFFFFFF, crc
        )
        entries = data[1024 : 1024 + 128 * 128]
        self.assertEqual(
            struct.unpack_from("<I", header, 88)[0], zlib.crc32(entries) & 0xFFFFFFFF
        )
        labels = []
        for index in range(4):
            entry = entries[index * 128 : (index + 1) * 128]
            if entry[:16] == b"\0" * 16:
                continue
            labels.append(entry[56:128].decode("utf-16-le").rstrip("\0"))
        self.assertEqual(labels, ["raw", "blank"])
        self.assertEqual(data[partitions[0].offset : partitions[0].offset + 4096], raw)
        self.assertEqual(
            data[partitions[1].offset : partitions[1].offset + 8192], b"\0" * 8192
        )
        total_sectors = len(data) // 512
        backup = data[(total_sectors - 1) * 512 : total_sectors * 512]
        self.assertEqual(backup[:8], b"EFI PART")
        self.assertEqual(
            data[(total_sectors - 33) * 512 : (total_sectors - 1) * 512], entries
        )

    def test_free_floor_is_enforced(self) -> None:
        with self.assertRaises(images.ImageError):
            images.ensure_free(self.root, 1 << 62)


if __name__ == "__main__":
    unittest.main()

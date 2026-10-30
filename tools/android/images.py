#!/usr/bin/env python3
"""Inspect, verify and prepare the pinned ARM64 Android virtual-device bundle.

This tool prepares inputs only.  It never starts QEMU and never claims that
Android boots.  The bundle is an AOSP Cuttlefish arm64 phone image set.  The
plan keeps Cuttlefish's QEMU ``virt`` board, PCI virtio devices, GPT partition
labels, Android boot images and bootconfig, but loads the kernel and the
concatenated generic+vendor ramdisk directly.  U-Boot is therefore not needed.

The manifest is ``artifacts-v1.json`` next to this file.  It contains immutable
build/branch/target pins, the archive size, the upstream GCS ETag/MD5/CRC32C,
a locally measured SHA-256, and every member size and CRC32.  Unknown hashes
are never invented: SHA-256 absence upstream is stated in the manifest.

Commands::

    python3 tools/android/images.py list
    python3 tools/android/images.py verify --bundle cf-arm64-only-phone-16644040
    python3 tools/android/images.py fetch  --bundle cf-arm64-only-phone-16644040
    python3 tools/android/images.py inspect --bundle cf-arm64-only-phone-16644040
    python3 tools/android/images.py prepare --bundle cf-arm64-only-phone-16644040
    python3 tools/android/images.py status

``prepare`` writes ``build/android/images/prepared/<bundle>/`` containing the
unpacked kernel, concatenated initrd, bootconfig, cmdline, generated GPT disk,
and ``prepared.json`` plus a ``run-config.json`` in the schema consumed by
``tools/android/run.py``.  The archive remains the source of truth; the large
sparse ``super`` and ``userdata`` members are streamed from it into the disk and
are not kept as duplicate extracted files.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "build" / "android" / "images"
MANIFEST_PATH = Path(__file__).with_name("artifacts-v1.json")
DEFAULT_QEMU = ROOT / "build" / "qemu" / "build-tci5" / "qemu-system-aarch64"
SECTOR = 512
MIB = 1 << 20
GIB = 1 << 30
MAX_MEMBER_BYTES = 9 * GIB
MAX_ARCHIVE_MEMBERS = 64
MAX_COMPRESSION_RATIO = 100

SPARSE_MAGIC = 0xED26FF3A
SPARSE_RAW = 0xCAC1
SPARSE_FILL = 0xCAC2
SPARSE_SKIP = 0xCAC3
SPARSE_CRC32 = 0xCAC4
LP_GEOMETRY_MAGIC = 0x616C4467
LP_HEADER_MAGIC = 0x414C5030
BOOT_MAGIC = b"ANDROID!"
VENDOR_BOOT_MAGIC = b"VNDRBOOT"
AVB_MAGIC = b"AVB0"
BOOTCONFIG_TRAILER = b"#BOOTCONFIG\n"
ARM64_IMAGE_MAGIC = b"ARM\x64"

LINUX_FS_GUID = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
CUTTLEFISH_REFERENCE = {
    "source": "https://android.googlesource.com/device/google/cuttlefish/+/refs/heads/main/host/libs/vm_manager/qemu_manager.cpp",
    "observed_qemu_shape": [
        "-machine virt[,gic-version=3] -cpu max (host-compatible acceleration; TCG uses gic-version=2 in Cuttlefish)",
        "-device virtio-blk-pci-non-transitional for the first disk with bootindex=1",
        "-device virtio-gpu-pci for guest_swiftshader; bootconfig gralloc=minigbm, hwcomposer=drm, egl=angle, vulkan=pastel, opengles.version=196609",
        "-device virtio-serial-pci-non-transitional with virtconsole ports /dev/hvc0.. for kernel log, console, logcat, keymaster, gatekeeper, sensors",
        "-device vhost-vsock-pci only on Linux hosts",
        "-device virtio-rng-pci, virtio-mouse-pci, virtio-keyboard-pci, virtio-balloon-pci",
        "ARM64 boot uses -bios <u-boot> plus a separate persistent GPT disk for uboot_env; x86 uses pflash",
    ],
    "direct_kernel_adaptation": [
        "Uses the same virt board, PCI virtio transport, GPT labels and bootconfig semantics.",
        "Replaces U-Boot with -kernel Image and -initrd (vendor_boot ramdisk then init_boot ramdisk, then bootconfig trailer).",
        "Keeps virtio-blk-pci-non-transitional as disk 0, so Android sees vda and /dev/block/by-name/<label>.",
        "Omits vhost-vsock (not built in the TCI binary), host-backed virtio-console services, audio, network and input switches.",
        "Selects nonsecure keymint and gatekeeper APEXes and the DRM hwcomposer APEX, which the vendor image contains.",
    ],
    "fstab_by_name": [
        "/dev/block/by-name/boot",
        "/dev/block/by-name/init_boot",
        "/dev/block/by-name/vendor_boot",
        "/dev/block/by-name/userdata",
        "/dev/block/by-name/metadata",
        "/dev/block/by-name/misc",
        "logical system, system_ext, product, vendor, odm, system_dlkm, vendor_dlkm, odm_dlkm inside super",
    ],
    "ramdisk_order": "vendor_boot vendor ramdisk, then init_boot generic ramdisk, then bootconfig and #BOOTCONFIG trailer",
    "fstab_source": "https://android.googlesource.com/device/google/cuttlefish/+/refs/heads/main/shared/config/fstab.in",
}
GPT_ENTRY_COUNT = 128
GPT_ENTRY_SIZE = 128
GPT_HEADER_SIZE = 92
GPT_RESERVED_SECTORS = 34
GPT_ALIGN_SECTORS = 2048  # 1 MiB


class ImageError(RuntimeError):
    """A refused or failed artifact operation."""


# ---------------------------------------------------------------------------
# small utilities


def human_bytes(value: int) -> str:
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            return f"{value:.2f} {unit}"
        value /= 1024.0
    raise AssertionError("unreachable")


def sha256_file(path: Path, chunk_size: int = 8 * MIB) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def md5_base64(path: Path, chunk_size: int = 8 * MIB) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as error:
        raise ImageError(f"missing file: {path}") from error
    except json.JSONDecodeError as error:
        raise ImageError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(data, dict):
        raise ImageError(f"{path} must contain a JSON object")
    return data


def is_hex_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def is_crc32_hex(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{8}", value) is not None


def decode_base64(value: object, length: int) -> bytes:
    if not isinstance(value, str):
        raise ImageError("base64 value must be a string")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ImageError(f"invalid base64 value {value!r}: {error}") from error
    if len(decoded) != length:
        raise ImageError(
            f"base64 value {value!r} decodes to {len(decoded)} bytes, expected {length}"
        )
    return decoded


def ensure_free(path: Path, floor: int) -> None:
    free = shutil.disk_usage(path).free
    if free < floor:
        raise ImageError(
            f"free space {human_bytes(free)} is below the required floor {human_bytes(floor)} at {path}"
        )


def relative_name(name: str) -> PurePosixPath:
    """Return a safe relative member path or refuse it."""
    if not isinstance(name, str) or not name:
        raise ImageError("archive member has an empty name")
    if "\x00" in name or "\\" in name:
        raise ImageError(f"archive member {name!r} contains a forbidden character")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise ImageError(f"archive member {name!r} is absolute")
    path = PurePosixPath(name)
    if any(part in ("", ".", "..") for part in path.parts):
        raise ImageError(f"archive member {name!r} has an unsafe path component")
    if path.as_posix() != name:
        raise ImageError(f"archive member {name!r} is not a canonical relative path")
    return path


# ---------------------------------------------------------------------------
# manifest


def validate_manifest(data: dict) -> dict:
    if data.get("schema") != "aster.android.artifacts/v1":
        raise ImageError("manifest schema must be 'aster.android.artifacts/v1'")
    budgets = data.get("budgets")
    if not isinstance(budgets, dict):
        raise ImageError("manifest budgets must be an object")
    for key in ("max_download_bytes", "max_new_disk_bytes", "free_floor_bytes"):
        value = budgets.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ImageError(f"manifest budgets.{key} must be a positive integer")
    if budgets["free_floor_bytes"] < 3 * GIB:
        raise ImageError("free_floor_bytes must be at least 3 GiB")

    bundles = data.get("bundles")
    if not isinstance(bundles, list) or not bundles:
        raise ImageError("manifest bundles must be a non-empty list")

    seen_ids: set[str] = set()
    for bundle in bundles:
        if not isinstance(bundle, dict):
            raise ImageError("each bundle must be an object")
        bundle_id = bundle.get("id")
        if not isinstance(bundle_id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9.-]{2,63}", bundle_id
        ):
            raise ImageError(f"invalid bundle id: {bundle_id!r}")
        if bundle_id in seen_ids:
            raise ImageError(f"duplicate bundle id: {bundle_id}")
        seen_ids.add(bundle_id)

        for key in (
            "description",
            "branch",
            "target",
            "build_id",
            "build_fingerprint",
            "android_release",
            "kernel",
            "archive",
            "members",
            "required_members",
            "partition_plan",
            "bootconfig",
            "qemu",
            "license",
            "budget",
            "blockers",
        ):
            if key not in bundle:
                raise ImageError(f"{bundle_id}: missing key {key}")

        archive = bundle["archive"]
        if not isinstance(archive, dict):
            raise ImageError(f"{bundle_id}: archive must be an object")
        size = archive.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ImageError(f"{bundle_id}: archive.size must be a positive integer")
        if size > budgets["max_download_bytes"]:
            raise ImageError(f"{bundle_id}: archive exceeds the download budget")
        if not is_hex_sha256(archive.get("sha256")):
            raise ImageError(
                f"{bundle_id}: archive.sha256 must be 64 lowercase hex digits"
            )
        decode_base64(archive.get("md5_base64"), 16)
        decode_base64(archive.get("crc32c_base64"), 4)
        page_url = archive.get("page_url")
        if not isinstance(page_url, str) or not page_url.startswith("https://"):
            raise ImageError(f"{bundle_id}: archive.page_url must be an https URL")
        if archive.get("download_resolver") != "ci_artifact_viewer_jsvariables_v1":
            raise ImageError(f"{bundle_id}: unsupported download resolver")

        members = bundle["members"]
        if not isinstance(members, list) or not members:
            raise ImageError(f"{bundle_id}: members must be a non-empty list")
        by_name: dict[str, dict] = {}
        total_uncompressed = 0
        for member in members:
            if not isinstance(member, dict):
                raise ImageError(f"{bundle_id}: member must be an object")
            name = member.get("name")
            relative_name(name)
            if name in by_name:
                raise ImageError(f"{bundle_id}: duplicate member {name}")
            if not is_crc32_hex(member.get("crc32")):
                raise ImageError(f"{bundle_id}: member {name} has an invalid crc32")
            member_size = member.get("size")
            if (
                isinstance(member_size, bool)
                or not isinstance(member_size, int)
                or member_size <= 0
            ):
                raise ImageError(f"{bundle_id}: member {name} has an invalid size")
            if member_size > MAX_MEMBER_BYTES:
                raise ImageError(f"{bundle_id}: member {name} exceeds the bounded size")
            by_name[name] = member
            total_uncompressed += member_size

        required = bundle["required_members"]
        if not isinstance(required, list) or not required:
            raise ImageError(f"{bundle_id}: required_members must be a non-empty list")
        for name in required:
            if name not in by_name:
                raise ImageError(
                    f"{bundle_id}: required member {name!r} is absent from members"
                )

        plan = bundle["partition_plan"]
        if not isinstance(plan, list) or not plan:
            raise ImageError(f"{bundle_id}: partition_plan must be a non-empty list")
        labels: set[str] = set()
        for partition in plan:
            if not isinstance(partition, dict):
                raise ImageError(f"{bundle_id}: partition entry must be an object")
            label = partition.get("label")
            if not isinstance(label, str) or not re.fullmatch(
                r"[a-z][a-z0-9_]{1,35}", label
            ):
                raise ImageError(f"{bundle_id}: invalid partition label {label!r}")
            names = (
                [label + suffix for suffix in ("_a", "_b")]
                if partition.get("ab")
                else [label]
            )
            for name in names:
                if name in labels:
                    raise ImageError(f"{bundle_id}: duplicate GPT label {name}")
                labels.add(name)
            if "member" in partition and partition["member"] not in by_name:
                raise ImageError(
                    f"{bundle_id}: partition {label} uses unknown member {partition['member']!r}"
                )
            generated = partition.get("generated")
            if generated is not None:
                if generated != "blank":
                    raise ImageError(
                        f"{bundle_id}: partition {label} has unsupported generated kind"
                    )
                gen_size = partition.get("size")
                if (
                    isinstance(gen_size, bool)
                    or not isinstance(gen_size, int)
                    or gen_size <= 0
                ):
                    raise ImageError(
                        f"{bundle_id}: partition {label} needs a positive generated size"
                    )
            if "member" not in partition and generated is None:
                raise ImageError(f"{bundle_id}: partition {label} has no source")

        qemu = bundle["qemu"]
        args_template = qemu.get("args_template")
        if (
            not isinstance(args_template, list)
            or not args_template
            or any(not isinstance(a, str) for a in args_template)
        ):
            raise ImageError(
                f"{bundle_id}: qemu.args_template must be a non-empty string list"
            )
        joined = " ".join(args_template)
        if "{nonce}" not in joined and "{cmdline}" not in joined:
            raise ImageError(
                f"{bundle_id}: qemu args must contain the {{nonce}} placeholder "
                "directly or through {cmdline}"
            )

        budget = bundle["budget"]
        if not isinstance(budget, dict):
            raise ImageError(f"{bundle_id}: budget must be an object")
        for key in ("estimated_prepared_bytes", "estimated_peak_bytes"):
            value = budget.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ImageError(
                    f"{bundle_id}: budget.{key} must be a positive integer"
                )
        if budget["estimated_prepared_bytes"] > budgets["max_new_disk_bytes"]:
            raise ImageError(
                f"{bundle_id}: estimated prepared bytes exceed the disk budget"
            )

        bootconfig = bundle["bootconfig"]
        if not isinstance(bootconfig, dict) or not isinstance(
            bootconfig.get("settings"), dict
        ):
            raise ImageError(f"{bundle_id}: bootconfig.settings must be an object")
        if not isinstance(bootconfig.get("fstab_suffix"), str):
            raise ImageError(f"{bundle_id}: bootconfig.fstab_suffix must be a string")

    return data


def load_manifest(path: Path | None = None) -> dict:
    return validate_manifest(read_json(MANIFEST_PATH if path is None else path))


def bundle_by_id(data: dict, bundle_id: str) -> dict:
    for bundle in data["bundles"]:
        if bundle["id"] == bundle_id:
            return bundle
    raise ImageError(f"unknown bundle {bundle_id!r}")


# ---------------------------------------------------------------------------
# archive validation and safe extraction


@dataclass(frozen=True)
class ArchiveMember:
    name: str
    size: int
    crc32: int
    compress_size: int
    info: zipfile.ZipInfo


def inspect_archive(zip_path: Path, bundle: dict) -> dict[str, ArchiveMember]:
    """Validate the zip directory and every member name before any extraction."""
    declared = {m["name"]: m for m in bundle["members"]}
    required = set(bundle["required_members"])
    try:
        archive = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as error:
        raise ImageError(f"cannot read archive {zip_path}: {error}") from error
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise ImageError(
                f"archive has {len(infos)} members, over the bounded limit"
            )
        seen: set[str] = set()
        result: dict[str, ArchiveMember] = {}
        total_uncompressed = 0
        for info in infos:
            name = info.filename
            relative_name(name)
            if name in seen:
                raise ImageError(f"duplicate archive member: {name}")
            seen.add(name)
            if info.is_dir():
                raise ImageError(f"directory member {name!r} is not expected")
            if info.flag_bits & 0x1:
                raise ImageError(f"encrypted archive member: {name}")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ImageError(f"unsupported compression method for {name}")
            if info.file_size <= 0 or info.file_size > MAX_MEMBER_BYTES:
                raise ImageError(f"member {name} has an out-of-bounds size")
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode and (mode & 0o170000) == 0o120000:
                raise ImageError(f"symbolic-link archive member is refused: {name}")
            if info.compress_size == 0 and info.file_size:
                raise ImageError(f"member {name} has no compressed bytes")
            if (
                info.file_size > 64 * MIB
                and info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO
            ):
                raise ImageError(f"member {name} has an implausible compression ratio")
            if name not in declared:
                raise ImageError(
                    f"undeclared archive member {name!r}; refusing an unexpected set"
                )
            expected = declared[name]
            if info.file_size != expected["size"]:
                raise ImageError(
                    f"member {name} size {info.file_size} does not match manifest {expected['size']}"
                )
            if info.CRC != int(expected["crc32"], 16):
                raise ImageError(f"member {name} CRC32 does not match the manifest")
            result[name] = ArchiveMember(
                name, info.file_size, info.CRC, info.compress_size, info
            )
            seen.add(name)
            total_uncompressed += info.file_size

        missing = required - set(result)
        if missing:
            raise ImageError(
                "archive is incomplete; missing required member(s): "
                + ", ".join(sorted(missing))
            )
        if total_uncompressed > MAX_MEMBER_BYTES * 2:
            raise ImageError("archive uncompressed size exceeds the bounded total")
        return result


def extract_members(
    zip_path: Path,
    members: dict[str, ArchiveMember],
    names: list[str],
    destination: Path,
) -> None:
    """Stream selected members, verifying CRC and refusing to follow links."""
    destination.mkdir(parents=True, exist_ok=True)
    for name in names:
        member = members[name]
        relative = relative_name(name)
        current = destination
        for part in relative.parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise ImageError(f"refusing to extract through symlink {current}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            raise ImageError(f"refusing to overwrite existing prepared input {target}")
        part = target.with_name(target.name + ".part")
        crc = 0
        written = 0
        with (
            zipfile.ZipFile(zip_path) as archive,
            archive.open(member.info) as source,
            part.open("wb") as sink,
        ):
            while True:
                chunk = source.read(8 * MIB)
                if not chunk:
                    break
                written += len(chunk)
                if written > member.size:
                    part.unlink(missing_ok=True)
                    raise ImageError(f"member {name} produced more bytes than declared")
                crc = zlib.crc32(chunk, crc)
                sink.write(chunk)
        if written != member.size or (crc & 0xFFFFFFFF) != member.crc32:
            part.unlink(missing_ok=True)
            raise ImageError(
                f"member {name} failed its size/CRC check during extraction"
            )
        os.replace(part, target)


def archive_sha256_matches(zip_path: Path, bundle: dict) -> bool:
    archive = bundle["archive"]
    if zip_path.stat().st_size != archive["size"]:
        return False
    if sha256_file(zip_path) != archive["sha256"]:
        return False
    if md5_base64(zip_path) != archive["md5_base64"]:
        return False
    return True


# ---------------------------------------------------------------------------
# network


def resolve_ci_artifact(page_url: str) -> str:
    """Resolve the short-lived signed GCS URL from the artifact viewer page."""
    request = urllib.request.Request(page_url, headers={"User-Agent": "aster-images/1"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            html = response.read(2 * MIB).decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as error:
        raise ImageError(f"cannot open artifact page {page_url}: {error}") from error
    match = re.search(r"var JSVariables = (\{.*?\});", html, re.S)
    if not match:
        raise ImageError("artifact page did not expose JSVariables")
    try:
        variables = json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise ImageError(
            f"artifact page variables are invalid JSON: {error}"
        ) from error
    url = variables.get("artifactUrl")
    if not isinstance(url, str) or not url.startswith(
        "https://storage.googleapis.com/android-build/"
    ):
        raise ImageError(f"artifact page returned an unexpected download URL: {url!r}")
    return url


def fetch_archive(
    bundle: dict, destination: Path, floor: int, timeout: int = 120
) -> Path:
    archive = bundle["archive"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if archive_sha256_matches(destination, bundle):
            return destination
        raise ImageError(
            f"{destination} exists but does not match the pinned size/SHA-256/MD5; refusing to overwrite it"
        )
    ensure_free(WORK, floor + archive["size"])
    url = resolve_ci_artifact(archive["page_url"])
    part = destination.with_name(destination.name + f".part-{os.getpid()}")
    part.unlink(missing_ok=True)
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    written = 0
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "aster-images/1"})
        with (
            urllib.request.urlopen(request, timeout=timeout) as response,
            part.open("wb") as sink,
        ):
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) != archive["size"]:
                raise ImageError(
                    f"server Content-Length {declared} does not match the pinned size"
                )
            upstream_md5 = response.headers.get("x-goog-hash", "")
            while True:
                chunk = response.read(8 * MIB)
                if not chunk:
                    break
                written += len(chunk)
                if written > archive["size"]:
                    raise ImageError("download exceeded the pinned size")
                sha.update(chunk)
                md5.update(chunk)
                sink.write(chunk)
        if written != archive["size"]:
            raise ImageError(
                f"download is incomplete: {written} of {archive['size']} bytes"
            )
        if sha.hexdigest() != archive["sha256"]:
            raise ImageError(
                "downloaded archive SHA-256 does not match the manifest pin"
            )
        if base64.b64encode(md5.digest()).decode() != archive["md5_base64"]:
            raise ImageError(
                "downloaded archive MD5 does not match the upstream GCS digest"
            )
        if (
            "md5=" in upstream_md5
            and upstream_md5.split("md5=", 1)[1].split(",", 1)[0]
            != archive["md5_base64"]
        ):
            raise ImageError("GCS response MD5 header does not match the manifest pin")
        part.rename(destination)
        return destination
    except Exception:
        part.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# image formats


@dataclass(frozen=True)
class BootImage:
    path: Path
    header_version: int
    header_size: int
    kernel_size: int
    ramdisk_size: int
    cmdline: str


def parse_boot_image(path: Path, require_kernel: bool = True) -> BootImage:
    with path.open("rb") as handle:
        header = handle.read(4096)
        if len(header) < 4096 or header[:8] != BOOT_MAGIC:
            raise ImageError(f"{path} is not an Android boot image")
        kernel_size, ramdisk_size = struct.unpack_from("<II", header, 8)
        header_size = struct.unpack_from("<I", header, 20)[0]
        version = struct.unpack_from("<I", header, 40)[0]
        if version not in (3, 4):
            raise ImageError(
                f"{path} uses boot header v{version}; only v3/v4 are supported"
            )
        if header_size not in (1584,):
            raise ImageError(f"{path} has unexpected boot header size {header_size}")
        cmdline = (
            header[44 : 44 + 1536].split(b"\0", 1)[0].decode("utf-8", errors="replace")
        )
        kernel_offset = 4096
        if kernel_size:
            handle.seek(kernel_offset + 56)
            if handle.read(4) != ARM64_IMAGE_MAGIC:
                raise ImageError(
                    f"{path} kernel is not an ARM64 Image (missing ARM\\x64 magic)"
                )
        elif require_kernel:
            raise ImageError(f"{path} has no kernel")
        if not require_kernel and ramdisk_size == 0:
            raise ImageError(f"{path} has no generic ramdisk")
        ramdisk_offset = kernel_offset + ((kernel_size + 4095) // 4096) * 4096
        if ramdisk_size:
            handle.seek(ramdisk_offset)
            magic = handle.read(4)
            if magic not in (b"\x02\x21\x4c\x18", b"\x1f\x8b", b"0707"):
                raise ImageError(f"{path} ramdisk is not LZ4-legacy, gzip or newc")
        if ramdisk_offset + ramdisk_size > path.stat().st_size:
            raise ImageError(f"{path} ramdisk extends past the file")
        return BootImage(path, version, header_size, kernel_size, ramdisk_size, cmdline)


@dataclass(frozen=True)
class VendorRamdisk:
    name: str
    ramdisk_type: int
    offset: int
    size: int


@dataclass(frozen=True)
class VendorBootImage:
    path: Path
    header_version: int
    page_size: int
    ramdisk_size: int
    ramdisks: tuple[VendorRamdisk, ...]
    bootconfig: bytes
    cmdline: str
    dtb_size: int
    ramdisk_offset: int


def parse_vendor_boot_image(path: Path) -> VendorBootImage:
    data = path.read_bytes()
    if len(data) < 4096 or data[:8] != VENDOR_BOOT_MAGIC:
        raise ImageError(f"{path} is not an Android vendor_boot image")
    version, page_size, _kernel_addr, _ramdisk_addr, ramdisk_size = struct.unpack_from(
        "<IIIII", data, 8
    )
    if version != 4:
        raise ImageError(f"{path} uses vendor_boot v{version}; only v4 is supported")
    if page_size < 2048 or page_size > 65536 or page_size & (page_size - 1):
        raise ImageError(f"{path} has an invalid page size {page_size}")
    header_size = struct.unpack_from("<I", data, 2096)[0]
    dtb_size = struct.unpack_from("<I", data, 2100)[0]
    table_size, entry_count, entry_size, bootconfig_size = struct.unpack_from(
        "<IIII", data, 2112
    )
    if header_size != 2128:
        raise ImageError(f"{path} has unexpected vendor_boot header size {header_size}")
    if entry_size < 108:
        raise ImageError(f"{path} has an invalid ramdisk table entry size {entry_size}")
    ramdisk_offset = _align(header_size, page_size)
    dtb_offset = _align(ramdisk_offset + ramdisk_size, page_size)
    table_offset = _align(dtb_offset + dtb_size, page_size)
    bootconfig_offset = _align(table_offset + table_size, page_size)
    if bootconfig_offset + bootconfig_size > len(data):
        raise ImageError(f"{path} bootconfig extends past the file")
    if table_size != entry_count * entry_size:
        raise ImageError(f"{path} vendor ramdisk table size is inconsistent")

    ramdisks: list[VendorRamdisk] = []
    for index in range(entry_count):
        offset = table_offset + index * entry_size
        size, data_offset, ramdisk_type = struct.unpack_from("<III", data, offset)
        name = (
            data[offset + 12 : offset + 44]
            .split(b"\0", 1)[0]
            .decode("ascii", errors="replace")
        )
        if data_offset + size > ramdisk_size:
            raise ImageError(
                f"{path} vendor ramdisk {index} extends past the ramdisk section"
            )
        ramdisks.append(VendorRamdisk(name, ramdisk_type, data_offset, size))
    bootconfig = data[bootconfig_offset : bootconfig_offset + bootconfig_size]
    cmdline = data[28 : 28 + 2048].split(b"\0", 1)[0].decode("utf-8", errors="replace")
    return VendorBootImage(
        path,
        version,
        page_size,
        ramdisk_size,
        tuple(ramdisks),
        bootconfig,
        cmdline,
        dtb_size,
        ramdisk_offset,
    )


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


@dataclass(frozen=True)
class SparseHeader:
    block_size: int
    total_blocks: int
    total_chunks: int
    file_header_size: int
    chunk_header_size: int

    @property
    def expanded_size(self) -> int:
        return self.block_size * self.total_blocks


def parse_sparse_header(head: bytes, source: str) -> SparseHeader:
    if len(head) < 28:
        raise ImageError(f"{source}: truncated sparse header")
    (
        magic,
        _major,
        _minor,
        file_header_size,
        chunk_header_size,
        block_size,
        total_blocks,
        total_chunks,
        _checksum,
    ) = struct.unpack_from("<IHHHHIIII", head, 0)
    if magic != SPARSE_MAGIC:
        raise ImageError(f"{source}: not an Android sparse image")
    if file_header_size != 28 or chunk_header_size != 12:
        raise ImageError(f"{source}: unsupported sparse header sizes")
    if block_size == 0 or block_size % 4 or block_size > 1 << 20:
        raise ImageError(f"{source}: invalid sparse block size {block_size}")
    if total_blocks == 0 or total_chunks == 0:
        raise ImageError(f"{source}: empty sparse image")
    expanded = block_size * total_blocks
    if expanded > 9 * GIB:
        raise ImageError(f"{source}: expanded sparse image is out of bounds")
    return SparseHeader(
        block_size, total_blocks, total_chunks, file_header_size, chunk_header_size
    )


def expand_sparse(
    source,
    destination_path: Path,
    offset: int,
    expected_size: int,
    floor: int,
    work: Path,
) -> None:
    """Expand one sparse stream into an existing raw file at ``offset``.

    ``source`` is a binary stream positioned at the start of the sparse header.
    SKIP chunks leave a hole, which reads as zero in a freshly truncated file.
    """
    head = source.read(28)
    header = parse_sparse_header(head, str(destination_path))
    if header.expanded_size != expected_size:
        raise ImageError(
            f"sparse image expands to {header.expanded_size}, expected partition size {expected_size}"
        )
    position = 0
    written = 0
    chunk_head = b""
    chunk = b""
    with destination_path.open("r+b") as sink:
        for index in range(header.total_chunks):
            raw = source.read(header.chunk_header_size)
            if len(raw) != header.chunk_header_size:
                raise ImageError(f"sparse chunk {index} header is truncated")
            chunk_type, _reserved, chunk_blocks, total_size = struct.unpack(
                "<HHII", raw
            )
            length = chunk_blocks * header.block_size
            if position + length > header.expanded_size:
                raise ImageError(
                    f"sparse chunk {index} exceeds the declared expanded size"
                )
            if chunk_type == SPARSE_RAW:
                if total_size != header.chunk_header_size + length:
                    raise ImageError(
                        f"sparse chunk {index} has an inconsistent total size"
                    )
                sink.seek(offset + position)
                remaining = length
                while remaining:
                    data = source.read(min(8 * MIB, remaining))
                    if not data:
                        raise ImageError(f"sparse chunk {index} data is truncated")
                    sink.write(data)
                    remaining -= len(data)
                    written += len(data)
            elif chunk_type == SPARSE_FILL:
                if total_size != header.chunk_header_size + 4:
                    raise ImageError(
                        f"sparse fill chunk {index} has an inconsistent total size"
                    )
                fill = source.read(4)
                if len(fill) != 4:
                    raise ImageError(f"sparse fill chunk {index} is truncated")
                sink.seek(offset + position)
                pattern = (fill * ((MIB // 4) + 1))[:MIB]
                remaining = length
                while remaining:
                    piece = pattern[: min(MIB, remaining)]
                    sink.write(piece)
                    remaining -= len(piece)
                    written += len(piece)
            elif chunk_type == SPARSE_SKIP:
                if total_size != header.chunk_header_size:
                    raise ImageError(
                        f"sparse skip chunk {index} has an inconsistent total size"
                    )
            elif chunk_type == SPARSE_CRC32:
                if total_size != header.chunk_header_size + 4:
                    raise ImageError(
                        f"sparse crc chunk {index} has an inconsistent total size"
                    )
                source.read(4)
            else:
                raise ImageError(
                    f"sparse chunk {index} has unknown type {chunk_type:#x}"
                )
            position += length
            if written > 512 * MIB:
                written = 0
                ensure_free(work, floor)
    if position != header.expanded_size:
        raise ImageError("sparse image chunks do not cover the declared expanded size")


def sparse_header_from_stream(source, description: str) -> SparseHeader:
    head = source.read(28)
    return parse_sparse_header(head, description)


def sparse_prefix(source, limit: int) -> bytes:
    """Expand at most ``limit`` bytes of a sparse image (for LP metadata checks)."""
    head = source.read(28)
    header = parse_sparse_header(head, "sparse prefix")
    out = bytearray()
    for _index in range(header.total_chunks):
        raw = source.read(12)
        if len(raw) != 12:
            raise ImageError("truncated sparse chunk header")
        chunk_type, _reserved, chunk_blocks, _total = struct.unpack("<HHII", raw)
        length = chunk_blocks * header.block_size
        if chunk_type == SPARSE_RAW:
            remaining = length
            while remaining:
                data = source.read(min(8 * MIB, remaining))
                if not data:
                    raise ImageError("truncated sparse chunk data")
                out.extend(data)
                remaining -= len(data)
                if len(out) >= limit:
                    return bytes(out[:limit])
        elif chunk_type == SPARSE_FILL:
            fill = source.read(4)
            if len(fill) != 4:
                raise ImageError("truncated sparse fill")
            while len(out) < limit and length:
                take = min(4096, length)
                out.extend((fill * ((take // 4) + 1))[:take])
                length -= take
        elif chunk_type == SPARSE_SKIP:
            if len(out) < limit:
                out.extend(b"\0" * min(length, limit - len(out)))
        elif chunk_type == SPARSE_CRC32:
            source.read(4)
        else:
            raise ImageError(f"unknown sparse chunk type {chunk_type:#x}")
        if len(out) >= limit:
            return bytes(out[:limit])
    return bytes(out)


def check_lp_metadata(prefix: bytes) -> dict:
    if len(prefix) < 12288 + 256:
        raise ImageError("super metadata prefix is truncated")
    geometry = prefix[4096 : 4096 + 52]
    magic, struct_size = struct.unpack_from("<II", geometry, 0)
    if magic != LP_GEOMETRY_MAGIC or struct_size != 52:
        raise ImageError("super has no valid logical-partition geometry")
    metadata_max_size, slot_count, logical_block_size = struct.unpack_from(
        "<III", geometry, 40
    )
    if metadata_max_size <= 0 or metadata_max_size > 16 * MIB or slot_count < 1:
        raise ImageError("super geometry has invalid metadata bounds")
    header = prefix[12288 : 12288 + 256]
    magic, major, minor, header_size = struct.unpack_from("<IHHI", header, 0)
    if magic != LP_HEADER_MAGIC or header_size != 256:
        raise ImageError("super has no valid logical-partition header")
    tables_size = struct.unpack_from("<I", header, 44)[0]
    partitions_offset, partition_count, partition_entry_size = struct.unpack_from(
        "<III", header, 80
    )
    if tables_size > 8 * MIB or partition_entry_size < 52:
        raise ImageError("super metadata tables are out of bounds")
    if partition_count > 4096:
        raise ImageError("super partition table has an implausible entry count")
    base = 12288 + header_size + partitions_offset
    end = base + partition_count * partition_entry_size
    if end > len(prefix):
        raise ImageError("super partition table is truncated")
    names = []
    for index in range(partition_count):
        entry = prefix[
            base + index * partition_entry_size : base
            + (index + 1) * partition_entry_size
        ]
        name = entry[:36].split(b"\0", 1)[0].decode("ascii", errors="replace")
        if not name:
            raise ImageError("super partition has an empty name")
        names.append(name)
    return {
        "geometry_version": f"{major}.{minor}",
        "logical_block_size": logical_block_size,
        "partitions": sorted(names),
    }


def check_avb(path: Path) -> None:
    with path.open("rb") as handle:
        if handle.read(4) != AVB_MAGIC:
            raise ImageError(f"{path} is not an AVB0 vbmeta image")


# ---------------------------------------------------------------------------
# bootconfig, initrd and cmdline


def bootconfig_bytes(
    bundle: dict, vendor_bootconfig: bytes, selinux: str, memory_mb: int
) -> bytes:
    settings: dict[str, str] = {}
    for line in vendor_bootconfig.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        settings[key.strip()] = value.strip()
    config = bundle["bootconfig"]
    settings["androidboot.hardware"] = config["hardware"]
    settings["androidboot.slot_suffix"] = config["slot_suffix"]
    settings["androidboot.fstab_suffix"] = config["fstab_suffix"]
    settings["androidboot.boot_devices"] = config["boot_devices"]
    settings["androidboot.selinux"] = selinux
    for key, value in config["settings"].items():
        settings[key] = str(value)
    settings["androidboot.ddr_size"] = f"{memory_mb}MB"
    lines = [f"{key}={value}" for key, value in settings.items()]
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_initrd(
    vendor_ramdisk: bytes, generic_ramdisk: bytes, bootconfig: bytes
) -> bytes:
    """Vendor ramdisk, then generic ramdisk, then bootconfig and its trailer.

    The Android boot-image header specifies that the bootloader loads the vendor
    ramdisk first and the generic ramdisk immediately after it.  The bootconfig
    trailer is appended uncompressed because the kernel scans the raw initrd
    buffer for it before unpacking the compressed ramdisk streams.
    """
    length = len(bootconfig)
    checksum = sum(bootconfig) & 0xFFFFFFFF
    return (
        vendor_ramdisk
        + generic_ramdisk
        + bootconfig
        + struct.pack("<II", length, checksum)
        + BOOTCONFIG_TRAILER
    )


def verify_bootconfig_trailer(data: bytes, expected: bytes) -> None:
    if not data.endswith(BOOTCONFIG_TRAILER):
        raise ImageError("initrd is missing the bootconfig trailer")
    length, checksum = struct.unpack_from("<II", data, len(data) - 20)
    if length != len(expected):
        raise ImageError(
            "initrd bootconfig length does not match the prepared bootconfig"
        )
    if checksum != (sum(expected) & 0xFFFFFFFF):
        raise ImageError(
            "initrd bootconfig checksum does not match the prepared bootconfig"
        )
    if data[-20 - length : -20] != expected:
        raise ImageError(
            "initrd bootconfig payload does not match the prepared bootconfig"
        )


def build_cmdline(
    bundle: dict, vendor_cmdline: str, selinux: str, memory_mb: int
) -> str:
    config = bundle["bootconfig"]
    args = [
        "console=ttyAMA0",
        "earlycon=pl011,mmio32,0x09000000",
        vendor_cmdline.strip(),
        "androidboot.hardware=" + config["hardware"],
        "androidboot.fstab_suffix=" + config["fstab_suffix"],
        "androidboot.boot_devices=" + config["boot_devices"],
        "androidboot.slot_suffix=" + config["slot_suffix"],
        "androidboot.selinux=" + selinux,
        "androidboot.ddr_size=" + str(memory_mb) + "MB",
    ]
    for key, value in config["settings"].items():
        if key == "androidboot.ddr_size":
            continue
        args.append(f"{key}={value}")
    args.append("androidboot.aster_nonce={nonce}")
    return " ".join(part for part in args if part)


# ---------------------------------------------------------------------------
# GPT


@dataclass
class Partition:
    label: str
    size: int
    source: dict
    offset: int = 0
    last_lba: int = 0


def _guid(value: str) -> bytes:
    return uuid.UUID(value).bytes_le


def _gpt_entry(partition: Partition, disk_id: str) -> bytes:
    unique = uuid.uuid5(
        uuid.NAMESPACE_URL, f"aster.android.gpt.v1/{disk_id}/{partition.label}"
    )
    name = partition.label.encode("utf-16-le")
    if len(name) > 72:
        raise ImageError(f"GPT label is too long: {partition.label}")
    return struct.pack(
        "<16s16sQQQ72s",
        _guid(LINUX_FS_GUID),
        unique.bytes_le,
        partition.offset // SECTOR,
        partition.last_lba,
        0,
        name.ljust(72, b"\0"),
    )


def _gpt_header(
    current: int, backup: int, first: int, last: int, disk_guid: bytes, entries_crc: int
) -> bytes:
    header = struct.pack(
        "<8sIIIIQQQQ16sQIII",
        b"EFI PART",
        0x00010000,
        GPT_HEADER_SIZE,
        0,
        0,
        current,
        backup,
        first,
        last,
        disk_guid,
        2,
        GPT_ENTRY_COUNT,
        GPT_ENTRY_SIZE,
        entries_crc,
    )
    crc = zlib.crc32(header) & 0xFFFFFFFF
    return header[:16] + struct.pack("<I", crc) + header[20:]


def plan_partitions(bundle: dict, sizes: dict[str, int]) -> list[Partition]:
    partitions: list[Partition] = []
    for plan in bundle["partition_plan"]:
        labels = (
            [plan["label"] + suffix for suffix in ("_a", "_b")]
            if plan.get("ab")
            else [plan["label"]]
        )
        source = (
            {"generated": "blank", "size": plan["size"]}
            if "generated" in plan
            else {"member": plan["member"]}
        )
        size = plan["size"] if "generated" in plan else sizes[plan["member"]]
        if size <= 0:
            raise ImageError(f"partition {plan['label']} has no size")
        for label in labels:
            partitions.append(Partition(label, size, source))
    return partitions


def write_gpt(
    path: Path,
    bundle: dict,
    partitions: list[Partition],
    zip_path: Path,
    members: dict[str, ArchiveMember],
    floor: int,
) -> None:
    cursor = GPT_RESERVED_SECTORS
    for partition in partitions:
        sectors = (partition.size + SECTOR - 1) // SECTOR
        start = (
            (cursor + GPT_ALIGN_SECTORS - 1) // GPT_ALIGN_SECTORS
        ) * GPT_ALIGN_SECTORS
        partition.offset = start * SECTOR
        partition.last_lba = start + sectors - 1
        cursor = start + sectors
    total_sectors = (
        (cursor + GPT_RESERVED_SECTORS + GPT_ALIGN_SECTORS - 1) // GPT_ALIGN_SECTORS
    ) * GPT_ALIGN_SECTORS
    last_usable = total_sectors - GPT_RESERVED_SECTORS
    if partitions[-1].last_lba > last_usable:
        raise ImageError("partition table does not fit on the disk")

    disk_id = bundle["id"]
    disk_guid = uuid.uuid5(
        uuid.NAMESPACE_URL, f"aster.android.gpt.v1/{disk_id}"
    ).bytes_le
    entries = b"".join(_gpt_entry(partition, disk_id) for partition in partitions)
    entries = entries.ljust(GPT_ENTRY_COUNT * GPT_ENTRY_SIZE, b"\0")
    entries_crc = zlib.crc32(entries) & 0xFFFFFFFF

    ensure_free(WORK, floor + 64 * MIB)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ImageError(f"refusing to overwrite an existing prepared disk {path}")
    part = path.with_name(path.name + ".part")
    part.unlink(missing_ok=True)
    try:
        with part.open("wb") as handle:
            handle.truncate(total_sectors * SECTOR)
            mbr = bytearray(512)
            struct.pack_into("<B", mbr, 446 + 4, 0xEE)
            struct.pack_into("<I", mbr, 446 + 8, 1)
            struct.pack_into("<I", mbr, 446 + 12, min(total_sectors - 1, 0xFFFFFFFF))
            struct.pack_into("<H", mbr, 510, 0xAA55)
            handle.seek(0)
            handle.write(mbr)
            handle.seek(SECTOR)
            handle.write(
                _gpt_header(
                    1,
                    total_sectors - 1,
                    GPT_RESERVED_SECTORS,
                    last_usable,
                    disk_guid,
                    entries_crc,
                )
            )
            handle.seek(2 * SECTOR)
            handle.write(entries)
            handle.seek((total_sectors - 1 - 32) * SECTOR)
            handle.write(entries)
            handle.seek((total_sectors - 1) * SECTOR)
            handle.write(
                _gpt_header(
                    total_sectors - 1,
                    1,
                    GPT_RESERVED_SECTORS,
                    last_usable,
                    disk_guid,
                    entries_crc,
                )
            )
            handle.flush()
            os.fsync(handle.fileno())

        with zipfile.ZipFile(zip_path) as archive:
            for partition in partitions:
                source = partition.source
                if source.get("generated") == "blank":
                    continue
                name = source["member"]
                with archive.open(members[name].info) as stream:
                    first = stream.read(28)
                    if first[:4] == struct.pack("<I", SPARSE_MAGIC):
                        stream.seek(0)
                        expand_sparse(
                            stream, part, partition.offset, partition.size, floor, WORK
                        )
                    else:
                        stream.seek(0)
                        with part.open("r+b") as sink:
                            sink.seek(partition.offset)
                            remaining = partition.size
                            written = 0
                            while remaining:
                                chunk = stream.read(min(8 * MIB, remaining))
                                if not chunk:
                                    break
                                sink.write(chunk)
                                remaining -= len(chunk)
                                written += len(chunk)
                                if written > 256 * MIB:
                                    written = 0
                                    ensure_free(WORK, floor)
                            if remaining:
                                # A raw member shorter than its partition is zero-padded by the hole.
                                pass
                    trailing = stream.read()
                    if trailing:
                        raise ImageError(
                            f"{name} has bytes beyond the {partition.size}-byte partition"
                        )
                ensure_free(WORK, floor)
        part.rename(path)
    except Exception:
        part.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# inspect and prepare


def bundle_paths(bundle: dict) -> dict[str, Path]:
    base = WORK
    return {
        "downloads": base / "downloads",
        "archive": base / "downloads" / bundle["archive"]["artifact"],
        "prepared": base / "prepared" / bundle["id"],
        "inputs": base / "prepared" / bundle["id"] / "inputs",
    }


def check_userdata(prefix: bytes) -> dict:
    if len(prefix) < 1028 or struct.unpack_from("<I", prefix, 1024)[0] != 0xF2F52010:
        raise ImageError("Expected F2FS userdata for the cf.f2fs.hctr2 fstab")
    return {"filesystem": "f2fs", "superblock_magic": "f2f52010"}


def inspect_bundle(bundle: dict, archive: Path) -> dict:
    members = inspect_archive(archive, bundle)
    with zipfile.ZipFile(archive) as zf:
        with zf.open(members["boot.img"].info) as stream:
            boot_head = stream.read(4096)
        with zf.open(members["init_boot.img"].info) as stream:
            init_head = stream.read(4096)
        with zf.open(members["vendor_boot.img"].info) as stream:
            vendor_head = stream.read(4096)
        with zf.open(members["super.img"].info) as stream:
            super_prefix = sparse_prefix(stream, 12288 + 1 * MIB)
        with zf.open(members["userdata.img"].info) as stream:
            userdata = check_userdata(sparse_prefix(stream, 4096))
    for name, head in (("boot.img", boot_head), ("init_boot.img", init_head)):
        if head[:8] != BOOT_MAGIC:
            raise ImageError(f"{name} has no Android boot magic")
        if struct.unpack_from("<I", head, 40)[0] != 4:
            raise ImageError(f"{name} is not boot header v4")
    if vendor_head[:8] != VENDOR_BOOT_MAGIC:
        raise ImageError("vendor_boot.img has no vendor boot magic")
    if struct.unpack_from("<I", vendor_head, 8)[0] != 4:
        raise ImageError("vendor_boot.img is not vendor boot v4")
    super_info = check_lp_metadata(super_prefix)
    return {
        "archive_members": sorted(members),
        "boot": {
            "kernel_size": struct.unpack_from("<I", boot_head, 8)[0],
            "ramdisk_size": struct.unpack_from("<I", boot_head, 12)[0],
        },
        "init_boot": {
            "kernel_size": struct.unpack_from("<I", init_head, 8)[0],
            "ramdisk_size": struct.unpack_from("<I", init_head, 12)[0],
        },
        "vendor_boot": {
            "page_size": struct.unpack_from("<I", vendor_head, 12)[0],
            "vendor_ramdisk_size": struct.unpack_from("<I", vendor_head, 24)[0],
            "bootconfig_size": struct.unpack_from("<I", vendor_head, 2124)[0],
        },
        "super": super_info,
        "userdata": userdata,
    }


def prepare_bundle(
    bundle: dict,
    archive: Path,
    qemu: Path,
    memory_mb: int = 4096,
    selinux: str = "permissive",
    timeout_seconds: int = 1800,
) -> dict:
    budgets = load_manifest()["budgets"]
    floor = budgets["free_floor_bytes"]
    members = inspect_archive(archive, bundle)
    paths = bundle_paths(bundle)
    prepared = paths["prepared"]
    inputs = paths["inputs"]
    if (prepared / "prepared.json").exists():
        raise ImageError(
            f"prepared bundle already exists at {prepared}; verify it instead of overwriting"
        )
    if any(
        (prepared / name).exists()
        for name in ("kernel", "initrd.img", "android.img", "inputs")
    ):
        raise ImageError(
            f"partial prepared state exists at {prepared}; remove it before retrying"
        )
    prepared.mkdir(parents=True, exist_ok=True)

    estimated = bundle["budget"]["estimated_prepared_bytes"]
    ensure_free(WORK, floor + estimated)

    boot = parse_boot_image_from_zip(archive, members["boot.img"])
    init_boot = parse_boot_image_from_zip(
        archive, members["init_boot.img"], require_kernel=False
    )
    vendor = parse_vendor_boot_from_zip(archive, members["vendor_boot.img"])
    if boot.ramdisk_size:
        generic_ramdisk = read_member_bytes(
            archive,
            members["boot.img"],
            4096 + ((boot.kernel_size + 4095) // 4096) * 4096,
            boot.ramdisk_size,
        )
    else:
        generic_ramdisk = read_member_bytes(
            archive,
            members["init_boot.img"],
            4096 + ((init_boot.kernel_size + 4095) // 4096) * 4096,
            init_boot.ramdisk_size,
        )
    vendor_ramdisk = read_member_bytes(
        archive, members["vendor_boot.img"], vendor.ramdisk_offset, vendor.ramdisk_size
    )
    kernel = read_member_bytes(archive, members["boot.img"], 4096, boot.kernel_size)
    config = bootconfig_bytes(bundle, vendor.bootconfig, selinux, memory_mb)
    initrd = build_initrd(vendor_ramdisk, generic_ramdisk, config)
    verify_bootconfig_trailer(initrd, config)
    cmdline = build_cmdline(bundle, vendor.cmdline, selinux, memory_mb)

    (prepared / "kernel").write_bytes(kernel)
    (prepared / "initrd.img").write_bytes(initrd)
    (prepared / "bootconfig.txt").write_bytes(config)
    (prepared / "cmdline.txt").write_text(cmdline + "\n")

    # Generated blank partitions stay sparse; only the GPT labels and sizes matter.
    for name, size in (("metadata.img", 64 * MIB), ("misc.img", 1 * MIB)):
        target = prepared / name
        if not target.exists():
            with target.open("wb") as handle:
                handle.truncate(size)

    extracted_names = [
        name
        for name in bundle["required_members"]
        if name not in ("super.img", "userdata.img")
    ]
    for name in extracted_names:
        if not (inputs / name).exists():
            extract_members(archive, members, [name], inputs)
        if name.startswith("vbmeta"):
            check_avb(inputs / name)

    sizes = {name: member.size for name, member in members.items()}
    for name in ("super.img", "userdata.img"):
        with zipfile.ZipFile(archive) as zf, zf.open(members[name].info) as stream:
            header = sparse_header_from_stream(stream, name)
            sizes[name] = header.expanded_size
    with zipfile.ZipFile(archive) as zf, zf.open(members["super.img"].info) as stream:
        super_info = check_lp_metadata(sparse_prefix(stream, 12288 + 1 * MIB))
    partitions = plan_partitions(bundle, sizes)
    disk_path = prepared / "android.img"
    if disk_path.exists():
        raise ImageError(f"refusing to overwrite an existing prepared disk {disk_path}")
    write_gpt(disk_path, bundle, partitions, archive, members, floor)

    # Verify the prepared files and freeze their identity.
    artifacts: dict[str, dict] = {}
    for name in (
        "kernel",
        "initrd.img",
        "bootconfig.txt",
        "cmdline.txt",
        "android.img",
    ):
        target = prepared / name
        artifacts[name] = {
            "path": str(target),
            "size": target.stat().st_size,
            "allocated_bytes": target.stat().st_blocks * 512,
            "sha256": sha256_file(target),
        }
    input_hashes = {}
    for name in sorted(extracted_names):
        target = inputs / name
        input_hashes[name] = {
            "size": target.stat().st_size,
            "sha256": sha256_file(target),
        }

    args, rendered = render_qemu_args(bundle, prepared, cmdline, memory_mb)
    run_config = {
        "schema_version": 1,
        "qemu": str(qemu.resolve()),
        "args": args,
        "timeout_seconds": timeout_seconds,
        "input_paths": [
            str((prepared / name).resolve())
            for name in ("kernel", "initrd.img", "bootconfig.txt", "android.img")
        ],
        "hashes": {
            str((prepared / name).resolve()): artifacts[name]["sha256"]
            for name in ("kernel", "initrd.img", "bootconfig.txt", "android.img")
        },
        "evidence_root": str((ROOT / "build" / "android" / "boot-runs").resolve()),
        "stop_on_completion": True,
    }
    (prepared / "run-config.json").write_text(json.dumps(run_config, indent=2) + "\n")

    layout = {
        "sector_size": SECTOR,
        "disk_size": disk_path.stat().st_size,
        "allocated_bytes": disk_path.stat().st_blocks * 512,
        "partitions": [
            {
                "label": partition.label,
                "offset": partition.offset,
                "size": partition.size,
                "last_lba": partition.last_lba,
                "source": partition.source,
            }
            for partition in partitions
        ],
    }
    (prepared / "disk-layout.json").write_text(json.dumps(layout, indent=2) + "\n")

    receipt = {
        "schema": "aster.android.prepared/v1",
        "bundle": {
            "id": bundle["id"],
            "branch": bundle["branch"],
            "target": bundle["target"],
            "build_id": bundle["build_id"],
            "build_fingerprint": bundle["build_fingerprint"],
            "android_release": bundle["android_release"],
            "kernel_version": bundle["kernel"]["version"],
        },
        "source": {
            "archive": str(archive),
            "size": archive.stat().st_size,
            "sha256": bundle["archive"]["sha256"],
            "md5_base64": bundle["archive"]["md5_base64"],
            "crc32c_base64": bundle["archive"]["crc32c_base64"],
            "etag": bundle["archive"]["etag"],
            "page_url": bundle["archive"]["page_url"],
        },
        "prepared_root": str(prepared),
        "artifacts": artifacts,
        "inputs": input_hashes,
        "boot": {
            "kernel_cmdline": cmdline,
            "bootconfig": str(prepared / "bootconfig.txt"),
            "bootconfig_text": config.decode("utf-8"),
            "initrd": str(prepared / "initrd.img"),
            "ramdisk_order": "vendor_boot_then_init_boot",
            "selinux": selinux,
            "memory_mb": memory_mb,
        },
        "super": super_info,
        "disk": {
            "path": str(disk_path),
            "size": layout["disk_size"],
            "allocated_bytes": layout["allocated_bytes"],
            "layout": str(prepared / "disk-layout.json"),
            "partitions": layout["partitions"],
        },
        "qemu": {
            "binary": str(qemu),
            "args": args,
            "rendered": rendered,
        },
        "run_config": str(prepared / "run-config.json"),
        "budget": {
            "archive_downloaded_bytes": archive.stat().st_size,
            "prepared_allocated_bytes": sum(
                item["allocated_bytes"] for item in artifacts.values()
            ),
        },
        "blockers": bundle["blockers"],
        "license": bundle["license"],
        "cuttlefish_reference": CUTTLEFISH_REFERENCE,
        "limits": {
            "real_boot_executed": False,
            "android_boot_claimed": False,
            "qemu_started": False,
        },
    }
    (prepared / "prepared.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def read_member_bytes(
    archive: Path, member: ArchiveMember, offset: int, size: int
) -> bytes:
    if offset < 0 or size < 0 or offset + size > member.size:
        raise ImageError(f"requested range for {member.name} is outside the member")
    with zipfile.ZipFile(archive) as zf, zf.open(member.info) as stream:
        stream.seek(offset)
        data = stream.read(size)
    if len(data) != size:
        raise ImageError(f"short read from {member.name}")
    return data


def parse_boot_image_from_zip(
    archive: Path, member: ArchiveMember, require_kernel: bool = True
) -> BootImage:
    data = read_member_bytes(archive, member, 0, 4096)
    with zipfile.ZipFile(archive) as zf, zf.open(member.info) as stream:
        if data[:8] != BOOT_MAGIC:
            raise ImageError(f"{member.name} has no Android boot magic")
        kernel_size, ramdisk_size = struct.unpack_from("<II", data, 8)
        header_size = struct.unpack_from("<I", data, 20)[0]
        version = struct.unpack_from("<I", data, 40)[0]
        if version != 4 or header_size != 1584:
            raise ImageError(f"{member.name} is not Android boot v4")
        if require_kernel and kernel_size == 0:
            raise ImageError(f"{member.name} has no kernel")
        if not require_kernel and ramdisk_size == 0:
            raise ImageError(f"{member.name} has no generic ramdisk")
        if kernel_size:
            stream.seek(4096 + 56)
            if stream.read(4) != ARM64_IMAGE_MAGIC:
                raise ImageError(f"{member.name} kernel is not an ARM64 Image")
        ramdisk_offset = 4096 + ((kernel_size + 4095) // 4096) * 4096
        if ramdisk_size:
            stream.seek(ramdisk_offset)
            if stream.read(4) not in (b"\x02\x21\x4c\x18", b"\x1f\x8b", b"0707"):
                raise ImageError(f"{member.name} ramdisk has an unsupported format")
        cmdline = (
            data[44 : 44 + 1536].split(b"\0", 1)[0].decode("utf-8", errors="replace")
        )
        return BootImage(
            archive / member.name,
            version,
            header_size,
            kernel_size,
            ramdisk_size,
            cmdline,
        )


def parse_vendor_boot_from_zip(archive: Path, member: ArchiveMember) -> VendorBootImage:
    data = read_member_bytes(archive, member, 0, 4096)
    if data[:8] != VENDOR_BOOT_MAGIC:
        raise ImageError(f"{member.name} has no vendor boot magic")
    version, page_size, _kernel_addr, _ramdisk_addr, ramdisk_size = struct.unpack_from(
        "<IIIII", data, 8
    )
    if version != 4:
        raise ImageError(f"{member.name} is not vendor boot v4")
    header_size = struct.unpack_from("<I", data, 2096)[0]
    dtb_size = struct.unpack_from("<I", data, 2100)[0]
    table_size, entry_count, entry_size, bootconfig_size = struct.unpack_from(
        "<IIII", data, 2112
    )
    if header_size != 2128 or entry_size < 108:
        raise ImageError(f"{member.name} has an invalid v4 header")
    ramdisk_offset = _align(header_size, page_size)
    dtb_offset = _align(ramdisk_offset + ramdisk_size, page_size)
    table_offset = _align(dtb_offset + dtb_size, page_size)
    bootconfig_offset = _align(table_offset + table_size, page_size)
    with zipfile.ZipFile(archive) as zf, zf.open(member.info) as stream:
        stream.seek(table_offset)
        table = stream.read(table_size)
        stream.seek(bootconfig_offset)
        bootconfig = stream.read(bootconfig_size)
    if len(table) != table_size or len(bootconfig) != bootconfig_size:
        raise ImageError(f"{member.name} table or bootconfig is truncated")
    ramdisks: list[VendorRamdisk] = []
    for index in range(entry_count):
        offset = index * entry_size
        size, data_offset, ramdisk_type = struct.unpack_from("<III", table, offset)
        name = (
            table[offset + 12 : offset + 44]
            .split(b"\0", 1)[0]
            .decode("ascii", errors="replace")
        )
        if data_offset + size > ramdisk_size:
            raise ImageError(f"{member.name} ramdisk {index} is out of bounds")
        ramdisks.append(VendorRamdisk(name, ramdisk_type, data_offset, size))
    cmdline = data[28 : 28 + 2048].split(b"\0", 1)[0].decode("utf-8", errors="replace")
    return VendorBootImage(
        archive / member.name,
        version,
        page_size,
        ramdisk_size,
        tuple(ramdisks),
        bootconfig,
        cmdline,
        dtb_size,
        ramdisk_offset,
    )


def render_qemu_args(
    bundle: dict, prepared: Path, cmdline: str, memory_mb: int
) -> tuple[list[str], str]:
    replacements = {
        "{kernel}": str((prepared / "kernel").resolve()),
        "{initrd}": str((prepared / "initrd.img").resolve()),
        "{disk}": str((prepared / "android.img").resolve()),
        "{cmdline}": cmdline,
        "{memory_mb}": str(memory_mb),
    }
    args: list[str] = []
    for value in bundle["qemu"]["args_template"]:
        for key, replacement in replacements.items():
            value = value.replace(key, replacement)
        args.append(value)
    rendered = " ".join(args)
    return args, rendered


# ---------------------------------------------------------------------------
# CLI


def cmd_list(args) -> int:
    manifest = load_manifest()
    for bundle in manifest["bundles"]:
        archive = bundle["archive"]
        print(f"{bundle['id']}: {bundle['target']} build {bundle['build_id']}")
        print(
            f"  Android {bundle['android_release']} (SDK {bundle['android_sdk']}), kernel {bundle['kernel']['version']}"
        )
        print(
            f"  archive {archive['artifact']} {human_bytes(archive['size'])} sha256 {archive['sha256']}"
        )
    return 0


def cmd_verify(args) -> int:
    manifest = load_manifest()
    bundle = bundle_by_id(manifest, args.bundle)
    archive = bundle_paths(bundle)["archive"]
    if not archive.exists():
        raise ImageError(f"archive is absent: {archive}; run fetch first")
    if not archive_sha256_matches(archive, bundle):
        raise ImageError("archive does not match the pinned size, SHA-256 or MD5")
    info = inspect_bundle(bundle, archive)
    print(json.dumps(info, indent=2))
    if args.prepared:
        receipt_path = bundle_paths(bundle)["prepared"] / "prepared.json"
        receipt = read_json(receipt_path)
        for name, recorded in receipt["artifacts"].items():
            path = Path(recorded["path"])
            if not path.is_file():
                raise ImageError(f"prepared artifact is missing: {path}")
            if (
                path.stat().st_size != recorded["size"]
                or sha256_file(path) != recorded["sha256"]
            ):
                raise ImageError(
                    f"prepared artifact does not match its receipt: {path}"
                )
        print("prepared artifacts verified")
    return 0


def cmd_fetch(args) -> int:
    manifest = load_manifest()
    bundle = bundle_by_id(manifest, args.bundle)
    destination = bundle_paths(bundle)["archive"]
    floor = manifest["budgets"]["free_floor_bytes"]
    fetch_archive(bundle, destination, floor)
    print(destination)
    print(bundle["archive"]["sha256"])
    return 0


def cmd_inspect(args) -> int:
    manifest = load_manifest()
    bundle = bundle_by_id(manifest, args.bundle)
    archive = bundle_paths(bundle)["archive"]
    if not archive.exists():
        raise ImageError(f"archive is absent: {archive}; run fetch first")
    print(json.dumps(inspect_bundle(bundle, archive), indent=2))
    return 0


def cmd_prepare(args) -> int:
    manifest = load_manifest()
    bundle = bundle_by_id(manifest, args.bundle)
    archive = bundle_paths(bundle)["archive"]
    floor = manifest["budgets"]["free_floor_bytes"]
    if not archive.exists():
        if not args.fetch:
            raise ImageError(
                f"archive is absent: {archive}; use --fetch or fetch first"
            )
        fetch_archive(bundle, archive, floor)
    if not archive_sha256_matches(archive, bundle):
        raise ImageError("archive does not match the pinned size, SHA-256 or MD5")
    qemu = args.qemu.resolve()
    if not qemu.is_file() or not os.access(qemu, os.X_OK):
        raise ImageError(f"QEMU binary is not an executable file: {qemu}")
    if args.memory_mb < 1024 or args.memory_mb > 8192:
        raise ImageError("--memory-mb must be between 1024 and 8192")
    receipt = prepare_bundle(
        bundle, archive, qemu, args.memory_mb, args.selinux, args.timeout
    )
    print(receipt["prepared_root"])
    for name, item in receipt["artifacts"].items():
        print(
            f"  {name}: {human_bytes(item['size'])} (allocated {human_bytes(item['allocated_bytes'])}) sha256 {item['sha256']}"
        )
    print(f"  run config: {receipt['run_config']}")
    print(
        f"  disk: {human_bytes(receipt['disk']['size'])} logical, {human_bytes(receipt['disk']['allocated_bytes'])} allocated"
    )
    return 0


def cmd_status(args) -> int:
    manifest = load_manifest()
    for bundle in manifest["bundles"]:
        paths = bundle_paths(bundle)
        print(f"{bundle['id']}")
        print(
            f"  archive: {paths['archive']} {'present' if paths['archive'].exists() else 'absent'}"
        )
        receipt = paths["prepared"] / "prepared.json"
        print(
            f"  prepared: {paths['prepared']} {'present' if receipt.exists() else 'absent'}"
        )
        if receipt.exists():
            data = read_json(receipt)
            print(f"  run config: {data['run_config']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    global MANIFEST_PATH
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list pinned bundles")

    verify = sub.add_parser(
        "verify", help="verify the archive, members and optionally prepared files"
    )
    verify.add_argument("--bundle", required=True)
    verify.add_argument("--prepared", action="store_true")

    fetch = sub.add_parser("fetch", help="download the pinned archive")
    fetch.add_argument("--bundle", required=True)

    inspect = sub.add_parser("inspect", help="inspect formats and dynamic partitions")
    inspect.add_argument("--bundle", required=True)

    prepare = sub.add_parser(
        "prepare", help="prepare kernel, initrd, bootconfig and GPT disk"
    )
    prepare.add_argument("--bundle", required=True)
    prepare.add_argument(
        "--fetch", action="store_true", help="download the archive if absent"
    )
    prepare.add_argument("--qemu", type=Path, default=DEFAULT_QEMU)
    prepare.add_argument("--memory-mb", type=int, default=4096)
    prepare.add_argument(
        "--selinux", choices=("permissive", "enforcing"), default="permissive"
    )
    prepare.add_argument("--timeout", type=int, default=1800)

    sub.add_parser("status", help="show local archive and prepared state")

    args = parser.parse_args(argv)
    MANIFEST_PATH = args.manifest
    try:
        if args.command == "list":
            return cmd_list(args)
        if args.command == "verify":
            return cmd_verify(args)
        if args.command == "fetch":
            return cmd_fetch(args)
        if args.command == "inspect":
            return cmd_inspect(args)
        if args.command == "prepare":
            return cmd_prepare(args)
        if args.command == "status":
            return cmd_status(args)
        raise AssertionError("unreachable")
    except ImageError as error:
        print(f"images: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("images: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

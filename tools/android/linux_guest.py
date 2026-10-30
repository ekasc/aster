#!/usr/bin/env python3
"""Prepare an Alpine Linux guest on the Android GKI kernel.

Why this exists: the Cuttlefish Android userspace needs host-side HAL services
over vsock, and this host's QEMU build has no vsock (see preflight.py). A Linux
guest is the supported fallback path in docs/phase2-plan.md: it gives a real
kernel with Binder, a writable userspace, and a place to run an Android
container later without any host daemon.

This module only prepares and describes inputs. It never boots anything.

Inputs are pinned by SHA-256:
  * Alpine minirootfs (userspace)
  * the already-prepared Android GKI kernel and vendor module archive
"""

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "build/android/linux-guest"
MINIROOTFS_SHA256 = "TO_BE_PINNED_ON_FIRST_FETCH"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def safe_members(archive):
    for member in archive.getmembers():
        name = member.name
        if name.startswith("/") or ".." in Path(name).parts:
            raise ValueError(f"unsafe archive path: {name}")
        if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
            raise ValueError(f"unsupported archive entry: {name}")
        yield member


def read_cpio(data):
    offset = 0
    while offset + 110 <= len(data):
        header = data[offset : offset + 110]
        if header[:6] != b"070701":
            raise ValueError("expected newc cpio")
        fields = [int(header[i : i + 8], 16) for i in range(6, 110, 8)]
        size, name_size = fields[6], fields[11]
        name = data[offset + 110 : offset + 110 + name_size - 1].decode()
        cursor = (offset + 110 + name_size + 3) & ~3
        if name == "TRAILER!!!":
            return
        yield name, fields[1], data[cursor : cursor + size]
        offset = (cursor + size + 3) & ~3


def newc(entries):
    out = bytearray()
    for inode, (name, mode, data) in enumerate(entries, 1):
        encoded = name.encode() + b"\0"
        fields = [inode, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(encoded), 0]
        out += b"070701" + b"".join(f"{value:08x}".encode() for value in fields)
        out += encoded
        out += b"\0" * (-len(out) % 4)
        out += data
        out += b"\0" * (-len(out) % 4)
    return bytes(out)


def minirootfs_entries(path):
    """Turn an Alpine minirootfs tarball into newc entries rooted at /newroot."""
    with tarfile.open(path) as archive:
        for member in safe_members(archive):
            name = "newroot/" + member.name.rstrip("/")
            if member.isdir():
                yield name, 0o040755, b""
            elif member.issym():
                yield name, 0o120777, member.linkname.encode()
            elif member.islnk():
                yield name, 0o100644, b""
            else:
                handle = archive.extractfile(member)
                data = handle.read() if handle else b""
                yield name, 0o100755 if member.mode & 0o100 else 0o100644, data


def build_initramfs(minirootfs, modules, init_script):
    entries = [
        ("bin", 0o040755, b""),
        ("newroot", 0o040755, b""),
        ("newroot/proc", 0o040755, b""),
        ("newroot/sys", 0o040755, b""),
        ("newroot/dev", 0o040755, b""),
    ]
    entries.extend(minirootfs_entries(minirootfs))
    for name, data in modules.items():
        entries.append(("modules/" + name, 0o100644, data))
    entries.append(("init", 0o100755, init_script.read_bytes()))
    entries.append(("TRAILER!!!", 0, b""))
    return gzip.compress(newc(entries), mtime=0)


def module_archive_entries(path):
    """Modules from the Android vendor ramdisk (lz4), keyed by basename."""
    import subprocess

    raw = subprocess.run(
        ["lz4", "-dc", str(path)], check=True, capture_output=True
    ).stdout
    wanted = (
        "virtio_mmio.ko",
        "virtio_blk.ko",
        "virtio_pci.ko",
        "virtio_rng.ko",
        "virtio_input.ko",
        "virtio-gpu.ko",
        "virtio_console.ko",
    )
    found = {}
    for name, _mode, data in read_cpio(raw):
        base = name.rsplit("/", 1)[-1]
        if base in wanted and base not in found:
            found[base] = data
    missing = [name for name in wanted if name not in found and name != "virtio_pci.ko"]
    if missing:
        raise ValueError(f"module archive lacks {missing}")
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minirootfs", type=Path, required=True)
    parser.add_argument(
        "--modules",
        type=Path,
        required=True,
        help="vendor_boot ramdisk (lz4) that carries lib/modules",
    )
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument(
        "--init", type=Path, default=ROOT / "tools/android/linux_guest_init.sh"
    )
    parser.add_argument("--output", type=Path, default=WORK)
    parser.add_argument("--memory-mb", type=int, default=1024)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--qemu", type=Path, default=ROOT / "build/qemu/build-tci5/qemu-system-aarch64"
    )
    args = parser.parse_args()
    if MINIROOTFS_SHA256 != "TO_BE_PINNED_ON_FIRST_FETCH":
        if sha256(args.minirootfs) != MINIROOTFS_SHA256:
            parser.error("minirootfs does not match the pinned hash")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    modules = module_archive_entries(args.modules)
    initramfs = output / "initramfs.gz"
    initramfs.write_bytes(build_initramfs(args.minirootfs, modules, args.init))
    config = {
        "schema_version": 1,
        "qemu": str(args.qemu.resolve()),
        "args": [
            "-M",
            "virt,gic-version=3",
            "-cpu",
            "max",
            "-accel",
            "tcg,thread=single,tb-size=32",
            "-smp",
            "1",
            "-m",
            str(args.memory_mb),
            "-nodefaults",
            "-display",
            "none",
            "-monitor",
            "none",
            "-serial",
            "stdio",
            "-nic",
            "none",
            "-no-reboot",
            "-snapshot",
            "-kernel",
            str(args.kernel.resolve()),
            "-initrd",
            str(initramfs),
            "-append",
            "console=ttyAMA0 rdinit=/init panic=-1",
        ],
        "timeout_seconds": args.timeout,
        "stop_on_completion": False,
        "input_paths": [str(args.kernel.resolve()), str(initramfs)],
        "hashes": {
            str(args.kernel.resolve()): sha256(args.kernel),
            str(initramfs): sha256(initramfs),
        },
        "evidence_root": str(output / "runs"),
    }
    (output / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    print(
        json.dumps(
            {
                "initramfs": str(initramfs),
                "modules": sorted(modules),
                "config": str(output / "config.json"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

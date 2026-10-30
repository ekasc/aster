#!/usr/bin/env python3
"""Boot a pinned Android kernel with diagnostic Linux userspace, not Android."""

import argparse
import base64
import gzip
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "build/android"
ARTIFACTS = {
    "Image.gz": (
        "kernel/prebuilts/6.6/arm64",
        "6104d37087c872b8183f8483ba32e3bc165d3517/kernel-6.6-gz",
        "994b3aeb16185a45d59219eae4e7de945d420f788882b98ff77ea1ac2e731d71",
    ),
    "modules-initramfs.img": (
        "kernel/prebuilts/common-modules/virtual-device/6.6/arm64",
        "4ac926c8fe96fd75b5c036e9a7c517cb01151f81/initramfs.img",
        "77fb5081ba4a94d0cbc206c041eb195ecaf4654bc6f32d50c7b265defad27713",
    ),
}
MODULES = ("virtio_mmio.ko", "virtio_blk.ko", "virtio-rng.ko", "virtio_input.ko")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def artifacts(fetch):
    for name, (repo, revision, expected) in ARTIFACTS.items():
        path = WORK / name
        if not path.exists():
            if not fetch:
                raise ValueError(f"Missing {path}; use --fetch for pinned downloads")
            url = f"https://android.googlesource.com/{repo}/+/{revision}?format=TEXT"
            with urllib.request.urlopen(url, timeout=120) as response:
                encoded = response.read(64 * 1024 * 1024 + 1)
            if len(encoded) > 64 * 1024 * 1024:
                raise ValueError("Artifact exceeds 64 MiB encoded download limit")
            data = base64.b64decode(encoded, validate=True)
            if digest(data) != expected:
                raise ValueError(f"Checksum mismatch for {url}")
            path.write_bytes(data)
        if digest(path.read_bytes()) != expected:
            raise ValueError(f"Checksum mismatch for {path}")


def read_cpio(data):
    offset = 0
    while offset + 110 <= len(data):
        header = data[offset : offset + 110]
        if header[:6] != b"070701":
            raise ValueError("Expected newc initramfs")
        fields = [int(header[i : i + 8], 16) for i in range(6, 110, 8)]
        size, namesize = fields[6], fields[11]
        name = data[offset + 110 : offset + 110 + namesize - 1].decode()
        offset = (offset + 110 + namesize + 3) & ~3
        if name == "TRAILER!!!":
            return
        yield name, data[offset : offset + size]
        offset = (offset + size + 3) & ~3


def newc(entries):
    output = bytearray()
    for inode, (name, mode, data, major, minor) in enumerate(entries, 1):
        encoded = name.encode() + b"\0"
        fields = [
            inode,
            mode,
            0,
            0,
            1,
            0,
            len(data),
            0,
            0,
            major,
            minor,
            len(encoded),
            0,
        ]
        output.extend(b"070701" + b"".join(f"{value:08x}".encode() for value in fields))
        output.extend(encoded)
        output.extend(b"\0" * (-len(output) % 4))
        output.extend(data)
        output.extend(b"\0" * (-len(output) % 4))
    return bytes(output)


def probe_passed(output, returncode, timed_out):
    lines = output.splitlines()
    required = (
        "ASTER_BINDERFS_DEVICES_OK",
        "ASTER_SELINUXFS_OK",
        "ASTER_VIRTIO_BLOCK_READ_OK",
        "ASTER_VIRTIO_RNG_AND_INPUT_ENUMERATED",
        "ASTER_KERNEL_PROBE_PASS",
    )
    return (
        not timed_out
        and returncode == 0
        and "ASTER_KERNEL_PROBE_FAIL" not in lines
        and all(marker in lines for marker in required)
    )


def prepare(alpine):
    kernel = gzip.decompress((WORK / "Image.gz").read_bytes())
    if kernel[56:60] != b"ARM\x64":
        raise ValueError("Not an ARM64 Linux Image")
    start = kernel.index(b"IKCFG_ST") + 8
    config = gzip.decompress(kernel[start : kernel.index(b"IKCFG_ED", start)])
    required = [
        "ANDROID_BINDER_IPC",
        "ANDROID_BINDERFS",
        "SECURITY_SELINUX",
        "SERIAL_AMBA_PL011_CONSOLE",
        "BLK_DEV_INITRD",
        "RD_GZIP",
    ]
    for option in required:
        if f"CONFIG_{option}=y\n".encode() not in config:
            raise ValueError(f"Missing built-in {option}")
    (WORK / "Image").write_bytes(kernel)
    (WORK / "kernel.config").write_bytes(config)
    alpine_files = dict(read_cpio(gzip.decompress(alpine.read_bytes())))
    module_archive = subprocess.run(
        ["lz4", "-dc", str(WORK / "modules-initramfs.img")],
        check=True,
        capture_output=True,
    ).stdout
    module_files = dict(read_cpio(module_archive))
    entries = [
        (name, 0o40755, b"", 0, 0)
        for name in ("bin", "lib", "modules", "proc", "sys", "dev")
    ]
    for name in ("bin/busybox", "lib/ld-musl-aarch64.so.1"):
        data = alpine_files[name]
        if data[:4] != b"\x7fELF" or struct.unpack_from("<H", data, 18)[0] != 183:
            raise ValueError(f"Expected ARM64 ELF for {name}")
        entries.append((name, 0o100755, data, 0, 0))
    for module in MODULES:
        matches = [
            value for name, value in module_files.items() if name.endswith("/" + module)
        ]
        if len(matches) != 1:
            raise ValueError(f"Expected one {module}")
        entries.append(("modules/" + module, 0o100644, matches[0], 0, 0))
    entries.extend(
        [
            ("dev/console", 0o20600, b"", 5, 1),
            ("dev/null", 0o20666, b"", 1, 3),
            (
                "init",
                0o100755,
                (Path(__file__).parent / "probe-init.sh").read_bytes(),
                0,
                0,
            ),
            ("TRAILER!!!", 0, b"", 0, 0),
        ]
    )
    (WORK / "probe.cpio.gz").write_bytes(gzip.compress(newc(entries), mtime=0))
    disk = b"ASTER_ANDROID_KERNEL_BLOCK_PROBE\n".ljust(1024 * 1024, b"\0")
    (WORK / "probe.raw").write_bytes(disk)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument(
        "--qemu", type=Path, default=ROOT / "build/qemu/build-tci5/qemu-system-aarch64"
    )
    parser.add_argument(
        "--alpine-initramfs",
        type=Path,
        default=ROOT / "build/qemu/images/initramfs-virt",
    )
    args = parser.parse_args()
    if not 1 <= args.timeout <= 600:
        parser.error("--timeout must be between 1 and 600 seconds")
    WORK.mkdir(parents=True, exist_ok=True)
    artifacts(args.fetch)
    prepare(args.alpine_initramfs)
    if args.prepare_only:
        return
    run = Path(tempfile.mkdtemp(prefix="run-", dir=WORK))
    command = [
        str(args.qemu.resolve()),
        "-M",
        "virt,gic-version=3",
        "-cpu",
        "max",
        "-accel",
        "tcg,thread=single",
        "-smp",
        "1",
        "-m",
        "768",
        "-display",
        "none",
        "-monitor",
        "none",
        "-serial",
        "stdio",
        "-nic",
        "none",
        "-no-reboot",
        "-kernel",
        str(WORK / "Image"),
        "-initrd",
        str(WORK / "probe.cpio.gz"),
        "-append",
        "console=ttyAMA0 earlycon=pl011,0x09000000 rdinit=/init panic=-1",
        "-global",
        "virtio-mmio.force-legacy=false",
        "-drive",
        f"if=none,id=probe,format=raw,readonly=on,file={WORK / 'probe.raw'}",
        "-device",
        "virtio-blk-device,drive=probe",
        "-device",
        "virtio-rng-device",
        "-device",
        "virtio-keyboard-device",
    ]
    metadata = {
        "command": command,
        "qemu_sha256": digest(args.qemu.read_bytes()),
        "qemu_version": subprocess.check_output(
            [str(args.qemu), "--version"], text=True
        ),
        "alpine_initramfs_sha256": digest(args.alpine_initramfs.read_bytes()),
        "inputs": {
            name: digest((WORK / name).read_bytes())
            for name in ("Image", "probe.cpio.gz", "probe.raw", *ARTIFACTS)
        },
        "timeout_seconds": args.timeout,
    }
    config_header = args.qemu.parent / "config-host.h"
    metadata["tcg_interpreter_build_header"] = (
        "#define CONFIG_TCG_INTERPRETER" in config_header.read_text()
        if config_header.exists()
        else None
    )
    (run / "command.json").write_text(json.dumps(metadata, indent=2) + "\n")
    start = time.monotonic()
    timed_out = False
    with (run / "serial.log").open("wb") as log:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT
        )
        try:
            process.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    output = (run / "serial.log").read_text(errors="replace")
    passed = probe_passed(output, process.returncode, timed_out)
    result = {
        "passed": passed,
        "android_booted": False,
        "timed_out": timed_out,
        "exit_code": process.returncode,
        "elapsed_seconds": round(time.monotonic() - start, 3),
        "pid": process.pid,
        "process_reaped": process.poll() is not None,
    }
    (run / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(run)
    print(json.dumps(result))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()

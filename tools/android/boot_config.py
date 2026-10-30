#!/usr/bin/env python3
"""Prepare a direct-kernel QEMU virt boot of the pinned Cuttlefish bundle.

This is a host configuration, not a claim of Android compatibility. It keeps
the upstream hvc0..16 port numbers and uses guest SwiftShader with 2D virtio-gpu.
No host GPU acceleration, TCP ADB, or external Cuttlefish HAL services are added.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import struct

ROOT = Path(__file__).resolve().parents[2]
MAGIC = b"#BOOTCONFIG\n"
UPSTREAM = "https://android.googlesource.com/device/google/cuttlefish/+/283645aacf6cdb56cc31a9362f54d412dbc132a1/host/libs/vm_manager/qemu_manager.cpp"


def parse_properties(text):
    properties = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(
            r'([a-zA-Z0-9_.-]+)\s*=\s*(?:"([^"\r\n]*)"|([^\s";]+))\s*;?', line
        )
        if not match:
            raise ValueError(f"Unsupported bootconfig syntax: {line!r}")
        key, quoted, bare = match.groups()
        value = quoted if quoted is not None else bare
        if key in properties:
            raise ValueError(f"Duplicate bootconfig key: {key}")
        properties[key] = value
    return properties


def append_bootconfig(ramdisk, properties):
    if ramdisk.endswith(MAGIC):
        raise ValueError("Input already has a bootconfig trailer")
    lines = []
    for key, value in sorted(properties.items()):
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", key) or any(
            c in value for c in '\x00\r\n"\\'
        ):
            raise ValueError("Invalid bootconfig property")
        lines.append(f'{key} = "{value}"\n')
    data = "".join(lines).encode() + b"\x00"
    return (
        ramdisk + data + struct.pack("<II", len(data), sum(data) & 0xFFFFFFFF) + MAGIC
    )


def strip_bootconfig(ramdisk):
    if not ramdisk.endswith(MAGIC):
        return ramdisk
    if len(ramdisk) < 20:
        raise ValueError("Truncated bootconfig trailer")
    size, checksum = struct.unpack("<II", ramdisk[-20:-12])
    if size > len(ramdisk) - 20:
        raise ValueError("Invalid bootconfig length")
    payload = ramdisk[-20 - size : -20]
    if sum(payload) & 0xFFFFFFFF != checksum:
        raise ValueError("Invalid bootconfig checksum")
    return ramdisk[: -20 - size]


def arguments(
    kernel, ramdisk, disk, console, ram_mib, vendor_cmdline="", kernel_audit="1"
):
    extras = " ".join(
        word
        for word in shlex.split(vendor_cmdline)
        if not word.startswith(("androidboot.", "console=", "earlycon=", "panic="))
        and not word.startswith("audit=")
        and word != "bootconfig"
    )
    args = [
        "-M",
        "virt,gic-version=3",
        "-cpu",
        "max",
        "-accel",
        "tcg,thread=single,tb-size=32",
        "-smp",
        "1",
        "-m",
        str(ram_mib),
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
        str(kernel),
        "-initrd",
        str(ramdisk),
        "-append",
        "console=ttyAMA0 earlycon=pl011,mmio32,0x09000000 "
        + extras
        + f" audit={kernel_audit}"
        + " bootconfig androidboot.aster_nonce={nonce} panic=-1",
        "-device",
        "virtio-gpu-pci,disable-legacy=on,xres=480,yres=800",
        "-device",
        "virtio-serial-pci-non-transitional,id=virtio-serial,max_ports=31",
    ]
    for port in range(17):
        if port == 1:
            chardev = f"socket,id=hvc{port},path={console},server=on,wait=off"
        elif port == 2:
            chardev = "file,id=hvc2,path={evidence_dir}/android.log"
        else:
            chardev = f"null,id=hvc{port}"
        args += [
            "-chardev",
            chardev,
            "-device",
            f"virtconsole,bus=virtio-serial.0,chardev=hvc{port}",
        ]
    args += [
        "-drive",
        f"if=none,id=os,format=raw,file={disk}",
        "-device",
        "virtio-blk-pci-non-transitional,drive=os,bootindex=1",
        "-device",
        "virtio-rng-pci-non-transitional",
        "-device",
        "virtio-keyboard-pci,disable-legacy=on",
        "-device",
        "virtio-mouse-pci,disable-legacy=on",
    ]
    return args


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["kernel", "ramdisk", "disk", "base-bootconfig", "output"]:
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument(
        "--qemu", type=Path, default=ROOT / "build/qemu/build-tci5/qemu-system-aarch64"
    )
    parser.add_argument("--ram-mib", type=int, default=1536)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--vendor-cmdline", type=Path)
    parser.add_argument(
        "--kernel-audit",
        choices=["0", "1"],
        default="1",
        help="Disable only for an explicitly recorded audit-overhead experiment",
    )
    parser.add_argument(
        "--disk-sha256",
        help="Expected disk hash, otherwise read adjacent prepared.json",
    )
    args = parser.parse_args()
    if not 768 <= args.ram_mib <= 2048 or not 1 <= args.timeout <= 1800:
        parser.error("RAM must be 768..2048 MiB and timeout 1..1800 seconds")
    inputs = [args.kernel, args.ramdisk, args.disk, args.base_bootconfig]
    for path in inputs:
        if not path.is_file():
            parser.error(f"Missing input: {path}")
    disk_sha256 = args.disk_sha256
    if disk_sha256 is None:
        receipt = args.disk.parent / "prepared.json"
        if not receipt.is_file():
            parser.error("Provide --disk-sha256 or an adjacent prepared.json receipt")
        recorded = json.loads(receipt.read_text())["artifacts"]["android.img"]
        if args.disk.stat().st_size != recorded["size"]:
            parser.error("Disk size differs from the prepared receipt")
        disk_sha256 = recorded["sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", disk_sha256):
        parser.error("Expected disk SHA-256 must be 64 lowercase hex characters")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    properties = parse_properties(args.base_bootconfig.read_text())
    properties.update(
        {
            "androidboot.hardware": "cutf_cvm",
            "androidboot.serialno": "ASTERCF0001",
            "androidboot.boot_devices": "4010000000.pcie",
            "androidboot.slot_suffix": "_a",
            "androidboot.force_normal_boot": "1",
            "androidboot.verifiedbootstate": "orange",
            "androidboot.vbmeta.device_state": "unlocked",
            "androidboot.console": "hvc1",
            "androidboot.serialconsole": "1",
            "androidboot.hardware.gralloc": "minigbm",
            "androidboot.hardware.hwcomposer.display_finder_mode": "drm",
            "androidboot.hardware.hwcomposer.display_framebuffer_format": "rgba",
            "androidboot.hardware.egl": "angle",
            "androidboot.hardware.vulkan": "pastel",
            "androidboot.cpuvulkan.version": "4202496",
            "androidboot.opengles.version": "196609",
            "androidboot.ddr_size": f"{args.ram_mib}MB",
            # The nobody-owned AOSP light HAL defaults to privileged vsock port 0.
            "androidboot.vsock_lights_port": "7000",
            "androidboot.vendor.apex.com.google.emulated.camera.provider.hal": "com.google.emulated.camera.provider.hal",
        }
    )
    ramdisk = output / "initrd.img"
    properties.setdefault("androidboot.hardware.hwcomposer", "ranchu")
    ramdisk.write_bytes(
        append_bootconfig(strip_bootconfig(args.ramdisk.read_bytes()), properties)
    )
    console = output / "console.sock"
    if len(str(console).encode()) >= 104:
        parser.error(
            "macOS UNIX socket path must be shorter than 104 bytes; choose a shorter output directory"
        )
    config = {
        "schema_version": 1,
        "qemu": str(args.qemu.resolve()),
        "args": arguments(
            args.kernel.resolve(),
            ramdisk,
            args.disk.resolve(),
            console,
            args.ram_mib,
            args.vendor_cmdline.read_text() if args.vendor_cmdline else "",
            args.kernel_audit,
        ),
        "timeout_seconds": args.timeout,
        "stop_on_completion": True,
        "input_paths": [
            str(args.kernel.resolve()),
            str(ramdisk),
            str(args.disk.resolve()),
        ],
        "hashes": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [args.kernel.resolve(), ramdisk]
        },
        "evidence_root": str(output / "runs"),
    }
    config["hashes"][str(args.disk.resolve())] = disk_sha256
    (output / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    (output / "boot-properties.json").write_text(
        json.dumps(properties, indent=2) + "\n"
    )
    (output / "configuration-source.txt").write_text(UPSTREAM + "\n")
    print(output / "config.json")
    print("Console:", console)


if __name__ == "__main__":
    main()

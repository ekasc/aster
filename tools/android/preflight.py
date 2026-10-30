#!/usr/bin/env python3
"""Fail fast before spending a boot budget on a capability the host lacks.

The Cuttlefish bundle's HALs require a guest/host transport. This host's QEMU
build has no vsock device and no vhost support, so those HALs abort and the
framework stalls. That cost four long boots to discover once; this check makes
it a two-second answer.

Checks, all read-only:
  * the QEMU binary lists the device names the planned configuration uses;
  * the adjacent config-host.h records vsock/vhost capability;
  * each HAL the bundle needs has a transport that actually exists.
"""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]

REQUIRED_DEVICES = (
    "virtio-gpu-pci",
    "virtio-serial-pci-non-transitional",
    "virtconsole",
    "virtio-blk-pci-non-transitional",
    "virtio-rng-pci-non-transitional",
)

# HALs shipped by this bundle that need host-side services over vsock.
HOST_SERVICE_HALS = ("android.hardware.sensors", "android.hardware.light")


def qemu_devices(qemu):
    try:
        completed = subprocess.run(
            [str(qemu), "-device", "help"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as error:
        return None, f"cannot execute QEMU: {error}"
    names = set(re.findall(r'name "([^"]+)"', completed.stdout))
    return names, None


def host_capabilities(header):
    if not header.is_file():
        return {"header": str(header), "present": False}
    text = header.read_text(errors="replace")

    def enabled(name):
        return bool(re.search(rf"^#define {name} 1\b", text, re.MULTILINE))

    return {
        "header": str(header),
        "present": True,
        "af_vsock": enabled("CONFIG_AF_VSOCK"),
        "vhost": enabled("CONFIG_VHOST"),
        "vhost_vsock": enabled("CONFIG_VHOST_VSOCK"),
    }


def check(qemu, need_host_hal_transport=True):
    devices, error = qemu_devices(qemu)
    findings = {
        "qemu": str(qemu),
        "error": error,
        "missing_devices": [],
        "capabilities": host_capabilities(Path(str(qemu)).with_name("config-host.h")),
        "unserviceable_hals": [],
        "ok": False,
        "warnings": [],
    }
    if devices is None:
        return findings
    findings["missing_devices"] = [
        name for name in REQUIRED_DEVICES if name not in devices
    ]
    findings["vsock_device_present"] = any("vsock" in name for name in devices)
    if need_host_hal_transport and not (
        findings["vsock_device_present"] or findings["capabilities"].get("vhost_vsock")
    ):
        findings["unserviceable_hals"] = list(HOST_SERVICE_HALS)
        findings["warnings"].append(
            "Bundle HALs need a host vsock service; this QEMU build cannot provide one, "
            "so the framework will stall before sys.boot_completed=1."
        )
    findings["ok"] = (
        not findings["missing_devices"] and not findings["unserviceable_hals"]
    )
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--qemu", type=Path, default=ROOT / "build/qemu/build-tci5/qemu-system-aarch64"
    )
    parser.add_argument(
        "--expect",
        action="append",
        default=[],
        help="Require this device name to be present",
    )
    args = parser.parse_args()
    findings = check(args.qemu)
    for name in args.expect:
        if name not in findings.get("devices", []) and findings.get("error") is None:
            devices, _ = qemu_devices(args.qemu)
            if name not in devices:
                findings["missing_devices"].append(name)
                findings["ok"] = False
    print(json.dumps(findings, indent=2))
    return 0 if findings["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

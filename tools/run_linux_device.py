#!/usr/bin/env python3
"""Build and exercise the single-shot system engine on the paired iPhone."""

import argparse
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def collect(command, output, timeout, summary=b"[linux] summary "):
    started = time.monotonic()
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    captured = bytearray()
    try:
        with output.open("wb") as log:
            while time.monotonic() - started < timeout:
                for key, _ in selector.select(timeout=0.25):
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        raise RuntimeError(
                            "Device console closed before the system-engine summary"
                        )
                    captured.extend(block)
                    log.write(block)
                    log.flush()
                if summary in captured:
                    return bytes(captured)
        raise TimeoutError("Device console did not produce a system-engine summary")
    finally:
        selector.close()
        cleanup_requested = process.poll() is None
        if process.poll() is None:
            process.terminate()
        escalated = False
        try:
            tail, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            escalated = True
            process.kill()
            tail, _ = process.communicate()
        with output.open("ab") as log:
            log.write(tail)
        output.with_suffix(".collector.json").write_text(
            json.dumps(
                {
                    "pid": process.pid,
                    "reaped": process.poll() is not None,
                    "cleanup_requested": cleanup_requested,
                    "escalated": escalated,
                    "returncode": process.returncode,
                    "console_signal_forwarding": "devicectl forwards catchable signals to the app",
                },
                indent=2,
            )
            + "\n"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--device",
        default=os.environ.get("ASTER_DEVICE_UDID", "00008150-001C056A3693401C"),
    )
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--test", choices=["boot", "cancel", "timeout"], default="boot")
    args = parser.parse_args()
    bundle = os.environ.get("ASTER_BUNDLE_ID", "dev.ekas.aster")
    evidence = (
        ROOT
        / "build/qemu"
        / ("device-" + args.test + "-" + time.strftime("%Y%m%d-%H%M%S"))
    )
    evidence.mkdir(parents=True)
    if not args.skip_build:
        subprocess.run(["bash", "tools/build_qemu_ios.sh"], cwd=ROOT, check=True)
        subprocess.run(
            ["python3", "tools/gen_xcodeproj.py"],
            cwd=ROOT,
            env=dict(os.environ, ASTER_SYSTEM_ENGINE="1"),
            check=True,
        )
        with (evidence / "build.log").open("wb") as log:
            subprocess.run(
                [
                    "xcodebuild",
                    "-project",
                    "app/Aster.xcodeproj",
                    "-scheme",
                    "Aster",
                    "-configuration",
                    "Release",
                    "-destination",
                    "id=" + args.device,
                    "-derivedDataPath",
                    "build/xcode",
                    "-allowProvisioningUpdates",
                    "build",
                ],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
            )
        subprocess.run(
            [
                "xcrun",
                "devicectl",
                "device",
                "install",
                "app",
                "--device",
                args.device,
                str(ROOT / "build/xcode/Build/Products/Release-iphoneos/Aster.app"),
            ],
            check=True,
        )
    run_id = str(uuid.uuid4())
    command = [
        "xcrun",
        "devicectl",
        "device",
        "process",
        "launch",
        "--device",
        args.device,
        "--console",
        "--terminate-existing",
        bundle,
        "--linux",
        "--linux-run-id=" + run_id,
    ]
    if args.test != "boot":
        command.append("--linux-" + args.test + "-test")
    (evidence / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    collect(command, evidence / "console.log", 210)
    for name in ["linux-serial.log", "linux-result.json"]:
        subprocess.run(
            [
                "xcrun",
                "devicectl",
                "device",
                "copy",
                "from",
                "--device",
                args.device,
                "--domain-type",
                "appDataContainer",
                "--domain-identifier",
                bundle,
                "--source",
                "Documents/" + name,
                "--destination",
                str(evidence / name),
            ],
            check=True,
        )
    result = json.loads((evidence / "linux-result.json").read_text())
    if result.get("run_id") != run_id:
        raise RuntimeError("Measurement belongs to a different app launch")
    print(json.dumps(result, indent=2))
    print("Evidence:", evidence)
    if args.test == "boot":
        return 0 if result["passed"] else 1
    flag = "cancelled" if args.test == "cancel" else "timed_out"
    return (
        0 if result["status"] == 0 and result[flag] == 1 and not result["passed"] else 1
    )


if __name__ == "__main__":

    def interrupted(signum, frame):
        raise KeyboardInterrupt("Console collection interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())

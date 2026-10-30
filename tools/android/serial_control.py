#!/usr/bin/env python3
"""Run the boot validator with a private UNIX-socket Android debug console.

The VM config must attach console_path to the guest's root debug console.
Kernel UART output still goes to validator stdout. Once a shell prompt arrives,
this driver sends guest_gate.sh as a quoted in-memory userdebug su command.
It needs no early-shell temporary directory. The guest reads real Android
properties and Binder service state, then writes evidence to /dev/ttyAMA0.
The raw control stream and exact submitted script are retained separately.
No TCP listener or ADB port is created by this driver.
"""

import argparse
import json
import os
from pathlib import Path
import re
import signal
import shlex
import socket
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
PROMPT = re.compile(rb"(?:^|[\r\n])[^\r\n]{0,100}[:/][^\r\n]{0,80} [#$] ?$")


def guest_command(script):
    return (
        "su 0 /system/bin/sh -c " + shlex.quote(script.decode("utf-8")) + "\n"
    ).encode()


def drive(config, console_path, output, *, timeout, runner=None):
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    if console_path.exists():
        raise ValueError("Refusing a pre-existing console socket")
    runner = runner or [
        sys.executable,
        str(ROOT / "tools/android/run.py"),
        "--config",
        str(config),
    ]
    script = (ROOT / "tools/android/guest_gate.sh").read_bytes()
    command = guest_command(script)
    (output / "submitted-script.txt").write_bytes(command)
    process = None
    connection = None
    connected = False
    submitted = False
    status = None
    started = time.monotonic()
    try:
        with (
            (output / "runner.log").open("wb") as runner_log,
            (output / "control.log").open("wb") as control_log,
        ):
            process = subprocess.Popen(
                runner,
                cwd=ROOT,
                stdout=runner_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            recent = b""
            while process.poll() is None and time.monotonic() - started < timeout:
                if connection is None:
                    candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    candidate.settimeout(0.2)
                    try:
                        candidate.connect(str(console_path))
                    except (FileNotFoundError, ConnectionRefusedError, socket.timeout):
                        candidate.close()
                        time.sleep(0.1)
                        continue
                    connection = candidate
                    connected = True
                try:
                    block = connection.recv(65536)
                except socket.timeout:
                    continue
                if not block:
                    connection.close()
                    connection = None
                    if submitted:
                        break
                    continue
                control_log.write(block)
                control_log.flush()
                recent = (recent + block)[-4096:]
                clean = re.sub(rb"\x1b\[[0-9;?]*[a-zA-Z]", b"", recent)
                if not submitted and PROMPT.search(clean):
                    connection.sendall(command)
                    submitted = True
            remaining = max(0.1, timeout - (time.monotonic() - started))
            try:
                status = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                status = None
    finally:
        if connection is not None:
            connection.close()
        escalated = False
        if process is not None and process.poll() is None:
            # SIGINT lets the Python validator reap QEMU in its exception path.
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                escalated = True
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        record = {
            "runner_pid": None if process is None else process.pid,
            "connected": connected,
            "script_submitted": submitted,
            "runner_returncode": None if process is None else process.returncode,
            "runner_reaped": process is not None and process.poll() is not None,
            "cleanup_escalated": escalated,
            "wall_seconds": time.monotonic() - started,
        }
        if console_path.exists() and stat.S_ISSOCK(console_path.stat().st_mode):
            console_path.unlink()
        record["console_socket_removed"] = not console_path.exists()
        (output / "control-result.json").write_text(json.dumps(record, indent=2) + "\n")
    return 0 if status == 0 and submitted else (status or 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--console", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=930)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 1830:
        parser.error("--timeout must be 1..1830 seconds")
    os.umask(0o077)
    return drive(
        args.config.resolve(),
        args.console.resolve(),
        args.output.resolve(),
        timeout=args.timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())

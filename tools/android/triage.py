#!/usr/bin/env python3
"""Summarise where an Android boot stopped, from one preserved run directory.

Reads the kernel/init serial transcript and the guest logcat stream. It reports
observed facts only: the last framework-related binder lookup, the dominant
system_server wait channels emitted by guest_gate.sh, whether the framework
services are still absent, and any process abort messages. It never declares a
boot successful; only tools/android/run.py owns that verdict.
"""

import argparse
import collections
import gzip
import json
from pathlib import Path
import re

BINDER = re.compile(
    r"servicemanager: Caller\(pid=(\d+),uid=1000,sid=u:r:system_server:s0\) (.*)"
)
DIAG = re.compile(r"ASTER-DIAG\|[0-9a-f]{32}\|\d+\|(\S+)\|(.*)")
WCHAN = re.compile(r"wchan count=(\d+) where=(\S+)")
ABORT = re.compile(r"Abort message: '(.*)'")


def read_text(directory, name):
    plain = directory / name
    if plain.exists():
        return plain.read_text(errors="replace")
    packed = directory / (name + ".gz")
    if packed.exists():
        return gzip.decompress(packed.read_bytes()).decode("utf-8", "replace")
    return ""


def triage(directory):
    serial = read_text(directory, "serial.log")
    logcat = read_text(directory, "android.log")
    system_server_pid = None
    lookups = []
    for line in serial.splitlines():
        match = BINDER.search(line)
        if match:
            system_server_pid = match.group(1)
            lookups.append(match.group(2))
    snapshots = []
    current = None
    for line in serial.splitlines():
        match = DIAG.search(line)
        if not match:
            continue
        tag, value = match.groups()
        if tag == "snapshot_end":
            if current:
                snapshots.append(current)
            current = None
            continue
        if current is None:
            current = {"tag": tag, "summary": None, "wchan": [], "services": {}}
        if value.startswith("uptime="):
            current["summary"] = value
        if value.startswith("service="):
            name = value.split("=", 1)[1].split()[0]
            current["services"][name] = value
        channel = WCHAN.search(value)
        if channel:
            current["wchan"].append((int(channel.group(1)), channel.group(2)))
    if current:
        snapshots.append(current)
    aborts = collections.Counter(match.group(1) for match in ABORT.finditer(logcat))
    activity = [
        line
        for line in serial.splitlines()
        if "'activity' could not be found" in line or "aidl/activity" in line
    ]
    return {
        "system_server_pid": system_server_pid,
        "system_server_binder_lookups": len(lookups),
        "last_binder_lookup": lookups[-1] if lookups else None,
        "activity_unavailable_observations": len(activity),
        "guest_snapshots": snapshots,
        "process_aborts": dict(aborts.most_common(8)),
        "logcat_last_line": (logcat.rstrip().splitlines() or [None])[-1],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(triage(args.directory), indent=2))


if __name__ == "__main__":
    main()

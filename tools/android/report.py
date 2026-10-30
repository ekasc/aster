#!/usr/bin/env python3
"""Reanalyse a stored boot with the current parser without changing its evidence.

Stage timestamps are guest kernel uptime from the original serial lines, not
host elapsed boot latency. Missing timestamps remain null. Process measurements
are copied from the completed run's wait4 record, never inferred from log text.
"""

import argparse
import gzip
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import boot_evidence


def report(directory):
    result = json.loads((directory / "result.json").read_text())
    command = json.loads((directory / "command.json").read_text())
    if result["nonce"] != command["nonce"]:
        raise ValueError("Stale result: command and measurement nonces differ")
    measurement = result["measurement"]
    serial = directory / "serial.log"
    raw = (
        serial.read_bytes()
        if serial.exists()
        else gzip.decompress((directory / "serial.log.gz").read_bytes())
    )
    transcript = raw.decode("utf-8", errors="replace")
    analysis = boot_evidence.analyze(
        transcript,
        result["nonce"],
        timed_out=measurement["timed_out"],
        exit_code=measurement["exit_code"],
        term_signal=measurement["term_signal"],
        runner_stop_requested=measurement["runner_stop_requested"],
    )
    lines = transcript.splitlines()
    stages = {}
    for stage, evidence in analysis.stage_evidence.items():
        line = lines[evidence["line"] - 1]
        timestamp = re.match(r"^\[\s*(\d+\.\d+)\]", line)
        stages[stage] = {
            "line": evidence["line"],
            "serial_text": line,
            "guest_kernel_uptime_seconds": float(timestamp[1]) if timestamp else None,
        }
    return {
        "schema_version": 1,
        "nonce": result["nonce"],
        "framework_verified": analysis.ok,
        "furthest_stage": analysis.furthest_stage,
        "stage_timing_scope": "guest kernel uptime where printed, not host boot latency",
        "stages": stages,
        "measurement": measurement,
        "failures": analysis.failures,
        "diagnostics": analysis.diagnostics,
        "completion": analysis.completion,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(report(args.directory), indent=2))


if __name__ == "__main__":
    main()

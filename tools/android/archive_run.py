#!/usr/bin/env python3
"""Preserve a completed boot attempt, exact compressed logs, and current analysis."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report


def archive(attempt, output):
    runs = list((attempt / "runs").glob("*/result.json"))
    if len(runs) != 1:
        raise ValueError("Expected exactly one completed run in the attempt directory")
    run = runs[0].parent
    analysis = report.report(run)
    output.mkdir(parents=True, exist_ok=False)
    for name in ["command.json", "result.json"]:
        shutil.copyfile(run / name, output / name)
    hashes = {}
    sources = [(run / name, name) for name in ["serial.log", "android.log"]]
    sources += [
        (attempt / "control" / name, name) for name in ["control.log", "runner.log"]
    ]
    for source, name in sources:
        if not source.exists():
            continue
        with source.open("rb") as handle:
            hashes[name] = hashlib.file_digest(handle, "sha256").hexdigest()
        with (
            source.open("rb") as handle,
            (output / (name + ".gz")).open("wb") as destination,
        ):
            with gzip.GzipFile(
                fileobj=destination, mode="wb", mtime=0, filename=""
            ) as compressed:
                shutil.copyfileobj(handle, compressed)
    for name in ["submitted-script.txt", "control-result.json"]:
        source = attempt / "control" / name
        if source.exists():
            shutil.copyfile(source, output / name)
    for name in ["config.json", "boot-properties.json"]:
        source = attempt / name
        if source.exists():
            shutil.copyfile(source, output / name)
    (output / "raw-log-sha256.json").write_text(json.dumps(hashes, indent=2) + "\n")
    (output / "stage-report.json").write_text(json.dumps(analysis, indent=2) + "\n")
    return analysis


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("attempt", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    analysis = archive(args.attempt, args.output)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "framework_verified": analysis["framework_verified"],
            }
        )
    )


if __name__ == "__main__":
    main()

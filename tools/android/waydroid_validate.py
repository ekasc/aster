#!/usr/bin/env python3
"""Validate one Waydroid early-init boot on the host and parse its serial log.

Why this exists: early bootstrap attempts exited before Android init logged
anything, and a serial transcript full of Alpine guest markers is easy to
misread as progress. This runner is the bounded, reproducible check: it boots
exactly the configuration ``linux_guest.py`` produced, keeps the raw transcript,
measures the one child it owns, and then says what the transcript does and does
not prove. It never infers an Android boot from an exit code, from Alpine
markers, or from Android-looking kernel text.

Input contract
--------------
``--config`` is the ``config.json`` written by ``linux_guest.py``::

    {
      "schema_version": 1,
      "qemu": "/absolute/path/to/qemu-system-aarch64",
      "args": ["-serial", "stdio", "-monitor", "none", ...],
      "timeout_seconds": 300,
      "extra_disks": ["build/android/waydroid/system.img"],
      "stop_on_completion": false,
      "input_paths": ["/absolute/path/Image"],
      "hashes": {"/absolute/path/Image": "<64 hex sha256>"},
      "evidence_root": "/absolute/path/runs"
    }

Unknown keys, a non-executable QEMU path, an argument list that would hide the
serial console (reusing ``run.check_console_args``), a bad timeout, unreadable
``extra_disks``, or an input hash mismatch abort before anything is started, so
a failed contract never leaves a run directory behind. ``stop_on_completion``
must be false: this validator waits out the deadline and does not watch the
transcript while the guest runs.

Guest evidence contract
-----------------------
The validator reads two independent sources and never merges them into a
single "looks fine" score.

1. Guest markers written by the init script, one fact per line, anchored at
   column 0 so prose, ``dmesg`` output or a quoted shell line cannot match::

       ASTER-WAYDROID|<key>=<value>

   Keys with defined semantics: ``init_stage`` (one of ``first_stage_init``,
   ``second_stage_init``, ``zygote``, ``system_server``),
   ``prop=<name>=<value>`` (only ``sys.boot_completed=1`` counts),
   ``system_server_pid=<pid>``, ``binderfs_nodes=``/``binder_devices=`` (a
   non-empty list proves the nodes exist), ``android_init_exit=<n>``, and
   ``<device>_mount=failed``. Every other key is retained as an
   unrecognised key, never interpreted.

2. Android init and kernel log text, after stripping a leading ``<N>`` syslog
   level and ``[time][Tpid]`` kernel groups, matched by anchored rules for
   first stage init, second stage init, zygote, system_server and Binder
   activity. A line is classified; nothing is inferred from neighbouring
   lines.

Verdict
-------
``framework_booted`` requires first- and second-stage init, system_server,
explicit ``prop=sys.boot_completed=1``, ActivityManager found, and Binder, with no hard
failure and no blocking diagnostic. Otherwise the verdict is
``android_init_started`` (some Android init stage seen), ``failed`` (a hard
failure or blocking diagnostic exists) or ``no_android_init``. Missing facts
are listed verbatim in ``unknowns``; they are not quietly treated as absent
success. A timeout, a signal, or a nonzero exit is reported in ``process`` and
``unknowns``; it is never a pass criterion, and a clean exit alone is never a
pass.

Lifecycle and output
--------------------
Each run creates ``<output>/run-<UTC stamp>-<8 hex>`` with ``command.json``
(verbatim config bytes plus their digest, resolved argv, QEMU identity, input
digests; written once with ``O_EXCL`` at mode 0444), ``serial.log`` (combined
stdout and stderr of the child) and ``result.json`` (measurement, parser
verdict, digests). The child is the only process signalled: deadline sends
SIGTERM, waits five seconds, then SIGKILL, then reaps with ``os.wait4``.

Exit codes: ``0`` framework boot verified, ``1`` not verified (evidence is
still written), ``2`` contract error before boot.
"""

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

_MODULE_DIR = str(Path(__file__).resolve().parent)
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)

import run as boot_runner

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = ROOT / "build/android/validation"
SCHEMA_VERSION = 1
PARSER_VERSION = 2
TIMEOUT_MIN = 1
TIMEOUT_MAX = 1800
ALLOWED_KEYS = frozenset(
    {
        "schema_version",
        "qemu",
        "args",
        "timeout_seconds",
        "extra_disks",
        "stop_on_completion",
        "input_paths",
        "hashes",
        "evidence_root",
    }
)

GUEST_PREFIX = "ASTER-WAYDROID|"
ALPINE_PREFIX = "ASTER-LINUX-GUEST|"
GUEST_PATTERN = re.compile(r"\AASTER-WAYDROID\|([a-z0-9_]+)=(.*)\Z")
GUEST_PROPERTY = re.compile(r"\A([a-z0-9_.]+)=(.*)\Z")
SHA256_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")
BOOT_COMPLETED = "sys.boot_completed=1"
GUEST_STAGES = ("first_stage_init", "second_stage_init", "zygote", "system_server")
STAGES = (
    "kernel",
    "first_stage_init",
    "second_stage_init",
    "zygote",
    "system_server",
    "boot_completed",
)
_STAGE_RANK = {name: index for index, name in enumerate(STAGES)}

_KERNEL_GROUP = re.compile(r"\A(?:<\d+>\s*|\[\s*(?:\d+\.\d+|[TC]\d+)\s*\]\s*)")

_STAGE_RULES = (
    ("kernel", (r"\ARun /init as init process", r"\ABooting Linux")),
    (
        "first_stage_init",
        (
            r"\Ainit: init first stage started",
            r"\Ainit: First stage init",
            r"\Ainit: Loading SELinux policy",
        ),
    ),
    (
        "second_stage_init",
        (
            r"\Ainit: init second stage started",
            r"\Ainit: Running /system/bin/init second_stage",
        ),
    ),
    (
        "zygote",
        (
            r"\Ainit: starting service 'zygote'",
            r"\Azygote\b",
            r"\AZygote\b",
            r"\A[A-Z] Zygote\b",
        ),
    ),
    (
        "system_server",
        (
            r"\Ainit: starting service 'system_server'",
            r"\Asystem_server\b",
        ),
    ),
)
_STAGE_RULES = tuple(
    (
        name,
        tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns),
    )
    for name, patterns in _STAGE_RULES
)

_BINDER_RULES = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\Abinder: \d+:\d+",
        r"\Abinder_alloc: ",
        r"\Abinderfs: ",
        r"\Ainit: Created device node '/dev/(?:binder|hwbinder|vndbinder)'",
    )
)

_FAILURE_RULES = (
    (
        "init_first_stage_abort",
        re.compile(r"\Ainit: Init encountered errors starting first stage, aborting"),
    ),
    ("init_fatal_reboot", re.compile(r"\Ainit: InitFatalReboot:")),
    ("kernel_panic", re.compile(r"Kernel panic - not syncing")),
    ("guest_namespace_failure", re.compile(r"\Aunshare: .*Invalid argument")),
    ("selinux_policy_load", re.compile(r"\Ainit: Failed to load SELinux policy")),
    ("system_server_crash", re.compile(r"FATAL EXCEPTION IN SYSTEM PROCESS")),
)
_GUEST_FAILURE_KEYS = ("system_mount", "vendor_mount", "android_init")
_INFORMATIONAL_GUEST_KEYS = frozenset(
    {
        "init_start",
        "cmdline",
        "modules_dir",
        "dmesg",
        "blocks",
        "device",
        "system_top",
        "system_system",
        "system_bin",
        "vendor_top",
        "vendor_bin",
        "init",
        "vendor_rc",
        "vendor_lib64",
        "vendor_hw",
        "release",
        "sdk",
        "fingerprint",
        "system_rc_count",
        "vendor_rc_count",
        "waydroid_rc",
        "halium",
        "gralloc",
        "insmod",
        "stop",
    }
)


class ContractError(ValueError):
    """The supplied configuration does not satisfy the documented contract."""


@dataclass(frozen=True)
class GuestConfig:
    path: Path
    raw: dict
    raw_bytes: bytes
    qemu: Path
    args: tuple
    timeout_seconds: int
    extra_disks: tuple = ()
    input_paths: tuple = ()
    hashes: dict = field(default_factory=dict)
    evidence_root: Path = DEFAULT_OUTPUT_ROOT
    config_sha256: str = ""


class Analysis:
    """Facts and their provenance, with no success inference."""

    def __init__(self):
        self.stages = {}
        self.facts = {
            "first_stage_init": False,
            "second_stage_init": False,
            "zygote": False,
            "system_server": False,
            "boot_completed": False,
            "binder": False,
            "activity": False,
        }
        self.guest_keys = {}
        self.guest_properties = {}
        self.alpine_marker_lines = 0
        self.failures = []
        self.diagnostics = []
        self.unknowns = []
        self.unrecognised_guest_keys = set()
        self.line_count = 0

    def stage(self, name, line):
        if name not in self.stages:
            self.stages[name] = line

    def as_dict(self, *, timed_out, exit_code, term_signal):
        ordered = sorted(self.stages, key=lambda name: _STAGE_RANK[name])
        observed = [{"stage": name, "line": self.stages[name]} for name in ordered]
        furthest = ordered[-1] if ordered else "none"
        android_init = any(
            self.stages.get(name) is not None
            for name in (
                "first_stage_init",
                "second_stage_init",
                "zygote",
                "system_server",
            )
        )

        unknowns = list(self.unknowns)
        if not android_init:
            unknowns.append("no_android_init_marker_observed")
        for fact in (
            "first_stage_init",
            "second_stage_init",
            "system_server",
            "boot_completed",
            "binder",
            "activity",
        ):
            if not self.facts[fact]:
                unknowns.append(
                    "binder_activity_not_observed"
                    if fact == "binder"
                    else f"{fact}_not_observed"
                )
        if self.alpine_marker_lines:
            unknowns.append("transcript_includes_alpine_guest_markers")
        if self.unrecognised_guest_keys:
            unknowns.append("guest_markers_without_known_semantics")
        if timed_out:
            unknowns.append("process_still_running_at_deadline")
        if exit_code not in (None, 0):
            unknowns.append("process_exit_nonzero")
        if term_signal is not None:
            unknowns.append("process_terminated_by_signal")

        blocking = bool(self.failures) or any(
            item.get("blocking") for item in self.diagnostics
        )
        complete = all(
            (
                self.facts["first_stage_init"],
                self.facts["second_stage_init"],
                self.facts["system_server"],
                self.facts["boot_completed"],
                self.facts["binder"],
                self.facts["activity"],
            )
        )
        if blocking:
            verdict = "failed"
        elif complete:
            verdict = "framework_booted"
        elif android_init:
            verdict = "android_init_started"
        else:
            verdict = "no_android_init"

        return {
            "parser_version": PARSER_VERSION,
            "verdict": verdict,
            "android_boot_verified": verdict == "framework_booted",
            "android_init_observed": android_init,
            "furthest_stage": furthest,
            "observed_stages": observed,
            "facts": dict(self.facts),
            "guest_marker_keys": dict(sorted(self.guest_keys.items())),
            "guest_properties": dict(sorted(self.guest_properties.items())),
            "unrecognised_guest_keys": sorted(self.unrecognised_guest_keys),
            "alpine_guest_marker_lines": self.alpine_marker_lines,
            "failures": self.failures,
            "diagnostics": self.diagnostics,
            "unknowns": unknowns,
            "line_count": self.line_count,
            "inference": (
                "verdict uses only anchored transcript evidence; process exit "
                "status never contributes to a pass"
            ),
        }


def _strip_kernel_prefix(line):
    subject = line
    for _ in range(8):
        match = _KERNEL_GROUP.match(subject)
        if match is None:
            break
        subject = subject[match.end() :]
    return subject


def _absolute_file(value, key, *, executable=False):
    if not isinstance(value, str) or not value:
        raise ContractError(f"{key} must be a non-empty string path")
    path = Path(value)
    if not path.is_absolute():
        raise ContractError(f"{key} must be an absolute path: {value}")
    if not path.is_file():
        raise ContractError(f"{key} is not an existing file: {value}")
    if executable and not os.access(path, os.X_OK):
        raise ContractError(f"{key} is not executable: {value}")
    return path


def load_config(path):
    """Read one ``linux_guest.py`` config file and validate it before boot."""
    config_path = Path(path)
    if not config_path.is_file():
        raise ContractError(f"config is not an existing file: {config_path}")
    raw_bytes = config_path.read_bytes()
    try:
        data = json.loads(raw_bytes)
    except json.JSONDecodeError as error:
        raise ContractError(f"config is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise ContractError("config must be a JSON object")

    unknown = sorted(set(data) - ALLOWED_KEYS)
    if unknown:
        raise ContractError(
            "unknown config key(s): "
            + ", ".join(unknown)
            + "; allowed: "
            + ", ".join(sorted(ALLOWED_KEYS))
        )

    version = data.get("schema_version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise ContractError(
            f"schema_version must be the integer {SCHEMA_VERSION}, got {version!r}"
        )

    qemu = _absolute_file(data.get("qemu"), "qemu", executable=True)

    args = data.get("args")
    if not isinstance(args, list) or not args:
        raise ContractError("args must be a non-empty list of strings")
    if any(not isinstance(arg, str) for arg in args):
        raise ContractError("every entry in args must be a string")
    try:
        boot_runner.check_console_args(args)
    except boot_runner.ContractError as error:
        raise ContractError(str(error)) from error

    timeout = data.get("timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise ContractError("timeout_seconds must be an integer")
    if not TIMEOUT_MIN <= timeout <= TIMEOUT_MAX:
        raise ContractError(
            f"timeout_seconds must be between {TIMEOUT_MIN} and {TIMEOUT_MAX}"
        )

    raw_inputs = data.get("input_paths", [])
    if not isinstance(raw_inputs, list):
        raise ContractError("input_paths must be a list of absolute paths")
    input_paths = tuple(
        _absolute_file(value, f"input_paths[{index}]")
        for index, value in enumerate(raw_inputs)
    )

    raw_hashes = data.get("hashes", {})
    if not isinstance(raw_hashes, dict):
        raise ContractError("hashes must be an object mapping path to sha256")
    hashes = {}
    for value, expected in raw_hashes.items():
        hashed = _absolute_file(value, "hashes key")
        if (
            not isinstance(expected, str)
            or SHA256_PATTERN.match(expected.lower()) is None
        ):
            raise ContractError(f"hashes[{value!r}] must be a 64 hex digit sha256")
        hashes[str(hashed)] = expected.lower()

    raw_disks = data.get("extra_disks", [])
    if not isinstance(raw_disks, list):
        raise ContractError("extra_disks must be a list of paths")
    extra_disks = []
    for index, value in enumerate(raw_disks):
        if not isinstance(value, str) or not value:
            raise ContractError(f"extra_disks[{index}] must be a non-empty path string")
        disk = Path(value)
        if not disk.is_absolute():
            disk = ROOT / disk
        if not disk.is_file():
            raise ContractError(
                f"extra_disks[{index}] is not an existing file: {value}"
            )
        extra_disks.append(str(disk.resolve()))

    raw_root = data.get("evidence_root", str(DEFAULT_OUTPUT_ROOT))
    if not isinstance(raw_root, str) or not raw_root:
        raise ContractError("evidence_root must be a non-empty path string")
    evidence_root = Path(raw_root)
    if not evidence_root.is_absolute():
        evidence_root = ROOT / evidence_root

    stop_on_completion = data.get("stop_on_completion", False)
    if stop_on_completion is not False:
        raise ContractError(
            "stop_on_completion must be false: this validator waits out the "
            "deadline instead of watching the transcript mid-run"
        )

    return GuestConfig(
        path=config_path.resolve(),
        raw=data,
        raw_bytes=raw_bytes,
        qemu=qemu.resolve(),
        args=tuple(args),
        timeout_seconds=timeout,
        extra_disks=tuple(extra_disks),
        input_paths=input_paths,
        hashes=hashes,
        evidence_root=evidence_root,
        config_sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )


def _new_run_dir(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    path = root / f"run-{stamp}-{os.urandom(4).hex()}"
    path.mkdir(parents=True, exist_ok=False)
    return path.resolve()


def _write_once(path, payload):
    """Create a file exactly once, read-only, so the record cannot be rewritten."""
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(payload)
    return path


def analyze(text, *, timed_out=False, exit_code=None, term_signal=None):
    """Classify one transcript. Pure: the process outcome only adds unknowns."""
    analysis = Analysis()
    analysis.line_count = len(text.splitlines())

    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\r")
        if line.startswith(ALPINE_PREFIX):
            analysis.alpine_marker_lines += 1
            continue
        if line.startswith(GUEST_PREFIX):
            match = GUEST_PATTERN.match(line)
            if match is None:
                analysis.unrecognised_guest_keys.add("malformed_marker")
                continue
            key, value = match.group(1), match.group(2)
            analysis.guest_keys[key] = analysis.guest_keys.get(key, 0) + 1
            if key == "init_stage":
                if value in GUEST_STAGES:
                    analysis.stage(value, number)
                    analysis.facts[value] = True
                else:
                    analysis.unknowns.append(f"unrecognised_init_stage:{value}")
            elif key == "prop":
                property_match = GUEST_PROPERTY.match(value)
                if property_match is None:
                    analysis.unknowns.append(f"malformed_property_marker:{value}")
                    continue
                name, prop_value = property_match.group(1), property_match.group(2)
                analysis.guest_properties[name] = prop_value
                if name == "sys.boot_completed" and prop_value == "1":
                    analysis.facts["boot_completed"] = True
                    analysis.stage("boot_completed", number)
            elif key == "system_server_pid":
                if value.isdigit() and int(value) > 0:
                    analysis.facts["system_server"] = True
                    analysis.stage("system_server", number)
                else:
                    analysis.unknowns.append(f"invalid_system_server_pid:{value}")
            elif key in ("binderfs_nodes", "binder_devices"):
                if value.strip():
                    analysis.facts["binder"] = True
            elif key == "service_activity":
                if value == "rc=0 detail=Service activity: found":
                    analysis.facts["activity"] = True
            elif key == "android_init_exit":
                if value.strip() not in ("0", ""):
                    analysis.failures.append(
                        {
                            "kind": "android_init_exit_nonzero",
                            "line": number,
                            "excerpt": line[:200],
                        }
                    )
            elif key in _GUEST_FAILURE_KEYS and "fail" in value.lower():
                analysis.diagnostics.append(
                    {
                        "kind": f"guest_{key}_failure",
                        "line": number,
                        "excerpt": line[:200],
                        "blocking": True,
                    }
                )
            elif key not in _INFORMATIONAL_GUEST_KEYS:
                analysis.unrecognised_guest_keys.add(key)
            continue

        stripped = _strip_kernel_prefix(line)
        for name, patterns in _STAGE_RULES:
            if any(pattern.search(stripped) for pattern in patterns):
                analysis.stage(name, number)
                if name in analysis.facts:
                    analysis.facts[name] = True
                break
        if any(pattern.search(stripped) for pattern in _BINDER_RULES):
            analysis.facts["binder"] = True
        for kind, pattern in _FAILURE_RULES:
            if pattern.search(stripped):
                analysis.failures.append(
                    {"kind": kind, "line": number, "excerpt": stripped[:200]}
                )
                break

    return analysis.as_dict(
        timed_out=timed_out, exit_code=exit_code, term_signal=term_signal
    )


def run_config(config, *, output_root=None, timeout_seconds=None):
    """Boot one validated config and return its written result record."""
    qemu_record = boot_runner.inspect_qemu(config.qemu)
    try:
        inputs = boot_runner.verify_inputs(config)
    except boot_runner.ContractError as error:
        raise ContractError(str(error)) from error

    timeout = config.timeout_seconds if timeout_seconds is None else timeout_seconds
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise ContractError("timeout override must be an integer")
    if not TIMEOUT_MIN <= timeout <= TIMEOUT_MAX:
        raise ContractError(
            f"timeout override must be between {TIMEOUT_MIN} and {TIMEOUT_MAX}"
        )

    root = Path(output_root) if output_root is not None else config.evidence_root
    if not root.is_absolute():
        root = ROOT / root
    evidence = _new_run_dir(root)
    serial = evidence / "serial.log"
    argv = [str(config.qemu), *config.args]

    _write_once(
        evidence / "command.json",
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "parser_version": PARSER_VERSION,
                "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "evidence_dir": str(evidence),
                "config_path": str(config.path),
                "config_sha256": config.config_sha256,
                "config": config.raw,
                "command": {
                    "argv": argv,
                    "qemu": qemu_record,
                    "inputs": inputs,
                },
                "timeout_seconds": timeout,
                "immutability": (
                    "written once with O_EXCL at mode 0444 before the child "
                    "started; digest recorded in result.json"
                ),
            },
            indent=2,
        )
        + "\n",
    )
    command_digest = boot_runner.sha256_file(evidence / "command.json")

    outcome = boot_runner.supervise(argv, serial, timeout)

    data = serial.read_bytes()
    transcript = data.decode("utf-8", "replace")
    parsed = analyze(
        transcript,
        timed_out=outcome.timed_out,
        exit_code=outcome.exit_code,
        term_signal=outcome.term_signal,
    )

    result = {
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "verified": parsed["android_boot_verified"],
        "verdict": parsed["verdict"],
        "evidence_dir": str(evidence),
        "config_path": str(config.path),
        "config_sha256": config.config_sha256,
        "command_sha256": command_digest,
        "serial_path": str(serial),
        "serial_sha256": hashlib.sha256(data).hexdigest(),
        "serial_bytes": len(data),
        "completed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "timeout_seconds": timeout,
        "process": boot_runner.measurement_record(outcome),
        "analysis": parsed,
    }
    (evidence / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Boot one linux_guest config, keep the serial transcript, "
        "and classify Waydroid early-init evidence. The module docstring is "
        "the config and guest-marker contract."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        help="directory that receives a new run-<stamp>-<hex> evidence dir",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        help=f"override timeout_seconds ({TIMEOUT_MIN}..{TIMEOUT_MAX})",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        result = run_config(
            config, output_root=args.output, timeout_seconds=args.timeout
        )
    except ContractError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            "error: interrupted; the owned child was signalled and reaped",
            file=sys.stderr,
        )
        return 130

    print(json.dumps(result, indent=2))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

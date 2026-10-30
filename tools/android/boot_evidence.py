#!/usr/bin/env python3
"""Decide whether an Android host-boot serial transcript proves a booted framework.

The parser is evidence-positive. Android-looking kernel or init text never
passes on its own; the gate needs facts the guest emits through a per-run nonce.

Evidence protocol
-----------------
The runner generates a fresh 32-lowercase-hex nonce for every boot and gives it
to the guest out of band, for example
``androidboot.aster_nonce=<nonce>`` on the kernel command line or an equivalent
firmware channel. The runner never writes to the guest console, so nothing a
shell echoes back as typed input can carry the nonce by accident. The guest
emits one complete line per fact::

    ASTER-BOOT|<nonce>|<seq>|<kind>|<value>

``<seq>`` is a decimal counter that starts at 1 and strictly increases.
The whole line must match that grammar exactly: nothing before
``ASTER-BOOT|``, nothing after ``<value>`` except an optional carriage return.
Kinds:

``STAGE``
    ``first_stage_init``, ``second_stage_init``, ``zygote`` or
    ``system_server``.
``PROP``
    only ``sys.boot_completed=1``, as returned by
    ``getprop sys.boot_completed`` itself.
``FRAMEWORK``
    ``service_activity=found`` (from ``service check activity``) and
    ``system_server_pid=<decimal>`` (from ``pidof system_server``, greater
    than zero).
``FAIL``
    free text; any occurrence disqualifies the run.
``MARKER``
    free text; informational only.

A shell echo (``~ # echo ASTER-BOOT|...``), a trace line (``+ echo ...``), a
quoted copy, a truncated line, a repeated or out-of-order ``<seq>``, or a line
carrying another nonce is rejected. So is any ``<value>`` containing a shell
quoting character (``"``, ``'``, backtick, ``$`` or backslash), which keeps a
quoted echo from ever matching. Rejections are recorded per line, so a guest
protocol bug is visible rather than silently ignored.

Gate
----
A run passes only when all of the following hold on this transcript:

* kernel boot evidence appears before the first protocol line;
* a nonce-bound ``PROP|sys.boot_completed=1`` line exists;
* nonce-bound ``FRAMEWORK|service_activity=found`` and
  ``FRAMEWORK|system_server_pid=<pid>`` lines exist;
* the completion line does not precede observed first-stage init evidence;
* no hard failure, no guest ``FAIL``, no completion payload under another
  nonce, no timeout and no nonzero exit.

Kernel and init log text is classified separately into stages and diagnostic
classes (partition and mount failures, SELinux denials, vendor/VINTF errors).
Those observations explain a failure and record the furthest verified stage;
they do not substitute for the nonce-bound gate. A transient init failure that
is followed by a real completion still passes, and its diagnostic is kept.
"""

import argparse
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
import sys

STAGES = (
    "kernel",
    "first_stage_init",
    "second_stage_init",
    "zygote",
    "system_server",
    "boot_completed",
)
_STAGE_RANK = {name: index for index, name in enumerate(STAGES)}
GUEST_STAGES = ("first_stage_init", "second_stage_init", "zygote", "system_server")

PROTOCOL_PREFIX = "ASTER-BOOT|"
NONCE_PATTERN = re.compile(r"\A[0-9a-f]{32}\Z")
PROTOCOL_PATTERN = re.compile(
    r"\AASTER-BOOT\|([0-9a-f]{32})\|([0-9]{1,9})\|([A-Z_]{3,16})\|([^|\r\n]*)\Z"
)
PROP_BOOT_COMPLETED = "sys.boot_completed=1"
FRAMEWORK_SERVICE_ACTIVITY = "service_activity=found"
FRAMEWORK_SYSTEM_SERVER_PID = re.compile(r"\Asystem_server_pid=([0-9]{1,10})\Z")

HARD_FAILURES = (
    "apex_bootstrap_failure",
    "alpine_userspace",
    "kernel_panic",
    "selinux_policy_load",
    "system_server_crash",
)
BLOCKING = (
    "mount_failure",
    "partition_failure",
    "selinux_error",
    "vendor_error",
    "vintf_error",
)

_KERNEL_PREFIX = re.compile(r"\A(?:\[\s*[0-9]+\.[0-9]+\]|\[\s*[TC][0-9]+\])\s*")


def _strip_prefix(line):
    """Remove leading ``[time]`` / ``[T0]`` bracketed kernel groups."""
    subject = line
    for _ in range(4):
        stripped = _KERNEL_PREFIX.sub("", subject, count=1)
        if stripped == subject:
            break
        subject = stripped
    return subject


_RULES = (
    (
        "failure",
        "apex_bootstrap_failure",
        (
            r"Found two APEX packages for module",
            r"bootstrap-apexd-failed",
        ),
    ),
    (
        "failure",
        "kernel_panic",
        (
            r"Kernel panic - not syncing",
            r"\Aend Kernel panic",
        ),
    ),
    (
        "failure",
        "selinux_policy_load",
        (
            r"Failed to load SELinux policy",
            r"SELinux: Could not load policy",
            r"init: Failed to load SELinux policy",
        ),
    ),
    (
        "failure",
        "system_server_crash",
        (r"FATAL EXCEPTION IN SYSTEM PROCESS",),
    ),
    (
        "diagnostic",
        "alpine_userspace",
        (
            r"initramfs emergency recovery shell",
            r"\AWelcome to Alpine",
            r"ASTER-LINUX-USERSPACE",
            r"ASTER_KERNEL_PROBE_PASS",
            r"ASTER_KERNEL_PROBE_FAIL",
        ),
    ),
    (
        "diagnostic",
        "root_shell_prompt",
        (r"\A~ # ",),
    ),
    (
        "diagnostic",
        "mount_failure",
        (
            r"init: Failed to mount",
            r"init: Unable to mount",
            r"fs_mgr: Failed to mount",
            r"mount: mounting .* failed",
            r"Failed to mount /dev/block",
            r"init: mount\(\) .* failed",
        ),
    ),
    (
        "diagnostic",
        "partition_failure",
        (
            r"init: cannot find partition",
            r"Can't find partition",
            r"Failed to open /dev/block",
            r"partition .* not found",
            r"VFS: Cannot open root device",
            r"init: Unable to open '/dev/block",
        ),
    ),
    (
        "diagnostic",
        "selinux_denial",
        (r"avc:\s+denied",),
    ),
    (
        "diagnostic",
        "selinux_error",
        (
            r"SELinux: .*(?:error|failed|denied)",
            r"libselinux.*(?:error|failed|denied|unable)",
        ),
    ),
    (
        "diagnostic",
        "vintf_error",
        (
            r"vintf.*(?:error|fail|cannot|unable|missing|incompatible)",
            r"Failed to (?:open|parse) .*manifest",
            r"VintfObject.*(?:error|fail)",
        ),
    ),
    (
        "diagnostic",
        "vendor_error",
        (
            r"hwservicemanager.*(?:cannot find|failed)",
            r"vendor.*(?:hal|manifest).*(?:not found|missing|failed)",
        ),
    ),
    (
        "stage",
        "kernel",
        (
            r"\ABooting Linux on physical CPU",
            r"\ALinux version \d",
            r"\AKernel command line:",
            r"\ARun /init as init process",
            r"\AMachine model:",
        ),
    ),
    (
        "stage",
        "first_stage_init",
        (
            r"init: init first stage started",
            r"init: Loading SELinux policy",
            r"init: init first stage done",
            r"init: Switching root to",
        ),
    ),
    (
        "stage",
        "second_stage_init",
        (
            r"init: init second stage started",
            r"init: [Ss]tarting service '(?:ueventd|logd|servicemanager|"
            r"hwservicemanager|vndservicemanager|apexd|installd)'",
            r"init: Parsing file /system/etc/init",
            r"init: Loading baseline SELinux policy",
        ),
    ),
    (
        "stage",
        "zygote",
        (
            r"init: [Ss]tarting service 'zygote",
            r"\A[VDIWEF]/Zygote\b",
            r"\bZygote\s*:.*(?:preload|fork|Init|started)",
        ),
    ),
    (
        "stage",
        "system_server",
        (
            r"SystemServer: Entered the Android system server",
            r"\A[VDIWEF]/SystemServer\b",
            r"SystemServerTiming",
            r"init: [Ss]tarting service 'system_server'",
            r"servicemanager: Caller\(pid=\d+,uid=1000,sid=u:r:system_server:s0\)",
        ),
    ),
)

_COMPILED_RULES = tuple(
    (
        kind,
        name,
        tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns),
    )
    for kind, name, patterns in _RULES
)


def classify_log_line(line):
    """Return ``(kind, name)`` for one serial log line, or ``None``.

    ``kind`` is ``stage``, ``failure`` or ``diagnostic``. Failure rules win over
    diagnostic rules, which win over stage rules.
    """
    subject = _strip_prefix(line).strip()
    if not subject:
        return None
    for kind, name, patterns in _COMPILED_RULES:
        for pattern in patterns:
            if pattern.search(subject):
                return kind, name
    return None


class StageTracker:
    """Monotonic record of boot stages. A later stage never regresses."""

    def __init__(self):
        self.evidence = {}
        self.order = []
        self.regressions = []

    def observe(self, stage, line_number, source):
        if stage not in _STAGE_RANK:
            raise ValueError(f"unknown stage {stage!r}")
        if stage in self.evidence:
            return
        if self.order and _STAGE_RANK[stage] < _STAGE_RANK[self.order[-1]]:
            self.regressions.append(
                {
                    "stage": stage,
                    "line": line_number,
                    "source": source,
                    "after": self.order[-1],
                }
            )
        self.evidence[stage] = {"line": line_number, "source": source}
        self.order.append(stage)

    def reached(self, stage):
        return stage in self.evidence

    def verified(self):
        return sorted(self.evidence, key=_STAGE_RANK.__getitem__)

    def furthest(self):
        return self.verified()[-1] if self.evidence else None


@dataclass
class Analysis:
    nonce: str
    ok: bool
    failures: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)
    blocking_observations: list = field(default_factory=list)
    stages: list = field(default_factory=list)
    furthest_stage: object = None
    stage_evidence: dict = field(default_factory=dict)
    completion: dict = field(default_factory=dict)
    protocol: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)

    def as_dict(self):
        return asdict(self)


def _validate_value(kind, value):
    if any(character in value for character in "\"'`$\\"):
        return "value contains shell quoting characters"
    if kind == "STAGE":
        if value not in GUEST_STAGES:
            return f"unknown stage {value!r}"
        return None
    if kind == "PROP":
        if value != PROP_BOOT_COMPLETED:
            return f"unsupported property {value!r}"
        return None
    if kind == "FRAMEWORK":
        if value == FRAMEWORK_SERVICE_ACTIVITY:
            return None
        match = FRAMEWORK_SYSTEM_SERVER_PID.match(value)
        if match is None:
            return f"unsupported framework fact {value!r}"
        if int(match.group(1)) == 0:
            return "system_server_pid must be greater than zero"
        return None
    if kind in ("FAIL", "MARKER"):
        if not value.strip():
            return f"{kind} needs a value"
        return None
    return f"unknown kind {kind!r}"


def _is_completion_payload(text):
    return (
        PROP_BOOT_COMPLETED in text
        or "service_activity=" in text
        or "system_server_pid=" in text
    )


def analyze(
    transcript,
    nonce,
    *,
    timed_out=False,
    exit_code=None,
    term_signal=None,
    runner_stop_requested=False,
):
    """Analyse one complete transcript and return an :class:`Analysis`."""
    if not isinstance(nonce, str) or NONCE_PATTERN.match(nonce) is None:
        raise ValueError("nonce must be 32 lowercase hex characters")
    if isinstance(transcript, (bytes, bytearray)):
        text = bytes(transcript).decode("utf-8", "replace")
    else:
        text = str(transcript)

    tracker = StageTracker()
    counts = {}
    samples = {}
    accepted = []
    rejected = []
    foreign = {}
    first_protocol_line = None
    last_seq = 0

    for index, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip("\r")
        if PROTOCOL_PREFIX in line:
            if line.startswith(PROTOCOL_PREFIX) and first_protocol_line is None:
                first_protocol_line = index
            match = PROTOCOL_PATTERN.match(line)
            if match is None:
                other = re.search(r"ASTER-BOOT\|([0-9a-f]{32})\|", line)
                if other is not None and other.group(1) != nonce:
                    foreign.setdefault(other.group(1), []).append(
                        {"line": index, "text": line[:300]}
                    )
                else:
                    rejected.append(
                        {
                            "line": index,
                            "reason": "line does not match "
                            "ASTER-BOOT|<nonce>|<seq>|<kind>|<value> exactly",
                            "text": line[:300],
                        }
                    )
                continue
            found_nonce, seq_text, kind, value = match.groups()
            if found_nonce != nonce:
                foreign.setdefault(found_nonce, []).append(
                    {"line": index, "text": line[:300]}
                )
                continue
            problem = _validate_value(kind, value)
            if problem is not None:
                rejected.append({"line": index, "reason": problem, "text": line[:300]})
                continue
            seq = int(seq_text)
            if seq <= last_seq:
                rejected.append(
                    {
                        "line": index,
                        "reason": f"sequence {seq} does not increase past {last_seq}",
                        "text": line[:300],
                    }
                )
                continue
            last_seq = seq
            accepted.append({"line": index, "seq": seq, "kind": kind, "value": value})
            if kind == "STAGE":
                tracker.observe(value, index, "protocol")
            elif kind == "PROP" and value == PROP_BOOT_COMPLETED:
                tracker.observe("boot_completed", index, "protocol")
            continue
        if not line.strip():
            continue
        classified = classify_log_line(line)
        if classified is None:
            continue
        kind, name = classified
        if kind == "stage":
            tracker.observe(name, index, "log")
        else:
            counts[name] = counts.get(name, 0) + 1
            samples.setdefault(name, {"line": index, "text": line[:300]})

    failures = []
    diagnostics = []

    for name in HARD_FAILURES:
        if counts.get(name):
            sample = samples[name]
            failures.append(f"{name}: {sample['text']} (line {sample['line']})")

    for entry in accepted:
        if entry["kind"] == "FAIL":
            failures.append(
                f"guest reported failure at line {entry['line']}: {entry['value']}"
            )

    for other, entries in foreign.items():
        if any(_is_completion_payload(entry["text"]) for entry in entries):
            failures.append(
                "stale evidence: completion or framework marker carries a "
                f"different nonce {other!r} (line {entries[0]['line']})"
            )
        else:
            diagnostics.append(
                f"ignored {len(entries)} nonce-tagged line(s) carrying nonce "
                f"{other!r}; first at line {entries[0]['line']}"
            )

    for regression in tracker.regressions:
        diagnostics.append(
            f"stage regression: {regression['stage']} observed at line "
            f"{regression['line']} after {regression['after']}"
        )
    for name in sorted(counts):
        if name in HARD_FAILURES:
            continue
        sample = samples[name]
        diagnostics.append(
            f"{name}: {counts[name]} occurrence(s); first at line "
            f"{sample['line']}: {sample['text']}"
        )
    for entry in rejected:
        diagnostics.append(
            f"rejected protocol line at {entry['line']}: {entry['reason']}: "
            f"{entry['text']}"
        )

    completion = {
        "boot_completed": None,
        "service_activity": None,
        "system_server_pid": None,
    }
    for entry in accepted:
        if entry["kind"] == "PROP" and entry["value"] == PROP_BOOT_COMPLETED:
            completion["boot_completed"] = completion["boot_completed"] or {
                "line": entry["line"],
                "seq": entry["seq"],
            }
        elif (
            entry["kind"] == "FRAMEWORK"
            and entry["value"] == FRAMEWORK_SERVICE_ACTIVITY
        ):
            completion["service_activity"] = completion["service_activity"] or {
                "line": entry["line"],
                "seq": entry["seq"],
            }
        elif entry["kind"] == "FRAMEWORK" and entry["value"].startswith(
            "system_server_pid="
        ):
            completion["system_server_pid"] = completion["system_server_pid"] or {
                "line": entry["line"],
                "seq": entry["seq"],
                "pid": int(entry["value"].split("=", 1)[1]),
            }

    if not tracker.reached("kernel"):
        failures.append(
            "no kernel boot evidence in transcript (expected e.g. "
            "'Booting Linux on physical CPU' or 'Linux version')"
        )

    if first_protocol_line is not None and tracker.reached("kernel"):
        kernel_line = tracker.evidence["kernel"]["line"]
        if first_protocol_line < kernel_line:
            failures.append(
                f"nonce-tagged evidence at line {first_protocol_line} precedes "
                f"kernel boot evidence at line {kernel_line}: stale or injected "
                "transcript"
            )

    if completion["boot_completed"] is None:
        failures.append(
            "boot did not complete: no nonce-bound 'PROP|sys.boot_completed=1' line"
        )
    if completion["service_activity"] is None:
        failures.append(
            "incomplete framework: no nonce-bound "
            "'FRAMEWORK|service_activity=found' line"
        )
    if completion["system_server_pid"] is None:
        failures.append(
            "incomplete framework: no nonce-bound "
            "'FRAMEWORK|system_server_pid=<pid>' line"
        )

    if completion["boot_completed"] is None and not timed_out and exit_code == 0:
        failures.append(
            "serial stream ended (QEMU exited 0) before boot completion: "
            "incomplete startup or a guest that never emitted the gate"
        )

    if completion["boot_completed"] is not None and tracker.reached("first_stage_init"):
        init_line = tracker.evidence["first_stage_init"]["line"]
        if completion["boot_completed"]["line"] < init_line:
            failures.append(
                f"boot completion at line {completion['boot_completed']['line']} "
                f"precedes first-stage init evidence at line {init_line}"
            )

    if timed_out:
        failures.append(
            "timeout: the run was stopped by the deadline before the gate was satisfied"
        )
    if exit_code is not None and exit_code != 0:
        failures.append(f"QEMU exited with status {exit_code}")
    if term_signal is not None and not runner_stop_requested:
        failures.append(f"QEMU was terminated by signal {term_signal}")

    blocking = [name for name in BLOCKING if counts.get(name)] if failures else []

    return Analysis(
        nonce=nonce,
        ok=not failures,
        failures=failures,
        diagnostics=diagnostics,
        blocking_observations=blocking,
        stages=tracker.verified(),
        furthest_stage=tracker.furthest(),
        stage_evidence=dict(tracker.evidence),
        completion=completion,
        protocol={
            "accepted": accepted,
            "rejected": rejected,
            "foreign_nonces": foreign,
            "first_line": first_protocol_line,
            "last_seq": last_seq,
        },
        counts=dict(sorted(counts.items())),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Analyse one stored serial transcript against a run nonce. "
        "The module docstring defines the protocol and the gate."
    )
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--timed-out", action="store_true")
    parser.add_argument("--exit-code", type=int)
    args = parser.parse_args(argv)
    try:
        analysis = analyze(
            args.transcript.read_bytes(),
            args.nonce,
            timed_out=args.timed_out,
            exit_code=args.exit_code,
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(analysis.as_dict(), indent=2))
    return 0 if analysis.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

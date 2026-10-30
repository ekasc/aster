#!/usr/bin/env python3
"""Run one configured Android host boot and gate it on nonce-bound evidence.

This runner is deliberately independent of artifact discovery. It boots exactly
the QEMU binary and argument list the caller supplies, captures the complete
serial transcript, measures the child, and hands the transcript to
``boot_evidence``. The board, kernel, ramdisk, partitions and the guest commands
that emit evidence belong to the caller.

Configuration contract (``--config PATH``, JSON object, ``schema_version`` 1)
-----------------------------------------------------------------------------
::

    {
      "schema_version": 1,
      "qemu": "/absolute/path/to/qemu-system-aarch64",
      "args": ["-M", "virt,gic-version=3", "-serial", "stdio", "...",
               "-append", "console=ttyAMA0 ... androidboot.aster_nonce={nonce}"],
      "timeout_seconds": 300,
      "input_paths": ["/absolute/path/Image"],
      "hashes": {"/absolute/path/Image": "<64 hex sha256>"},
      "evidence_root": "build/android/boot-runs",
      "stop_on_completion": false
    }

Rules enforced before anything is started:

* ``schema_version`` is the integer ``1`` and no unknown top-level key exists.
* ``qemu`` is an absolute path to an existing executable file. ``config-host.h``
  next to it must define ``CONFIG_TCG_INTERPRETER`` and ``CONFIG_TCG``; a
  missing header is unknown provenance and is refused by default. Override
  deliberately with ``--allow-unknown-qemu-provenance`` or ``--allow-non-tci``.
* ``args`` is a non-empty list of strings and must contain the ``{nonce}``
  placeholder at least once, so guest evidence cannot be unbound. The only
  placeholders are ``{nonce}`` (the fresh 32-hex run nonce) and
  ``{evidence_dir}`` (this run's new evidence directory). Unknown ``{name}``
  placeholders are refused.
* Serial has to reach the runner's stdout: at least one ``-serial stdio`` (or
  ``-nographic``), no ``-serial none``/``null``/``file:``/``mon:`` and no
  ``-monitor stdio``/``tcp:``. ``-nographic`` also needs ``-monitor none``
  because it would otherwise multiplex the monitor into the transcript.
* ``-daemonize`` is refused: the runner has to own and reap the process.
* No unauthenticated host forward to guest ADB port 5555.
* ``timeout_seconds`` is an integer from 1 through 1800.
* ``input_paths`` (optional) are absolute existing files that get hashed into
  ``command.json``. ``hashes`` (optional) maps absolute paths to expected
  SHA-256 digests; a mismatch aborts before boot.
* ``evidence_root`` (optional) defaults to ``build/android/boot-runs``.
* ``stop_on_completion`` (optional, default false) sends one cooperative
  SIGTERM once the gate is already satisfied, instead of waiting out the
  deadline. When the runner asked for that stop, a signalled or nonzero exit is
  not a failure; the stop is recorded.

Guest evidence protocol
-----------------------
The runner never writes to the guest console. The nonce is delivered through
``args`` (for example ``androidboot.aster_nonce={nonce}`` on the kernel command
line) and the guest emits::

    ASTER-BOOT|<nonce>|<seq>|<kind>|<value>

See ``boot_evidence`` for the full grammar and the gate. Required facts are
``PROP|sys.boot_completed=1``, ``FRAMEWORK|service_activity=found`` and
``FRAMEWORK|system_server_pid=<pid>``, all under the current nonce.

Measurement
-----------
Wall and CPU time and peak RSS come from ``os.wait4`` for this exact child, so
the peak RSS is that process's own high-water mark. ``resource.getrusage(
RUSAGE_CHILDREN)`` is deliberately not used: it is cumulative over every child
this Python process ever reaped and cannot be attributed to one boot. On Darwin
``ru_maxrss`` is bytes; elsewhere it is KiB and is converted. Values are
kernel-accounted, not sampled; the result labels this explicitly.

Lifecycle and output
--------------------
Each run creates a new directory named ``run-<UTC stamp>-<nonce prefix>`` under
``evidence_root`` and writes ``command.json`` (validated config, resolved argv,
QEMU identity, input hashes), ``serial.log`` (the raw transcript) and
``result.json`` (measurement plus evidence analysis). The transcript is the
guest console captured from stdout, so the process's exit is the end of stream.

A deadline or a requested stop sends SIGTERM to that exact child, waits up to
five seconds, then SIGKILL, then reaps it. No other process is signalled and no
child is left behind on any branch, including an exception in the caller.

Exit codes: ``0`` gate passed, ``1`` gate failed (evidence and measurement are
still written), ``2`` configuration or provenance error before boot.
"""

import argparse
from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

_MODULE_DIR = str(Path(__file__).resolve().parent)
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)

import boot_evidence

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVIDENCE_ROOT = ROOT / "build/android/boot-runs"
SCHEMA_VERSION = 1
TIMEOUT_MIN = 1
TIMEOUT_MAX = 1800
ALLOWED_KEYS = frozenset(
    {
        "schema_version",
        "qemu",
        "args",
        "timeout_seconds",
        "input_paths",
        "hashes",
        "evidence_root",
        "stop_on_completion",
    }
)
PLACEHOLDERS = ("nonce", "evidence_dir")
_PLACEHOLDER_PATTERN = re.compile(r"\{([a-z_]+)\}")
_SHA256_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")


class ContractError(ValueError):
    """The supplied configuration does not satisfy the documented contract."""


@dataclass(frozen=True)
class BootConfig:
    path: Path
    qemu: Path
    args: tuple
    timeout_seconds: int
    input_paths: tuple = ()
    hashes: dict = field(default_factory=dict)
    evidence_root: Path = DEFAULT_EVIDENCE_ROOT
    stop_on_completion: bool = False


@dataclass
class Outcome:
    pid: int
    exit_code: object = None
    term_signal: object = None
    timed_out: bool = False
    runner_stop_requested: bool = False
    cooperative_stop: bool = False
    escalated: bool = False
    reaped: bool = True
    wall_seconds: float = 0.0
    cpu_seconds: object = None
    peak_rss_bytes: object = None


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


def _option_values(args, option):
    values = []
    for index, arg in enumerate(args):
        if arg == option and index + 1 < len(args):
            values.append(args[index + 1])
        elif arg.startswith(option + "="):
            values.append(arg[len(option) + 1 :])
    return values


def check_console_args(args):
    """Refuse argument lists whose console would not reach the transcript."""
    if "-daemonize" in args:
        raise ContractError(
            "-daemonize is refused: the runner must own and reap the QEMU process"
        )
    serial = _option_values(args, "-serial")
    monitor = _option_values(args, "-monitor")
    if "-nographic" in args:
        if not monitor:
            raise ContractError(
                "-nographic without -monitor none would multiplex the QEMU "
                "monitor into the serial transcript"
            )
    if not serial and "-nographic" not in args:
        raise ContractError(
            "no serial console is configured; add '-serial stdio' (or -nographic)"
        )
    for value in serial:
        if value in ("none", "null") or value.startswith(("file:", "mon:")):
            raise ContractError(
                f"serial console {value!r} does not reach this runner; use '-serial stdio'"
            )
    for value in monitor:
        if value.startswith(("stdio", "mon:stdio", "tcp:")):
            raise ContractError(
                f"monitor {value!r} is refused: it would enter the transcript "
                "or expose an unauthenticated listener; use '-monitor none'"
            )
    for value in _option_values(args, "-qmp"):
        if value.startswith("tcp:"):
            raise ContractError(
                f"QMP socket {value!r} is refused: it would expose an "
                "unauthenticated control listener"
            )
    for arg in args:
        if "hostfwd" in arg and re.search(r"(?:^|[^0-9])5555(?:[^0-9]|$)", arg):
            raise ContractError(
                "refusing a host forward to guest port 5555 (unauthenticated ADB)"
            )


def _check_placeholders(args):
    for arg in args:
        for name in _PLACEHOLDER_PATTERN.findall(arg):
            if name not in PLACEHOLDERS:
                raise ContractError(
                    f"unknown placeholder {{{name}}} in args; allowed: "
                    + ", ".join("{" + item + "}" for item in PLACEHOLDERS)
                )
    if not any("{nonce}" in arg for arg in args):
        raise ContractError(
            "args must contain the {nonce} placeholder so guest evidence is "
            "bound to this run (for example '-append ... androidboot.aster_nonce={nonce}')"
        )


def load_config(path):
    """Read and validate one boot configuration file."""
    config_path = Path(path)
    if not config_path.is_file():
        raise ContractError(f"config is not an existing file: {config_path}")
    try:
        data = json.loads(config_path.read_text())
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
    _check_placeholders(args)
    check_console_args(args)

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
        path = _absolute_file(value, "hashes key")
        if (
            not isinstance(expected, str)
            or _SHA256_PATTERN.match(expected.lower()) is None
        ):
            raise ContractError(f"hashes[{value!r}] must be a 64 hex digit sha256")
        hashes[str(path)] = expected.lower()

    raw_root = data.get("evidence_root", str(DEFAULT_EVIDENCE_ROOT))
    if not isinstance(raw_root, str) or not raw_root:
        raise ContractError("evidence_root must be a non-empty path string")
    evidence_root = Path(raw_root)
    if not evidence_root.is_absolute():
        evidence_root = ROOT / evidence_root

    stop_on_completion = data.get("stop_on_completion", False)
    if not isinstance(stop_on_completion, bool):
        raise ContractError("stop_on_completion must be a boolean")

    return BootConfig(
        path=config_path.resolve(),
        qemu=qemu.resolve(),
        args=tuple(args),
        timeout_seconds=timeout,
        input_paths=input_paths,
        hashes=hashes,
        evidence_root=evidence_root,
        stop_on_completion=stop_on_completion,
    )


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _active_define(text, name):
    return re.search(rf"^#define {name}\b", text, re.MULTILINE) is not None


def inspect_qemu(qemu):
    """Record the executable identity and the adjacent interpreter build header."""
    header = Path(qemu).parent / "config-host.h"
    record = {
        "path": str(qemu),
        "sha256": sha256_file(qemu),
        "config_host_h": str(header),
        "config_host_h_present": header.is_file(),
        "tcg": None,
        "tcg_interpreter": None,
    }
    if header.is_file():
        text = header.read_text(errors="replace")
        record["config_host_h_sha256"] = hashlib.sha256(header.read_bytes()).hexdigest()
        record["tcg"] = bool(re.search(r"^#define CONFIG_TCG 1\b", text, re.MULTILINE))
        record["tcg_interpreter"] = _active_define(text, "CONFIG_TCG_INTERPRETER")
    return record


def require_tci_provenance(record, *, allow_unknown=False, allow_non_tci=False):
    if not record["config_host_h_present"]:
        if allow_unknown:
            return
        raise ContractError(
            "QEMU provenance unknown: no config-host.h beside "
            f"{record['path']}; refusing to treat it as a TCI build. Pass "
            "--allow-unknown-qemu-provenance to record that uncertainty "
            "deliberately."
        )
    if record["tcg"] and record["tcg_interpreter"]:
        return
    if allow_non_tci:
        return
    raise ContractError(
        "QEMU is not a TCI build: "
        f"{record['config_host_h']} has tcg={record['tcg']} "
        f"tcg_interpreter={record['tcg_interpreter']}. Pass --allow-non-tci to "
        "run it anyway."
    )


def qemu_version(qemu):
    try:
        completed = subprocess.run(
            [str(qemu), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"unavailable: {error}"
    lines = completed.stdout.strip().splitlines()
    if completed.returncode != 0 or not lines:
        return f"unavailable: exit {completed.returncode}"
    return lines[0]


def build_argv(config, nonce, evidence_dir):
    argv = []
    for arg in config.args:
        argv.append(
            arg.replace("{nonce}", nonce).replace("{evidence_dir}", str(evidence_dir))
        )
    return [str(config.qemu), *argv]


def new_evidence_dir(root, nonce):
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    path = Path(root) / f"run-{stamp}-{nonce[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    return path.resolve()


def verify_inputs(config):
    digests = {}
    for path in config.input_paths:
        digests[str(path)] = sha256_file(path)
    for value, expected in config.hashes.items():
        actual = digests.get(value)
        if actual is None:
            actual = sha256_file(Path(value))
            digests[value] = actual
        if actual != expected:
            raise ContractError(
                f"input hash mismatch for {value}: expected {expected}, got {actual}"
            )
    return digests


def _spawn(argv, log_path):
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    null_fd = os.open(os.devnull, os.O_RDONLY)
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child always execs or _exits
        try:
            signal.signal(signal.SIGPIPE, signal.SIG_DFL)
            os.dup2(null_fd, 0)
            os.dup2(log_fd, 1)
            os.dup2(log_fd, 2)
            os.close(log_fd)
            os.close(null_fd)
            os.execv(argv[0], list(argv))
        except BaseException:
            os._exit(127)
    os.close(log_fd)
    os.close(null_fd)
    return pid


def _terminate(pid, grace):
    """Stop one owned child: cooperative signal first, then kill, then reap."""
    cooperative = False
    try:
        os.kill(pid, signal.SIGTERM)
        cooperative = True
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        done, status, usage = os.wait4(pid, os.WNOHANG)
        if done == pid:
            return status, usage, cooperative, False
        time.sleep(0.02)
    escalated = False
    try:
        os.kill(pid, signal.SIGKILL)
        escalated = True
    except ProcessLookupError:
        pass
    _, status, usage = os.wait4(pid, 0)
    return status, usage, cooperative, escalated


def _decode(status):
    if os.WIFSIGNALED(status):
        return None, os.WTERMSIG(status)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status), None
    return None, None


def _peak_rss_bytes(value):
    if not value or value <= 0:
        return None
    return int(value) if sys.platform == "darwin" else int(value) * 1024


def supervise(
    argv, log_path, timeout_seconds, *, should_stop=None, poll=0.05, grace=5.0
):
    """Start one child, capture its stdout/stderr to ``log_path``, and reap it.

    Every branch reaps the child: normal exit, deadline, requested stop or an
    exception from the caller's ``should_stop``. Only this child is signalled.
    """
    argv = [str(item) for item in argv]
    started = time.monotonic()
    pid = _spawn(argv, log_path)
    status = None
    usage = None
    timed_out = False
    stop_requested = False
    try:
        deadline = started + timeout_seconds
        while True:
            done, raw_status, raw_usage = os.wait4(pid, os.WNOHANG)
            if done == pid:
                status, usage = raw_status, raw_usage
                break
            now = time.monotonic()
            if now >= deadline:
                timed_out = True
                break
            if should_stop is not None and should_stop():
                stop_requested = True
                break
            time.sleep(min(poll, max(0.0, deadline - now)))
    except BaseException:
        try:
            _terminate(pid, grace)
        except ChildProcessError:
            pass
        raise

    cooperative = False
    escalated = False
    if status is None:
        status, usage, cooperative, escalated = _terminate(pid, grace)
    wall = time.monotonic() - started
    exit_code, term_signal = _decode(status)
    cpu = None
    peak = None
    if usage is not None:
        cpu = float(usage.ru_utime) + float(usage.ru_stime)
        peak = _peak_rss_bytes(usage.ru_maxrss)
    return Outcome(
        pid=pid,
        exit_code=exit_code,
        term_signal=term_signal,
        timed_out=timed_out,
        runner_stop_requested=stop_requested,
        cooperative_stop=cooperative,
        escalated=escalated,
        reaped=True,
        wall_seconds=round(wall, 6),
        cpu_seconds=None if cpu is None else round(cpu, 6),
        peak_rss_bytes=peak,
    )


class _GrowingTranscript:
    """Read new bytes from a transcript another process is still writing."""

    def __init__(self, path):
        self._handle = open(path, "rb")
        self._text = ""

    def read(self):
        block = self._handle.read()
        if block:
            self._text += block.decode("utf-8", "replace")
        return self._text

    def close(self):
        self._handle.close()


def measurement_record(outcome):
    percent = None
    if outcome.cpu_seconds is not None and outcome.wall_seconds > 0:
        percent = round(outcome.cpu_seconds / outcome.wall_seconds * 100.0, 3)
    return {
        "pid": outcome.pid,
        "exit_code": outcome.exit_code,
        "term_signal": outcome.term_signal,
        "timed_out": outcome.timed_out,
        "runner_stop_requested": outcome.runner_stop_requested,
        "cooperative_stop": outcome.cooperative_stop,
        "escalated_to_kill": outcome.escalated,
        "reaped": outcome.reaped,
        "wall_seconds": outcome.wall_seconds,
        "cpu_seconds": outcome.cpu_seconds,
        "cpu_percent_of_one_core": percent,
        "peak_rss_bytes": outcome.peak_rss_bytes,
        "rusage_scope": (
            "os.wait4 rusage for this QEMU host process; guest kernel and "
            "userspace execute within it. "
            "resource.getrusage(RUSAGE_CHILDREN) is cumulative over every child "
            "this process reaped and is not used."
        ),
        "peak_rss_source": None
        if outcome.peak_rss_bytes is None
        else (
            "ru_maxrss from wait4: true high-water mark of this child, "
            + (
                "bytes (Darwin)"
                if sys.platform == "darwin"
                else "KiB converted to bytes"
            )
        ),
        "sampling": "none; kernel-accounted child values, not sampled estimates",
    }


def run(
    config, *, allow_unknown_qemu_provenance=False, allow_non_tci=False, nonce=None
):
    """Execute one boot and return its written result record."""
    provenance = inspect_qemu(config.qemu)
    require_tci_provenance(
        provenance,
        allow_unknown=allow_unknown_qemu_provenance,
        allow_non_tci=allow_non_tci,
    )

    nonce = (nonce or uuid.uuid4().hex).lower()
    if boot_evidence.NONCE_PATTERN.match(nonce) is None:
        raise ContractError("nonce must be 32 lowercase hex characters")

    inputs = verify_inputs(config)
    evidence = new_evidence_dir(config.evidence_root, nonce)
    transcript = evidence / "serial.log"
    transcript.touch()
    argv = build_argv(config, nonce, evidence)

    (evidence / "command.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "nonce": nonce,
                "evidence_dir": str(evidence),
                "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "config_path": str(config.path),
                "config": json.loads(config.path.read_text()),
                "command": argv,
                "qemu": {
                    **provenance,
                    "version": qemu_version(config.qemu),
                },
                "timeout_seconds": config.timeout_seconds,
                "stop_on_completion": config.stop_on_completion,
                "inputs": inputs,
            },
            indent=2,
        )
        + "\n"
    )

    watcher = None
    should_stop = None
    if config.stop_on_completion:
        watcher = _GrowingTranscript(transcript)
        checked = [0.0]

        def should_stop():
            text = watcher.read()
            now = time.monotonic()
            if now - checked[0] < 0.25:
                return False
            checked[0] = now
            if not all(
                marker in text
                for marker in (
                    f"ASTER-BOOT|{nonce}|",
                    "PROP|sys.boot_completed=1",
                    "FRAMEWORK|service_activity=found",
                    "FRAMEWORK|system_server_pid=",
                )
            ):
                return False
            return boot_evidence.analyze(text, nonce).ok

    try:
        outcome = supervise(
            argv,
            transcript,
            config.timeout_seconds,
            should_stop=should_stop,
        )
    finally:
        if watcher is not None:
            watcher.close()

    analysis = boot_evidence.analyze(
        transcript.read_bytes(),
        nonce,
        timed_out=outcome.timed_out,
        exit_code=outcome.exit_code,
        term_signal=outcome.term_signal,
        runner_stop_requested=outcome.runner_stop_requested,
    )

    result = {
        "schema_version": SCHEMA_VERSION,
        "passed": analysis.ok,
        "android_booted": analysis.ok,
        "nonce": nonce,
        "evidence_dir": str(evidence),
        "transcript": str(transcript),
        "config_path": str(config.path),
        "completed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "timeout_seconds": config.timeout_seconds,
        "measurement": measurement_record(outcome),
        "evidence": analysis.as_dict(),
    }
    (evidence / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run one configured Android host boot and gate it on "
        "nonce-bound guest evidence. The module docstring is the config contract."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--allow-unknown-qemu-provenance", action="store_true")
    parser.add_argument("--allow-non-tci", action="store_true")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        if args.evidence_root is not None:
            root = args.evidence_root
            if not root.is_absolute():
                root = ROOT / root
            config = replace(config, evidence_root=root)
        result = run(
            config,
            allow_unknown_qemu_provenance=args.allow_unknown_qemu_provenance,
            allow_non_tci=args.allow_non_tci,
        )
    except ContractError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("error: interrupted; any owned QEMU process was reaped", file=sys.stderr)
        return 130

    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

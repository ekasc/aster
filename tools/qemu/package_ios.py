#!/usr/bin/env python3
"""Package the actual executable link inputs, excluding its process entry point."""

from pathlib import Path
import shlex
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
build = root / "build/qemu/build-ios-tci"
commands = subprocess.check_output(
    ["ninja", "-t", "commands", "qemu-system-aarch64-unsigned"], cwd=build, text=True
)
link = shlex.split(commands.splitlines()[-1])
inputs = [
    str(build / word) if not Path(word).is_absolute() else word
    for word in link
    if word.endswith(".o") and not word.endswith("/system_main.c.o")
]
compile_commands = subprocess.check_output(
    ["ninja", "-t", "commands", "libqemu-aarch64-softmmu.a"], cwd=build, text=True
)
compile = next(
    shlex.split(line)
    for line in compile_commands.splitlines()
    if " -c " in line and " -DCONFIG_SOFTMMU " in line
)
output = build / "aster-ios-engine.o"
for flag in ["-MD", "-MQ", "-MF", "-o", "-c"]:
    if flag in compile:
        index = compile.index(flag)
        del compile[index : index + (1 if flag == "-MD" else 2)]
compile += [
    "-I" + str(root / "core/include"),
    "-c",
    str(root / "tools/qemu/ios_engine.c"),
    "-o",
    str(output),
]
subprocess.run(compile, cwd=build, check=True)
subprocess.run(
    [
        "xcrun",
        "libtool",
        "-static",
        "-o",
        str(build / "libaster-qemu.a"),
        str(output),
        *inputs,
    ],
    check=True,
)
print(build / "libaster-qemu.a")
libraries = [
    str(build / word) if not Path(word).is_absolute() else word
    for word in link
    if word.endswith(".a")
]
flags = [
    "-Wl,-force_load," + str(build / "libaster-qemu.a"),
    *libraries,
    "-lz",
    "-liconv",
    "-framework",
    "CoreFoundation",
]
(build / "ios-link.rsp").write_text(shlex.join(flags) + "\n")

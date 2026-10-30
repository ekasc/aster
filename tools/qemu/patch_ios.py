#!/usr/bin/env python3
"""Version-specific iOS TCI changes, with exact-match guards."""

from pathlib import Path
import sys

root = Path(sys.argv[1])
assert (root / "VERSION").read_text().strip() == "11.1.3"


def replace(name, before, after):
    path = root / name
    text = path.read_text()
    if after in text:
        return
    if text.count(before) != 1:
        raise SystemExit(f"{name}: expected one matching patch location")
    path.write_text(text.replace(before, after))


replace(
    "include/qemu/osdep.h",
    "#ifdef __APPLE__\nstatic inline void qemu_thread_jit_execute",
    "#if defined(__APPLE__) && !defined(CONFIG_TCG_INTERPRETER)\nstatic inline void qemu_thread_jit_execute",
)
replace(
    "block/file-posix.c",
    "#if defined(__APPLE__) && (__MACH__) && defined(HAVE_HOST_BLOCK_DEVICE)",
    "#ifdef __APPLE__\n#include <sys/mount.h>\n#endif\n\n#if defined(__APPLE__) && (__MACH__) && defined(HAVE_HOST_BLOCK_DEVICE)",
)
replace(
    "tcg/region.c",
    "#ifdef CONFIG_DARWIN\n    /* Applicable to both iOS and macOS (Apple Silicon). */",
    "#if defined(CONFIG_DARWIN) && !defined(CONFIG_TCG_INTERPRETER)\n    /* TCI stores bytecode, not executable host instructions. */",
)
replace(
    "accel/tcg/tcg-accel-ops-rr.c",
    "static void rr_wait_io_event(void)",
    "#ifdef ASTER_EMBEDDED\nextern bool aster_qemu_vcpu_stopping(void);\n#else\nstatic bool aster_qemu_vcpu_stopping(void) { return false; }\n#endif\n\nstatic void rr_wait_io_event(void)",
)
replace(
    "accel/tcg/tcg-accel-ops-rr.c",
    "while (all_cpu_threads_idle()) {",
    "while (all_cpu_threads_idle() && !aster_qemu_vcpu_stopping()) {",
)
replace(
    "accel/tcg/tcg-accel-ops-rr.c",
    "        rr_wait_io_event();\n",
    "        rr_wait_io_event();\n        if (aster_qemu_vcpu_stopping()) {\n            tcg_cpu_destroy(first_cpu);\n            bql_unlock();\n            rcu_remove_force_rcu_notifier(&force_rcu);\n            rcu_unregister_thread();\n            return NULL;\n        }\n",
)
replace(
    "system/main.c",
    "int main(int argc, char **argv)",
    "#ifdef ASTER_EMBEDDED\nbool aster_qemu_vcpu_stopping(void);\nbool aster_qemu_vcpu_stopping(void) { return false; }\n#endif\n\nint main(int argc, char **argv)",
)

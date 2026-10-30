// Phase 1 payload: must fault on an unmapped guest address. The host test
// asserts FaultKind::Unmapped rather than a successful exit.
#include "aster_guest.h"

void _start(void) {
  ast_puts("ASTER-FAULT-UNMAPPED start\n");
  *(volatile ast_u64*)0x1000 = 0xDEADBEEFCAFEF00DUL;
  ast_puts("ASTER-FAULT-UNMAPPED NOT-REACHED\n");
  ast_exit(0);
}

// Phase 1 payload: must fault with a permission error when writing a page that
// the ELF loader mapped read-only (.rodata).
#include "aster_guest.h"

static const ast_u64 readonly_table[4] = {1, 2, 3, 4};

void _start(void) {
  ast_puts("ASTER-FAULT-RODATA start\n");
  ast_mark("rodata_addr", (ast_u64)readonly_table);
  *(volatile ast_u64*)&readonly_table[1] = 0xFFFFFFFFFFFFFFFFUL;
  ast_puts("ASTER-FAULT-RODATA NOT-REACHED\n");
  ast_exit(0);
}

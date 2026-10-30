// Phase 1 payload: deterministic compute + data/BSS + unaligned access checks.
#include "aster_guest.h"

static ast_u64 aligned_data[4] = {0x1122334455667788UL, 0x99AABBCCDDEEFF00UL,
                                  0x0123456789ABCDEFUL, 0xFEDCBA9876543210UL};
static ast_u64 zero_data[8];
static ast_u8 scratch[256];

static ast_u64 mix(ast_u64 x) {
  return x * 6364136223846793005UL + 1442695040888963407UL;
}

void _start(void) {
  ast_puts("ASTER-PHASE1 hello\n");
  ast_puts("ping=");
  ast_put_hex(ast_ping());
  ast_puts("\n");

  ast_u64 sum = 0;
  for (ast_u64 i = 0; i < 200000UL; ++i) {
    sum = mix(sum ^ i);
  }

  int data_ok = 1;
  for (int i = 0; i < 4; ++i) {
    if (aligned_data[i] == 0) data_ok = 0;
  }
  if (aligned_data[0] != 0x1122334455667788UL) data_ok = 0;

  int bss_ok = 1;
  for (int i = 0; i < 8; ++i) {
    if (zero_data[i] != 0) bss_ok = 0;
  }

  for (int i = 0; i < 256; ++i) scratch[i] = (ast_u8)(i * 7 + 3);
  ast_u32 unaligned = *(volatile ast_u32*)(scratch + 1);
  ast_u32 expected = (ast_u32)scratch[1] | ((ast_u32)scratch[2] << 8) |
                     ((ast_u32)scratch[3] << 16) | ((ast_u32)scratch[4] << 24);
  int unaligned_ok = unaligned == expected;

  ast_puts("checksum=");
  ast_put_hex(sum);
  ast_puts("\n");
  ast_mark("data_ok", (ast_u64)data_ok);
  ast_mark("bss_ok", (ast_u64)bss_ok);
  ast_mark("unaligned_ok", (ast_u64)unaligned_ok);
  ast_mark("data_addr", (ast_u64)aligned_data);
  ast_mark("stack_top", (ast_u64)__builtin_frame_address(0));
  ast_mark("ticks_ns", ast_ticks());
  ast_mark("cpu_count", ast_cpu_count());

  if (!data_ok || !bss_ok || !unaligned_ok) {
    ast_puts("ASTER-PHASE1 FAIL\n");
    ast_exit(3);
  }
  ast_puts("ASTER-PHASE1 OK\n");
  ast_exit(0);
}

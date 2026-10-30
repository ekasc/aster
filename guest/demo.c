// Phase 1 payload: control flow, calls, recursion, loops, byte/half/word
// loads and stores, CRC32 and sorting. The host test compares these results
// against a native reference implementation of the same algorithms.
#include "aster_guest.h"

static ast_u32 crc32(const ast_u8* data, ast_u64 len) {
  ast_u32 crc = 0xFFFFFFFFu;
  for (ast_u64 i = 0; i < len; ++i) {
    crc ^= data[i];
    for (int bit = 0; bit < 8; ++bit) {
      ast_u32 mask = 0u - (crc & 1u);
      crc = (crc >> 1) ^ (0xEDB88320u & mask);
    }
  }
  return ~crc;
}

static ast_u64 fib(ast_u64 n) {
  if (n < 2) return n;
  return fib(n - 1) + fib(n - 2);
}

static void insertion_sort(ast_u16* values, int n) {
  for (int i = 1; i < n; ++i) {
    ast_u16 key = values[i];
    int j = i - 1;
    while (j >= 0 && values[j] > key) {
      values[j + 1] = values[j];
      --j;
    }
    values[j + 1] = key;
  }
}

static ast_u8 buffer[4096];
static ast_u16 numbers[256];
static ast_u8 heap_copy[512];

void _start(void) {
  ast_puts("ASTER-DEMO start\n");

  ast_u64 state = 0x123456789ABCDEF0UL;
  for (ast_u64 i = 0; i < sizeof(buffer); ++i) {
    state = state * 6364136223846793005UL + 1442695040888963407UL;
    buffer[i] = (ast_u8)(state >> 33);
  }
  ast_u32 crc = crc32(buffer, sizeof(buffer));
  ast_u64 f = fib(20);

  for (int i = 0; i < 256; ++i) numbers[i] = (ast_u16)((i * 40503 + 17) & 0xFFFF);
  insertion_sort(numbers, 256);
  ast_u64 sum = 0;
  int sorted = 1;
  for (int i = 0; i < 256; ++i) {
    sum += numbers[i];
    if (i > 0 && numbers[i - 1] > numbers[i]) sorted = 0;
  }

  for (int i = 0; i < 512; ++i) heap_copy[i] = (ast_u8)(buffer[i] ^ 0xA5);
  ast_u64 copy_sum = 0;
  for (int i = 0; i < 512; ++i) copy_sum += heap_copy[i];

  ast_puts("crc32=");
  ast_put_hex(crc);
  ast_puts("\n");
  ast_mark("fib20", f);
  ast_mark("sort_sum", sum);
  ast_mark("sorted", (ast_u64)sorted);
  ast_mark("copy_sum", copy_sum);

  // The host test recomputes crc32/sum independently from the same seed and
  // compares. The guest only asserts invariants it can check locally.
  if (f != 6765 || !sorted) {
    ast_puts("ASTER-DEMO FAIL\n");
    ast_exit(5);
  }
  ast_puts("ASTER-DEMO OK\n");
  ast_exit(0);
}

// Phase 1 payload: guest threading, shared memory and exclusive monitor.
#include "aster_guest.h"

#define THREADS 4
#define ITERATIONS 20000UL

static volatile ast_u64 counter;
static volatile ast_u64 done_count;
static ast_u8 stacks[THREADS][16384] __attribute__((aligned(16)));

// Lock-free increment built on LDXR/STXR, so the payload also exercises the
// interpreter's exclusive monitor under real host-thread contention.
static ast_u64 atomic_add(volatile ast_u64* p, ast_u64 value) {
  ast_u64 old, next;
  ast_u32 status;
  do {
    __asm__ volatile(
        "ldxr %0, [%3]\n"
        "add  %1, %0, %4\n"
        "stxr %w2, %1, [%3]\n"
        : "=&r"(old), "=&r"(next), "=&r"(status)
        : "r"(p), "r"(value)
        : "memory");
  } while (status != 0);
  return old;
}

static void worker(void* arg) {
  ast_u64 id = (ast_u64)arg;
  for (ast_u64 i = 0; i < ITERATIONS; ++i) {
    atomic_add(&counter, 1);
  }
  atomic_add(&done_count, 1);
  (void)id;
  ast_thread_exit(0);
}

void _start(void) {
  ast_puts("ASTER-THREADS start\n");
  ast_u32 spawned = 0;
  for (ast_u64 i = 0; i < THREADS; ++i) {
    ast_u32 tid = ast_spawn(worker, (void*)i, &stacks[i][16384]);
    if (tid != 0) ++spawned;
  }

  for (ast_u64 i = 0; i < ITERATIONS; ++i) {
    atomic_add(&counter, 1);
  }

  ast_u64 spins = 0;
  while (done_count < THREADS && spins < 200000000UL) {
    ast_yield();
    ++spins;
  }

  ast_mark("spawned", spawned);
  ast_mark("counter", counter);
  ast_mark("expected", (ast_u64)(THREADS + 1) * ITERATIONS);
  ast_mark("spins", spins);

  if (spawned != THREADS || counter != (ast_u64)(THREADS + 1) * ITERATIONS) {
    ast_puts("ASTER-THREADS FAIL\n");
    ast_exit(4);
  }
  ast_puts("ASTER-THREADS OK\n");
  ast_exit(0);
}

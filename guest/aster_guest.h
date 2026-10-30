// Guest-side runtime for Aster Phase 1 payloads.
//
// Freestanding AArch64, no libc, no Linux syscalls: the only host interface is
// HVC. That is what lets the same payload run under the interpreter on stock
// iOS without a kernel behind it.
#pragma once

typedef unsigned char ast_u8;
typedef unsigned short ast_u16;
typedef unsigned int ast_u32;
typedef unsigned long ast_u64;
typedef long ast_i64;

static inline ast_u64 ast_hvc(ast_u64 id, ast_u64 a1, ast_u64 a2, ast_u64 a3) {
  // Every argument must be pinned to the architectural register the HVC ABI
  // names. Generic "r" operands are not enough: the compiler may place them in
  // any register, while the guest ABI reads x0..x3.
  register ast_u64 x0 __asm__("x0") = id;
  register ast_u64 x1 __asm__("x1") = a1;
  register ast_u64 x2 __asm__("x2") = a2;
  register ast_u64 x3 __asm__("x3") = a3;
  __asm__ volatile("hvc #0"
                   : "+r"(x0), "+r"(x1)
                   : "r"(x2), "r"(x3)
                   : "memory");
  return x0;
}

#define AST_HYPER_PING 0
#define AST_HYPER_WRITE 1
#define AST_HYPER_EXIT 2
#define AST_HYPER_TICKS 3
#define AST_HYPER_SPAWN 4
#define AST_HYPER_YIELD 5
#define AST_HYPER_THREAD_EXIT 6
#define AST_HYPER_SLEEP_NS 8
#define AST_HYPER_CPU_COUNT 10

static inline ast_u64 ast_ping(void) { return ast_hvc(AST_HYPER_PING, 0, 0, 0); }
static inline ast_u64 ast_write(int fd, const void* buf, ast_u64 len) {
  return ast_hvc(AST_HYPER_WRITE, (ast_u64)fd, (ast_u64)buf, len);
}
static inline ast_u64 ast_ticks(void) { return ast_hvc(AST_HYPER_TICKS, 0, 0, 0); }
static inline ast_u32 ast_spawn(void (*entry)(void*), void* arg, void* stack_top) {
  return (ast_u32)ast_hvc(AST_HYPER_SPAWN, (ast_u64)entry, (ast_u64)arg,
                          (ast_u64)stack_top);
}
static inline void ast_yield(void) { (void)ast_hvc(AST_HYPER_YIELD, 0, 0, 0); }
static inline void ast_sleep_ns(ast_u64 ns) {
  (void)ast_hvc(AST_HYPER_SLEEP_NS, ns, 0, 0);
}
static inline ast_u32 ast_cpu_count(void) { return (ast_u32)ast_hvc(AST_HYPER_CPU_COUNT, 0, 0, 0); }

__attribute__((noreturn)) static inline void ast_exit(int code) {
  (void)ast_hvc(AST_HYPER_EXIT, (ast_u64)code, 0, 0);
  for (;;) {
  }
}

__attribute__((noreturn)) static inline void ast_thread_exit(int code) {
  (void)ast_hvc(AST_HYPER_THREAD_EXIT, (ast_u64)code, 0, 0);
  for (;;) {
  }
}

// libc bits the compiler may emit calls to in freestanding mode.
void* memset(void* dst, int value, ast_u64 len);
void* memcpy(void* dst, const void* src, ast_u64 len);
int memcmp(const void* a, const void* b, ast_u64 len);
ast_u64 strlen(const char* s);

static inline void ast_puts(const char* s) {
  ast_write(1, s, strlen(s));
}

static inline void ast_put_hex(ast_u64 value) {
  static const char digits[] = "0123456789abcdef";
  char buf[18];
  buf[0] = '0';
  buf[1] = 'x';
  for (int i = 0; i < 16; ++i) {
    buf[2 + i] = digits[(value >> (60 - 4 * i)) & 0xF];
  }
  ast_write(1, buf, sizeof(buf));
}

static inline void ast_put_dec(ast_i64 value) {
  char buf[24];
  int pos = 0;
  ast_u64 v;
  if (value < 0) {
    ast_write(1, "-", 1);
    v = (ast_u64)(-value);
  } else {
    v = (ast_u64)value;
  }
  if (v == 0) {
    ast_write(1, "0", 1);
    return;
  }
  while (v > 0 && pos < (int)sizeof(buf)) {
    buf[pos++] = (char)('0' + (v % 10));
    v /= 10;
  }
  for (int i = 0; i < pos / 2; ++i) {
    char t = buf[i];
    buf[i] = buf[pos - 1 - i];
    buf[pos - 1 - i] = t;
  }
  ast_write(1, buf, (ast_u64)pos);
}

static inline void ast_mark(const char* name, ast_u64 value) {
  ast_puts(name);
  ast_puts("=");
  ast_put_dec((ast_i64)value);
  ast_puts("\n");
}

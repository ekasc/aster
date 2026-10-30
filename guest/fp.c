// Phase 1 payload: floating point arithmetic and conversions.
#include "aster_guest.h"

void _start(void) {
  ast_puts("ASTER-FP start\n");
  // Volatile inputs keep the compiler from folding the whole computation
  // into integer constants: the FP instructions must actually execute.
  volatile double va = 1.5;
  volatile double vb = 2.25;
  volatile double three = 3.0;
  double a = va;
  double b = vb;
  double c = (a * b + three) / b;
  double d = c * 1000.0;
  ast_i64 di = (ast_i64)d;

  volatile float vf = 7.0f;
  volatile float vg = 2.0f;
  float e = vf / vg;
  float g = e * 10.0f;
  ast_i64 gi = (ast_i64)g;

  volatile double four = 4.0;
  double neg = -a * four;
  ast_i64 ni = (ast_i64)neg;

  ast_mark("double_milli", (ast_u64)di);
  ast_mark("float_tenths", (ast_u64)gi);
  ast_mark("negative", (ast_u64)ni);
  ast_mark("cmp_lt", (ast_u64)(a < b));

  if (di != 2833 || gi != 35 || ni != -6) {
    ast_puts("ASTER-FP FAIL\n");
    ast_exit(6);
  }
  ast_puts("ASTER-FP OK\n");
  ast_exit(0);
}

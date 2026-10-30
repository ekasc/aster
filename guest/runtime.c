// Freestanding replacements for the mem* intrinsics clang may emit.

typedef unsigned long ast_size;

void* memset(void* dst, int value, ast_size len) {
  unsigned char* d = (unsigned char*)dst;
  for (ast_size i = 0; i < len; ++i) d[i] = (unsigned char)value;
  return dst;
}

void* memcpy(void* dst, const void* src, ast_size len) {
  unsigned char* d = (unsigned char*)dst;
  const unsigned char* s = (const unsigned char*)src;
  for (ast_size i = 0; i < len; ++i) d[i] = s[i];
  return dst;
}

int memcmp(const void* a, const void* b, ast_size len) {
  const unsigned char* x = (const unsigned char*)a;
  const unsigned char* y = (const unsigned char*)b;
  for (ast_size i = 0; i < len; ++i) {
    if (x[i] != y[i]) return (int)x[i] - (int)y[i];
  }
  return 0;
}

ast_size strlen(const char* s) {
  ast_size n = 0;
  while (s[n] != 0) ++n;
  return n;
}

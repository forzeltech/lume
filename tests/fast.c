#include "lume_rt.h"
int64_t c_popcount(uint64_t x) { return __builtin_popcountll(x); }
int64_t c_strsum(lu_str s) { int64_t t = 0; for (int64_t i = 0; i < s.len; i++) t += (unsigned char)s.p[i]; return t; }
double c_hypot(double a, double b) { return __builtin_sqrt(a * a + b * b); }

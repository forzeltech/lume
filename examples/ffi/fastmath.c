/* Low-level code lives in plain C files. This is the only place in a Lume
 * program where memory unsafety is possible. */
#include "lume_rt.h"
#include <math.h>

double c_sqrt(double x) { return sqrt(x); }

/* CRC-32 (IEEE), bitwise. Strings arrive as lu_str = { const char *p; int64_t len; } */
uint32_t c_crc32(lu_str s) {
    uint32_t crc = 0xFFFFFFFFu;
    for (int64_t i = 0; i < s.len; i++) {
        crc ^= (unsigned char)s.p[i];
        for (int k = 0; k < 8; k++) crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
    }
    return ~crc;
}

int64_t c_popcount(uint64_t x) { return __builtin_popcountll(x); }

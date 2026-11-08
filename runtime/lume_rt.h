/* Lume runtime: arenas, strings, arrays, errors, tasks, panics.
 * Every Lume program includes this header; generated code is plain C11 plus
 * GNU statement expressions (GCC / Clang). */
#ifndef LUME_RT_H
#define LUME_RT_H
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>

/* ---------- arenas (regions) ---------- */
typedef struct lu_chunk { struct lu_chunk *next; size_t used, cap; _Alignas(16) char data[]; } lu_chunk;
struct lu_task;
typedef struct lu_arena {
    lu_chunk *head;
    struct lu_arena *fwd;       /* set when a thread's root arena is adopted by its joiner */
    struct lu_arena *adopted;   /* heap-allocated child arena headers to free with us */
    struct lu_arena *next_adopted;
    struct lu_task *tasks;      /* tasks spawned here; joined before we are freed */
} lu_arena;

extern __thread lu_arena *lu_cur;
void  lu_arena_init(lu_arena *a);
void  lu_arena_free(lu_arena *a);
void *lu_alloc_in(lu_arena *a, size_t n);
static inline void *lu_alloc(size_t n) { return lu_alloc_in(lu_cur, n); }
void  lu_arena_adopt(lu_arena *dst, lu_arena *child);
int64_t lu_live_bytes(void);

/* ---------- panics ---------- */
#define LU_LOC __FILE__, __LINE__
_Noreturn void lu_panic(const char *msg, const char *file, int line);
_Noreturn void lu_panic_index(int64_t i, int64_t len, const char *file, int line);

/* ---------- strings: immutable byte slices ---------- */
typedef struct { const char *p; int64_t len; } lu_str;
#define LU_STR(lit) ((lu_str){ (lit), (int64_t)sizeof(lit) - 1 })
static inline bool lu_str_eq(lu_str a, lu_str b) { return a.len == b.len && (a.len == 0 || memcmp(a.p, b.p, (size_t)a.len) == 0); }
lu_str lu_str_copy(lu_str s, lu_arena *a);
lu_str lu_str_concat(lu_str a, lu_str b);
static inline uint8_t lu_str_at(lu_str s, int64_t i, const char *f, int l) {
    if ((uint64_t)i >= (uint64_t)s.len) lu_panic_index(i, s.len, f, l);
    return (uint8_t)s.p[i];
}

static inline lu_str lu_str_slice(lu_str s, int64_t lo, int64_t hi, const char *f, int l) {
    if (lo < 0 || hi < lo || hi > s.len) lu_panic("string slice out of bounds", f, l);
    return (lu_str){ s.p + lo, hi - lo };
}

/* string builder used by interpolation and to-string */
typedef struct { char *p; int64_t len, cap; } lu_sb;
void lu_sb_put(lu_sb *b, const char *s, int64_t n);
static inline void lu_sb_str(lu_sb *b, lu_str s) { lu_sb_put(b, s.p, s.len); }
void lu_sb_i64(lu_sb *b, int64_t v);
void lu_sb_u64(lu_sb *b, uint64_t v);
void lu_sb_f64(lu_sb *b, double v);
static inline void lu_sb_bool(lu_sb *b, bool v) { if (v) lu_sb_put(b, "true", 4); else lu_sb_put(b, "false", 5); }
void lu_sb_quoted(lu_sb *b, lu_str s);
static inline lu_str lu_sb_done(lu_sb *b) { return (lu_str){ b->p, b->len }; }
void lu_print_str(lu_str s, bool to_err);

/* ---------- errors ---------- */
typedef struct { lu_str msg; } lu_err;
void lu_trace_reset(void);
void lu_trace_push(const char *file, int line);
void lu_trace_print(void);
static inline lu_err lu_err_new(lu_str msg, const char *file, int line) {
    lu_trace_reset(); lu_trace_push(file, line); return (lu_err){ msg };
}
static inline lu_err lu_err_keep(lu_err e, lu_arena *a) { e.msg = lu_str_copy(e.msg, a); return e; }

#define LU_RES_DEF(N, T) typedef struct { bool ok; T val; lu_err err; } N;
typedef struct { bool ok; lu_err err; } lu_res_void;

/* ---------- dynamic arrays (region-owned) ---------- */
#define LU_ARR_TYPE(N, T) typedef struct N N; struct N { T *data; int64_t len, cap; lu_arena *ar; };
#define LU_ARR_FUNCS(N, T) \
static inline N *N##_new(int64_t cap) { \
    N *a = (N *)lu_alloc(sizeof(N)); a->ar = lu_cur; a->len = 0; a->cap = cap; \
    a->data = cap ? (T *)lu_alloc(sizeof(T) * (size_t)cap) : NULL; return a; } \
static inline void N##_push(N *a, T v) { \
    if (a->len == a->cap) { int64_t nc = a->cap ? a->cap * 2 : 4; \
        T *nd = (T *)lu_alloc_in(a->ar, sizeof(T) * (size_t)nc); \
        if (a->len) memcpy(nd, a->data, sizeof(T) * (size_t)a->len); \
        a->data = nd; a->cap = nc; } \
    a->data[a->len++] = v; } \
static inline T *N##_at(N *a, int64_t i, const char *f, int l) { \
    if ((uint64_t)i >= (uint64_t)a->len) { lu_panic_index(i, a->len, f, l); } return &a->data[i]; } \
static inline N *N##_slice(N *a, int64_t lo, int64_t hi, const char *f, int l) { \
    if (lo < 0 || hi < lo || hi > a->len) { lu_panic("array slice out of bounds", f, l); } \
    N *r = N##_new(hi - lo); if (hi > lo) memcpy(r->data, a->data + lo, sizeof(T) * (size_t)(hi - lo)); r->len = hi - lo; return r; } \
static inline T N##_pop(N *a, const char *f, int l) { \
    if (a->len == 0) { lu_panic("pop from empty array", f, l); } return a->data[--a->len]; }

/* ---------- tasks (threads with their own arena) ---------- */
typedef struct lu_task {
    void *thread;               /* pthread_t, boxed to keep pthread.h out of user code */
    lu_arena *arena;            /* child's root arena (heap header) */
    void *(*body)(struct lu_task *);
    void *args;                 /* copied into child arena before start */
    void *result;               /* points into child arena */
    bool joined;
    struct lu_task *next;       /* in spawning arena's task list */
} lu_task;
lu_task *lu_task_new(void *(*body)(lu_task *));
void lu_task_start(lu_task *t);
void *lu_task_join(lu_task *t, const char *file, int line);

/* ---------- cycle tracking for print / == / deep copy of arrays ---------- */
void *lu_visit_find(const void *old);
void lu_visit_push(const void *old, void *mapped);
void lu_visit_pop(void);

/* ---------- process ---------- */
void lu_init(int argc, char **argv);
int64_t lu_argc(void);
lu_str lu_argv(int64_t i, const char *file, int line);
int64_t lu_now_ns(void);

/* ---------- checked arithmetic ---------- */
#define LU_ADD(T, a, b) ({ T __r; if (__builtin_add_overflow((a), (b), &__r)) lu_panic("integer overflow in +", LU_LOC); __r; })
#define LU_SUB(T, a, b) ({ T __r; if (__builtin_sub_overflow((a), (b), &__r)) lu_panic("integer overflow in -", LU_LOC); __r; })
#define LU_MUL(T, a, b) ({ T __r; if (__builtin_mul_overflow((a), (b), &__r)) lu_panic("integer overflow in *", LU_LOC); __r; })
#define LU_SIGNED(T) ((T)-1 < (T)0)
#define LU_DIV(T, a, b) ({ T __a = (a), __b = (b); if (__b == 0) lu_panic("division by zero", LU_LOC); \
    T __r; if (LU_SIGNED(T) && __b == (T)-1) { if (__builtin_sub_overflow((T)0, __a, &__r)) lu_panic("integer overflow in /", LU_LOC); } \
    else __r = (T)(__a / __b); __r; })
#define LU_MOD(T, a, b) ({ T __a = (a), __b = (b); if (__b == 0) lu_panic("division by zero", LU_LOC); \
    (LU_SIGNED(T) && __b == (T)-1) ? (T)0 : (T)(__a % __b); })
#define LU_SHL(T, a, b) ({ T __a = (a); int64_t __b = (int64_t)(b); if (__b < 0 || __b >= (int64_t)(sizeof(T)*8)) lu_panic("shift out of range", LU_LOC); (T)((uint64_t)__a << __b); })
#define LU_SHR(T, a, b) ({ T __a = (a); int64_t __b = (int64_t)(b); if (__b < 0 || __b >= (int64_t)(sizeof(T)*8)) lu_panic("shift out of range", LU_LOC); (T)(__a >> __b); })
#define LU_NEG(T, a) ({ T __r; if (__builtin_sub_overflow((T)0, (a), &__r)) lu_panic("integer overflow in unary -", LU_LOC); __r; })
#define LU_CAST(T, a) ({ __typeof__(a) __v = (a); T __r = (T)__v; \
    if ((__typeof__(a))__r != __v || ((__r < 0) != (__v < 0))) lu_panic("integer cast out of range", LU_LOC); __r; })

LU_ARR_TYPE(lu_arr_str, lu_str)
LU_ARR_FUNCS(lu_arr_str, lu_str)

#endif

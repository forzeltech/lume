#define _GNU_SOURCE
#include "lume_rt.h"
#include <pthread.h>
#include <setjmp.h>
#include <stdatomic.h>
#include <time.h>
#include <unistd.h>

__thread lu_arena *lu_cur;

/* ---------- arenas ---------- */
#define LU_CHUNK 65536
#define LU_CACHE_MAX 32
static __thread lu_chunk *chunk_cache;
static __thread int chunk_cache_n;
static atomic_llong live_bytes;

int64_t lu_live_bytes(void) { return atomic_load(&live_bytes); }

void lu_arena_init(lu_arena *a) { memset(a, 0, sizeof *a); }

static lu_chunk *chunk_get(size_t need) {
    size_t cap = need > LU_CHUNK ? need : LU_CHUNK;
    lu_chunk *c;
    if (cap == LU_CHUNK && chunk_cache) { c = chunk_cache; chunk_cache = c->next; chunk_cache_n--; }
    else {
        c = malloc(sizeof(lu_chunk) + cap);
        if (!c) { fputs("lume: out of memory\n", stderr); abort(); }
        c->cap = cap;
    }
    atomic_fetch_add(&live_bytes, (long long)c->cap);
    c->used = 0; c->next = NULL;
    return c;
}

static void chunk_put(lu_chunk *c) {
    atomic_fetch_sub(&live_bytes, (long long)c->cap);
#ifdef LU_NO_CACHE
    free(c);
#else
    if (c->cap == LU_CHUNK && chunk_cache_n < LU_CACHE_MAX) { c->next = chunk_cache; chunk_cache = c; chunk_cache_n++; }
    else free(c);
#endif
}

void *lu_alloc_in(lu_arena *a, size_t n) {
    while (a->fwd) a = a->fwd;
    n = (n + 15) & ~(size_t)15;
    if (n == 0) n = 16;
    lu_chunk *c = a->head;
    if (!c || c->used + n > c->cap) {
        c = chunk_get(n);
        if (a->head && n > LU_CHUNK / 4) { c->next = a->head->next; a->head->next = c; } /* big: keep current chunk active */
        else { c->next = a->head; a->head = c; }
    }
    void *p = c->data + c->used;
    c->used += n;
    return p;
}

static void task_join_raw(lu_task *t);

void lu_arena_free(lu_arena *a) {
    /* structured concurrency: no task outlives the region that spawned it */
    for (lu_task *t = a->tasks; t; t = t->next) if (!t->joined) task_join_raw(t);
    a->tasks = NULL;
    lu_chunk *c = a->head;
    while (c) { lu_chunk *n = c->next; chunk_put(c); c = n; }
    a->head = NULL;
    lu_arena *ad = a->adopted;
    while (ad) { lu_arena *n = ad->next_adopted; free(ad); ad = n; }
    a->adopted = NULL;
}

/* O(1)-ish hand-off: splice the child's chunks into dst. No copying. */
void lu_arena_adopt(lu_arena *dst, lu_arena *child) {
    while (dst->fwd) dst = dst->fwd;
    if (child->head) {
        lu_chunk *tail = child->head;
        while (tail->next) tail = tail->next;
        if (dst->head) { tail->next = dst->head->next; dst->head->next = child->head; }
        else dst->head = child->head;
        child->head = NULL;
    }
    if (child->adopted) {
        lu_arena *t = child->adopted;
        while (t->next_adopted) t = t->next_adopted;
        t->next_adopted = dst->adopted; dst->adopted = child->adopted; child->adopted = NULL;
    }
    child->fwd = dst;
    child->next_adopted = dst->adopted;
    dst->adopted = child;
}

/* ---------- panics, test mode ---------- */
__thread jmp_buf *lu_test_jmp;   /* per thread: a panic never longjmps across threads */
_Noreturn void lu_panic(const char *msg, const char *file, int line) {
    fflush(stdout);
    fprintf(stderr, "panic: %s\n    at %s:%d\n", msg, file, line);
    if (lu_test_jmp) longjmp(*lu_test_jmp, 1);
    exit(101);
}
_Noreturn void lu_panic_index(int64_t i, int64_t len, const char *file, int line) {
    char buf[96];
    snprintf(buf, sizeof buf, "index %lld out of bounds (len %lld)", (long long)i, (long long)len);
    lu_panic(buf, file, line);
}

/* ---------- strings ---------- */
lu_str lu_str_copy(lu_str s, lu_arena *a) {
    if (s.len == 0) return s;
    char *p = lu_alloc_in(a, (size_t)s.len);
    memcpy(p, s.p, (size_t)s.len);
    return (lu_str){ p, s.len };
}
lu_str lu_str_concat(lu_str a, lu_str b) {
    if (!a.len) return b;
    if (!b.len) return a;
    char *p = lu_alloc((size_t)(a.len + b.len));
    memcpy(p, a.p, (size_t)a.len); memcpy(p + a.len, b.p, (size_t)b.len);
    return (lu_str){ p, a.len + b.len };
}
void lu_sb_put(lu_sb *b, const char *s, int64_t n) {
    if (b->len + n > b->cap) {
        int64_t nc = b->cap ? b->cap * 2 : 32;
        while (nc < b->len + n) nc *= 2;
        char *np = lu_alloc((size_t)nc);
        if (b->len) memcpy(np, b->p, (size_t)b->len);
        b->p = np; b->cap = nc;
    }
    if (n) memcpy(b->p + b->len, s, (size_t)n);
    b->len += n;
}
void lu_sb_i64(lu_sb *b, int64_t v) { char t[24]; int n = snprintf(t, sizeof t, "%lld", (long long)v); lu_sb_put(b, t, n); }
void lu_sb_u64(lu_sb *b, uint64_t v) { char t[24]; int n = snprintf(t, sizeof t, "%llu", (unsigned long long)v); lu_sb_put(b, t, n); }
void lu_sb_f64(lu_sb *b, double v) {
    char t[40]; int n = snprintf(t, sizeof t, "%.15g", v);
    if (strtod(t, NULL) != v) n = snprintf(t, sizeof t, "%.17g", v);
    lu_sb_put(b, t, n);
    if (!strpbrk(t, ".eEn")) lu_sb_put(b, ".0", 2);   /* keep floats visibly floats; skip nan/inf */
}
void lu_sb_quoted(lu_sb *b, lu_str s) {
    lu_sb_put(b, "\"", 1);
    for (int64_t i = 0; i < s.len; i++) {
        char c = s.p[i];
        if (c == '"' || c == '\\') { lu_sb_put(b, "\\", 1); lu_sb_put(b, &c, 1); }
        else if (c == '\n') lu_sb_put(b, "\\n", 2);
        else lu_sb_put(b, &c, 1);
    }
    lu_sb_put(b, "\"", 1);
}
void lu_print_str(lu_str s, bool to_err) {
    FILE *f = to_err ? stderr : stdout;
    fwrite(s.p, 1, (size_t)s.len, f);
    fputc('\n', f);
}

/* ---------- error return traces ---------- */
#define LU_TRACE_MAX 32
static __thread struct { const char *file; int line; } trace[LU_TRACE_MAX];
static __thread int trace_n, trace_dropped;
void lu_trace_reset(void) { trace_n = 0; trace_dropped = 0; }
void lu_trace_push(const char *file, int line) {
    if (trace_n < LU_TRACE_MAX) { trace[trace_n].file = file; trace[trace_n].line = line; trace_n++; }
    else trace_dropped++;
}
void lu_trace_print(void) {
    for (int i = 0; i < trace_n; i++)
        fprintf(stderr, "    %s %s:%d\n", i == 0 ? "raised at" : "passed through", trace[i].file, trace[i].line);
    if (trace_dropped) fprintf(stderr, "    ... %d more frames\n", trace_dropped);
}

/* ---------- tasks ---------- */
lu_task *lu_task_new(void *(*body)(lu_task *)) {
    lu_task *t = lu_alloc(sizeof *t);
    memset(t, 0, sizeof *t);
    t->body = body;
    t->arena = malloc(sizeof(lu_arena));
    lu_arena_init(t->arena);
    t->next = lu_cur->tasks;      /* registered with the spawning region */
    lu_cur->tasks = t;
    return t;
}
static void *task_main(void *p) {
    lu_task *t = p;
    lu_cur = t->arena;
    t->result = t->body(t);
    /* structured concurrency: a task's own subtasks finish before it does */
    for (lu_task *s = t->arena->tasks; s; s = s->next) if (!s->joined) task_join_raw(s);
    t->arena->tasks = NULL;
    lu_cur = NULL;
    return NULL;
}
void lu_task_start(lu_task *t) {
    pthread_t *th = lu_alloc(sizeof(pthread_t));
    t->thread = th;
    if (pthread_create(th, NULL, task_main, t) != 0) lu_panic("could not start thread", LU_LOC);
}
static void task_join_raw(lu_task *t) {
    pthread_join(*(pthread_t *)t->thread, NULL);
    t->joined = true;
    lu_arena_free(t->arena);       /* result discarded */
    free(t->arena);
    t->arena = NULL;
}
void *lu_task_join(lu_task *t, const char *file, int line) {
    if (t->joined) lu_panic("task joined twice", file, line);
    pthread_join(*(pthread_t *)t->thread, NULL);
    t->joined = true;
    lu_arena_adopt(lu_cur, t->arena);   /* result's memory now belongs to the joiner */
    t->arena = NULL;
    return t->result;
}

/* ---------- cycle tracking ---------- */
static __thread struct { const void *old; void *mapped; } *visit;
static __thread int visit_n, visit_cap;
void *lu_visit_find(const void *old) {
    for (int i = visit_n - 1; i >= 0; i--) if (visit[i].old == old) return visit[i].mapped;
    return NULL;
}
void lu_visit_push(const void *old, void *mapped) {
    if (visit_n == visit_cap) {
        visit_cap = visit_cap ? visit_cap * 2 : 64;
        visit = realloc(visit, sizeof *visit * (size_t)visit_cap);
        if (!visit) { fputs("lume: out of memory\n", stderr); abort(); }
    }
    visit[visit_n].old = old; visit[visit_n].mapped = mapped; visit_n++;
}
void lu_visit_pop(void) { visit_n--; }
void lu_visit_reset(void) { visit_n = 0; }

/* ---------- process ---------- */
static int g_argc; static char **g_argv;
#include <signal.h>
static void on_segv(int sig) {
    (void)sig;
    static const char m[] = "panic: stack overflow (or invalid memory access in extern C code)\n";
    if (write(2, m, sizeof m - 1) < 0) {}
    _exit(101);
}
void lu_init(int argc, char **argv) {
    g_argc = argc; g_argv = argv;
#if !defined(__SANITIZE_ADDRESS__) && !defined(__SANITIZE_THREAD__)
    static char altstack[65536];
    stack_t ss = { .ss_sp = altstack, .ss_size = sizeof altstack, .ss_flags = 0 };
    sigaltstack(&ss, NULL);
    struct sigaction sa; memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_segv; sa.sa_flags = SA_ONSTACK;
    sigaction(SIGSEGV, &sa, NULL);
#endif
    setvbuf(stdout, NULL, _IOFBF, 1 << 16);
}
int64_t lu_argc(void) { return g_argc; }
lu_str lu_argv(int64_t i, const char *file, int line) {
    if (i < 0 || i >= g_argc) lu_panic_index(i, g_argc, file, line);
    return (lu_str){ g_argv[i], (int64_t)strlen(g_argv[i]) };
}
int64_t lu_now_ns(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

_Noreturn void lu_panic_str(lu_str s, const char *file, int line) {
    char *buf = malloc((size_t)s.len + 1);
    memcpy(buf, s.p, (size_t)s.len); buf[s.len] = 0;
    lu_panic(buf, file, line);
}

/* ---------- files ---------- */
#include <errno.h>
typedef struct lu_res_str { bool ok; lu_str val; lu_err err; } lu_res_str;

static lu_res_str io_err(const char *what, lu_str path, const char *f, int l) {
    lu_sb b = {0};
    lu_sb_put(&b, what, (int64_t)strlen(what)); lu_sb_put(&b, " '", 2); lu_sb_str(&b, path);
    lu_sb_put(&b, "': ", 3); const char *m = strerror(errno); lu_sb_put(&b, m, (int64_t)strlen(m));
    return (lu_res_str){ .ok = false, .err = lu_err_new(lu_sb_done(&b), f, l) };
}
static char *cpath(lu_str p) { char *s = lu_alloc((size_t)p.len + 1); memcpy(s, p.p, (size_t)p.len); s[p.len] = 0; return s; }
static lu_res_str read_all(FILE *fp) {
    lu_sb b = {0}; char buf[65536]; size_t n;
    while ((n = fread(buf, 1, sizeof buf, fp)) > 0) lu_sb_put(&b, buf, (int64_t)n);
    return (lu_res_str){ .ok = true, .val = lu_sb_done(&b) };
}
lu_res_str lu_read_file(lu_str path, const char *f, int l) {
    FILE *fp = fopen(cpath(path), "rb");
    if (!fp) return io_err("cannot open", path, f, l);
    lu_res_str r = read_all(fp);
    if (ferror(fp)) r = io_err("cannot read", path, f, l);
    fclose(fp);
    return r;
}
lu_res_str lu_read_stdin(const char *f, int l) { (void)f; (void)l; return read_all(stdin); }
lu_res_void lu_write_file(lu_str path, lu_str data, const char *f, int l) {
    FILE *fp = fopen(cpath(path), "wb");
    if (!fp) { lu_res_str e = io_err("cannot create", path, f, l); return (lu_res_void){ .ok = false, .err = e.err }; }
    size_t w = fwrite(data.p, 1, (size_t)data.len, fp);
    int bad = (w != (size_t)data.len) | (fclose(fp) != 0);
    if (bad) { lu_res_str e = io_err("cannot write", path, f, l); return (lu_res_void){ .ok = false, .err = e.err }; }
    return (lu_res_void){ .ok = true };
}

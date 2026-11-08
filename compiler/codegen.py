"""Lume checker + C code generator (single pass).

Region model
------------
Every heap value (str, arrays, structs holding them, tasks) lives in an arena.
Each function body and each loop body is a *candidate region*: if no heap
value created inside it can escape, the compiler gives it its own arena that
is freed when the scope exits. If something escapes, the candidate is
re-checked as a plain scope, and its allocations go to the enclosing region.
`region { }` is a *hard* region: an escape out of it is a compile error.

Every expression gets a "depth": the region nesting level of the arena its
heap data lives in (-1 = static / no heap data). A value may only be stored
into a place whose depth is >= the value's depth.
"""
import os
from lexer import CompileError
from parser import Node
from ltypes import *  # noqa


def c_string(s):
    out = []
    for b in s.encode("utf-8"):
        ch = chr(b)
        if ch == '"': out.append('\\"')
        elif ch == "\\": out.append("\\\\")
        elif ch == "\n": out.append("\\n")
        elif ch == "\t": out.append("\\t")
        elif ch == "\r": out.append("\\r")
        elif 32 <= b < 127: out.append(ch)
        else: out.append(f'\\{b:03o}')
    return '"' + "".join(out) + '"'


def has_heap(t, structs, seen=None):
    if isinstance(t, Prim): return t.name == "str"
    if isinstance(t, (Arr, TaskT, ErrT)): return True
    if isinstance(t, Fn): return False
    if isinstance(t, Res): return True  # error message
    if isinstance(t, Tup): return any(has_heap(x, structs, seen) for x in t.items)
    if isinstance(t, StructT):
        seen = seen or set()
        if t.name in seen: return False
        seen.add(t.name)
        return any(has_heap(ft, structs, seen) for _, ft in structs[t.name])
    return False


class Var:
    def __init__(self, name, ty, mutable, depth, cname, ptr=False):
        self.name, self.ty, self.mutable, self.depth, self.cname, self.ptr = name, ty, mutable, depth, cname, ptr
        self.lo = INF      # outermost region of any mutable container this value may alias
        self.key = None    # declaration identity, for alias hints that persist across re-checks
        self.used = False
        self.check_used = False

    @property
    def code(self):
        return f"(*{self.cname})" if self.ptr else self.cname


INF = 10 ** 9


class Cand:
    """A region candidate (function or loop body) or a hard `region` block."""
    _n = 0

    def __init__(self, depth, hard):
        Cand._n += 1
        self.id, self.depth, self.hard = Cand._n, depth, hard
        self.escaped = False
        self.allocs = False


class Scope:
    def __init__(self, parent, depth, cand=None, loop=False, fn=False):
        self.parent, self.depth, self.cand, self.loop, self.fn = parent, depth, cand, loop, fn
        self.vars = {}
        self.cleanups = []   # C code strings, run in reverse order at exit


class Gen:
    def __init__(self, files):
        self.files = files            # list of (path, decls)
        self.structs = {}             # name -> [(field, type)]
        self.struct_lines = {}
        self.file_fns = {}            # file -> {name: fn Node}   (declared in that file)
        self.file_consts = {}         # file -> {name: (type, cname, pub)}
        self.struct_file = {}         # struct name -> (file, pub)
        self.imports = {f[0]: f[2] for f in files}
        self.vis_fns, self.vis_consts, self.vis_structs = {}, {}, {}
        self.file_idx = {f[0]: i for i, f in enumerate(files)}
        self.types = {}               # ordered set of types needing C definitions
        self.extern_files = []
        self.tmp = 0
        self.spawn_cache = {}
        self.aux = []                 # spawn thunks etc.
        self.fn_defs = []
        self.protos = []
        self.const_defs = []
        self.tests = []
        self.file = None
        self.ret = None
        self.scope = None
        self.in_defer = False
        self._decl_nodes = {}
        self.hints = {}      # decl key -> lowest alias depth seen (fixed point across re-checks)
        self.redo = False

    # ------------------------------------------------------------ utils
    def err(self, msg, node):
        raise CompileError(msg, self.file, node.line if node else 0)

    def t(self, prefix="t"):
        self.tmp += 1
        return f"__{prefix}{self.tmp}"

    def need(self, t):
        if t in self.types: return
        if isinstance(t, Arr): self.need(t.elem)
        elif isinstance(t, Res): self.need(t.inner)
        elif isinstance(t, TaskT): self.need(t.inner)
        elif isinstance(t, Tup):
            for x in t.items: self.need(x)
        elif isinstance(t, Fn):
            for p in t.params: self.need(p)
            self.need(t.ret)
        self.types[t] = True
        if isinstance(t, StructT):
            for _, ft in self.structs[t.name]: self.need(ft)

    def heap(self, t):
        return has_heap(t, self.structs)

    def container(self, t, seen=None):
        """Does a value of type t reach a mutable container (an array)?"""
        if isinstance(t, Arr): return True
        if isinstance(t, Res): return self.container(t.inner, seen)
        if isinstance(t, Tup): return any(self.container(x, seen) for x in t.items)
        if isinstance(t, StructT):
            seen = seen or set()
            if t.name in seen: return False
            seen.add(t.name)
            return any(self.container(ft, seen) for _, ft in self.structs[t.name])
        return False

    def set_lo(self, v, decl, lo):
        v.key = id(decl)
        if not self.container(v.ty):
            v.lo = INF
            return
        v.lo = min(lo, self.hints.get(v.key, INF)) if v.mutable else lo

    def lower(self, v, lo):
        """A container aliasing region `lo` was stored into v (or into something v aliases)."""
        if lo >= v.lo or not self.container(v.ty): return
        v.lo = lo
        if v.key is not None and v.mutable and lo < self.hints.get(v.key, INF):
            self.hints[v.key] = lo
            self.redo = True

    def lo(self, e):
        """Outermost region of any mutable container reachable from (already checked) expression e."""
        ty = getattr(e, "ty", None)
        if ty is None or not self.container(ty): return INF
        k = e.kind
        if k == "name":
            v = self.lookup(e.name)
            return v.lo if v else INF
        if k in ("field", "index"): return self.lo(e.base)
        if k == "slice": return min(self.depth, self.lo(e.base))
        if k == "struct_lit": return min([INF] + [self.lo(fe) for _, fe, _ in e.fields])
        if k == "array": return min([self.depth] + [self.lo(it) for it in e.items])
        if k == "call": return getattr(e, "call_lo", self.depth)
        if k == "keep": return 0
        return self.depth

    def resolve_type(self, n):
        if n is None: return None
        k = n.kind
        if k == "tname":
            if n.name in PRIM_C: return Prim(n.name)
            if n.name == "error": return ERR
            if n.name in self.structs:
                self.check_struct_visible(n.name, n)
                return StructT(n.name)
            if n.name == "int": self.err("unknown type 'int'; Lume uses sized integers like i64 or i32", n)
            if n.name == "float": self.err("unknown type 'float'; use f64 or f32", n)
            self.err(f"unknown type '{n.name}'", n)
        if k == "tarr":
            el = self.resolve_type(n.elem)
            if el == VOID: self.err("arrays of void are not allowed", n)
            return Arr(el)
        if k == "tres":
            inner = self.resolve_type(n.inner)
            if isinstance(inner, (Res, ErrT)): self.err("a function returns at most one error", n)
            return Res(inner)
        if k == "tfn":
            return Fn(tuple(self.resolve_type(p) for p in n.params), self.resolve_type(n.ret))
        if k == "ttask":
            return TaskT(self.resolve_type(n.inner))
        if k == "ttup":
            items = tuple(self.resolve_type(x) for x in n.items)
            for x in items:
                if x == VOID or isinstance(x, (Res, Tup)): self.err(f"{x} cannot be one of several return values", n)
            return Tup(items)
        raise AssertionError(k)

    # ------------------------------------------------------------ scopes
    def push(self, **kw):
        depth = kw.pop("depth", self.scope.depth if self.scope else 0)
        self.scope = Scope(self.scope, depth, **kw)
        return self.scope

    def pop(self):
        s = self.scope
        for v in s.vars.values():
            if v.check_used and not v.used:
                self.err(f"error '{v.name}' is never checked; test it with `if ({v.name} != nil)` "
                         f"or bind it to `_` to ignore it on purpose", self._decl_nodes.get(id(v)))
        self.scope = s.parent
        return s

    def lookup(self, name):
        s = self.scope
        while s:
            if name in s.vars: return s.vars[name]
            s = s.parent
        return None

    def declare(self, name, ty, mutable, depth, node, ptr=False):
        if name in self.scope.vars:
            self.err(f"'{name}' is already declared in this scope", node)
        if name in self.fns or name in self.consts:
            self.err(f"'{name}' shadows a top-level declaration", node)
        v = Var(name, ty, mutable, depth, f"{name}_{self.t('v')[3:]}", ptr)
        self.scope.vars[name] = v
        return v

    @property
    def depth(self):
        return self.scope.depth

    def multi_slots(self, ty, n, node):
        """[(element type, C field)] for destructuring a multiple-return value into n names."""
        if isinstance(ty, Res) and ty.inner != VOID:
            slots = [(ty.inner, "val"), (ERR, "err")]
        elif isinstance(ty, Tup):
            slots = [(x, f"v{i}") for i, x in enumerate(ty.items)]
        else:
            self.err(f"this is a single value of type {ty}; bind it with `let x = ...`", node)
        if len(slots) != n:
            self.err(f"this returns {len(slots)} values {ty}, but {n} name(s) are given", node)
        return slots

    def track_err(self, v, node):
        if v.ty == ERR:
            v.check_used = True
            self._decl_nodes[id(v)] = node

    def mark_alloc(self):
        s = self.scope
        while s:
            if s.cand:
                s.cand.allocs = True
                return
            s = s.parent

    def escape(self, vdepth, target, node, what):
        """A value at `vdepth` is being stored somewhere living at `target`."""
        if vdepth <= target: return
        s, softs, hard = self.scope, [], None
        while s:
            if s.cand and target < s.cand.depth <= vdepth:
                if s.cand.hard: hard = hard or s.cand
                else: softs.append(s.cand)
            s = s.parent
        if softs:
            for c in softs: c.escaped = True
            return
        self.err(f"{what}: this value lives in an inner region and would be freed when the "
                 f"region ends; copy it out with `keep`", node)

    def cleanups_until(self, stop):
        """Code for leaving scopes from the current one up to and including `stop`."""
        out, s = [], self.scope
        while s:
            out.extend(reversed(s.cleanups))
            if s is stop: break
            s = s.parent
        return " ".join(out)

    def fn_scope(self):
        s = self.scope
        while not s.fn: s = s.parent
        return s

    # ------------------------------------------------------------ program
    def run(self, test_mode=False):
        # collect declarations
        for path, decls, _ in self.files:
            self.file = path
            self.file_fns[path], self.file_consts[path] = {}, {}
            for d in decls:
                if d.kind == "struct":
                    if d.name in self.structs:
                        other = self.struct_file[d.name][0]
                        where = "" if other == path else f" (also declared in {other}; struct names must be unique across files for now)"
                        self.err(f"struct '{d.name}' declared twice{where}", d)
                    self.struct_file[d.name] = (path, d.pub)
                    if d.name in PRIM_C or d.name == "Task": self.err(f"'{d.name}' is a built-in type", d)
                    self.structs[d.name] = None
                    self.struct_lines[d.name] = (path, d)
        self.build_struct_visibility()
        for path, decls, _ in self.files:
            self.file = path
            for d in decls:
                if d.kind == "struct":
                    seen, fields = set(), []
                    for fname, fty, fl in d.fields:
                        if fname in seen: self.err(f"duplicate field '{fname}'", d)
                        seen.add(fname)
                        t = self.resolve_type(fty)
                        if t == VOID or isinstance(t, (TaskT, Res, ErrT)):
                            self.err(f"field '{fname}' cannot have type {t}", d)
                        fields.append((fname, t))
                    self.structs[d.name] = fields
        for name in self.structs: self.need(StructT(name))
        self.check_struct_cycles()
        for path, decls, _ in self.files:
            self.file = path
            for d in decls:
                if d.kind == "fn":
                    self.declare_fn(d, path)
                elif d.kind == "extern":
                    cpath = os.path.normpath(os.path.join(os.path.dirname(path), d.path))
                    if cpath not in self.extern_files: self.extern_files.append(cpath)
                    for f in d.fns: self.declare_fn(f, path)
                elif d.kind == "const":
                    self.declare_const(d)
        self.build_visibility()
        self.file = self.files[0][0]
        if "main" not in self.fns and not test_mode:
            raise CompileError("no 'fn main()' found", self.files[0][0], 1)
        for path, decls, _ in self.files:
            self.file = path
            for d in decls:
                if d.kind == "fn": self.gen_fn(d)
                elif d.kind == "test" and test_mode and path == self.files[0][0]: self.gen_test(d)
        return self.assemble(test_mode)

    # ------------------------------------------------------------ visibility (pub)
    @property
    def fns(self):
        return self.vis_fns.get(self.file) or self.file_fns.get(self.file, {})

    @property
    def consts(self):
        return self.vis_consts.get(self.file) or {k: v[:2] for k, v in self.file_consts.get(self.file, {}).items()}

    def build_struct_visibility(self):
        for path, _, imps in self.files:
            vis = {n for n, (f, _) in self.struct_file.items() if f == path}
            for imp in imps:
                vis |= {n for n, (f, pub) in self.struct_file.items() if f == imp and pub}
            self.vis_structs[path] = vis

    def check_struct_visible(self, name, node):
        if name in self.vis_structs.get(self.file, ()): return
        f, pub = self.struct_file[name]
        why = "is not `pub`" if pub is False else "is not imported here"
        if pub and f not in self.imports.get(self.file, []): why = f"lives in {f}, which this file doesn't import"
        self.err(f"struct '{name}' {why} (declared in {f})", node)

    def build_visibility(self):
        for path, decls, imps in self.files:
            self.file = path
            fv = dict(self.file_fns[path])
            cv = {k: v[:2] for k, v in self.file_consts[path].items()}
            for imp in imps:
                for n, d in self.file_fns[imp].items():
                    if not d.pub: continue
                    if n in fv and fv[n] is not d:
                        node = next((x for x in decls if getattr(x, "name", None) == n), None)
                        self.err(f"'{n}' conflicts with pub fn '{n}' imported from {imp}", node)
                    fv[n] = d
                for n, (t, c, pub) in self.file_consts[imp].items():
                    if not pub: continue
                    if n in cv and cv[n][1] != c: self.err(f"'{n}' conflicts with pub const '{n}' imported from {imp}", None)
                    cv[n] = (t, c)
            self.vis_fns[path], self.vis_consts[path] = fv, cv
        # a pub item must not expose a private struct
        for path, decls, _ in self.files:
            self.file = path
            for d in decls:
                if d.kind == "fn" and d.pub: self.check_exposed(d.fty, d, f"pub fn '{d.name}'")
                if d.kind == "extern":
                    for f in d.fns:
                        if f.pub: self.check_exposed(f.fty, f, f"pub fn '{f.name}'")
                if d.kind == "struct" and d.pub:
                    for fname, ft in self.structs[d.name]:
                        self.check_exposed(ft, d, f"field '{fname}' of pub struct '{d.name}'")

    def check_exposed(self, t, node, what):
        if isinstance(t, StructT):
            if not self.struct_file[t.name][1]:
                self.err(f"{what} uses struct '{t.name}', which is not `pub`; mark it `pub struct {t.name}`", node)
        elif isinstance(t, Arr): self.check_exposed(t.elem, node, what)
        elif isinstance(t, (Res, TaskT)): self.check_exposed(t.inner, node, what)
        elif isinstance(t, Tup):
            for x in t.items: self.check_exposed(x, node, what)
        elif isinstance(t, Fn):
            for x in t.params: self.check_exposed(x, node, what)
            self.check_exposed(t.ret, node, what)

    def check_struct_cycles(self):
        state = {}
        def visit(n, chain):
            if state.get(n) == 1:
                path, d = self.struct_lines[n]
                self.file = path
                self.err(f"struct '{n}' contains itself by value ({' -> '.join(chain + [n])}); "
                         f"use an array field instead", d)
            if state.get(n) == 2: return
            state[n] = 1
            for _, ft in self.structs[n]:
                if isinstance(ft, StructT): visit(ft.name, chain + [n])
            state[n] = 2
        for n in self.structs: visit(n, [])

    def declare_fn(self, d, path):
        if d.name in self.file_fns[path]: self.err(f"function '{d.name}' declared twice", d)
        if d.extern:
            for f2 in (f for fs in self.file_fns.values() for f in fs.values()):
                if f2.extern and f2.name == d.name: self.err(f"extern function '{d.name}' is declared in two files", d)
        if d.name in PRIM_C or d.name in BUILTINS: self.err(f"'{d.name}' is a built-in name", d)
        params, seen = [], set()
        for p in d.params:
            if p.name in seen: self.err(f"duplicate parameter '{p.name}'", p)
            seen.add(p.name)
            t = self.resolve_type(p.ty)
            if t == VOID: self.err("parameters cannot be void", p)
            params.append((p.name, t, p.mut))
        ret = self.resolve_type(d.ret)
        if d.extern:
            for n, t, m in params:
                if m: self.err("extern functions cannot take mut parameters", d)
                if isinstance(t, (TaskT, Res, ErrT, Fn)): self.err(f"type {t} cannot cross the C boundary", d)
            if isinstance(ret, (TaskT, Res, ErrT, Fn, Arr)): self.err(f"type {ret} cannot be returned from C", d)
        if d.name == "main":
            if params: self.err("main takes no parameters; use arg(i) and argc()", d)
            if ret not in (VOID, Res(VOID)): self.err("main must return nothing or (error)", d)
        d.sig = (params, ret)
        d.file = path
        idx = self.file_idx[path]
        d.cname = d.name if d.extern else (f"lu_f_{d.name}" if idx == 0 else f"lu_f_{d.name}_m{idx}")
        d.fty = Fn(tuple(t for _, t, _ in params), ret)
        self.file_fns[path][d.name] = d
        self.need(d.fty)

    def declare_const(self, d):
        if d.name in self.file_consts[self.file] or d.name in self.file_fns[self.file]: self.err(f"'{d.name}' declared twice", d)
        v = d.value
        neg = False
        if v.kind == "unop" and v.op == "-" and v.operand.kind in ("int", "float"):
            neg, v = True, v.operand
        want = self.resolve_type(d.ty)
        if v.kind == "int":
            t = want or I64
            val = -v.value if neg else v.value
            if is_float(t): code = repr(float(val))
            elif is_int(t):
                lo, hi = INT_TYPES[t.name][1:]
                if not lo <= val <= hi: self.err(f"{val} does not fit in {t}", d)
                code = self.int_lit(val, t)
            else: self.err(f"integer constant cannot have type {t}", d)
        elif v.kind == "float":
            t = want or F64
            if not is_float(t): self.err(f"float constant cannot have type {t}", d)
            code = repr(-v.value if neg else v.value)
        elif v.kind == "bool" and not neg:
            t, code = BOOL, "true" if v.value else "false"
            if want and want != BOOL: self.err("type mismatch in const", d)
        elif v.kind == "str" and not neg and all(isinstance(p, str) for p in v.parts):
            t = STR
            if want and want != STR: self.err("type mismatch in const", d)
            s = "".join(v.parts)
            code = f"{{{c_string(s)}, {len(s.encode())}}}"
        else:
            self.err("const values must be literals", d)
        idx = self.file_idx[self.file]
        cname = f"lu_c_{d.name}" if idx == 0 else f"lu_c_{d.name}_m{idx}"
        self.const_defs.append(f"static const {ctype(t)} {cname} = {code};")
        self.file_consts[self.file][d.name] = (t, cname, d.pub)

    # ------------------------------------------------------------ functions
    def gen_fn(self, d):
        if d.extern: return
        self.file = d.file
        params, ret = d.sig
        self.ret = ret
        cps = []
        for n, t, m in params:
            cps.append(f"{ctype(t)}{'*' if m else ''} {n}_p")
        head = f"{ctype(ret)} {d.cname}({', '.join(cps) or 'void'})"
        self.protos.append(head + ";")
        body = self.gen_body(d, params, ret, d.body)
        self.fn_defs.append(head + " {\n" + body + "}\n")

    def gen_test(self, d):
        self.file = self.files[0][0]
        self.ret = Res(VOID)
        self.need(self.ret)
        cname = f"lu_test_{len(self.tests)}"
        head = f"lu_res_void {cname}(void)"
        body = self.gen_body(d, [], self.ret, d.body)
        self.fn_defs.append(head + " {\n" + body + "}\n")
        self.tests.append((d.name, cname))

    def gen_body(self, d, params, ret, block):
        """Generate a function body; repeat until alias information reaches a fixed point."""
        for _ in range(100):
            self.redo = False
            out = self.gen_body_once(d, params, ret, block)
            if not self.redo: return out
        raise CompileError("alias analysis did not converge (compiler bug)", self.file, d.line)

    def gen_body_once(self, d, params, ret, block):
        """Generate a function body, first trying it as an auto-region."""
        for attempt in (True, False):
            self.scope = None
            self.push(depth=0, fn=True)
            for n, t, m in params:
                v = self.declare(n, t, m, 0, d, ptr=m)
                v.cname = f"{n}_p"
                v.lo = 0 if self.container(t) else INF
            cand = Cand(1, hard=False) if attempt else None
            self.push(depth=1 if attempt else 0, cand=cand)
            ph = f"@@REGION{self.tmp + 1}@@"
            self.tmp += 1
            if cand: self.scope.cleanups.append(ph + "F")
            lines = [f"lu_arena *__fn_arena = lu_cur; (void)__fn_arena;", ph + "I"]
            lines += self.gen_stmts(block.stmts)
            if ret != VOID and not isinstance(ret, Res) and not self.diverges(block):
                self.err(f"function must return a value of type {ret} on every path", d)
            tail = self.cleanups_until(self.scope)
            if tail: lines.append(tail)
            if ret == Res(VOID):
                lines.append("return (lu_res_void){ .ok = true };")
            elif isinstance(ret, Res) and not self.diverges(block):
                self.err(f"function must return a value of type {ret.inner} or an error on every path", d)
            self.pop(); self.pop()
            if cand and cand.escaped: continue
            use = cand is not None and cand.allocs
            return self.fill_region("\n".join(lines), ph, use) + "\n"
        raise AssertionError

    def fill_region(self, code, ph, use):
        if use:
            r = ph.strip("@")
            init = f"lu_arena __ra_{r}; lu_arena_init(&__ra_{r}); lu_arena *__rs_{r} = lu_cur; lu_cur = &__ra_{r};"
            free = f"lu_cur = __rs_{r}; lu_arena_free(&__ra_{r});"
        else:
            init = free = ""
        return code.replace(ph + "I", init).replace(ph + "F", free)

    def diverges(self, b):
        if not b.stmts: return False
        s = b.stmts[-1]
        if s.kind in ("return", "break", "continue"): return True
        if s.kind == "block": return self.diverges(s)
        if s.kind == "if": return s.els is not None and self.diverges(s.then) and self.diverges(s.els)
        if s.kind == "while" and s.cond.kind == "bool" and s.cond.value:
            return not self.has_break(s.body)
        if s.kind == "expr_stmt" and s.expr.kind == "call" and s.expr.fn.kind == "name" and s.expr.fn.name == "panic":
            return True
        return False

    def has_break(self, b):
        for s in b.stmts:
            if s.kind == "break": return True
            if s.kind in ("block", "region"):
                if self.has_break(s if s.kind == "block" else s.body): return True
            if s.kind == "if":
                if self.has_break(s.then) or (s.els and self.has_break(s.els)): return True
        return False

    # ------------------------------------------------------------ statements
    def line(self, n):
        return f'#line {n.line} "{self.file}"'

    def gen_stmts(self, stmts):
        out = []
        for s in stmts:
            out.append(self.line(s))
            out.append(self.gen_stmt(s))
        return out

    def gen_block(self, b, **scope_kw):
        self.push(**scope_kw)
        lines = self.gen_stmts(b.stmts)
        tail = self.cleanups_until(self.scope)
        self.pop()
        return "{\n" + "\n".join(lines) + ("\n" + tail if tail else "") + "\n}"

    def gen_candidate(self, body, hard=False, loop=True, extra_decl=None):
        """Generate a loop body / region block, trying it as a region first."""
        attempts = [True] if hard else [True, False]
        for attempt in attempts:
            cand = Cand(self.depth + 1, hard) if attempt else None
            self.push(depth=self.depth + 1 if attempt else self.depth, cand=cand, loop=loop)
            ph = f"@@REGION{self.tmp + 1}@@"
            self.tmp += 1
            if cand: self.scope.cleanups.append(ph + "F")
            pre = extra_decl(self) if extra_decl else []
            lines = [ph + "I"] + pre + self.gen_stmts(body.stmts)
            tail = self.cleanups_until(self.scope)
            self.pop()
            if cand and cand.escaped and not hard: continue
            use = cand is not None and (cand.allocs or hard)
            code = "{\n" + "\n".join(lines) + ("\n" + tail if tail else "") + "\n}"
            return self.fill_region(code, ph, use)
        raise AssertionError

    def gen_stmt(self, s):
        k = s.kind
        if k == "let":
            want = self.resolve_type(s.ty)
            ty, code, dep = self.expr(s.value, want)
            if want == ERR and ty == Res(VOID):
                ty, code, dep = ERR, f"({code}).err", self.depth
            if want is not None:
                code = self.coerce(want, ty, code, s.value)
                ty = want
            if ty == VOID: self.err("cannot bind a void value", s)
            if isinstance(ty, Res):
                if ty.inner != VOID:
                    self.err(f"this returns two values {ty}; bind both: `let v, err = ...`", s)
                r = self.t("r")
                code, ty, dep = f"({{ {ctype(Res(VOID))} {r} = {code}; {r}.err; }})", ERR, self.depth
            if isinstance(ty, Tup):
                self.err(f"this returns {len(ty.items)} values {ty}; bind them all: `let a, b, ... = ...`", s)
            if s.name == "_":
                return f"(void)({code});"
            vd = dep if not s.mutable else self.depth
            v = self.declare(s.name, ty, s.mutable, vd, s)
            self.set_lo(v, s, self.lo(s.value))
            self.track_err(v, s)
            self.need(ty)
            return f"{ctype(ty)} {v.cname} = {code};"
        if k == "let_multi":
            ty, code, dep = self.expr(s.value, None)
            slots = self.multi_slots(ty, len(s.names), s)
            r = self.t("r")
            self.need(ty)
            out = [f"{ctype(ty)} {r} = {code};"]
            for name, (et, field) in zip(s.names, slots):
                if name == "_": continue
                if et == ERR:
                    v = self.declare(name, ERR, s.mutable, self.depth, s)
                    self.track_err(v, s)
                else:
                    v = self.declare(name, et, s.mutable, dep if not s.mutable else self.depth, s)
                    self.set_lo(v, s, self.lo(s.value))
                out.append(f"{ctype(et)} {v.cname} = {r}.{field};")
            return " ".join(out)
        if k == "assign_multi":
            ty, code, dep = self.expr(s.value, None)
            slots = self.multi_slots(ty, len(s.targets), s)
            r = self.t("r")
            self.need(ty)
            out = [f"{ctype(ty)} {r} = {code};"]
            for tgt, (want, field) in zip(s.targets, slots):
                if tgt.kind == "name" and tgt.name == "_": continue
                tty, tcode, root, indexed = self.lvalue(tgt)
                if tty != want: self.err(f"type mismatch: expected {tty}, got {want}", tgt)
                if self.heap(tty):
                    self.escape(self.depth if want == ERR else dep, root.lo if indexed else root.depth, s,
                                f"assignment to '{root.name}'")
                    if want != ERR: self.lower(root, self.lo(s.value))
                out.append(f"{tcode} = {r}.{field};")
            return "{ " + " ".join(out) + " }"
        if k == "assign":
            return self.gen_assign(s)
        if k == "expr_stmt":
            ty, code, _ = self.expr(s.expr, None)
            if isinstance(ty, Tup) and ERR in ty.items:
                self.err(f"unchecked error: this call returns {ty}; bind the results and check the error "
                         f"(use `_` for values you don't need)", s)
            if isinstance(ty, Res):
                self.err(f"unchecked error: this call returns {ty}; write `let err = ...` or "
                         f"`let v, err = ...` and check it (bind to `_` to ignore on purpose)", s)
            return f"(void)({code});" if ty != VOID else f"{code};"
        if k == "if":
            cond = self.cond(s.cond)
            out = f"if ({cond}) " + self.gen_block(s.then)
            if s.els: out += " else " + self.gen_block(s.els)
            return out
        if k == "while":
            cond = self.cond(s.cond)
            return f"while ({cond}) " + self.gen_candidate(s.body)
        if k == "for_range":
            if self.is_untyped_lit(s.lo) and self.is_untyped_lit(s.hi):
                lt, lo, _ = self.expr(s.lo, I64); ht, hi, _ = self.expr(s.hi, I64)
            elif self.is_untyped_lit(s.lo):
                ht, hi, _ = self.expr(s.hi); lt, lo, _ = self.expr(s.lo, ht)
            else:
                lt, lo, _ = self.expr(s.lo); ht, hi, _ = self.expr(s.hi, lt)
            if not (is_int(lt) and lt == ht):
                self.err(f"range bounds must be the same integer type (got {lt} and {ht})", s)
            iv, hv = self.t("i"), self.t("hi")
            def decl(g):
                v = g.declare(s.var, lt, False, -1, s)
                return [f"{ctype(lt)} {v.cname} = {iv};"]
            body = self.gen_candidate(s.body, extra_decl=decl)
            c = ctype(lt)
            return f"for ({c} {iv} = {lo}, {hv} = {hi}; {iv} < {hv}; {iv}++) {body}"
        if k == "for_each":
            ty, code, dep = self.expr(s.iter, None)
            if not isinstance(ty, Arr):
                self.err(f"can only loop over arrays (got {ty}); use `for (i in a..b)` for ranges", s)
            av, iv = self.t("a"), self.t("i")
            el = ty.elem
            def decl(g):
                lines = []
                if s.idx:
                    v = g.declare(s.idx, I64, False, -1, s)
                    lines.append(f"int64_t {v.cname} = {iv};")
                v = g.declare(s.var, el, False, dep if g.heap(el) else -1, s)
                v.lo = g.lo(s.iter) if g.container(el) else INF
                lines.append(f"{ctype(el)} {v.cname} = {av}->data[{iv}];")
                return lines
            body = self.gen_candidate(s.body, extra_decl=decl)
            return f"{{ {ctype(ty)} {av} = {code}; for (int64_t {iv} = 0; {iv} < {av}->len; {iv}++) {body} }}"
        if k == "return":
            return self.gen_return(s)
        if k in ("break", "continue"):
            if self.in_defer: self.err(f"'{k}' is not allowed inside defer", s)
            sc = self.scope
            while sc and not sc.loop:
                if sc.fn: sc = None; break
                sc = sc.parent
            if not sc: self.err(f"'{k}' outside of a loop", s)
            return f"{{ {self.cleanups_until(sc)} {k}; }}"
        if k == "defer":
            if self.in_defer: self.err("nested defer is not allowed", s)
            self.in_defer = True
            code = self.gen_block(s.body)
            self.in_defer = False
            self.scope.cleanups.append(code)
            return ""
        if k == "region":
            return self.gen_candidate(s.body, hard=True, loop=False)
        if k == "block":
            return self.gen_block(s)
        raise AssertionError(k)

    def cond(self, e):
        ty, code, _ = self.expr(e, BOOL)
        if ty != BOOL: self.err(f"condition must be bool, got {ty}", e)
        return code

    def gen_return(self, s):
        if self.in_defer: self.err("'return' is not allowed inside defer", s)
        ret = self.ret
        fs = self.fn_scope()
        rc = ctype(ret)
        r = self.t("r")
        if s.value is None:
            if ret == VOID: return f"{{ {self.cleanups_until(fs)} return; }}"
            if ret == Res(VOID): return f"{{ {self.cleanups_until(fs)} return (lu_res_void){{ .ok = true }}; }}"
            self.err(f"missing return value of type {ret}", s)
        vals = getattr(s, "values", [s.value] + ([s.err] if s.err is not None else []))
        if isinstance(ret, Tup):
            return self.gen_return_tuple(s, ret, vals, fs, rc, r)
        if isinstance(ret, Res) and len(vals) > 2:
            self.err(f"this function returns {ret}: two values", s)
        if not isinstance(ret, Res):
            if len(vals) > 1: self.err(f"this function returns one value ({ret})", s)
            if ret == VOID: self.err("this function returns nothing", s)
            ty, code, dep = self.expr(s.value, ret)
            code = self.coerce(ret, ty, code, s.value)
            if self.heap(ty): self.escape(dep, 0, s, "returned value escapes")
            return f"{{ {rc} {r} = {code}; {self.cleanups_until(fs)} return {r}; }}"
        if s.err is None:
            if ret.inner != VOID and s.value.kind == "call":
                ty, code, dep = self.expr(s.value, None)
                if ty == ret:      # return f()  -- pass both values through
                    self.escape(dep, 0, s, "returned value escapes")
                    val = (f"({{ {rc} {r}x = {code}; if (!{r}x.ok) {{ lu_trace_push(LU_LOC); "
                           f"{r}x.err = lu_err_keep({r}x.err, __fn_arena); }} {r}x; }})")
                    return f"{{ {rc} {r} = {val}; {self.cleanups_until(fs)} return {r}; }}"
            if ret.inner != VOID:
                self.err(f"this function returns {ret}; return both: `return value, nil` or `return nil, err`", s)
            vcode, ecode, enode = None, None, s.value
        else:
            if ret.inner == VOID: self.err("this function returns only an error: `return err` or `return nil`", s)
            ty, vcode, dep = self.expr(s.value, ret.inner)
            vcode = self.coerce(ret.inner, ty, vcode, s.value)
            if self.heap(ty): self.escape(dep, 0, s, "returned value escapes")
            enode = s.err
        et, ecode, _ = self.expr(enode, ERR)
        if et == Res(VOID) and enode.kind == "call":     # return save()  (an (error)-only call)
            ecode = f"({ecode}).err"
        elif et != ERR:
            self.err(f"expected an error here, got {et}", enode)
        fresh = enode.kind == "call" and enode.fn.kind == "name" and enode.fn.name == "error"
        trace = "" if fresh else "lu_trace_push(LU_LOC); "
        e = self.t("e")
        setv = f"{r}.val = {vcode}; " if vcode is not None else ""
        val = (f"({{ {rc} {r}; {setv}lu_err {e} = {ecode}; {r}.ok = ({e}.msg.p == NULL); "
               f"if (!{r}.ok) {{ {trace}{r}.err = lu_err_keep({e}, __fn_arena); }} else {r}.err = (lu_err){{0}}; {r}; }})")
        return f"{{ {rc} {r}y = {val}; {self.cleanups_until(fs)} return {r}y; }}"

    def gen_return_tuple(self, s, ret, vals, fs, rc, r):
        if len(vals) == 1 and vals[0].kind == "call":       # return f()  -- pass all values through
            ty, code, dep = self.expr(vals[0], None)
            if ty == ret:
                self.escape(dep, 0, s, "returned value escapes")
                return f"{{ {rc} {r} = {code}; {self.cleanups_until(fs)} return {r}; }}"
        if len(vals) != len(ret.items):
            self.err(f"this function returns {len(ret.items)} values {ret}; got {len(vals)}", s)
        sets = []
        for i, (v, want) in enumerate(zip(vals, ret.items)):
            ty, code, dep = self.expr(v, want)
            if want == ERR and ty == Res(VOID) and v.kind == "call":
                ty, code = ERR, f"({code}).err"
            code = self.coerce(want, ty, code, v)
            if want == ERR:
                fresh = v.kind == "call" and v.fn.kind == "name" and v.fn.name == "error"
                e = self.t("e")
                trace = "" if fresh else "lu_trace_push(LU_LOC); "
                code = f"({{ lu_err {e} = {code}; if ({e}.msg.p) {{ {trace}{e} = lu_err_keep({e}, __fn_arena); }} {e}; }})"
            elif self.heap(ty):
                self.escape(dep, 0, v, "returned value escapes")
            sets.append(f"{r}.v{i} = {code};")
        return f"{{ {rc} {r}; {' '.join(sets)} {self.cleanups_until(fs)} return {r}; }}"

    def gen_assign(self, s):
        tty, tcode, root, indexed = self.lvalue(s.target)
        if s.op == "=":
            vty, vcode, vdep = self.expr(s.value, tty)
            if tty == ERR and vty == Res(VOID):
                vty, vcode, vdep = ERR, f"({vcode}).err", self.depth
        else:
            op = s.op[:-1]
            vty, vcode, vdep = self.binop_code(op, s.target, s.value, s, lhs_pre=(tty, tcode))
        vcode = self.coerce(tty, vty, vcode, s.value)
        if self.heap(tty):
            # storing through an array writes into the aliased container; otherwise into the variable
            self.escape(vdep, root.lo if indexed else root.depth, s, f"assignment to '{root.name}'")
            self.lower(root, self.lo(s.value))
        v = self.t("v")
        return f"{{ {ctype(tty)} {v} = {vcode}; {tcode} = {v}; }}"

    def lvalue(self, e):
        """-> (type, C lvalue code, root Var, went_through_index)"""
        if e.kind == "name":
            v = self.lookup(e.name)
            if not v:
                if e.name in self.consts: self.err(f"cannot assign to constant '{e.name}'", e)
                self.err(f"unknown variable '{e.name}'", e)
            if not v.mutable:
                hint = " (declare it with `var`)" if not v.ptr else ""
                if self.is_param(v): hint = f" (declare the parameter as `mut {v.name}`)"
                self.err(f"cannot modify '{v.name}': it is immutable{hint}", e)
            return v.ty, v.code, v, False
        if e.kind == "field":
            bty, bcode, root, ix = self.lvalue(e.base)
            if isinstance(bty, StructT):
                ft = dict(self.structs[bty.name]).get(e.name)
                if ft is None: self.err(f"struct {bty} has no field '{e.name}'", e)
                return ft, f"{bcode}.{e.name}", root, ix
            self.err(f"cannot assign to field '{e.name}' of {bty}", e)
        if e.kind == "index":
            bty, bcode, root, _ = self.lvalue(e.base)
            if not isinstance(bty, Arr): self.err(f"cannot index-assign into {bty}", e)
            ity, icode, _ = self.expr(e.index, I64)
            if not is_int(ity): self.err("index must be an integer", e)
            return bty.elem, f"(*{arr_name(bty)}_at({bcode}, (int64_t)({icode}), LU_LOC))", root, True
        self.err("left side of assignment is not assignable", e)

    def is_param(self, v):
        s = self.fn_scope()
        return v.name in s.vars and s.vars[v.name] is v

    # ------------------------------------------------------------ expressions
    def coerce(self, want, got, code, node):
        if want == got or want is None: return code
        self.err(f"type mismatch: expected {want}, got {got}", node)

    def int_lit(self, val, t):
        if t.name == "u64": return f"UINT64_C({val})"
        if t.name == "i64":
            return "INT64_MIN" if val == -2**63 else f"INT64_C({val})"
        return f"(({ctype(t)}){val})"

    def is_float_lit(self, e):
        return e.kind == "float" or (e.kind == "unop" and self.is_float_lit(e.operand))

    def is_untyped_lit(self, e):
        if e.kind in ("int", "float"): return True
        return e.kind == "unop" and e.op == "-" and self.is_untyped_lit(e.operand)

    def expr(self, e, want=None):
        """-> (type, C code, depth)"""
        r = self.expr_(e, want)
        e.ty = r[0]
        return r

    def expr_(self, e, want=None):
        k = e.kind
        d0 = -1
        if k == "int":
            if e.byte and want is None: want = U8
            t = want if is_num(want) else I64
            if is_float(t): return t, repr(float(e.value)), d0
            lo, hi = INT_TYPES[t.name][1:]
            if not lo <= e.value <= hi: self.err(f"literal {e.value} does not fit in {t}", e)
            return t, self.int_lit(e.value, t), d0
        if k == "float":
            t = want if is_float(want) else F64
            return t, (repr(e.value) + ("f" if t.name == "f32" else "")), d0
        if k == "bool":
            return BOOL, "true" if e.value else "false", d0
        if k == "nil":
            if want is None: self.err("cannot tell what type this `nil` is; add a type", e)
            code = self.zero(want, e)
            if self.container(want):
                self.mark_alloc()
                return want, code, self.depth
            return want, code, -1
        if k == "str":
            if all(isinstance(p, str) for p in e.parts):
                s = "".join(e.parts)
                return STR, f"((lu_str){{ {c_string(s)}, {len(s.encode())} }})", d0
            self.mark_alloc()
            b = self.t("sb")
            return STR, f"({{ lu_sb {b} = {{0}}; {self.interp_into(b, e.parts)} lu_sb_done(&{b}); }})", self.depth
        if k == "name":
            v = self.lookup(e.name)
            if v:
                v.used = True
                return v.ty, v.code, (v.depth if self.heap(v.ty) else -1)
            if e.name in self.consts:
                t, c = self.consts[e.name]
                return t, c, -1
            if e.name in self.fns:
                f = self.fns[e.name]
                if any(m for _, _, m in f.sig[0]):
                    self.err(f"function '{e.name}' has mut parameters and cannot be used as a value", e)
                return f.fty, f.cname, -1
            self.err(f"unknown name '{e.name}'", e)
        if k == "binop":
            return self.binop_code(e.op, e.lhs, e.rhs, e, want=want)
        if k == "unop":
            if e.op == "-":
                if e.operand.kind in ("int", "float"):
                    t = want if is_num(want) else (I64 if e.operand.kind == "int" else F64)
                    if e.operand.kind == "int" and is_int(t):
                        lo, hi = INT_TYPES[t.name][1:]
                        if not lo <= -e.operand.value <= hi: self.err(f"literal -{e.operand.value} does not fit in {t}", e)
                        return t, self.int_lit(-e.operand.value, t), d0
                ty, c, _ = self.expr(e.operand, want)
                if is_int(ty):
                    if not is_signed(ty): self.err(f"cannot negate unsigned {ty}", e)
                    return ty, f"LU_NEG({ctype(ty)}, {c})", d0
                if is_float(ty): return ty, f"(-({c}))", d0
                self.err(f"cannot negate {ty}", e)
            ty, c, _ = self.expr(e.operand, want)
            if e.op == "!":
                if ty != BOOL: self.err(f"'!' needs a bool, got {ty}", e)
                return BOOL, f"(!({c}))", d0
            if e.op == "~":
                if not is_int(ty): self.err(f"'~' needs an integer, got {ty}", e)
                return ty, f"(({ctype(ty)})~({c}))", d0
        if k == "call":
            return self.call(e, want)
        if k == "field":
            return self.field(e)
        if k == "index":
            bty, bc, bd = self.expr(e.base)
            ity, ic, _ = self.expr(e.index, I64)
            if not is_int(ity): self.err(f"index must be an integer, got {ity}", e.index)
            if isinstance(bty, Arr):
                self.need(bty)
                return bty.elem, f"(*{arr_name(bty)}_at({bc}, (int64_t)({ic}), LU_LOC))", (bd if self.heap(bty.elem) else -1)
            if bty == STR:
                return U8, f"lu_str_at({bc}, (int64_t)({ic}), LU_LOC)", -1
            self.err(f"cannot index {bty}", e)
        if k == "slice":
            bty, bc, bd = self.expr(e.base)
            lt, lc, _ = self.expr(e.lo, I64)
            ht, hc, _ = self.expr(e.hi, I64)
            if not (is_int(lt) and is_int(ht)): self.err("slice bounds must be integers", e)
            if bty == STR:
                return STR, f"lu_str_slice({bc}, (int64_t)({lc}), (int64_t)({hc}), LU_LOC)", bd
            if isinstance(bty, Arr):
                self.need(bty); self.mark_alloc()
                return bty, f"{arr_name(bty)}_slice({bc}, (int64_t)({lc}), (int64_t)({hc}), LU_LOC)", max(bd, self.depth)
            self.err(f"cannot slice {bty}", e)
        if k == "struct_lit":
            if e.name not in self.structs: self.err(f"unknown struct '{e.name}'", e)
            self.check_struct_visible(e.name, e)
            fields = self.structs[e.name]
            fmap = dict(fields)
            given, parts, dep = set(), [], -1
            for fname, fe, fl in e.fields:
                if fname not in fmap: self.err(f"struct {e.name} has no field '{fname}'", fe)
                if fname in given: self.err(f"field '{fname}' given twice", fe)
                given.add(fname)
                ft, fc, fd = self.expr(fe, fmap[fname])
                fc = self.coerce(fmap[fname], ft, fc, fe)
                if self.heap(ft): dep = max(dep, fd)
                parts.append(f".{fname} = {fc}")
            missing = [f for f, _ in fields if f not in given]
            if missing: self.err(f"missing field(s) in {e.name} literal: {', '.join(missing)}", e)
            t = StructT(e.name)
            return t, f"(({ctype(t)}){{ {', '.join(parts)} }})", dep
        if k == "array":
            el = want.elem if isinstance(want, Arr) else None
            items = []
            for it in e.items:
                ty, c, _ = self.expr(it, el)
                if el is None: el = ty
                c = self.coerce(el, ty, c, it)
                items.append(c)
            if el is None: self.err("cannot infer the element type of an empty array; add a type, e.g. `var a []i64 = []`", e)
            if el == VOID or isinstance(el, (Res, ErrT)): self.err(f"arrays of {el} are not allowed", e)
            t = Arr(el)
            self.need(t)
            self.mark_alloc()
            a = self.t("arr")
            n = arr_name(t)
            pushes = " ".join(f"{n}_push({a}, {c});" for c in items)
            return t, f"({{ {n} *{a} = {n}_new({len(items)}); {pushes} {a}; }})", self.depth
        if k == "keep":
            ty, c, _ = self.expr(e.expr, want)
            if self.contains_task(ty):
                self.err("values containing tasks cannot be copied with keep", e)
            if not self.heap(ty): return ty, c, -1
            self.need(ty)
            return ty, f"lu_cp_{mangle(ty)}({c}, __fn_arena)", 0
        if k == "spawn":
            return self.spawn(e)
        if k == "mut_arg":
            self.err("`mut` here only applies to arguments of functions with mut parameters", e)
        self.err(f"unsupported expression '{k}'", e)

    def zero(self, t, node):
        """C code for the zero value of t (what `nil` means)."""
        if is_int(t) or is_float(t): return f"(({ctype(t)})0)"
        if t == BOOL: return "false"
        if t == STR: return "((lu_str){0})"
        if t == ERR: return "((lu_err){0})"
        if isinstance(t, Arr):
            self.need(t)
            return f"{arr_name(t)}_new(0)"
        if isinstance(t, StructT):
            if self.contains_task(t): self.err(f"{t} holds tasks and has no zero value", node)
            self.need(t)
            return f"lu_zero_{mangle(t)}()"
        self.err(f"`nil` has no meaning for {t}", node)

    def contains_task(self, t, seen=None):
        if isinstance(t, TaskT): return True
        if isinstance(t, Arr): return self.contains_task(t.elem, seen)
        if isinstance(t, StructT):
            seen = seen or set()
            if t.name in seen: return False
            seen.add(t.name)
            return any(self.contains_task(ft, seen) for _, ft in self.structs[t.name])
        return False

    def interp_into(self, b, parts):
        out = []
        for p in parts:
            if isinstance(p, str):
                if p: out.append(f"lu_sb_put(&{b}, {c_string(p)}, {len(p.encode())});")
            else:
                ty, c, _ = self.expr(p)
                out.append(self.write_top(b, ty, c, p))
        return " ".join(out)

    def write_top(self, b, ty, c, node):
        if ty == VOID: self.err("cannot print a void value", node)
        if isinstance(ty, (Res, Tup)): self.err(f"cannot print a multiple-return value {ty}; bind it first: `let a, b = ...`", node)
        if ty == STR: return f"lu_sb_str(&{b}, {c});"
        if isinstance(ty, ErrT):
            e = self.t("e")
            return f"{{ lu_err {e} = {c}; if ({e}.msg.p) lu_sb_str(&{b}, {e}.msg); else lu_sb_put(&{b}, \"<nil>\", 5); }}"
        self.need(ty)
        return f"lu_w_{mangle(ty)}(&{b}, {c});"

    def binop_code(self, op, lhs, rhs, node, want=None, lhs_pre=None):
        if op in ("&&", "||"):
            lt, lc, _ = self.expr(lhs, BOOL)
            rt, rc, _ = self.expr(rhs, BOOL)
            if lt != BOOL or rt != BOOL: self.err(f"'{op}' needs bool operands, got {lt} and {rt}", node)
            return BOOL, f"({lc} {op} {rc})", -1
        if op in ("==", "!=") and (lhs.kind == "nil" or rhs.kind == "nil"):
            other = rhs if lhs.kind == "nil" else lhs
            ot, oc, _ = self.expr(other)
            if ot != ERR: self.err(f"only errors can be compared with nil (got {ot})", node)
            return BOOL, f"(({oc}).msg.p {op} NULL)", -1
        num_want = want if is_num(want) and op not in ("==", "!=", "<", "<=", ">", ">=") else None
        if lhs_pre:
            lt, lc = lhs_pre
            rt, rc, rd = self.expr(rhs, lt)
            ld = -1
        elif self.is_untyped_lit(lhs) and not self.is_untyped_lit(rhs):
            rt, rc, rd = self.expr(rhs, num_want)
            lt, lc, ld = self.expr(lhs, rt)
        else:
            lt, lc, ld = self.expr(lhs, num_want)
            want_r = lt if op not in ("<<", ">>") else None
            rt, rc, rd = self.expr(rhs, want_r)
        if op in ("<<", ">>"):
            if not (is_int(lt) and is_int(rt)): self.err(f"'{op}' needs integers, got {lt} and {rt}", node)
            return lt, f"LU_{'SHL' if op == '<<' else 'SHR'}({ctype(lt)}, {lc}, {rc})", -1
        if lt != rt:
            self.err(f"mismatched types for '{op}': {lt} and {rt} (Lume never converts implicitly; "
                     f"use a cast like {rt}(x))", node)
        t = lt
        if op in ("==", "!="):
            if is_num(t) or t == BOOL: return BOOL, f"({lc} {op} {rc})", -1
            if t == STR: return BOOL, f"({'' if op == '==' else '!'}lu_str_eq({lc}, {rc}))", -1
            if isinstance(t, (StructT, Arr)):
                self.need(t)
                return BOOL, f"({'' if op == '==' else '!'}lu_eq_{mangle(t)}({lc}, {rc}))", -1
            self.err(f"cannot compare {t} with '{op}'", node)
        if op in ("<", "<=", ">", ">="):
            if is_num(t): return BOOL, f"({lc} {op} {rc})", -1
            self.err(f"cannot order {t} with '{op}'", node)
        if op == "+" and t == STR:
            self.mark_alloc()
            return STR, f"lu_str_concat({lc}, {rc})", self.depth
        if op in ("+%", "-%", "*%"):
            if not is_int(t): self.err(f"wrapping '{op}' needs integers, got {t}", node)
            ut = "uint" + ctype(t).lstrip("u").replace("int", "", 1)
            return t, f"(({ctype(t)})(({ut})({lc}) {op[0]} ({ut})({rc})))", -1
        if op in ("&", "|", "^"):
            if not is_int(t): self.err(f"'{op}' needs integers, got {t}", node)
            return t, f"(({ctype(t)})({lc} {op} {rc}))", -1
        if op in ("+", "-", "*", "/", "%"):
            if is_int(t):
                m = {"+": "ADD", "-": "SUB", "*": "MUL", "/": "DIV", "%": "MOD"}[op]
                return t, f"LU_{m}({ctype(t)}, {lc}, {rc})", -1
            if is_float(t):
                if op == "%": return t, f"(({ctype(t)})fmod({lc}, {rc}))", -1
                return t, f"({lc} {op} {rc})", -1
            self.err(f"'{op}' is not defined for {t}", node)
        self.err(f"unknown operator {op}", node)

    def field(self, e):
        bty, bc, bd = self.expr(e.base)
        if isinstance(bty, StructT):
            ft = dict(self.structs[bty.name]).get(e.name)
            if ft is None: self.err(f"struct {bty} has no field '{e.name}'", e)
            return ft, f"({bc}).{e.name}", (bd if self.heap(ft) else -1)
        if e.name == "len" and (isinstance(bty, Arr) or bty == STR):
            if isinstance(bty, Arr): return I64, f"(({bc})->len)", -1
            return I64, f"(({bc}).len)", -1
        if isinstance(bty, ErrT) and e.name == "msg":
            return STR, f"(({bc}).msg)", bd
        self.err(f"{bty} has no field '{e.name}'", e)

    # ------------------------------------------------------------ calls
    def call(self, e, want):
        f = e.fn
        if f.kind == "name" and not self.lookup(f.name):
            if f.name in BUILTINS:
                return self.builtin(f.name, e, want)
            if f.name in PRIM_C and f.name not in ("void", "str", "bool"):
                return self.cast(Prim(f.name), e)
            if f.name in self.fns:
                return self.user_call(self.fns[f.name], e.args, e)
            self.err(f"unknown function '{f.name}'", f)
        if f.kind == "field":
            # method-style call: built-in methods first, then UFCS: x.f(a) == f(x, a)
            bty, bc, bd = self.expr(f.base)
            if isinstance(bty, Arr) and f.name in ("push", "pop", "clear"):
                return self.arr_method(f, bty, e)
            if isinstance(bty, TaskT) and f.name == "join":
                if e.args: self.err("join() takes no arguments", e)
                self.mark_alloc()
                inner = bty.inner
                if inner == VOID: return VOID, f"lu_task_join({bc}, LU_LOC)", -1
                return inner, f"(*({ctype(inner)} *)lu_task_join({bc}, LU_LOC))", self.depth
            if f.name in self.fns:
                return self.user_call(self.fns[f.name], [f.base] + e.args, e)
            self.err(f"{bty} has no method '{f.name}' and there is no function '{f.name}'", f)
        ty, c, _ = self.expr(f)
        if not isinstance(ty, Fn): self.err(f"cannot call a value of type {ty}", f)
        if len(e.args) != len(ty.params):
            self.err(f"expected {len(ty.params)} argument(s), got {len(e.args)}", e)
        args = []
        for a, pt in zip(e.args, ty.params):
            at, ac, _ = self.expr(a, pt)
            args.append(self.coerce(pt, at, ac, a))
        e.call_lo = min([self.depth] + [self.lo(a) for a in e.args])
        self.mark_alloc()
        return ty.ret, f"({c})({', '.join(args)})", (self.depth if self.heap(ty.ret) else -1)

    def user_call(self, fn, args, e):
        params, ret = fn.sig
        if len(args) != len(params):
            self.err(f"'{fn.name}' expects {len(params)} argument(s), got {len(args)}", e)
        out, heap_args, mut_args, los = [], [], [], []
        for i, (a, (pn, pt, mut)) in enumerate(zip(args, params)):
            receiver = i == 0 and getattr(e, "fn", None) is not None and e.fn.kind == "field" and a is e.fn.base
            if a.kind == "mut_arg":
                if not mut: self.err(f"parameter '{pn}' of '{fn.name}' is not `mut`; remove `mut` here", a)
                a = a.expr
            elif mut and not receiver:
                self.err(f"'{fn.name}' may modify '{pn}'; write `mut` at the call site: {fn.name}(..., mut x, ...)", a)
            if mut:
                lt, lc, root, ix = self.lvalue(a)
                if ix: self.err(f"cannot pass an array element as `mut {pn}`; copy it to a variable first", a)
                lt = self.coerce(pt, lt, lt, a)
                out.append(f"&({lc})")
                if self.heap(pt): mut_args.append((root, pn, a))
                los.append(root.lo)
            else:
                at, ac, ad = self.expr(a, pt)
                out.append(self.coerce(pt, at, ac, a))
                if self.heap(pt): heap_args.append((ad, a, self.lo(a)))
                los.append(self.lo(a))
        for root, pn, a in mut_args:
            # the callee may store new allocations (made in our current region) or the other
            # arguments into the mut argument, or into anything it aliases
            tgt = min(root.depth, root.lo)
            self.escape(self.depth, tgt, a, f"passing as `mut {pn}`")
            for ad, other, olo in heap_args:
                self.escape(ad, tgt, other, f"argument may be stored into `mut {pn}`")
                self.lower(root, olo)
            for r2, _, _ in mut_args:
                if r2 is not root:
                    self.escape(r2.depth, tgt, a, f"argument may be stored into `mut {pn}`")
                    self.lower(root, r2.lo)
        e.call_lo = min([self.depth] + los)
        if not fn.extern or self.heap(ret): self.mark_alloc()
        return ret, f"{fn.cname}({', '.join(out)})", (self.depth if self.heap(ret) else -1)

    def arr_method(self, f, bty, e):
        n = arr_name(bty)
        self.need(bty)
        lt, lc, root, _ = self.lvalue(f.base)
        if f.name == "push":
            if len(e.args) != 1: self.err("push takes one argument", e)
            at, ac, ad = self.expr(e.args[0], bty.elem)
            ac = self.coerce(bty.elem, at, ac, e.args[0])
            if self.heap(at):
                self.escape(ad, root.lo, e.args[0], f"pushing into '{root.name}'")
                self.lower(root, self.lo(e.args[0]))
            v = self.t("v")
            return VOID, f"({{ {ctype(at)} {v} = {ac}; {n}_push({lc}, {v}); }})", -1
        if e.args: self.err(f"{f.name}() takes no arguments", e)
        if f.name == "pop":
            e.call_lo = root.lo
            return bty.elem, f"{n}_pop({lc}, LU_LOC)", (root.depth if self.heap(bty.elem) else -1)
        return VOID, f"(({lc})->len = 0)", -1

    def cast(self, t, e):
        if len(e.args) != 1: self.err(f"{t}(x) takes one argument", e)
        a = e.args[0]
        if self.is_untyped_lit(a) and (is_float(t) or not self.is_float_lit(a)):
            return self.expr(a, t)
        at, ac, _ = self.expr(a)
        ct = ctype(t)
        if at == t: return t, ac, -1
        if is_int(t) and is_int(at): return t, f"LU_CAST({ct}, {ac})", -1
        if is_float(t) and is_num(at): return t, f"(({ct})({ac}))", -1
        if is_int(t) and is_float(at):
            lo, hi = INT_TYPES[t.name][1:]
            v = self.t("f")
            return t, (f"({{ double {v} = (double)({ac}); if (!({v} >= {float(lo)!r} && {v} < {float(hi) + 1!r})) "
                       f"lu_panic(\"float to {t} conversion out of range\", LU_LOC); ({ct}){v}; }})"), -1
        if is_int(t) and at == BOOL: return t, f"(({ct})({ac}))", -1
        self.err(f"cannot convert {at} to {t}", e)

    def spawn(self, e):
        call = e.call
        if call.fn.kind != "name" or call.fn.name not in self.fns or self.lookup(call.fn.name):
            self.err("spawn expects a direct call to a named function", e)
        fn = self.fns[call.fn.name]
        params, ret = fn.sig
        if any(m for _, _, m in params): self.err("cannot spawn a function with mut parameters", e)
        if len(call.args) != len(params):
            self.err(f"'{fn.name}' expects {len(params)} argument(s), got {len(call.args)}", e)
        for _, pt, _ in params:
            if self.contains_task(pt): self.err("tasks cannot be sent to another thread", e)
        key = fn.cname
        if key not in self.spawn_cache:
            sn = f"lu_sp_{fn.cname}"
            fields = " ".join(f"{ctype(pt)} a{i};" for i, (_, pt, _) in enumerate(params)) or "char _unused;"
            argl = ", ".join(f"a->a{i}" for i in range(len(params)))
            if ret == VOID:
                body = f"{fn.cname}({argl}); return NULL;"
            else:
                body = f"{ctype(ret)} *r = lu_alloc(sizeof *r); *r = {fn.cname}({argl}); return r;"
            self.aux.append(f"typedef struct {{ {fields} }} {sn}_args;\n"
                            f"static void *{sn}_body(lu_task *t) {{ {sn}_args *a = t->args; (void)a; {body} }}")
            self.spawn_cache[key] = sn
        sn = self.spawn_cache[key]
        t, a = self.t("task"), self.t("a")
        evals, sets = [], []
        for i, (arg, (_, pt, _)) in enumerate(zip(call.args, params)):
            at, ac, _ = self.expr(arg, pt)
            ac = self.coerce(pt, at, ac, arg)
            tv = f"{t}_a{i}"
            evals.append(f"{ctype(pt)} {tv} = {ac};")      # may exit early (try): no task exists yet
            if self.heap(pt):
                self.need(pt)
                tv = f"lu_cp_{mangle(pt)}({tv}, {t}->arena)"
            sets.append(f"{a}->a{i} = {tv};")
        self.mark_alloc()
        ty = TaskT(ret)
        self.need(ty)
        return ty, (f"({{ {' '.join(evals)} lu_task *{t} = lu_task_new({sn}_body); {sn}_args *{a} = lu_alloc_in({t}->arena, sizeof *{a}); "
                    f"{' '.join(sets)} {t}->args = {a}; lu_task_start({t}); {t}; }})"), self.depth

    def builtin(self, name, e, want):
        args = e.args
        def nargs(n):
            if len(args) != n: self.err(f"{name}() takes {n} argument(s), got {len(args)}", e)
        if name in ("print", "eprint"):
            b = self.t("psb")
            pa, pr = self.t("pa"), self.t("pr")
            parts = []
            self.push(depth=self.depth + 1, cand=Cand(self.depth + 1, hard=True))
            self.scope.cleanups.append(f"lu_cur = {pa}; lu_arena_free(&{pr});")
            for i, a in enumerate(args):
                if i: parts.append(f"lu_sb_put(&{b}, \" \", 1);")
                if a.kind == "str":
                    parts.append(self.interp_into(b, a.parts))
                else:
                    ty, c, _ = self.expr(a)
                    parts.append(self.write_top(b, ty, c, a))
            self.pop()
            return VOID, (f"({{ lu_arena *{pa} = lu_cur; lu_arena {pr}; lu_arena_init(&{pr}); lu_cur = &{pr}; "
                          f"lu_sb {b} = {{0}}; {' '.join(parts)} lu_print_str(lu_sb_done(&{b}), {'true' if name == 'eprint' else 'false'}); "
                          f"lu_cur = {pa}; lu_arena_free(&{pr}); }})"), -1
        if name == "panic":
            nargs(1)
            ty, c, _ = self.expr(args[0], STR)
            if ty != STR: self.err("panic() takes a str", e)
            return VOID, f"lu_panic_str({c}, LU_LOC)", -1
        if name == "assert":
            nargs(1)
            c = self.cond(args[0])
            return VOID, f"({{ if (!({c})) lu_panic(\"assertion failed\", LU_LOC); }})", -1
        if name == "assert_eq":
            nargs(2)
            if self.is_untyped_lit(args[0]) and not self.is_untyped_lit(args[1]):
                bt, bc, _ = self.expr(args[1]); at, ac, _ = self.expr(args[0], bt)
            else:
                at, ac, _ = self.expr(args[0]); bt, bc, _ = self.expr(args[1], at)
            if at != bt: self.err(f"assert_eq needs two values of the same type, got {at} and {bt}", e)
            if isinstance(at, (Res, TaskT, Fn, ErrT)) or at == VOID: self.err(f"cannot compare {at}", e)
            self.need(at)
            m = mangle(at)
            x, y = self.t("x"), self.t("y")
            if at == STR: eq = f"lu_str_eq({x}, {y})"
            elif isinstance(at, (StructT, Arr)): eq = f"lu_eq_{m}({x}, {y})"
            else: eq = f"({x} == {y})"
            w = (f"lu_sb_quoted(&__b, {{v}});" if at == STR else f"lu_w_{m}(&__b, {{v}});")
            return VOID, (f"({{ {ctype(at)} {x} = {ac}; {ctype(at)} {y} = {bc}; if (!({eq})) {{ lu_sb __b = {{0}}; "
                          f"lu_sb_put(&__b, \"assert_eq failed: left = \", 25); {w.format(v=x)} "
                          f"lu_sb_put(&__b, \", right = \", 10); {w.format(v=y)} lu_sb_put(&__b, \"\", 1); "
                          f"lu_panic(__b.p, LU_LOC); }} }})"), -1
        if name == "error":
            nargs(1)
            ty, c, d = self.expr(args[0], STR)
            if ty != STR: self.err("error() takes a str message", e)
            return ERR, f"lu_err_new({c}, LU_LOC)", d
        if name == "argc":
            nargs(0)
            return I64, "lu_argc()", -1
        if name == "arg":
            nargs(1)
            ty, c, _ = self.expr(args[0], I64)
            if not is_int(ty): self.err("arg() takes an integer", e)
            return STR, f"lu_argv((int64_t)({c}), LU_LOC)", -1
        if name == "now_ns":
            nargs(0)
            return I64, "lu_now_ns()", -1
        if name == "read_file":
            nargs(1)
            ty, c, _ = self.expr(args[0], STR)
            if ty != STR: self.err("read_file() takes a str path", e)
            self.mark_alloc(); self.need(Res(STR))
            return Res(STR), f"lu_read_file({c}, LU_LOC)", self.depth
        if name == "read_stdin":
            nargs(0)
            self.mark_alloc(); self.need(Res(STR))
            return Res(STR), "lu_read_stdin(LU_LOC)", self.depth
        if name == "write_file":
            nargs(2)
            pt, pc, _ = self.expr(args[0], STR)
            dt, dc, _ = self.expr(args[1], STR)
            if pt != STR or dt != STR: self.err("write_file(path: str, data: str)", e)
            self.mark_alloc()
            return Res(VOID), f"lu_write_file({pc}, {dc}, LU_LOC)", -1
        if name == "mem_live":
            nargs(0)
            return I64, "lu_live_bytes()", -1
        raise AssertionError(name)

    # ------------------------------------------------------------ assemble
    def assemble(self, test_mode):
        ts = list(self.types)
        out = ['#include "lume_rt.h"', "#include <math.h>",
               "_Noreturn void lu_panic_str(lu_str s, const char *f, int l);",
               "typedef struct lu_res_str lu_res_str; struct lu_res_str { bool ok; lu_str val; lu_err err; };",
               "lu_res_str lu_read_file(lu_str path, const char *f, int l);",
               "lu_res_str lu_read_stdin(const char *f, int l);",
               "lu_res_void lu_write_file(lu_str path, lu_str data, const char *f, int l);", ""]
        predefined = {"lu_arr_str", "lu_res_void", "lu_res_str"}
        # 1. forward declarations
        for t in ts:
            if isinstance(t, StructT): out.append(f"typedef struct {ctype(t)} {ctype(t)};")
            elif isinstance(t, Arr) and arr_name(t) not in predefined: out.append(f"typedef struct {arr_name(t)} {arr_name(t)};")
            elif isinstance(t, Res) and ctype(t) not in predefined: out.append(f"typedef struct {ctype(t)} {ctype(t)};")
            elif isinstance(t, Tup): out.append(f"typedef struct {ctype(t)} {ctype(t)};")
        for t in ts:
            if isinstance(t, Fn):
                out.append(f"typedef {ctype(t.ret)} (*{ctype(t)})({', '.join(ctype(p) for p in t.params) or 'void'});")
        # 2. by-value bodies in dependency order
        done = set()
        def body(t):
            if t in done or not isinstance(t, (StructT, Res, Tup)): return
            done.add(t)
            if isinstance(t, StructT):
                for _, ft in self.structs[t.name]: body(ft)
                fs = " ".join(f"{ctype(ft)} {fn};" for fn, ft in self.structs[t.name]) or "char _empty;"
                out.append(f"struct {ctype(t)} {{ {fs} }};")
            elif isinstance(t, Tup):
                for x in t.items: body(x)
                fs = " ".join(f"{ctype(x)} v{i};" for i, x in enumerate(t.items))
                out.append(f"struct {ctype(t)} {{ {fs} }};")
            else:
                if ctype(t) in predefined: return
                body(t.inner)
                out.append(f"struct {ctype(t)} {{ bool ok; {ctype(t.inner)} val; lu_err err; }};")
        for t in ts: body(t)
        for t in ts:
            if isinstance(t, Arr) and arr_name(t) not in predefined:
                out.append(f"struct {arr_name(t)} {{ {ctype(t.elem)} *data; int64_t len, cap; lu_arena *ar; }};")
        for t in ts:
            if isinstance(t, Arr) and arr_name(t) not in predefined:
                out.append(f"LU_ARR_FUNCS({arr_name(t)}, {ctype(t.elem)})")
        # 3. helpers: write (to string), eq, copy
        protos, defs = [], []
        for t in ts:
            self.helpers(t, protos, defs)
        out += protos + self.const_defs
        for f in (f for fs in self.file_fns.values() for f in fs.values()):
            if f.extern:
                params, ret = f.sig
                out.append(f"{ctype(ret)} {f.name}({', '.join(ctype(t) for _, t, _ in params) or 'void'});")
        out += self.protos + self.aux + defs + self.fn_defs
        if test_mode:
            out.append(self.test_main())
        else:
            f = self.file_fns[self.files[0][0]]["main"]
            call = f"lu_res_void r = lu_f_main(); if (!r.ok) {{ fflush(stdout); fprintf(stderr, \"error: %.*s\\n\", (int)r.err.msg.len, r.err.msg.p); lu_trace_print(); code = 1; }}" \
                if f.sig[1] == Res(VOID) else "lu_f_main();"
            out.append(f"int main(int argc, char **argv) {{ lu_init(argc, argv); lu_arena root; lu_arena_init(&root); lu_cur = &root; int code = 0; "
                       f"{call} lu_arena_free(&root); fflush(stdout); return code; }}")
        return "\n".join(out) + "\n"

    def test_main(self):
        lines = ["#include <setjmp.h>", "extern __thread jmp_buf *lu_test_jmp; void lu_visit_reset(void);",
                 "int main(int argc, char **argv) { lu_init(argc, argv); int pass = 0, fail = 0; jmp_buf jb;"]
        for name, cname in self.tests:
            nm = c_string(name)
            lines.append(
                f"  if (argc < 2 || strstr({nm}, argv[1])) {{ lu_arena root; lu_arena_init(&root); lu_cur = &root; lu_test_jmp = &jb;"
                f" if (!setjmp(jb)) {{ lu_res_void r = {cname}(); fflush(stdout);"
                f" if (r.ok) {{ pass++; fprintf(stderr, \"  ok    %s\\n\", {nm}); }}"
                f" else {{ fail++; fprintf(stderr, \"  FAIL  %s: error: %.*s\\n\", {nm}, (int)r.err.msg.len, r.err.msg.p); lu_trace_print(); }} }}"
                f" else {{ fail++; lu_visit_reset(); fprintf(stderr, \"  FAIL  %s\\n\", {nm}); }}"
                f" lu_test_jmp = NULL; lu_cur = &root; lu_arena_free(&root); }}")
        lines.append('  fprintf(stderr, "\\n%d passed, %d failed\\n", pass, fail); return fail ? 1 : 0; }')
        return "\n".join(lines)

    def helpers(self, t, protos, defs):
        m, c = mangle(t), ctype(t)
        A = "__attribute__((unused)) static"
        # writer
        if isinstance(t, Prim) and t.name != "void":
            if is_int(t): w = f"lu_sb_{'i64' if is_signed(t) else 'u64'}(b, v);"
            elif is_float(t): w = "lu_sb_f64(b, (double)v);"
            elif t == BOOL: w = "lu_sb_bool(b, v);"
            else: w = "lu_sb_quoted(b, v);"
        elif isinstance(t, Arr):
            w = (f"if (lu_visit_find(v)) {{ lu_sb_put(b, \"[...]\", 5); return; }} lu_visit_push(v, v); "
                 f"lu_sb_put(b, \"[\", 1); for (int64_t i = 0; i < v->len; i++) {{ if (i) lu_sb_put(b, \", \", 2); "
                 f"lu_w_{mangle(t.elem)}(b, v->data[i]); }} lu_sb_put(b, \"]\", 1); lu_visit_pop();")
        elif isinstance(t, StructT):
            parts = [f"lu_sb_put(b, {c_string(t.name + '{')}, {len(t.name) + 1});"]
            for i, (fn, ft) in enumerate(self.structs[t.name]):
                lab = (", " if i else "") + fn + ": "
                parts.append(f"lu_sb_put(b, {c_string(lab)}, {len(lab)}); lu_w_{mangle(ft)}(b, v.{fn});")
            parts.append("lu_sb_put(b, \"}\", 1);")
            w = " ".join(parts)
        elif isinstance(t, Fn): w = "(void)v; lu_sb_put(b, \"<fn>\", 4);"
        elif isinstance(t, TaskT): w = "(void)v; lu_sb_put(b, \"<task>\", 6);"
        elif isinstance(t, ErrT): w = "if (!v.msg.p) { lu_sb_put(b, \"<nil>\", 5); return; } lu_sb_put(b, \"error(\", 6); lu_sb_quoted(b, v.msg); lu_sb_put(b, \")\", 1);"
        else: w = None
        if w:
            protos.append(f"{A} void lu_w_{m}(lu_sb *b, {c} v);")
            defs.append(f"{A} void lu_w_{m}(lu_sb *b, {c} v) {{ {w} }}")
        # equality
        if isinstance(t, StructT):
            cmp = []
            for fn, ft in self.structs[t.name]:
                if ft == STR: cmp.append(f"lu_str_eq(a.{fn}, b.{fn})")
                elif isinstance(ft, (StructT, Arr)): cmp.append(f"lu_eq_{mangle(ft)}(a.{fn}, b.{fn})")
                elif isinstance(ft, Fn): cmp.append(f"(a.{fn} == b.{fn})")
                else: cmp.append(f"(a.{fn} == b.{fn})")
            protos.append(f"{A} bool lu_eq_{m}({c} a, {c} b);")
            defs.append(f"{A} bool lu_eq_{m}({c} a, {c} b) {{ (void)a; (void)b; return {' && '.join(cmp) or 'true'}; }}")
        elif isinstance(t, Arr):
            el = t.elem
            if el == STR: ec = "lu_str_eq(a->data[i], b->data[i])"
            elif isinstance(el, (StructT, Arr)): ec = f"lu_eq_{mangle(el)}(a->data[i], b->data[i])"
            else: ec = "(a->data[i] == b->data[i])"
            protos.append(f"{A} bool lu_eq_{m}({c} a, {c} b);")
            defs.append(f"{A} bool lu_eq_{m}({c} a, {c} b) {{ if (a == b) return true; if (a->len != b->len) return false; "
                        f"if (lu_visit_find(a) == b) return true; lu_visit_push(a, b); "
                        f"for (int64_t i = 0; i < a->len; i++) if (!{ec}) {{ lu_visit_pop(); return false; }} lu_visit_pop(); return true; }}")
        if isinstance(t, StructT) and not self.contains_task(t):
            fz = " ".join(f"v.{fn} = {self.zero(ft, None)};" for fn, ft in self.structs[t.name])
            protos.append(f"{A} {c} lu_zero_{m}(void);")
            defs.append(f"{A} {c} lu_zero_{m}(void) {{ {c} v; memset(&v, 0, sizeof v); {fz} return v; }}")
        # deep copy into an arena
        if isinstance(t, (Prim, Fn)) and t != VOID:
            body = "return lu_str_copy(v, a);" if t == STR else "(void)a; return v;"
        elif isinstance(t, Arr) and not self.contains_task(t):
            n = arr_name(t)
            body = (f"{n} *r = lu_visit_find(v); if (r) return r; "
                    f"r = lu_alloc_in(a, sizeof *r); r->ar = a; r->len = r->cap = v->len; "
                    f"r->data = v->len ? lu_alloc_in(a, sizeof(*r->data) * (size_t)v->len) : NULL; lu_visit_push(v, r); "
                    f"for (int64_t i = 0; i < v->len; i++) r->data[i] = lu_cp_{mangle(t.elem)}(v->data[i], a); lu_visit_pop(); return r;")
        elif isinstance(t, StructT) and not self.contains_task(t):
            body = " ".join(f"v.{fn} = lu_cp_{mangle(ft)}(v.{fn}, a);" for fn, ft in self.structs[t.name]) + " (void)a; return v;"
        elif isinstance(t, ErrT):
            body = "return lu_err_keep(v, a);"
        elif isinstance(t, Res):
            inner = "" if t.inner == VOID else f"if (v.ok) v.val = lu_cp_{mangle(t.inner)}(v.val, a); "
            body = f"{inner}if (!v.ok) v.err = lu_err_keep(v.err, a); return v;"
        else:
            body = None
        if body:
            protos.append(f"{A} {c} lu_cp_{m}({c} v, lu_arena *a);")
            defs.append(f"{A} {c} lu_cp_{m}({c} v, lu_arena *a) {{ {body} }}")


BUILTINS = {"read_file", "write_file", "read_stdin", "print", "eprint", "panic", "assert", "assert_eq", "error", "argc", "arg", "now_ns", "mem_live"}

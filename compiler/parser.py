"""Lume parser: tokens -> AST of generic Nodes."""
from lexer import lex, CompileError, Tok


class Node:
    def __init__(self, kind, line, **kw):
        self.kind, self.line = kind, line
        self.pub = False
        self.__dict__.update(kw)

    def __repr__(self):
        f = {k: v for k, v in self.__dict__.items() if k not in ("kind", "line")}
        return f"{self.kind}({f})"


# binary precedence (higher binds tighter)
BINOPS = {
    "||": 1, "&&": 2,
    "==": 3, "!=": 3, "<": 3, "<=": 3, ">": 3, ">=": 3,
    "|": 4, "^": 5, "&": 6,
    "<<": 7, ">>": 7,
    "+": 8, "-": 8, "+%": 8, "-%": 8, "*%": 9,
    "*": 9, "/": 9, "%": 9,
}
ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^="}


class Parser:
    def __init__(self, src, file, collect=None):
        self.file = file
        self.collect = collect   # migration mode: record where semicolons are missing instead of failing
        self.toks = lex(src, file)
        self.i = 0

    # ---- helpers
    @property
    def t(self):
        return self.toks[self.i]

    def peek(self, k=1):
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def err(self, msg, tok=None):
        raise CompileError(msg, self.file, (tok or self.t).line)

    def at(self, kind, val=None):
        t = self.t
        return t.kind == kind and (val is None or t.val == val)

    def at_sym(self, s):
        return self.t.kind == "sym" and self.t.val == s

    def at_kw(self, s):
        return self.t.kind == "kw" and self.t.val == s

    def eat_sym(self, s):
        if self.at_sym(s):
            self.i += 1
            return True
        return False

    def eat_kw(self, s):
        if self.at_kw(s):
            self.i += 1
            return True
        return False

    def expect_sym(self, s):
        if not self.at_sym(s):
            self.err(f"expected '{s}', found {self.desc()}")
        self.i += 1

    def semi(self, what="statement"):
        if self.eat_sym(";"):
            return
        if self.collect is not None:
            self.collect.append(self.toks[self.i - 1].end)
            return
        self.err(f"expected ';' after {what}, found {self.desc()}", self.toks[self.i - 1])

    def line_sensitive(self):
        return self.collect is not None   # old newline rules only when migrating old code

    def expect_name(self):
        if self.t.kind != "name":
            self.err(f"expected a name, found {self.desc()}")
        v = self.t.val
        self.i += 1
        return v

    def desc(self, t=None):
        t = t or self.t
        if t.kind == "eof":
            return "end of file"
        if t.kind == "str":
            return "a string"
        return f"'{t.val}'"

    # ---- program
    def program(self):
        decls = []
        while not self.at("eof"):
            if self.eat_sym(";"):
                continue
            decls.append(self.decl())
        return decls

    def decl(self):
        line = self.t.line
        if self.eat_kw("pub"):
            if not (self.at_kw("fn") or self.at_kw("struct") or self.at_kw("const")):
                self.err("`pub` can only mark a fn, struct or const")
            d = self.decl()
            d.pub = True
            return d
        if self.eat_kw("fn"):
            return self.fn_decl(line)
        if self.eat_kw("struct"):
            name = self.expect_name()
            self.expect_sym("{")
            fields = []
            while not self.eat_sym("}"):
                fl = self.t.line
                fname = self.expect_name()
                if self.at_sym(":"):
                    self.err("struct fields are written `name type` (no colon), e.g. `x f64`")
                fields.append((fname, self.type_(), fl))
                self.eat_sym(",") or self.eat_sym(";")
            return Node("struct", line, name=name, fields=fields)
        if self.eat_kw("extern"):
            if self.t.kind != "str" or len(self.t.parts) != 1 or not isinstance(self.t.parts[0], str):
                self.err("extern expects a plain string naming a .c file")
            path = self.t.parts[0]
            self.i += 1
            self.expect_sym("{")
            fns = []
            while not self.eat_sym("}"):
                fl = self.t.line
                pub = self.eat_kw("pub")
                if not self.eat_kw("fn"):
                    self.err("only 'fn' declarations are allowed inside extern")
                name = self.expect_name()
                params = self.params()
                ret = self.ret_type()
                fns.append(Node("fn", fl, name=name, params=params, ret=ret, body=None, extern=path, pub=pub))
                self.semi("extern declaration")
            return Node("extern", line, path=path, fns=fns)
        if self.eat_kw("test"):
            if self.t.kind != "str":
                self.err("test expects a name string")
            name = "".join(p for p in self.t.parts if isinstance(p, str))
            self.i += 1
            return Node("test", line, name=name, body=self.block())
        if self.eat_kw("import"):
            if self.t.kind != "str":
                self.err("import expects a path string")
            path = self.t.parts[0]
            self.i += 1
            self.semi("import")
            return Node("import", line, path=path)
        if self.eat_kw("const"):
            name = self.expect_name()
            ty = None
            if not self.at_sym("="):
                ty = self.type_()
            self.expect_sym("=")
            n = Node("const", line, name=name, ty=ty, value=self.expr())
            self.semi("const")
            return n
        self.err(f"expected a declaration (fn, struct, const, extern, test, import), found {self.desc()}")

    def fn_decl(self, line):
        name = self.expect_name()
        params = self.params()
        ret = self.ret_type()
        body = self.block()
        return Node("fn", line, name=name, params=params, ret=ret, body=body, extern=None)

    def params(self):
        self.expect_sym("(")
        ps = []
        while not self.eat_sym(")"):
            pl = self.t.line
            mut = self.eat_kw("mut")
            pname = self.expect_name()
            if self.at_sym(":"):
                self.err("parameters are written `name type` (no colon), e.g. `src str`")
            ps.append(Node("param", pl, name=pname, ty=self.type_(), mut=mut))
            if not self.at_sym(")"):
                self.expect_sym(",")
        return ps

    def ret_type(self):
        """`fn f() i64` or `fn f() (i64)` for one return, `fn f() (A, B)` for several.
        Omitted means nothing is returned."""
        if self.at_sym("(") and not (self.line_sensitive() and self.t.nl):
            return self.paren_type()
        if self.at_sym("{") or (self.line_sensitive() and self.t.nl) or self.at_sym(";") or self.at_sym("}") or self.at_sym(")") or self.at_sym(",") or self.at_sym("]") or self.at_sym("="):
            return Node("tname", self.t.line, name="void")
        line = self.t.line
        t = self.type_()                      # bare single return: `fn f() i64`
        if t.kind == "tname" and t.name == "error":
            return Node("tres", line, inner=Node("tname", line, name="void"))
        return t

    def paren_type(self):
        """`(T)`, `(error)`, `(T, error)` or a general multiple return `(A, B, ...)`."""
        line = self.t.line
        self.expect_sym("(")
        ts = [self.type_()]
        while self.eat_sym(","):
            ts.append(self.type_())
        self.expect_sym(")")
        is_err = lambda t: t.kind == "tname" and t.name == "error"
        if len(ts) == 1:
            return Node("tres", line, inner=Node("tname", line, name="void")) if is_err(ts[0]) else ts[0]
        if len(ts) == 2 and is_err(ts[1]) and not is_err(ts[0]):
            return Node("tres", line, inner=ts[0])
        return Node("ttup", line, items=ts)

    # ---- types
    def type_(self):
        line = self.t.line
        if self.at_sym("[") and self.peek().kind == "sym" and self.peek().val == "]":
            self.i += 2
            return Node("tarr", line, elem=self.type_())     # Go-style slice type: []T
        t = self.base_type()
        if self.at_sym("[") and self.peek().kind == "sym" and self.peek().val == "]":
            self.err(f"array types are written `[]T`, e.g. `[]{t.name if t.kind == 'tname' else 'i64'}`")
        return t

    def base_type(self):
        line = self.t.line
        if self.at_sym("!"):
            self.err("error unions are written Go-style: `(T, error)`")
        if self.at_sym("["):
            self.err("array types are written `[]T`, e.g. `[]i64`")
        if self.eat_kw("fn"):
            self.expect_sym("(")
            ps = []
            while not self.eat_sym(")"):
                ps.append(self.type_())
                if not self.at_sym(")"):
                    self.expect_sym(",")
            ret = self.ret_type()
            return Node("tfn", line, params=ps, ret=ret)
        name = self.expect_name()
        if name == "Task":
            self.expect_sym("[")
            inner = self.paren_type() if self.at_sym("(") else self.type_()
            self.expect_sym("]")
            return Node("ttask", line, inner=inner)
        return Node("tname", line, name=name)

    # ---- statements
    def block(self):
        line = self.t.line
        self.expect_sym("{")
        stmts = []
        while not self.eat_sym("}"):
            if self.at("eof"):
                self.err("unexpected end of file; missing '}'")
            if self.eat_sym(";"):
                continue
            stmts.append(self.stmt())
        return Node("block", line, stmts=stmts)

    def stmt(self):
        line = self.t.line
        if self.at_kw("let") or self.at_kw("var"):
            mutable = self.t.val == "var"
            self.i += 1
            name = self.expect_name()
            if self.eat_sym(","):
                names = [name, self.expect_name()]
                while self.eat_sym(","):
                    names.append(self.expect_name())
                self.expect_sym("=")
                n = Node("let_multi", line, names=names, value=self.expr(), mutable=mutable)
                self.semi()
                return n
            ty = None
            if self.at_sym(":"):
                self.err("type annotations are written `let name type = value` (no colon)")
            if not self.at_sym("="):
                ty = self.type_()
            self.expect_sym("=")
            n = Node("let", line, name=name, ty=ty, value=self.expr(), mutable=mutable)
            self.semi()
            return n
        if self.eat_kw("if"):
            return self.if_rest(line)
        if self.eat_kw("while"):
            cond = self.paren_cond("while")
            return Node("while", line, cond=cond, body=self.block())
        if self.eat_kw("for"):
            self.open_paren("for", "for (x in items) { ... }")
            n1 = self.expect_name()
            n2 = None
            if self.eat_sym(","):
                n2 = self.expect_name()
            if not self.eat_kw("in"):
                self.err("expected 'in'")
            it = self.expr()
            hi = None
            if self.eat_sym(".."):
                hi = self.expr()
            self.close_paren("for")
            body = self.block()
            if hi is not None:
                if n2:
                    self.err("a range loop binds a single name")
                return Node("for_range", line, var=n1, lo=it, hi=hi, body=body)
            return Node("for_each", line, idx=n1 if n2 else None, var=n2 or n1, iter=it, body=body)
        if self.eat_kw("return"):
            if self.at_sym("}") or self.at_sym(";") or (self.line_sensitive() and self.t.nl):
                self.semi("return")
                return Node("return", line, value=None, err=None)
            vals = [self.expr()]
            while self.eat_sym(","):
                vals.append(self.expr())
            self.semi("return")
            return Node("return", line, value=vals[0], err=vals[1] if len(vals) == 2 else None, values=vals)
        if self.eat_kw("break"):
            self.semi("break")
            return Node("break", line)
        if self.eat_kw("continue"):
            self.semi("continue")
            return Node("continue", line)
        if self.eat_kw("defer"):
            if self.at_sym("{"):
                return Node("defer", line, body=self.block())
            s = self.stmt()
            return Node("defer", line, body=Node("block", line, stmts=[s]))
        if self.eat_kw("region"):
            return Node("region", line, body=self.block())
        if self.at_sym("{"):
            return self.block()
        e = self.expr()
        if self.at_sym(","):
            self.i += 1
            targets = [e, self.expr()]
            while self.eat_sym(","):
                targets.append(self.expr())
            self.expect_sym("=")
            n = Node("assign_multi", line, targets=targets, value=self.expr())
            self.semi("assignment")
            return n
        if self.t.kind == "sym" and self.t.val in ASSIGN_OPS:
            op = self.t.val
            self.i += 1
            rhs = self.expr()
            self.semi("assignment")
            return Node("assign", line, target=e, op=op, value=rhs)
        self.semi()
        return Node("expr_stmt", line, expr=e)

    def open_paren(self, kw, example):
        if not self.eat_sym("("):
            self.err(f"`{kw}` needs parentheses: {example}")

    def close_paren(self, kw):
        if not self.eat_sym(")"):
            self.err(f"expected ')' to close the `{kw}` header, found {self.desc()}")

    def paren_cond(self, kw):
        self.open_paren(kw, f"{kw} (condition) {{ ... }}")
        cond = self.expr()
        self.close_paren(kw)
        return cond

    def if_rest(self, line):
        cond = self.paren_cond("if")
        then = self.block()
        els = None
        if self.eat_kw("else"):
            if self.at_kw("if"):
                l2 = self.t.line
                self.i += 1
                els = Node("block", l2, stmts=[self.if_rest(l2)])
            else:
                els = self.block()
        return Node("if", line, cond=cond, then=then, els=els)

    # ---- expressions
    def expr(self, no_struct=False):
        saved = getattr(self, "no_struct", False)
        self.no_struct = no_struct
        try:
            return self.catch_expr()
        finally:
            self.no_struct = saved

    def catch_expr(self):
        e = self.binary(1)
        if self.at_kw("catch"):
            self.err("Lume uses Go-style errors: `let v, err = f()` then `if (err != nil) { ... }`")
        while self.at_kw("catch"):
            line = self.t.line
            self.i += 1
            if self.eat_sym("|"):
                name = self.expect_name()
                self.expect_sym("|")
                saved = self.no_struct
                self.no_struct = False
                body = self.block()
                self.no_struct = saved
                e = Node("catch", line, expr=e, name=name, body=body, default=None)
            else:
                e = Node("catch", line, expr=e, name=None, body=None, default=self.binary(1))
        return e

    def binary(self, minp):
        lhs = self.unary()
        while self.t.kind == "sym" and self.t.val in BINOPS and BINOPS[self.t.val] >= minp:
            op = self.t.val
            line = self.t.line
            self.i += 1
            rhs = self.binary(BINOPS[op] + 1)
            lhs = Node("binop", line, op=op, lhs=lhs, rhs=rhs)
        return lhs

    def unary(self):
        line = self.t.line
        if self.t.kind == "sym" and self.t.val in ("-", "!", "~"):
            op = self.t.val
            self.i += 1
            return Node("unop", line, op=op, operand=self.unary())
        if self.at_kw("try"):
            self.err("Lume uses Go-style errors: `let v, err = f()` then `if (err != nil) { return nil, err }`")
        if self.eat_kw("keep"):
            return Node("keep", line, expr=self.unary())
        if self.eat_kw("spawn"):
            e = self.unary()
            if e.kind != "call":
                self.err("spawn expects a function call", )
            return Node("spawn", line, call=e)
        return self.postfix(self.primary())

    def postfix(self, e):
        while True:
            t = self.t
            if t.kind == "sym" and t.val == "(" and not (t.nl and self.line_sensitive()):
                self.i += 1
                args = []
                while not self.eat_sym(")"):
                    al = self.t.line
                    if self.eat_kw("mut"):
                        args.append(Node("mut_arg", al, expr=self.expr()))
                    else:
                        args.append(self.expr())
                    if not self.at_sym(")"):
                        self.expect_sym(",")
                e = Node("call", t.line, fn=e, args=args)
            elif t.kind == "sym" and t.val == "[" and not (t.nl and self.line_sensitive()):
                self.i += 1
                idx = self.expr()
                if self.eat_sym(".."):
                    hi = self.expr()
                    self.expect_sym("]")
                    e = Node("slice", t.line, base=e, lo=idx, hi=hi)
                    continue
                self.expect_sym("]")
                e = Node("index", t.line, base=e, index=idx)
            elif t.kind == "sym" and t.val == ".":
                self.i += 1
                name = self.expect_name()
                e = Node("field", t.line, base=e, name=name)
            else:
                return e

    def primary(self):
        t = self.t
        line = t.line
        if t.kind == "int":
            self.i += 1
            return Node("int", line, value=t.val, byte=getattr(t, "byte", False))
        if t.kind == "float":
            self.i += 1
            return Node("float", line, value=t.val)
        if t.kind == "str":
            self.i += 1
            parts = []
            for p in t.parts:
                if isinstance(p, str):
                    parts.append(p)
                else:
                    src, pl = p
                    sub = Parser(src, self.file)
                    for tk in sub.toks:
                        tk.line = pl
                    e = sub.expr()
                    if not sub.at("eof"):
                        sub.err("unexpected tokens in interpolation")
                    parts.append(e)
            return Node("str", line, parts=parts)
        if self.eat_kw("nil"):
            return Node("nil", line)
        if self.eat_kw("true"):
            return Node("bool", line, value=True)
        if self.eat_kw("false"):
            return Node("bool", line, value=False)
        if self.eat_sym("("):
            e = self.expr()
            self.expect_sym(")")
            return e
        if self.eat_sym("["):
            items = []
            while not self.eat_sym("]"):
                items.append(self.expr())
                if not self.at_sym("]"):
                    self.expect_sym(",")
            return Node("array", line, items=items)
        if t.kind == "name":
            self.i += 1
            # struct literal: Name { field: value, ... }
            if self.at_sym("{") and not self.no_struct and t.val[:1].isupper():
                self.i += 1
                fields = []
                while not self.eat_sym("}"):
                    fl = self.t.line
                    fname = self.expect_name()
                    self.expect_sym(":")
                    fields.append((fname, self.expr(), fl))
                    if not self.at_sym("}"):
                        self.expect_sym(",")
                return Node("struct_lit", line, name=t.val, fields=fields)
            return Node("name", line, name=t.val)
        self.err(f"expected an expression, found {self.desc()}")


def parse(src, file):
    return Parser(src, file).program()

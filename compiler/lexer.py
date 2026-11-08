"""Lume lexer. Produces tokens with line numbers and a 'newline before' flag
(used to make calls/indexing line-sensitive so semicolons are optional)."""
from dataclasses import dataclass, field


class CompileError(Exception):
    def __init__(self, msg, file, line, col=0):
        super().__init__(msg)
        self.msg, self.file, self.line, self.col = msg, file, line, col

    def __str__(self):
        return f"{self.file}:{self.line}: error: {self.msg}"


KEYWORDS = {
    "fn", "let", "var", "if", "else", "while", "for", "in", "return", "break",
    "continue", "struct", "true", "false", "defer", "region", "try", "catch",
    "spawn", "extern", "test", "import", "mut", "keep", "const", "nil", "pub",
}

SYMBOLS = [
    "+%", "-%", "*%", "..", "==", "!=", "<=", ">=", "&&", "||", "<<", ">>", "+=", "-=", "*=", "/=",
    "%=", "&=", "|=", "^=", "->",
    "+", "-", "*", "/", "%", "<", ">", "=", "!", "(", ")", "{", "}", "[", "]",
    ",", ":", ";", ".", "&", "|", "^", "~",
]


@dataclass
class Tok:
    kind: str          # 'name', 'kw', 'int', 'float', 'str', 'sym', 'eof'
    val: object
    line: int
    nl: bool = False   # newline between previous token and this one
    parts: list = field(default_factory=list)  # for strings: [str | (src, line)]


def lex(src, file):
    toks, i, line, n = [], 0, 1, len(src)
    nl = True

    def err(m):
        raise CompileError(m, file, line)

    while i < n:
        c = src[i]
        if c == "\n":
            line += 1; i += 1; nl = True; continue
        if c in " \t\r":
            i += 1; continue
        if src.startswith("//", i):
            while i < n and src[i] != "\n":
                i += 1
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                err("unterminated block comment")
            line += src.count("\n", i, j); i = j + 2; continue
        start_line = line
        tok_start = i
        if (c.isalpha() or c == "_") and not (c == "i" and i + 1 < n and src[i + 1] == "\"") :
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            w = src[i:j]
            toks.append(Tok("kw" if w in KEYWORDS else "name", w, line, nl))
            i = j
        elif c.isdigit():
            j = i
            is_float = False
            if src.startswith(("0x", "0X"), i):
                j += 2
                while j < n and (src[j] in "0123456789abcdefABCDEF_"):
                    j += 1
                val = int(src[i + 2:j].replace("_", ""), 16)
            elif src.startswith(("0b", "0B"), i):
                j += 2
                while j < n and src[j] in "01_":
                    j += 1
                val = int(src[i + 2:j].replace("_", ""), 2)
            else:
                while j < n and (src[j].isdigit() or src[j] == "_"):
                    j += 1
                if j + 1 < n and src[j] == "." and src[j + 1].isdigit():
                    is_float = True
                    j += 1
                    while j < n and (src[j].isdigit() or src[j] == "_"):
                        j += 1
                if j < n and src[j] in "eE":
                    k = j + 1
                    if k < n and src[k] in "+-":
                        k += 1
                    if k < n and src[k].isdigit():
                        is_float = True
                        j = k
                        while j < n and src[j].isdigit():
                            j += 1
                text = src[i:j].replace("_", "")
                val = float(text) if is_float else int(text)
            toks.append(Tok("float" if is_float else "int", val, line, nl))
            i = j
        elif c == "'":
            # byte literal: 'a', '\n'
            j = i + 1
            if j < n and src[j] == "\\":
                esc = src[j + 1] if j + 1 < n else ""
                m = {"n": 10, "t": 9, "r": 13, "0": 0, "\\": 92, "'": 39, '"': 34}
                if esc not in m:
                    err(f"unknown escape \\{esc}")
                val, j = m[esc], j + 2
            else:
                ch = src[j] if j < n else ""
                b = ch.encode("utf-8")
                if len(b) != 1:
                    err("byte literal must be a single ASCII character")
                val, j = b[0], j + 1
            if j >= n or src[j] != "'":
                err("unterminated byte literal")
            toks.append(Tok("int", val, line, nl))
            toks[-1].byte = True
            i = j + 1
        elif c == '"' or (c == "i" and i + 1 < n and src[i + 1] == '"'):
            # "plain" strings are literal; i"..." strings interpolate {expr}
            interp = c == "i"
            parts, buf, j = [], [], i + (2 if interp else 1)
            while True:
                if j >= n or src[j] == "\n":
                    err("unterminated string")
                ch = src[j]
                if ch == '"':
                    j += 1; break
                if ch == "\\":
                    esc = src[j + 1] if j + 1 < n else ""
                    m = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"', "{": "{", "}": "}"}
                    if esc not in m:
                        err(f"unknown escape \\{esc}")
                    buf.append(m[esc]); j += 2; continue
                if ch == "{" and interp:
                    k, depth = j + 1, 1
                    while k < n and depth:
                        if src[k] == "{": depth += 1
                        elif src[k] == "}": depth -= 1
                        elif src[k] == "\n": err("newline inside interpolation")
                        k += 1
                    if depth:
                        err("unterminated interpolation")
                    if k - 1 == j + 1:
                        err("empty interpolation {}")
                    if buf:
                        parts.append("".join(buf)); buf = []
                    parts.append((src[j + 1:k - 1], line))
                    j = k; continue
                buf.append(ch); j += 1
            if buf or not parts:
                parts.append("".join(buf))
            toks.append(Tok("str", None, line, nl, parts))
            i = j
        else:
            for s in SYMBOLS:
                if src.startswith(s, i):
                    toks.append(Tok("sym", s, line, nl))
                    i += len(s)
                    break
            else:
                err(f"unexpected character {c!r}")
        toks[-1].end = i          # source offset just past this token
        toks[-1].start = tok_start
        nl = False
        _ = start_line
    toks.append(Tok("eof", None, line, True))
    return toks

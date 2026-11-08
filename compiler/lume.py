#!/usr/bin/env python3
"""lume — the Lume toolchain.

  lume run   FILE.lm [args...]   compile (debug) and run
  lume build FILE.lm [-o OUT]    compile to a native binary
  lume test  FILE.lm [filter]    run the file's `test "..." { }` blocks
  lume emit  FILE.lm             print the generated C

Flags: --release (optimise), --asan (AddressSanitizer + UBSan), --tsan (ThreadSanitizer), --cc=CC, --keep-c
"""
import os, subprocess, sys, hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from lexer import CompileError  # noqa
from parser import parse  # noqa
from codegen import Gen  # noqa

RUNTIME = os.path.normpath(os.path.join(HERE, "..", "runtime"))


def load(path):
    files, seen, order = [], set(), [path]
    while order:
        p = order.pop(0)
        ap = os.path.normpath(p)
        if ap in seen:
            continue
        seen.add(ap)
        try:
            src = open(ap, encoding="utf-8").read()
        except OSError as e:
            raise CompileError(f"cannot read {ap}: {e.strerror}", path, 0)
        decls = parse(src, ap)
        imports = []
        for d in decls:
            if d.kind == "import":
                ip = os.path.normpath(os.path.join(os.path.dirname(ap), d.path))
                imports.append(ip)
                order.append(ip)
        files.append((ap, [d for d in decls if d.kind != "import"], imports))
    return files


def compile_c(path, test_mode=False):
    files = load(path)
    g = Gen(files)
    return g.run(test_mode=test_mode), g.extern_files


def build(path, out, opts, test_mode=False):
    csrc, externs = compile_c(path, test_mode)
    bdir = os.path.join(os.path.dirname(os.path.abspath(path)), ".lume-build")
    os.makedirs(bdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(path))[0] + ("_test" if test_mode else "")
    cfile = os.path.join(bdir, base + ".c")
    with open(cfile, "w") as f:
        f.write(csrc)
    cc = opts.get("cc") or os.environ.get("CC", "cc")
    flags = ["-std=gnu11", "-pthread", "-I", RUNTIME, "-Wall", "-Wno-unused-variable",
             "-Wno-unused-label", "-Wno-unused-value", "-Wno-unused-but-set-variable"]
    if opts.get("release"):
        flags += ["-O2"]
    else:
        flags += ["-O1", "-g", "-fno-omit-frame-pointer"]
    if opts.get("tsan"):
        flags += ["-fsanitize=thread"]
    if opts.get("asan"):
        flags += ["-fsanitize=address,undefined", "-fno-sanitize-recover=undefined", "-DLU_NO_CACHE"]
    cmd = [cc] + flags + ["-o", out, cfile, os.path.join(RUNTIME, "lume_rt.c")] + externs + ["-lm"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.stderr.write(f"\nlume: C compilation failed (generated C kept at {cfile}). "
                         "This is a compiler bug unless it comes from an extern .c file.\n")
        sys.exit(2)
    if r.stderr.strip() and os.environ.get("LUME_SHOW_CC_WARNINGS"):
        sys.stderr.write(r.stderr)
    return out


def main(argv):
    opts, rest = {}, []
    for a in argv[1:]:
        if a == "--release": opts["release"] = True
        elif a == "--asan": opts["asan"] = True
        elif a == "--tsan": opts["tsan"] = True
        elif a == "--keep-c": opts["keep"] = True
        elif a.startswith("--cc="): opts["cc"] = a[5:]
        else: rest.append(a)
    if len(rest) < 2 or rest[0] not in ("run", "build", "test", "emit"):
        print(__doc__.strip())
        return 0 if len(rest) == 0 else 2
    cmd, path, extra = rest[0], rest[1], rest[2:]
    try:
        if cmd == "emit":
            sys.stdout.write(compile_c(path)[0])
            return 0
        if cmd == "build":
            out = os.path.splitext(path)[0]
            if "-o" in extra:
                out = extra[extra.index("-o") + 1]
            build(path, out, opts)
            return 0
        bdir = os.path.join(os.path.dirname(os.path.abspath(path)), ".lume-build")
        tag = hashlib.md5(repr(sorted(opts.items())).encode()).hexdigest()[:6]
        exe = os.path.join(bdir, os.path.splitext(os.path.basename(path))[0] + ("_test" if cmd == "test" else "") + "_" + tag)
        build(path, exe, opts, test_mode=(cmd == "test"))
        return subprocess.call([exe] + extra)
    except CompileError as e:
        sys.stderr.write(str(e) + "\n")
        try:
            src_line = open(e.file, encoding="utf-8").read().split("\n")[e.line - 1]
            sys.stderr.write(f"    {e.line} | {src_line.strip()}\n")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

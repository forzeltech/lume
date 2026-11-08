#!/usr/bin/env python3
"""Lume test suite.

tests/run/*.lm    must compile and print exactly the `// expect:` lines (exit 0,
                  or `// exit: N`)
tests/fail/*.lm   must be rejected by the compiler; `// error: text` must appear
tests/panic/*.lm  must panic at runtime; `// panic: text` must appear on stderr
tests/unit/*.lm   run with `lume test`; all tests must pass

Usage: run_tests.py [--asan] [--release] [name-filter]
"""
import os, subprocess, sys, glob

ROOT = os.path.dirname(os.path.abspath(__file__))
LUME = [sys.executable, os.path.join(ROOT, "..", "compiler", "lume.py")]


def directives(path, key):
    out = []
    for line in open(path, encoding="utf-8"):
        s = line.strip()
        p = f"// {key}:"
        if s.startswith(p):
            v = s[len(p):]
            out.append(v[1:] if v.startswith(" ") else v)
    return out


def main():
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    filt = [a for a in sys.argv[1:] if not a.startswith("--")]
    passed, failed = 0, []
    for kind in ("run", "fail", "panic", "unit"):
        for path in sorted(glob.glob(os.path.join(ROOT, kind, "*.lm"))):
            name = f"{kind}/{os.path.basename(path)}"
            if filt and not any(f in name for f in filt):
                continue
            if directives(path, "nosan") and ("--asan" in flags or "--tsan" in flags):
                continue
            cmd = "test" if kind == "unit" else "run"
            r = subprocess.run(LUME + [cmd, path] + flags + directives(path, "args"),
                               capture_output=True, text=True, timeout=300)
            ok, why = True, ""
            if kind == "run":
                want = directives(path, "expect")
                got = r.stdout.rstrip("\n").split("\n") if r.stdout else []
                code = int((directives(path, "exit") or ["0"])[0])
                if r.returncode != code:
                    ok, why = False, f"exit {r.returncode} (want {code})\n{r.stderr}"
                elif got != want:
                    ok, why = False, "output differs:\n" + "\n".join(
                        f"  {'ok ' if i < len(want) and i < len(got) and want[i] == got[i] else '!! '}"
                        f"want={want[i] if i < len(want) else '<none>'!r} got={got[i] if i < len(got) else '<none>'!r}"
                        for i in range(max(len(want), len(got))))
                for e in directives(path, "stderr"):
                    if e not in r.stderr:
                        ok, why = False, f"stderr missing {e!r}:\n{r.stderr}"
            elif kind == "fail":
                if r.returncode != 1:
                    ok, why = False, f"expected compile error, got exit {r.returncode}\n{r.stdout}{r.stderr}"
                for e in directives(path, "error"):
                    if e not in r.stderr:
                        ok, why = False, f"error message missing {e!r}; got:\n{r.stderr}"
            elif kind == "panic":
                if r.returncode != 101:
                    ok, why = False, f"expected panic (exit 101), got {r.returncode}\n{r.stderr}"
                for e in directives(path, "panic"):
                    if e not in r.stderr:
                        ok, why = False, f"panic message missing {e!r}; got:\n{r.stderr}"
            else:
                if r.returncode != 0:
                    ok, why = False, r.stderr
            if "AddressSanitizer" in r.stderr or "runtime error:" in r.stderr:
                ok, why = False, "sanitizer report:\n" + r.stderr
            if ok:
                passed += 1
            else:
                failed.append((name, why))
                print(f"FAIL {name}\n{why}\n")
    # every example must build and run (exit status from `// exit: N`, default 0)
    ex_root = os.path.join(ROOT, "..", "examples")
    examples = sorted(glob.glob(os.path.join(ex_root, "*.lm")) + glob.glob(os.path.join(ex_root, "*", "main.lm")))
    for path in examples:
        name = "examples/" + os.path.relpath(path, ex_root)
        if filt and not any(f in name for f in filt):
            continue
        r = subprocess.run(LUME + ["run", path] + flags, capture_output=True, text=True, timeout=300)
        code = int((directives(path, "exit") or ["0"])[0].split()[0])
        ok = r.returncode == code and "Sanitizer" not in r.stderr and "runtime error:" not in r.stderr
        if ok and directives(path, "test") == [] and "test \"" in open(path).read():
            t = subprocess.run(LUME + ["test", path] + flags, capture_output=True, text=True, timeout=300)
            ok = t.returncode == 0
        if ok:
            passed += 1
        else:
            failed.append((name, r.stderr))
            print(f"FAIL {name} (exit {r.returncode}, want {code})\n{r.stderr}\n")
    print(f"\n{passed} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

"""Lume types and their C representations."""
from dataclasses import dataclass

INT_TYPES = {
    "i8": ("int8_t", -2**7, 2**7 - 1), "i16": ("int16_t", -2**15, 2**15 - 1),
    "i32": ("int32_t", -2**31, 2**31 - 1), "i64": ("int64_t", -2**63, 2**63 - 1),
    "u8": ("uint8_t", 0, 2**8 - 1), "u16": ("uint16_t", 0, 2**16 - 1),
    "u32": ("uint32_t", 0, 2**32 - 1), "u64": ("uint64_t", 0, 2**64 - 1),
}
FLOAT_TYPES = {"f32": "float", "f64": "double"}
PRIM_C = {**{k: v[0] for k, v in INT_TYPES.items()}, **FLOAT_TYPES,
          "bool": "bool", "str": "lu_str", "void": "void"}


@dataclass(frozen=True)
class Prim:
    name: str
    def __str__(self): return self.name


@dataclass(frozen=True)
class Arr:
    elem: object
    def __str__(self): return f"[]{self.elem}"


@dataclass(frozen=True)
class Res:
    inner: object
    def __str__(self): return "(error)" if self.inner == VOID else f"({self.inner}, error)"


@dataclass(frozen=True)
class Tup:
    """A general multiple return: (A, B, ...)."""
    items: tuple
    def __str__(self): return "(" + ", ".join(map(str, self.items)) + ")"


@dataclass(frozen=True)
class Fn:
    params: tuple
    ret: object
    def __str__(self):
        r = "" if self.ret == VOID else (f" {self.ret}" if isinstance(self.ret, (Res, Tup)) else f" ({self.ret})")
        return f"fn({', '.join(map(str, self.params))}){r}"


@dataclass(frozen=True)
class StructT:
    name: str
    def __str__(self): return self.name


@dataclass(frozen=True)
class TaskT:
    inner: object
    def __str__(self): return f"Task[{self.inner}]"


@dataclass(frozen=True)
class ErrT:
    def __str__(self): return "error"


VOID, BOOL, STR, I64, F64, U8 = Prim("void"), Prim("bool"), Prim("str"), Prim("i64"), Prim("f64"), Prim("u8")
ERR = ErrT()


def is_int(t): return isinstance(t, Prim) and t.name in INT_TYPES
def is_float(t): return isinstance(t, Prim) and t.name in FLOAT_TYPES
def is_num(t): return is_int(t) or is_float(t)
def is_signed(t): return is_int(t) and t.name.startswith("i")


def mangle(t):
    if isinstance(t, Prim): return t.name
    if isinstance(t, Arr): return "A" + mangle(t.elem) + "_"
    if isinstance(t, Res): return "R" + mangle(t.inner) + "_"
    if isinstance(t, Fn): return "F" + "".join(mangle(p) for p in t.params) + "_" + mangle(t.ret) + "_"
    if isinstance(t, StructT): return "S" + t.name
    if isinstance(t, TaskT): return "T" + mangle(t.inner) + "_"
    if isinstance(t, ErrT): return "E"
    if isinstance(t, Tup): return "U" + "".join(mangle(x) for x in t.items) + "_"
    raise AssertionError(t)


def ctype(t):
    if isinstance(t, Prim): return PRIM_C[t.name]
    if isinstance(t, Arr): return f"lu_arr_{mangle(t.elem)}*"
    if isinstance(t, Res): return f"lu_res_{mangle(t.inner)}"
    if isinstance(t, Fn): return f"lu_fn_{mangle(t)}"
    if isinstance(t, StructT): return f"lu_s_{t.name}"
    if isinstance(t, TaskT): return "lu_task*"
    if isinstance(t, ErrT): return "lu_err"
    if isinstance(t, Tup): return f"lu_tup_{mangle(t)}"
    raise AssertionError(t)


def arr_name(t):  # C struct name of an array type (without *)
    return f"lu_arr_{mangle(t.elem)}"

"""``envlayout@1``: the EnvLayout bound to the snapshot's QEMU binary, and its producer. WP17.

Producer (design §0 G21, plan WP17 outputs)
-------------------------------------------
:func:`produce_envlayout` runs ``gdb -nx -batch`` on the target executable
with ``ptype/o`` for ``struct CPUArchState`` and ``struct CPUState`` (whose
last member is ``CPUNegativeOffsetState neg``), parses the offset/size
columns, expands typedef'd struct/union members (``CPUARMTBFlags``,
``IcountDecr``, ``CPUTLB`` …) with further ``ptype/o`` calls, and converts
every leaf to an env-relative offset:

* ``CPUArchState`` leaves: offset as printed (``env`` region);
* ``CPUState.neg.*`` leaves: ``off - sizeof(CPUState)`` (``neg`` region);
* other ``CPUState`` leaves: ``off - sizeof(CPUState)`` (``cpu`` region),

using ``offsetof(ArchCPU, env) == sizeof(CPUState)`` (checked; G21's
``plugin_mem_value_high = env - 0x10`` follows). gdb never runs with a cwd
inside a QEMU tree (``cwd`` defaults to a fresh temporary directory) and
never reads an init file (``-nx``).

Analysis
--------
``envlayout@1`` (snapshot scope, input ``bundle`` is not needed): looks up
the layout for the snapshot's RuntimeIdentity: the v2 admission block's
``envlayout_id`` when present, else the packaged layout for
``qemu_sha256`` (:func:`scopt.formats.envlayout_v1.find_envlayout`). Value::

    {"bound": bool, "reason": None | "guard_layout_unbound",
     "envlayout_id": "sha256:…" | None, "exe_sha256": …, "layout": EnvLayout | None}
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..formats.envlayout_v1 import EnvField, EnvLayout, check_binding, find_envlayout
from .api import AnalysisSpec, ScopeRef

__all__ = [
    "GdbError", "produce_envlayout", "parse_ptype_o", "PtypeLeaf", "ENVLAYOUT", "ANALYSES",
    "envlayout_for_runtime", "sha256_file",
]

_ROW = re.compile(r"^/\*\s*(?P<off>[0-9: ]*?)\s*\|?\s*(?P<size>\d+)\s*\*/\s*(?P<decl>.*)$")
_CLOSE = re.compile(r"^\s*\}\s*(?P<name>[A-Za-z_]\w*)?\s*(?P<dims>(\[\d+\])*)\s*;?\s*$")
_ARR = re.compile(r"\[(\d+)\]")
_FNPTR = re.compile(r"\(\s*\*\s*(?P<name>[A-Za-z_]\w*)\s*\)")
_SCALARS = frozenset("""
_Bool bool char short int long float double void signed unsigned
int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t uintptr_t intptr_t size_t ssize_t
hwaddr vaddr target_ulong target_long ram_addr_t
""".split())


class GdbError(RuntimeError):
    pass


_MARK = "@@SCOPT_TYPE "


@dataclass
class PtypeLeaf:
    path: tuple[str, ...]
    off: int  # absolute offset inside the printed type
    size: int
    dims: tuple[int, ...] = ()
    type: str = ""

    @property
    def elem(self) -> int:
        n = 1
        for d in self.dims:
            n *= d
        return self.size // n if n else self.size


@dataclass
class _Frame:
    off: int | None
    size: int
    kind: str  # struct | union | top
    children: list[PtypeLeaf] = field(default_factory=list)


def _parse_decl(decl: str) -> tuple[str, str, tuple[int, ...]]:
    """``uint32_t regs[16];`` -> ("uint32_t", "regs", (16,)); bitfields keep the name."""
    d = decl.strip().rstrip(";").strip()
    m = _FNPTR.search(d)
    if m:
        return "fnptr", m.group("name"), ()
    if ":" in d:  # bitfield "unsigned int x : 3"
        d = d.split(":", 1)[0].strip()
    dims = tuple(int(x) for x in _ARR.findall(d))
    d = _ARR.sub("", d).strip()
    toks = d.replace("*", " * ").split()
    if not toks:
        return "", "", dims
    name = toks[-1]
    ty = " ".join(toks[:-1])
    return ty, name, dims


def _replicate(children: list[PtypeLeaf], base: int, size: int, dims: tuple[int, ...],
               name: str | None, cap: int) -> list[PtypeLeaf]:
    n = 1
    for d in dims:
        n *= d
    pre = (name,) if name else ()
    if not dims or n <= 1:
        return [PtypeLeaf(pre + c.path, c.off, c.size, c.dims, c.type) for c in children]
    if n * max(1, len(children)) > cap:
        return [PtypeLeaf(pre, base, size, dims, "struct")]
    esz = size // n
    out = []
    for i in range(n):
        idx = []
        k = i
        for d in reversed(dims):
            idx.append(k % d)
            k //= d
        tag = "".join(f"[{x}]" for x in reversed(idx))
        for c in children:
            p = (pre[0] + tag,) + c.path if pre else c.path
            out.append(PtypeLeaf(p, c.off + i * esz, c.size, c.dims, c.type))
    return out


def parse_ptype_o(text: str, *, cap: int = 4096) -> dict[str, tuple[int, list[PtypeLeaf]]]:
    """Parse the output of several ``ptype/o`` commands.

    Returns ``{type name: (size, leaves)}``; offsets are absolute within the
    type (gdb prints absolute offsets also inside nested anonymous members);
    union members without an offset column take the union's offset. A type
    that is not a struct/union (``type = unsigned long``) maps to ``(0, [])``.
    """
    out: dict[str, tuple[int, list[PtypeLeaf]]] = {}
    stack: list[_Frame] = []
    cur_name: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if "type = " in line:
            rest = line.split("type = ", 1)[1].strip()
            if rest.endswith("{"):
                hdr = rest[:-1].strip().split()
                cur_name = hdr[-1] if len(hdr) >= 2 else rest
                kind = hdr[0] if hdr else "struct"
                stack = [_Frame(0, 0, "top:" + kind)]
            else:
                out[rest] = (0, [])
                cur_name = None
                stack = []
            continue
        if not stack:
            continue
        st = line.strip()
        if not st:
            continue
        if st.startswith("/* XXX") or st.startswith("/* total size"):
            m = re.search(r"total size \(bytes\):\s*(\d+)", st)
            if m and len(stack) == 1:
                stack[0].size = int(m.group(1))
            continue
        if st.startswith("}"):
            if len(stack) == 1:
                if cur_name is not None:
                    out[cur_name] = (stack[0].size, stack[0].children)
                stack = []
                cur_name = None
                continue
            m = _CLOSE.match(st)
            fr = stack.pop()
            name = m.group("name") if m else None
            dims = tuple(int(x) for x in _ARR.findall(m.group("dims") or "")) if m else ()
            base = fr.off if fr.off is not None else 0
            stack[-1].children.extend(_replicate(fr.children, base, fr.size, dims, name, cap))
            continue
        m = _ROW.match(st)
        if not m:
            continue
        offs = m.group("off").strip()
        size = int(m.group("size"))
        decl = m.group("decl").strip()
        parent = stack[-1]
        if offs:
            off = int(offs.split(":")[0].strip())
        else:  # union member: the enclosing union's offset
            off = parent.off if parent.off is not None else 0
        if decl.endswith("{"):
            kind = decl[:-1].strip().split()[0] if decl[:-1].strip() else "struct"
            stack.append(_Frame(off, size, kind))
            continue
        ty, name, dims = _parse_decl(decl)
        if not name:
            continue
        parent.children.append(PtypeLeaf((name,), off, size, dims, ty))
    return out


def _gdb(exe: str | os.PathLike, cmds: Sequence[str], *, gdb: str = "gdb",
         timeout: float = 300.0, cwd: str | os.PathLike | None = None,
         check: bool = True) -> str:
    gdb_path = shutil.which(gdb)
    if gdb_path is None:
        raise GdbError(f"{gdb} not found")
    argv = [gdb_path, "-nx", "-batch", "-ex", "set max-value-size unlimited",
            "-ex", "set width 0", "-ex", "set print pretty off"]
    for c in cmds:
        argv += ["-ex", c]
    argv.append(os.fspath(exe))
    tmp = None
    if cwd is None:
        tmp = tempfile.mkdtemp(prefix="scopt-gdb-")
        cwd = tmp
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    if r.returncode != 0 and (check or not r.stdout):
        raise GdbError(f"gdb exited {r.returncode}: {r.stderr.strip()[:400]}")
    return r.stdout


def _print_ints(exe: str | os.PathLike, exprs: Sequence[str], **kw: Any) -> list[int]:
    out = _gdb(exe, [f"print (long)({e})" for e in exprs], **kw)
    vals = [int(m.group(1)) for m in re.finditer(r"^\$\d+ = (-?\d+)\s*$", out, re.M)]
    if len(vals) != len(exprs):
        raise GdbError(f"expected {len(exprs)} values from gdb, got {len(vals)}: {out[-400:]}")
    return vals


def sha256_file(path: str | os.PathLike) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return "sha256:" + h.hexdigest()


def _base_type(ty: str) -> str | None:
    t = ty.replace("const ", "").replace("volatile ", "").strip()
    if "*" in t or t == "fnptr" or not t:
        return None
    toks = t.split()
    if toks[0] in ("struct", "union", "enum"):
        if toks[0] == "enum":
            return None
        return " ".join(toks[:2])
    if all(x in _SCALARS for x in toks):
        return None
    return t


def _expand(exe: str | os.PathLike, top: Mapping[str, tuple[int, list[PtypeLeaf]]],
            depth: int, cap: int, kw: Mapping[str, Any]) -> tuple[dict[str, tuple[int, list]],
                                                               list[str]]:
    """Expand composite leaf types of every type in ``top`` (fixpoint up to ``depth``).

    Each requested type is preceded by an ``echo`` marker so that a type gdb
    cannot resolve (error on stderr, nothing on stdout) cannot shift the
    mapping of the others.
    """
    known: dict[str, tuple[int, list[PtypeLeaf]]] = dict(top)
    asked: list[str] = []
    for _ in range(depth):
        want = sorted({bt for _, leaves in known.values() for lf in leaves
                       if (bt := _base_type(lf.type)) and bt not in known})
        if not want:
            break
        asked += want
        cmds: list[str] = []
        for t in want:
            cmds += [f"echo {_MARK}{t}\\n", f"ptype/o {t}"]
        text = _gdb(exe, cmds, check=False, **kw)
        parts = text.split(_MARK)[1:]
        got: dict[str, str] = {}
        for part in parts:
            name, _, body = part.partition("\n")
            got[name.strip()] = body
        for t in want:
            p = parse_ptype_o(got.get(t, ""), cap=cap)
            known[t] = next(iter(p.values())) if p else (0, [])
    return known, asked


def _flatten(name: str, known: Mapping[str, tuple[int, list[PtypeLeaf]]], cap: int,
             depth: int = 4) -> list[PtypeLeaf]:
    """Leaves of type ``name`` with typedef'd composite members expanded (offsets absolute)."""
    _, leaves = known.get(name, (0, []))
    out: list[PtypeLeaf] = []
    for lf in leaves:
        bt = _base_type(lf.type)
        sub = known.get(bt) if bt else None
        if depth <= 0 or not sub or not sub[1]:
            out.append(lf)
            continue
        inner = _flatten(bt, known, cap, depth - 1)  # type: ignore[arg-type]
        n = 1
        for d in lf.dims:
            n *= d
        if lf.dims and n > 1:
            reps = _replicate(inner, 0, lf.size, lf.dims, lf.path[-1], cap)
            out.extend(PtypeLeaf(lf.path[:-1] + c.path, c.off + lf.off, c.size, c.dims, c.type)
                       for c in reps)
        else:
            out.extend(PtypeLeaf(lf.path + c.path, c.off + lf.off, c.size, c.dims, c.type)
                       for c in inner)
    return out


def produce_envlayout(exe: str | os.PathLike, *, gdb: str = "gdb", timeout: float = 300.0,
                      cap: int = 4096, exe_sha256: str | None = None) -> EnvLayout:
    """Build the :class:`EnvLayout` of ``exe`` from its DWARF (gdb ``-nx -batch``)."""
    kw = {"gdb": gdb, "timeout": timeout}
    text = _gdb(exe, ["ptype/o struct CPUArchState", "ptype/o struct CPUState"], **kw)
    top = parse_ptype_o(text, cap=cap)
    if "CPUArchState" not in top or "CPUState" not in top:
        raise GdbError("ptype/o did not describe CPUArchState and CPUState (no DWARF?)")
    known, asked = _expand(exe, top, 4, cap, kw)
    cs_size, ae_size, neg_size, env_off, neg_off = _print_ints(exe, [
        "sizeof(CPUState)", "sizeof(CPUArchState)", "sizeof(CPUNegativeOffsetState)",
        "&((ArchCPU*)0)->env", "&((CPUState*)0)->neg"], **kw)
    if env_off != cs_size:
        raise GdbError(f"offsetof(ArchCPU, env)={env_off} != sizeof(CPUState)={cs_size}")
    if neg_off + neg_size != cs_size:
        raise GdbError(f"CPUState.neg [{neg_off}, +{neg_size}) does not end at sizeof(CPUState)")
    fields: list[EnvField] = []
    for lf in _flatten("CPUArchState", known, cap):
        fields.append(_field(".".join(lf.path), "env", lf.off, lf))
    for lf in _flatten("CPUState", known, cap):
        if lf.path[0] in ("neg_align",) or lf.size == 0:
            continue
        region = "neg" if lf.path[0] == "neg" else "cpu"
        name = ".".join(lf.path) if region == "neg" else "cpu." + ".".join(lf.path)
        fields.append(_field(name, region, lf.off - cs_size, lf))
    try:
        ver = _gdb(exe, ["show version"], **kw).splitlines()[0].strip()
    except (GdbError, IndexError):
        ver = None
    return EnvLayout.build(
        exe_sha256=exe_sha256 or sha256_file(exe), fields=fields,
        sizes={"CPUState": cs_size, "CPUArchState": ae_size, "CPUNegativeOffsetState": neg_size},
        env_offset_in_cpu=env_off, neg_offset_in_cpustate=neg_off,
        producer={"tool": "gdb", "version": ver, "method": "ptype/o -nx -batch",
                  "types": ["struct CPUArchState", "struct CPUState"] + sorted(asked)})


def _field(name: str, region: str, off: int, lf: PtypeLeaf) -> EnvField:
    n = 1
    for d in lf.dims:
        n *= d
    elem = lf.size // n if lf.dims and n else lf.size
    return EnvField(name, region, off, lf.size, elem, tuple(lf.dims) if lf.dims and n > 1 else ())


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------

def envlayout_for_runtime(runtime: Mapping[str, Any] | None) -> dict[str, Any]:
    """The ``envlayout@1`` value for a RuntimeIdentity document (v1 or v2)."""
    exe = None if runtime is None else runtime.get("qemu_sha256")
    adm = None if runtime is None else runtime.get("admission")
    want_id = adm.get("envlayout_id") if isinstance(adm, Mapping) else None
    lay = find_envlayout(exe)
    reason = check_binding(lay, runtime, want_id)
    return {"bound": reason is None, "reason": reason,
            "envlayout_id": None if lay is None else lay.envlayout_id,
            "exe_sha256": exe, "layout": lay if reason is None else None}


def _compute(ctx: Any, scope: ScopeRef) -> dict[str, Any]:
    return envlayout_for_runtime(ctx.runtime)


ENVLAYOUT = AnalysisSpec(name="envlayout", version=1, scope="snapshot", requires=(),
                         inputs=frozenset({"meta"}), compute=_compute, result_schema=dict)
ANALYSES = (ENVLAYOUT,)


def _iter_names(fields: Iterable[EnvField]) -> list[str]:  # pragma: no cover - debugging aid
    return [f.name for f in fields]


__all__ += ["Path"]

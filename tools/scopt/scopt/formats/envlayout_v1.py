"""EnvLayout v1: env-relative field offsets bound to one QEMU executable (B3/B4 design §0 G21). WP17.

An :class:`EnvLayout` maps every leaf field reachable from the TCG env pointer
(``%rbp`` = ``&ARMCPU.env``) to its offset and size, for exactly one QEMU
executable (``exe_sha256``). It is produced from DWARF by
:func:`scopt.analysis.envlayout.produce_envlayout` (``gdb -nx -batch
'ptype/o …'``) and is the only source of field names for HostOps typing,
the observation contract and the guard template.

Regions (all offsets are relative to env, so ``neg``/``cpu`` offsets are
negative)::

    env   CPUARMState fields                    names "regs[15]", "hflags.flags2", "v7m.mpu_ctrl[0]"
    neg   CPUNegativeOffsetState (CPUState.neg)  names "neg.can_do_io", "neg.tlb.f[1].mask"
    cpu   other CPUState fields                  names "cpu.breakpoints", "cpu.singlestep_enabled"

Document (canonical JSON, ``schemas/envlayout.v1.json``)::

    {"schema": "scopt.envlayout/1", "exe_sha256": "sha256:…",
     "producer": {"tool": "gdb", "version": "…", "method": "ptype/o"},
     "sizes": {"CPUState": 11200, "CPUArchState": 78736, "CPUNegativeOffsetState": 10432},
     "env_offset_in_cpu": 11200, "neg_offset_in_cpustate": 768,
     "fields": [{"name": "regs", "region": "env", "off": 0, "size": 64,
                 "elem": 4, "dims": [16]}, …],
     "envlayout_id": "sha256:…"}

``envlayout_id`` is the content id of the document without itself. The
binding rule (design §9.1 V13/V20, reason ``guard_layout_unbound``): a layout
may be used for a RuntimeIdentity only when ``exe_sha256`` equals that
identity's ``qemu_sha256``; :func:`check_binding` returns the reason code.

Packaged layouts: ``scopt/formats/envlayouts/<exe sha256 hex>.json`` (the
pinned ``qint`` binary is shipped). :func:`find_envlayout` also searches the
directories in ``$SCOPT_ENVLAYOUT_PATH`` (``:``-separated).
"""

from __future__ import annotations

import bisect
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from ..canon import canonical_json, content_id, is_sha256_id
from . import schemas

__all__ = [
    "SCHEMA", "EnvField", "FieldRef", "EnvLayout", "EnvLayoutError", "read_envlayout",
    "find_envlayout", "packaged_envlayouts", "check_binding", "REGIONS", "G21_NAMES",
]

SCHEMA = "scopt.envlayout/1"
REGIONS = ("env", "neg", "cpu")

# Design §0 G21 short names -> EnvLayout element names. The guard (§4.1) and
# the observation contract (§7.2) refer to fields by these short names.
G21_NAMES: dict[str, str] = {
    "regs[15]": "regs[15]",
    "thumb": "thumb",
    "hflags.flags": "hflags.flags",
    "hflags.flags2": "hflags.flags2",
    "condexec_bits": "condexec_bits",
    "mpu_ctrl[0]": "v7m.mpu_ctrl[0]",
    "CPUState.breakpoints": "cpu.breakpoints.tqh_first",
    "CPUState.watchpoints": "cpu.watchpoints.tqh_first",
    "CPUState.singlestep_enabled": "cpu.singlestep_enabled",
    "plugin_mem_cbs": "neg.plugin_mem_cbs",
    "plugin_mem_value_low": "neg.plugin_mem_value_low",
    "plugin_mem_value_high": "neg.plugin_mem_value_high",
    "icount_decr": "neg.icount_decr.u32",
    "can_do_io": "neg.can_do_io",
}


class EnvLayoutError(ValueError):
    code = "envlayout_invalid"

    def __init__(self, detail: str, *, code: str | None = None):
        if code is not None:
            self.code = code
        self.detail = detail
        super().__init__(f"{self.code}: {detail}")


@dataclass(frozen=True, slots=True)
class EnvField:
    """One leaf field. ``dims`` is non-empty for arrays (``elem`` = element size)."""

    name: str
    region: str
    off: int
    size: int
    elem: int
    dims: tuple[int, ...] = ()

    @property
    def end(self) -> int:
        return self.off + self.size

    @property
    def count(self) -> int:
        n = 1
        for d in self.dims:
            n *= d
        return n

    def element_name(self, idx: int) -> str:
        """Name of flat element ``idx`` (row-major over ``dims``)."""
        if not self.dims:
            return self.name
        sub = []
        for d in reversed(self.dims):
            sub.append(idx % d)
            idx //= d
        return self.name + "".join(f"[{i}]" for i in reversed(sub))

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "region": self.region, "off": self.off,
                             "size": self.size, "elem": self.elem}
        if self.dims:
            d["dims"] = list(self.dims)
        return d

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "EnvField":
        return cls(str(d["name"]), str(d["region"]), int(d["off"]), int(d["size"]),
                   int(d["elem"]), tuple(int(x) for x in d.get("dims", ())))


@dataclass(frozen=True, slots=True)
class FieldRef:
    """What one env-relative access ``[env+disp, env+disp+width)`` touches.

    ``name`` is the element name (``"regs[3]"``); ``exact`` is true when the
    access covers exactly one whole element; otherwise ``sub`` is the byte
    offset inside the element (a partial access, e.g. ``neg.icount_decr.u32``
    read as 16 bits is never produced because the union's 16-bit member is a
    more specific exact match).
    """

    name: str
    region: str
    off: int  # env-relative offset of the element
    size: int  # element size
    sub: int = 0
    width: int = 0
    exact: bool = True

    @property
    def label(self) -> str:
        return self.name if self.exact else f"{self.name}+{self.sub}:{self.width}"


@dataclass(frozen=True)
class EnvLayout:
    exe_sha256: str
    fields: tuple[EnvField, ...]
    sizes: Mapping[str, int]
    env_offset_in_cpu: int
    neg_offset_in_cpustate: int
    producer: Mapping[str, Any] = field(default_factory=dict)
    envlayout_id: str = ""
    # lookup caches are plain instance attributes (not dataclass fields), so
    # they never enter value digests (scopt.analysis.store._enc)

    # -- identity -----------------------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        doc = {
            "schema": SCHEMA, "exe_sha256": self.exe_sha256,
            "producer": dict(self.producer), "sizes": dict(self.sizes),
            "env_offset_in_cpu": self.env_offset_in_cpu,
            "neg_offset_in_cpustate": self.neg_offset_in_cpustate,
            "fields": [f.to_json() for f in self.fields],
        }
        doc["envlayout_id"] = content_id(doc, "envlayout_id")
        return doc

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_json()) + b"\n"

    @classmethod
    def build(cls, *, exe_sha256: str, fields: Iterable[EnvField], sizes: Mapping[str, int],
              env_offset_in_cpu: int, neg_offset_in_cpustate: int,
              producer: Mapping[str, Any] | None = None) -> "EnvLayout":
        fs = tuple(sorted(fields, key=lambda f: (f.off, -f.size, f.name)))
        lay = cls(exe_sha256, fs, dict(sizes), env_offset_in_cpu, neg_offset_in_cpustate,
                  dict(producer or {}))
        return cls(lay.exe_sha256, lay.fields, lay.sizes, lay.env_offset_in_cpu,
                   lay.neg_offset_in_cpustate, lay.producer, lay.to_json()["envlayout_id"])

    # -- lookups ---------------------------------------------------------------------
    def _idx(self) -> tuple[list[int], list[EnvField], int]:
        if getattr(self, "_index", None) is None:
            offs = [f.off for f in self.fields]
            maxsize = max((f.size for f in self.fields), default=0)
            object.__setattr__(self, "_index", (offs, list(self.fields), maxsize))
        return self._index

    def covering(self, disp: int, width: int) -> list[EnvField]:
        """Every leaf that contains ``[disp, disp+width)`` entirely."""
        offs, fs, maxsize = self._idx()
        hi = bisect.bisect_right(offs, disp)
        out = []
        i = hi - 1
        while i >= 0 and fs[i].off > disp - maxsize - 1:
            f = fs[i]
            if f.off <= disp and disp + width <= f.end:
                out.append(f)
            i -= 1
        return out

    def classify(self, disp: int, width: int) -> FieldRef | None:
        """The most specific element covering the access, or None (unclassified)."""
        if width <= 0:
            width = 1
        cache = getattr(self, "_cls_cache", None)
        if cache is None:
            cache = {}
            object.__setattr__(self, "_cls_cache", cache)
        k = (disp, width)
        if k not in cache:
            cache[k] = self._classify(disp, width)
        return cache[k]

    def _classify(self, disp: int, width: int) -> FieldRef | None:
        cands = self.covering(disp, width)
        if not cands:
            return None
        best: FieldRef | None = None
        for f in cands:
            rel = disp - f.off
            idx, sub = divmod(rel, f.elem) if f.elem else (0, rel)
            if sub + width > f.elem:
                # spans two elements of an array: not one field
                continue
            ref = FieldRef(f.element_name(idx), f.region, f.off + idx * f.elem, f.elem, sub, width,
                           sub == 0 and width == f.elem)
            key = (not ref.exact, ref.size)
            if best is None or key < (not best.exact, best.size):
                best = ref
        return best

    def field(self, name: str) -> FieldRef:
        """Element by name (``"regs[15]"``, ``"neg.can_do_io"``); KeyError if unknown."""
        r = self._names().get(name)
        if r is None:
            raise KeyError(f"no EnvLayout field {name!r}")
        return r

    def has_field(self, name: str) -> bool:
        return name in self._names()

    def _names(self) -> dict[str, FieldRef]:
        nm = getattr(self, "_namemap", None)
        if nm is not None:
            return nm
        out: dict[str, FieldRef] = {}
        for f in self.fields:
            n = f.count if f.dims else 1
            if n > 4096:
                continue
            for i in range(n):
                nm = f.element_name(i)
                out.setdefault(nm, FieldRef(nm, f.region, f.off + i * f.elem, f.elem, 0, f.elem))
        object.__setattr__(self, "_namemap", out)
        return out

    def offset(self, name: str) -> int:
        return self.field(name).off

    def env_bounds(self) -> tuple[int, int]:
        """``[env - sizeof(CPUState), env + sizeof(CPUARMState))`` (design §7.5, V12)."""
        return -int(self.sizes["CPUState"]), int(self.sizes["CPUArchState"])

    def __hash__(self) -> int:
        return hash(self.envlayout_id)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, EnvLayout) and other.envlayout_id == self.envlayout_id

    def content_digest(self) -> str:
        return self.envlayout_id


def validate_doc(doc: Mapping[str, Any]) -> None:
    if doc.get("schema") != SCHEMA:
        raise EnvLayoutError(f"schema {doc.get('schema')!r} is not {SCHEMA}",
                             code="unknown_schema_version")
    errs = schemas.errors(dict(doc), "envlayout.v1")
    if errs:
        raise EnvLayoutError("; ".join(errs[:5]))
    want = content_id(doc, "envlayout_id")
    if doc.get("envlayout_id") != want:
        raise EnvLayoutError(f"envlayout_id {doc.get('envlayout_id')} != recomputed {want}",
                             code="envlayout_id_mismatch")


def read_envlayout(path_or_doc: str | os.PathLike | Mapping[str, Any]) -> EnvLayout:
    doc = path_or_doc if isinstance(path_or_doc, Mapping) else \
        json.loads(Path(path_or_doc).read_text(encoding="utf-8"))
    validate_doc(doc)
    lay = EnvLayout(doc["exe_sha256"], tuple(EnvField.from_json(f) for f in doc["fields"]),
                    dict(doc["sizes"]), int(doc["env_offset_in_cpu"]),
                    int(doc["neg_offset_in_cpustate"]), dict(doc.get("producer", {})),
                    doc["envlayout_id"])
    return lay


def _search_dirs() -> list[Path]:
    dirs = [Path(p) for p in os.environ.get("SCOPT_ENVLAYOUT_PATH", "").split(":") if p]
    try:
        dirs.append(Path(str(resources.files("scopt.formats").joinpath("envlayouts"))))
    except (ModuleNotFoundError, TypeError):  # pragma: no cover
        pass
    return dirs


def packaged_envlayouts() -> list[Path]:
    out = []
    for d in _search_dirs():
        if d.is_dir():
            out.extend(sorted(d.glob("*.json")))
    return out


def find_envlayout(exe_sha256: str | None) -> EnvLayout | None:
    """The layout bound to ``exe_sha256`` from the search path, or None."""
    if not exe_sha256 or not is_sha256_id(exe_sha256):
        return None
    hexd = exe_sha256.split(":", 1)[1]
    for d in _search_dirs():
        p = d / f"{hexd}.json"
        if p.is_file():
            lay = read_envlayout(p)
            if lay.exe_sha256 == exe_sha256:
                return lay
    return None


def check_binding(layout: EnvLayout | None, runtime: Mapping[str, Any] | None,
                  envlayout_id: str | None = None) -> str | None:
    """None when ``layout`` may be used for ``runtime``; else ``guard_layout_unbound``.

    ``envlayout_id`` (e.g. a RuntimeIdentity v2 ``admission.envlayout_id``),
    when given, must also equal the layout's id.
    """
    if layout is None or runtime is None:
        return "guard_layout_unbound"
    if runtime.get("qemu_sha256") != layout.exe_sha256:
        return "guard_layout_unbound"
    if envlayout_id is not None and envlayout_id != layout.envlayout_id:
        return "guard_layout_unbound"
    return None


def names(fields: Sequence[EnvField]) -> list[str]:  # pragma: no cover - debugging aid
    return [f.name for f in fields]

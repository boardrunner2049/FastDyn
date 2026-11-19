"""``helper-effects/1``: what each TCG helper may do to env, host memory and control flow.

TCG call flags are a contract about TCG *globals* only (``include/tcg/tcg.h``):
``TCG_CALL_NO_RWG`` (0x1) promises that the helper neither reads nor writes a TCG
global, ``TCG_CALL_NO_WG`` (0x2) that it writes none. Neither says anything about
the other env fields, about host memory outside env, or about faults. For example
``helper_rebuild_hflags_m32[_newel]`` is ``NO_RWG`` and assigns ``env->hflags``
(``target/arm/tcg/hflags.c:466,474``), which TB code and the B3 guards read. Treating
``NO_RWG`` as "no effect on env" (the pre-table model) therefore under-approximates.

This module replaces that rule with an explicit **per-helper effect table**, bound to
one QEMU executable exactly like the EnvLayout (``exe_sha256``), and fail-closed:

* a helper with a row gets the row's effect (exact, or an over-approximation that the
  row's ``src`` notes justify);
* a named helper without a row, or whose recorded TCG flags differ from the row's, gets
  :data:`FAIL_CLOSED`: it may read and write every env byte, read and write host memory,
  and fault or longjmp (sync class ``global`` = S4);
* when no table is bound to the layout at all, every helper is fail-closed.

Table document (``scopt/formats/helper_effects/<exe sha256 hex>.json``)::

    {"schema": "scopt.helper-effects/1", "exe_sha256": "sha256:…",
     "qemu_source": {...}, "rows": [ROW, …], "table_id": "sha256:…"}

    ROW = {"helper": "rebuild_hflags_m32_newel" | "<plugin>", "tcg_flags": 1 | null,
           "env_reads": ENVSET, "env_writes": ENVSET,
           "hostmem_reads": bool, "hostmem_writes": bool,
           "may_fault": bool, "returns": bool, "src": ["file:line …", …]}

    ENVSET = "all" | "none" | {"fields": [name, …]}
           | {"span": [first, last], "size": n}       # every byte from first's start to
                                                       # last's end (a struct, padding incl.)
           | {"all_except": [selector, …]}
    selector = an EnvLayout element name | "@globals" (the TCG globals of the ARM frontend)
             | "@can_do_io" | "@D1" | "@D2" | "@D3" (the obs-contract@1 classes)

``"<plugin>"`` is the row for unnamed helpers (FastDyn plugin callbacks; ``tcg_flags``
null = any). ``returns: false`` must agree with ``TCG_CALL_NO_RETURN``. ``env_writes``
is the returning path's write set; a fault path never returns into the TB, so what it
writes is observed only at the call's own sync event (``may_fault`` selects that event).

``table_id`` is the content id of the document without itself. Packaged tables live in
``scopt/formats/helper_effects/``; :func:`find_helper_effects` also searches the
directories in ``$SCOPT_HELPER_EFFECTS_PATH`` (``:``-separated, searched first).
:func:`register` binds an in-process table to a layout (synthetic layouts of the test
suites and mutation suites).

``qemu_source`` records the QEMU sources the rows were checked against: ``files`` (paths
relative to the QEMU tree), ``commit``, and ``file_sha256`` (``{path: "sha256:…"}`` of
each file's bytes). A table is bound to one executable, so a rebuilt QEMU starts with
every helper fail-closed. :func:`rebind_table` (``scopt helper-effects rebind``) copies a
table to another executable only when every recorded source file is byte-identical in
the new build's tree; otherwise the rows have to be reviewed again.

Consumers (each one fail-closed when the helper has no row):

* hostsym ``_call`` (``validate_sym/paths.py``): havoc exactly ``env_writes`` (keep the
  rest), bump the host-memory version iff ``hostmem_writes``, and compare ``env_reads``
  at the call event in addition to what the observation contract makes observable there;
* :func:`call_class` (effects, syncdead, callee summaries, B4 mutants): ``pure`` /
  ``raising`` only for a ``NO_RWG`` helper *with a row* (``raising`` iff ``may_fault``);
* syncdead: a call additionally needs every tracked byte in ``env_reads``;
* hconst (CP/BF) and exit_facts: a call keeps the global facts only when its TCG flags
  allow it *and* the row proves no TCG-global write.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from ..canon import content_id, is_sha256_id

__all__ = [
    "SCHEMA", "PLUGIN", "TCG_CALL_NO_RWG", "TCG_CALL_NO_WG", "TCG_CALL_NO_SE",
    "TCG_CALL_NO_RETURN", "EnvSet", "HelperEffect", "HelperEffectTable", "BoundEffects",
    "ResolvedEffect", "HelperEffectsError", "FAIL_CLOSED", "call_class", "read_helper_effects",
    "find_helper_effects", "packaged_helper_effects", "effects_for_layout",
    "effects_for_envlayout_id", "effects_for_hostops", "register", "unregister",
    "resolve_fail_closed", "SourceMismatch", "source_file_hashes", "recorded_source_hashes",
    "rebind_table", "helper_effects_text", "default_table_path",
]

SCHEMA = "scopt.helper-effects/1"
PLUGIN = "<plugin>"
LDST_SLOW = "<ldst_slow_path>"
LOOKUP = "lookup_tb_ptr"

TCG_CALL_NO_RWG = 0x0001
TCG_CALL_NO_WG = 0x0002
TCG_CALL_NO_SE = 0x0004
TCG_CALL_NO_RETURN = 0x0008

_GROUPS = ("@globals", "@can_do_io", "@D1", "@D2", "@D3")


class HelperEffectsError(ValueError):
    code = "helper_effects_invalid"


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EnvSet:
    """A set of env bytes, by field name (resolved against an EnvLayout)."""

    kind: str  # all | none | fields | span | all_except
    names: tuple[str, ...] = ()
    size: int | None = None

    @classmethod
    def from_json(cls, d: Any) -> "EnvSet":
        if d == "all":
            return cls("all")
        if d == "none":
            return cls("none")
        if isinstance(d, Mapping) and len(d) >= 1:
            if set(d) == {"fields"}:
                return cls("fields", tuple(str(x) for x in d["fields"]))
            if set(d) == {"span", "size"} and len(d["span"]) == 2:
                return cls("span", tuple(str(x) for x in d["span"]), int(d["size"]))
            if set(d) == {"all_except"}:
                return cls("all_except", tuple(str(x) for x in d["all_except"]))
        raise HelperEffectsError(f"bad env set {d!r}")

    def to_json(self) -> Any:
        if self.kind in ("all", "none"):
            return self.kind
        if self.kind == "span":
            return {"span": list(self.names), "size": self.size}
        return {self.kind: list(self.names)}


ALL = EnvSet("all")
NONE = EnvSet("none")


@dataclass(frozen=True)
class HelperEffect:
    helper: str
    tcg_flags: int | None
    env_reads: EnvSet
    env_writes: EnvSet
    hostmem_reads: bool
    hostmem_writes: bool
    may_fault: bool
    returns: bool
    src: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "HelperEffect":
        keys = {"helper", "tcg_flags", "env_reads", "env_writes", "hostmem_reads",
                "hostmem_writes", "may_fault", "returns", "src"}
        if set(d) != keys:
            raise HelperEffectsError(f"row keys {sorted(d)} != {sorted(keys)}")
        fl = d["tcg_flags"]
        return cls(str(d["helper"]), None if fl is None else int(fl),
                   EnvSet.from_json(d["env_reads"]), EnvSet.from_json(d["env_writes"]),
                   bool(d["hostmem_reads"]), bool(d["hostmem_writes"]), bool(d["may_fault"]),
                   bool(d["returns"]), tuple(str(s) for s in d["src"]))

    def to_json(self) -> dict[str, Any]:
        return {"helper": self.helper, "tcg_flags": self.tcg_flags,
                "env_reads": self.env_reads.to_json(), "env_writes": self.env_writes.to_json(),
                "hostmem_reads": self.hostmem_reads, "hostmem_writes": self.hostmem_writes,
                "may_fault": self.may_fault, "returns": self.returns, "src": list(self.src)}


#: the effect of a helper the table does not describe: anything (fail closed)
FAIL_CLOSED = HelperEffect("<fail-closed>", None, ALL, ALL, True, True, True, True,
                           ("no row in the helper effect table: fail closed",))


def _check_row(r: HelperEffect) -> None:
    fl = r.tcg_flags
    if r.helper == PLUGIN:
        if fl is not None:
            raise HelperEffectsError("the <plugin> row matches any flags (tcg_flags null)")
    elif fl is None:
        raise HelperEffectsError(f"{r.helper}: tcg_flags required")
    if fl is not None:
        if bool(fl & TCG_CALL_NO_RETURN) == r.returns:
            raise HelperEffectsError(f"{r.helper}: returns disagrees with TCG_CALL_NO_RETURN")
        if fl & (TCG_CALL_NO_RWG | TCG_CALL_NO_WG) and r.env_writes.kind == "all":
            raise HelperEffectsError(f"{r.helper}: NO_RWG/NO_WG helper cannot write all env")
        if fl & (TCG_CALL_NO_RWG | TCG_CALL_NO_WG) and r.env_writes.kind == "all_except" and \
                "@globals" not in r.env_writes.names:
            raise HelperEffectsError(f"{r.helper}: NO_RWG/NO_WG helper writes TCG globals")
    if not r.src:
        raise HelperEffectsError(f"{r.helper}: a row cites its source (src)")


# ---------------------------------------------------------------------------
# table
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HelperEffectTable:
    exe_sha256: str
    rows: tuple[HelperEffect, ...]
    qemu_source: Mapping[str, Any] = field(default_factory=dict)
    table_id: str = ""

    def to_json(self) -> dict[str, Any]:
        doc = {"schema": SCHEMA, "exe_sha256": self.exe_sha256,
               "qemu_source": dict(self.qemu_source),
               "rows": [r.to_json() for r in sorted(self.rows, key=lambda r: r.helper)]}
        doc["table_id"] = content_id(doc, "table_id")
        return doc

    @classmethod
    def build(cls, exe_sha256: str, rows: Iterable[HelperEffect],
              qemu_source: Mapping[str, Any] | None = None) -> "HelperEffectTable":
        rs = tuple(sorted(rows, key=lambda r: r.helper))
        names = [r.helper for r in rs]
        if len(set(names)) != len(names):
            raise HelperEffectsError("duplicate helper rows")
        for r in rs:
            _check_row(r)
        t = cls(exe_sha256, rs, dict(qemu_source or {}))
        return cls(t.exe_sha256, t.rows, t.qemu_source, t.to_json()["table_id"])

    def row(self, name: str | None, flags: int | None) -> HelperEffect | None:
        """The row of helper ``name`` (None = an unnamed plugin callback) recorded with
        TCG ``flags``, or None (no row, or the flags disagree with the row: fail closed)."""
        m = getattr(self, "_by_name", None)
        if m is None:
            m = {r.helper: r for r in self.rows}
            object.__setattr__(self, "_by_name", m)
        r = m.get(PLUGIN if name is None else name)
        if r is None:
            return None
        if r.tcg_flags is not None and r.tcg_flags != (0 if flags is None else flags):
            return None
        return r

    def content_digest(self) -> str:
        return self.table_id

    def __hash__(self) -> int:
        return hash(self.table_id)


def read_helper_effects(path_or_doc: str | os.PathLike | Mapping[str, Any]
                        ) -> HelperEffectTable:
    doc = path_or_doc if isinstance(path_or_doc, Mapping) else \
        json.loads(Path(path_or_doc).read_text(encoding="utf-8"))
    if doc.get("schema") != SCHEMA:
        raise HelperEffectsError(f"schema {doc.get('schema')!r} is not {SCHEMA}")
    if not is_sha256_id(doc.get("exe_sha256")):
        raise HelperEffectsError("exe_sha256 is not a sha256 id")
    want = content_id(doc, "table_id")
    if doc.get("table_id") != want:
        raise HelperEffectsError(f"table_id {doc.get('table_id')} != recomputed {want}")
    t = HelperEffectTable.build(doc["exe_sha256"],
                                [HelperEffect.from_json(r) for r in doc["rows"]],
                                doc.get("qemu_source", {}))
    if t.table_id != want:
        raise HelperEffectsError("rows are not in canonical order")
    return t


def _search_dirs() -> list[Path]:
    dirs = [Path(p) for p in os.environ.get("SCOPT_HELPER_EFFECTS_PATH", "").split(":") if p]
    try:
        dirs.append(Path(str(resources.files("scopt.formats").joinpath("helper_effects"))))
    except (ModuleNotFoundError, TypeError):  # pragma: no cover
        pass
    return dirs


def packaged_helper_effects() -> list[Path]:
    out = []
    for d in _search_dirs():
        if d.is_dir():
            out.extend(sorted(d.glob("*.json")))
    return out


_REGISTERED: dict[str, "BoundEffects"] = {}  # exe_sha256 or envlayout_id -> bound table
_FOUND: dict[str, HelperEffectTable | None] = {}


def find_helper_effects(exe_sha256: str | None, *, refresh: bool = False) -> HelperEffectTable | None:
    """The table bound to ``exe_sha256`` (registered, then the search path), or None."""
    if not exe_sha256:
        return None
    b = _REGISTERED.get(exe_sha256)
    if b is not None:
        return b.table
    if not is_sha256_id(exe_sha256):
        return None
    if not refresh and exe_sha256 in _FOUND:
        return _FOUND[exe_sha256]
    hexd = exe_sha256.split(":", 1)[1]
    out = None
    for d in _search_dirs():
        p = d / f"{hexd}.json"
        if p.is_file():
            t = read_helper_effects(p)
            if t.exe_sha256 == exe_sha256:
                out = t
                break
    _FOUND[exe_sha256] = out
    return out


# ---------------------------------------------------------------------------
# binding to a layout (byte sets)
# ---------------------------------------------------------------------------

def _field_bytes(layout: Any, name: str) -> frozenset[int] | None:
    if layout is None or not layout.has_field(name):
        return None
    f = layout.field(name)
    return frozenset(range(f.off, f.off + f.size))


def _group_names(g: str) -> tuple[str, ...]:
    from .hostops import TCG_GLOBALS
    from .obs_contract import D1_FIELDS, D2_FIELDS, D3_FIELDS

    return {"@globals": tuple(sorted(TCG_GLOBALS)), "@can_do_io": ("neg.can_do_io",),
            "@D1": D1_FIELDS, "@D2": D2_FIELDS, "@D3": D3_FIELDS}[g]


def _selector_bytes(layout: Any, sel: str, *, strict: bool) -> frozenset[int] | None:
    """Bytes of a selector; a missing name is None when ``strict`` (cannot bound a write
    set), else skipped (an exception list that cannot exclude a field keeps it in)."""
    names = _group_names(sel) if sel in _GROUPS else (sel,)
    out: set[int] = set()
    for n in names:
        b = _field_bytes(layout, n)
        if b is None:
            if strict and sel not in _GROUPS:
                return None
            continue
        out |= b
    return frozenset(out)


@dataclass(frozen=True)
class _Pred:
    """A byte predicate: ``all`` (every byte), or membership in ``bytes`` (``neg=False``)
    or outside it (``neg=True``)."""

    all: bool = False
    bytes: frozenset[int] = frozenset()
    neg: bool = False

    def __call__(self, b: int) -> bool:
        if self.all:
            return True
        return (b not in self.bytes) if self.neg else (b in self.bytes)

    @property
    def empty(self) -> bool:
        return not self.all and not self.neg and not self.bytes


_P_ALL = _Pred(all=True)
_P_NONE = _Pred()


def _resolve_set(s: EnvSet, layout: Any) -> _Pred | None:
    """None when the set cannot be resolved against ``layout`` (the row is then unusable)."""
    if s.kind == "all":
        return _P_ALL
    if s.kind == "none":
        return _P_NONE
    if s.kind == "fields":
        out: set[int] = set()
        for n in s.names:
            b = _selector_bytes(layout, n, strict=True)
            if b is None:
                return None
            out |= b
        return _Pred(bytes=frozenset(out))
    if s.kind == "span":
        a = _field_bytes(layout, s.names[0])
        z = _field_bytes(layout, s.names[1])
        if a is None or z is None:
            return None
        lo, hi = min(a), max(z) + 1
        if hi - lo != s.size:
            return None  # the layout disagrees with the struct size the row was checked for
        return _Pred(bytes=frozenset(range(lo, hi)))
    if s.kind == "all_except":
        out = set()
        for n in s.names:
            b = _selector_bytes(layout, n, strict=False)
            out |= b or frozenset()
        return _Pred(bytes=frozenset(out), neg=True)
    raise HelperEffectsError(s.kind)  # pragma: no cover


@dataclass(frozen=True)
class ResolvedEffect:
    """One helper's effect as byte predicates over env offsets."""

    effect: HelperEffect
    reads: _Pred
    writes: _Pred
    writes_globals: bool
    rowed: bool

    @property
    def keep(self) -> Callable[[int], bool] | None:
        """The ``ByteMem.havoc`` keep predicate (None = havoc every byte)."""
        w = self.writes
        if w.all:
            return None
        if w.neg:
            ex = w.bytes
            return lambda b, ex=ex: b in ex
        ws = w.bytes
        return lambda b, ws=ws: b not in ws

    @property
    def writes_env(self) -> bool:
        return not self.writes.empty

    @property
    def reads_all(self) -> bool:
        return self.reads.all


def _resolve(e: HelperEffect, layout: Any, rowed: bool) -> ResolvedEffect:
    r = _resolve_set(e.env_reads, layout)
    w = _resolve_set(e.env_writes, layout)
    if r is None or w is None:
        e, r, w, rowed = FAIL_CLOSED, _P_ALL, _P_ALL, False
    g = _selector_bytes(layout, "@globals", strict=False) or frozenset()
    if w.all:
        wg = True
    elif w.neg:
        wg = not g <= w.bytes
    else:
        wg = bool(g & w.bytes)
    return ResolvedEffect(e, r, w, wg, rowed)


def resolve_fail_closed(layout: Any = None) -> ResolvedEffect:
    return _resolve(FAIL_CLOSED, layout, False)


class BoundEffects:
    """A table (or none: every helper fail-closed) resolved against one layout."""

    def __init__(self, table: HelperEffectTable | None, layout: Any):
        self.table = table
        self.layout = layout
        self._cache: dict[tuple[str | None, int | None], ResolvedEffect] = {}

    @property
    def table_id(self) -> str | None:
        return None if self.table is None else self.table.table_id

    def row(self, name: str | None, flags: int | None) -> HelperEffect | None:
        if self.table is None or name in (LDST_SLOW,):
            return None
        return self.table.row(name, flags)

    def resolved(self, name: str | None, flags: int | None) -> ResolvedEffect:
        k = (name, flags)
        r = self._cache.get(k)
        if r is None:
            row = self.row(name, flags)
            r = _resolve(row, self.layout, True) if row is not None else \
                _resolve(FAIL_CLOSED, self.layout, False)
            if r.rowed and r.writes_globals and row.tcg_flags is not None and \
                    row.tcg_flags & (TCG_CALL_NO_RWG | TCG_CALL_NO_WG):
                # A NO_RWG/NO_WG row whose resolved write set covers a TCG global
                # contradicts its own flags (_check_row only sees "all"/"all_except"
                # without a layout): treat the call as unknown (fail closed).
                r = _resolve(FAIL_CLOSED, self.layout, False)
            self._cache[k] = r
        return r

    def call_class(self, name: str | None, flags: int | None) -> str:
        return call_class(name, flags, self)

    def keeps_globals(self, name: str | None, flags: int | None) -> bool:
        """True when the call provably writes no TCG global (row present, no global byte
        in its write set). The TCG flags are checked by the callers that rely on them."""
        r = self.resolved(name, flags)
        return r.rowed and not r.writes_globals


def call_class(name: str | None, flags: int | None, fx: BoundEffects | None = None) -> str:
    """The sync class of a call (``effects@1`` doc).

    ``ldst_slow``, ``plugin``, ``lookup`` and ``noreturn`` are structural (the call site,
    not the helper body, decides them). ``pure`` / ``raising`` need a ``NO_RWG`` helper
    **with a row** in the bound effect table (``raising`` iff the row may fault); a named
    helper without a row is ``global`` (S4: fail closed), whatever its flags.
    """
    if name == LDST_SLOW:
        return "ldst_slow"
    if name is None:
        return "plugin"
    if name == LOOKUP:
        return "lookup"
    f = 0 if flags is None else flags
    if f & TCG_CALL_NO_RETURN:
        return "noreturn"
    if f & TCG_CALL_NO_RWG:
        if fx is None or fx.row(name, flags) is None:
            return "global"
        r = fx.resolved(name, flags)
        if not r.rowed:  # the row contradicts its flags or the layout: fail closed
            return "global"
        return "raising" if r.effect.may_fault else "pure"
    return "global"


# ---------------------------------------------------------------------------
# lookup by layout / hostops
# ---------------------------------------------------------------------------

_BY_LAYOUT: dict[str, BoundEffects] = {}
_BY_LAYOUT_ID: dict[str, BoundEffects | None] = {}


def register(table: HelperEffectTable, layout: Any) -> BoundEffects:
    """Bind ``table`` to ``layout`` in this process (``layout.exe_sha256`` must match)."""
    if getattr(layout, "exe_sha256", None) != table.exe_sha256:
        raise HelperEffectsError("table and layout are bound to different executables")
    b = BoundEffects(table, layout)
    _REGISTERED[table.exe_sha256] = b
    lid = getattr(layout, "envlayout_id", None)
    if lid:
        _REGISTERED[lid] = b
        _BY_LAYOUT_ID.pop(lid, None)
    _BY_LAYOUT.clear()
    return b


def unregister(exe_sha256: str) -> None:
    b = _REGISTERED.pop(exe_sha256, None)
    if b is not None:
        lid = getattr(b.layout, "envlayout_id", None)
        if lid:
            _REGISTERED.pop(lid, None)
            _BY_LAYOUT_ID.pop(lid, None)
    _BY_LAYOUT.clear()


def _layout_key(layout: Any) -> str:
    return f"{getattr(layout, 'exe_sha256', None)}|{getattr(layout, 'envlayout_id', None)}" \
        f"|{id(layout) if getattr(layout, 'envlayout_id', None) is None else ''}"


def effects_for_layout(layout: Any) -> BoundEffects:
    """The effect table bound to ``layout``'s executable, resolved against ``layout``; with
    no table (or no layout) every helper is fail-closed."""
    k = _layout_key(layout)
    b = _BY_LAYOUT.get(k)
    if b is None:
        exe = getattr(layout, "exe_sha256", None)
        reg = _REGISTERED.get(exe) if exe else None
        if reg is not None and (reg.layout is layout or getattr(
                reg.layout, "envlayout_id", None) == getattr(layout, "envlayout_id", 0)):
            b = reg
        else:
            b = BoundEffects(find_helper_effects(exe) if exe else None, layout)
        if len(_BY_LAYOUT) > 64:
            _BY_LAYOUT.clear()
        _BY_LAYOUT[k] = b
        lid = getattr(layout, "envlayout_id", None)
        if lid and b.table is not None:
            # HostOps records only the layout id: let its consumers find this binding
            _BY_LAYOUT_ID.setdefault(lid, b)
    return b


def refresh_effects_for_layout(layout: Any) -> BoundEffects:
    """Refresh file-backed identity before creating a generation-bound plan."""
    exe = getattr(layout, "exe_sha256", None)
    if exe is not None and exe not in _REGISTERED:
        previous = _FOUND.get(exe)
        current = find_helper_effects(exe, refresh=True)
        if previous != current:
            _BY_LAYOUT.pop(_layout_key(layout), None)
            _BY_LAYOUT_ID.pop(getattr(layout, "envlayout_id", None), None)
    return effects_for_layout(layout)


def effects_for_envlayout_id(envlayout_id: str | None) -> BoundEffects:
    """The bound table of the layout with id ``envlayout_id`` (the id HostOps records):
    a registered layout, else a packaged EnvLayout whose executable has a table."""
    if not envlayout_id:
        return BoundEffects(None, None)
    reg = _REGISTERED.get(envlayout_id)
    if reg is not None:
        return reg
    if envlayout_id in _BY_LAYOUT_ID:
        b = _BY_LAYOUT_ID[envlayout_id]
        return b if b is not None else BoundEffects(None, None)
    from ..formats.envlayout_v1 import packaged_envlayouts, read_envlayout

    found: BoundEffects | None = None
    for p in packaged_envlayouts():
        try:
            lay = read_envlayout(p)
        except ValueError:
            continue
        if lay.envlayout_id == envlayout_id:
            found = effects_for_layout(lay)
            break
    _BY_LAYOUT_ID[envlayout_id] = found
    return found if found is not None else BoundEffects(None, None)


def effects_for_hostops(h: Any) -> BoundEffects:
    """The bound table of the layout a UnitHostOps was built with (``h.envlayout_id``)."""
    return effects_for_envlayout_id(getattr(h, "envlayout_id", None))


# ---------------------------------------------------------------------------
# rebinding a table to another build of the same sources
# ---------------------------------------------------------------------------

class SourceMismatch(HelperEffectsError):
    """The helper sources of the new build differ from the ones the table records."""

    code = "helper_sources_differ"

    def __init__(self, differing: Mapping[str, tuple[str | None, str | None]]):
        self.differing = dict(differing)
        lines = [f"  {p}: table {w or 'unknown'}, tree {h or 'missing'}"
                 for p, (w, h) in sorted(self.differing.items())]
        super().__init__("helper source files differ from the table's qemu_source:\n"
                         + "\n".join(lines))


def _sha256_bytes(b: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(b).hexdigest()


def source_file_hashes(qemu_src: str | os.PathLike, files: Iterable[str]
                       ) -> dict[str, str | None]:
    """``{path: sha256 id}`` of ``files`` under ``qemu_src`` (None for a missing file)."""
    root = Path(qemu_src)
    out: dict[str, str | None] = {}
    for f in files:
        p = root / f
        out[f] = _sha256_bytes(p.read_bytes()) if p.is_file() else None
    return out


def _git_blob_hashes(repo: Path, commit: str, files: Iterable[str]
                     ) -> dict[str, str | None] | None:
    import subprocess

    try:
        subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"],
                       check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    out: dict[str, str | None] = {}
    for f in files:
        r = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{f}"],
                           capture_output=True)
        out[f] = _sha256_bytes(r.stdout) if r.returncode == 0 else None
    return out


def recorded_source_hashes(table: HelperEffectTable,
                           qemu_src: str | os.PathLike | None = None) -> dict[str, str | None]:
    """The source hashes the table was checked against: ``qemu_source.file_sha256``, or,
    for a table without hashes, the files of ``qemu_source.commit`` read with git from
    ``qemu_src`` (which must contain that commit)."""
    src = table.qemu_source or {}
    files = [str(f) for f in src.get("files") or ()]
    if not files:
        raise HelperEffectsError("the table's qemu_source lists no source files")
    rec = src.get("file_sha256")
    if isinstance(rec, Mapping):
        return {f: (str(rec[f]) if rec.get(f) else None) for f in files}
    commit = src.get("commit")
    if commit and qemu_src is not None:
        got = _git_blob_hashes(Path(qemu_src), str(commit), files)
        if got is not None:
            return got
    raise HelperEffectsError(
        "the table records no file_sha256 and its commit "
        f"{commit or '(none)'} is not available in {qemu_src}: cannot check the sources")


def rebind_table(table: HelperEffectTable, new_exe_sha256: str,
                 qemu_src: str | os.PathLike) -> HelperEffectTable:
    """``table``'s rows bound to ``new_exe_sha256``, after checking that every source file
    in ``table.qemu_source`` is byte-identical under ``qemu_src``. Raises
    :class:`SourceMismatch` (with the differing files) otherwise. The new table records
    the checked hashes (``file_sha256``) and ``rebound_from``; ``table_id`` is recomputed."""
    if not is_sha256_id(new_exe_sha256):
        raise HelperEffectsError(f"{new_exe_sha256!r} is not a sha256 id")
    want = recorded_source_hashes(table, qemu_src)
    have = source_file_hashes(qemu_src, want)
    diff = {f: (want[f], have[f]) for f in want if want[f] is None or want[f] != have[f]}
    if diff:
        raise SourceMismatch(diff)
    src = dict(table.qemu_source)
    src["file_sha256"] = dict(sorted(have.items()))
    src["rebound_from"] = table.exe_sha256
    return HelperEffectTable.build(new_exe_sha256, table.rows, src)


def helper_effects_text(table: HelperEffectTable) -> str:
    """The table document as written in ``scopt/formats/helper_effects/``."""
    return json.dumps(table.to_json(), indent=1, sort_keys=True) + "\n"


def default_table_path(exe_sha256: str) -> Path | None:
    """``<first $SCOPT_HELPER_EFFECTS_PATH dir>/<exe hex>.json``, or None when unset."""
    dirs = [p for p in os.environ.get("SCOPT_HELPER_EFFECTS_PATH", "").split(":") if p]
    if not dirs:
        return None
    return Path(dirs[0]) / f"{exe_sha256.split(':', 1)[-1]}.json"

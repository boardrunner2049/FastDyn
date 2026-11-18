"""``patch-join@1``: provenance of a return-edge fact (B3/B4 design §3.2 "Provenance"). WP20.

A proposed fact ``env[field] == value`` (``exit-facts@1``) has one or more
defining host stores, each inside guest instruction ``def_insn`` of R. Its
provenance is the **last writer** of ``field`` at or before that instruction,
found by walking R's guest instructions backwards. At each instruction ``j``
the guest instruction itself runs *after* the FastDyn modifiers registered at
its vaddr (core.c ``vcpu_tb_trans`` injects modifiers at the instruction
start), so it is examined first:

* guest instruction ``j`` writes ``field``:
    - an unconditional immediate move (:func:`scopt.decode.thumb_sem.imm_defs`)
      with the same value -> ``source = guest_code``;
    - an immediate move with another value -> ``patch_join_mismatch``;
    - anything else (computed, conditional) -> ``provenance_unexplained``;
* a modifier ``rN <- imm`` at ``vaddr(j)`` targets ``field``: equal value ->
  ``source = patch`` (with the plan entry); other value ->
  ``patch_join_mismatch``; ``rN <- rM`` -> ``provenance_unexplained``;
  ``[rN] <- imm`` (FastDyn writes rN instead of memory, map-fastdyn-patching
  §3 caveat; a contract the docs do not state) -> ``unsupported_contract_fact_source``;
* no writer in R -> ``provenance_unexplained``.

Whole-plan conditions (fail closed, every fact of the bundle):

* the image is not the original one (Student 1 output) and no
  ``patch_bindings`` / origin map is supported here -> ``no_patch_bindings``;
* the plan has a fuzzing schema (its gated modifiers, core.c
  ``core_register_gated_modifier``, are not in ``modifiers.txt``) ->
  ``unsupported_contract_fact_source``;
* the modifiers content is not available (non-empty in the manifest but no
  verified file given) -> ``provenance_unexplained`` (detail
  ``patch_plan_unavailable``); no guest bytes -> ``provenance_unexplained``
  (detail ``guest_bytes_unavailable``).

The Tier-1 PLUGIN_CBS cross-check (``ops_bytes_mismatch``) needs WP23, which
is not approved; it is reported ``not_run``.

The patch plan (:class:`PatchPlan`) is the content hashed into
``patch_plan_hash`` (bundle_v1: ``virtuals``, ``modifiers``, ``finline``,
``fuzzing_schema``, ``preprocessing_artifact_*``): each file given is checked
against the manifest's content sha256, and the manifest's plan hash against
the bundle's.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..decode.thumb_sem import GInsn, imm_defs

__all__ = ["PlanEntry", "PatchPlan", "PatchPlanError", "Provenance", "join_def", "join_fact",
           "EMPTY_SHA256", "insn_vaddrs", "plan_condition"]

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_ASSIGN = re.compile(r"^\s*(\S+)\s+(\S+)\s*(<-|:=|->|=)\s*(\S+)\s*$")
_REG = re.compile(r"^[rx](\d+)$")


class PatchPlanError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True, slots=True)
class PlanEntry:
    kind: str  # "modifier" | "virtual"
    index: int  # line index in its file
    vaddr: int
    text: str  # the line
    form: str  # modifier: reg_imm | reg_reg | deref | mem | pc | other; virtual: the name
    field: str | None = None  # guest register name targeted by a register modifier
    imm: int | None = None

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind, "index": self.index,
                             "vaddr": f"0x{self.vaddr:x}", "text": self.text, "form": self.form}
        if self.field is not None:
            d["field"] = self.field
        if self.imm is not None:
            d["value"] = f"0x{self.imm:x}"
        return d


def _int(s: str) -> int | None:
    try:
        return int(s, 0)
    except ValueError:
        return None


def parse_modifier(line: str, index: int) -> PlanEntry | None:
    m = _ASSIGN.match(line)
    if not m:
        return None
    addr = _int(m.group(1))
    if addr is None:
        return None
    tgt, val = m.group(2).lower(), m.group(4).lower()
    imm = _int(val)
    form, fld = "other", None
    rm = _REG.match(tgt)
    if tgt.startswith("["):
        form = "deref"
        inner = _REG.match(tgt.strip("[]"))
        fld = f"r{int(inner.group(1))}" if inner else None
    elif rm:
        n = int(rm.group(1))
        if n == 15:
            form = "pc"
        else:
            fld = {13: "sp", 14: "lr"}.get(n, f"r{n}")
            form = "reg_imm" if imm is not None else "reg_reg"
    elif tgt in ("pc", "rip", "riscv_pc", "pc32"):
        form = "pc"
    elif _int(tgt) is not None:
        form = "mem"
    return PlanEntry("modifier", index, addr, line.strip(), form, fld,
                     None if imm is None else imm & 0xffffffff)


def parse_virtual(line: str, index: int) -> PlanEntry | None:
    parts = line.split()
    if len(parts) < 2:
        return None
    addr = _int(parts[0])
    if addr is None:
        return None
    return PlanEntry("virtual", index, addr, line.strip(), parts[1])


@dataclass(frozen=True)
class PatchPlan:
    """The patch-plan content of one bundle (or what is known of it)."""

    patch_plan_hash: str | None
    modifiers_status: str  # absent | empty | verified | unavailable
    virtuals_status: str
    entries: tuple[PlanEntry, ...] = ()
    fuzzing_schema: bool = False
    finline: str | None = None
    preprocessing: tuple[str, ...] = ()
    sources: Mapping[str, str] = field(default_factory=dict)

    @property
    def modifiers_known(self) -> bool:
        return self.modifiers_status in ("absent", "empty", "verified")

    def at(self, vaddr: int, kind: str | None = None) -> list[PlanEntry]:
        return [e for e in self.entries if e.vaddr == vaddr and (kind is None or e.kind == kind)]

    def in_range(self, lo: int, hi: int) -> list[PlanEntry]:
        return [e for e in self.entries if lo <= e.vaddr < hi]

    def to_json(self) -> dict[str, Any]:
        return {"patch_plan_hash": self.patch_plan_hash,
                "modifiers": self.modifiers_status, "virtuals": self.virtuals_status,
                "fuzzing_schema": self.fuzzing_schema, "finline": self.finline,
                "preprocessing": list(self.preprocessing),
                "entries": [e.to_json() for e in self.entries],
                "sources": dict(self.sources)}

    # -- construction ------------------------------------------------------------------
    @classmethod
    def unknown(cls, patch_plan_hash: str | None = None) -> "PatchPlan":
        return cls(patch_plan_hash, "unavailable", "unavailable")

    @classmethod
    def load(cls, manifest: Mapping[str, Any] | str | Path, *,
             bundle: Mapping[str, Any] | None = None,
             modifiers: str | Path | None = None, virtuals: str | Path | None = None,
             ) -> "PatchPlan":
        """Build the plan from a FastDyn manifest and the generated ``modifiers.txt`` /
        ``virtuals.txt``. Every file given must hash to the manifest's content sha256
        (``patch_plan_content_mismatch``); the manifest's plan hash must equal the
        bundle's (``patch_plan_hash_mismatch``)."""
        from ..formats.bundle_v1 import manifest_patch_plan_hash

        man = manifest if isinstance(manifest, Mapping) else json.loads(Path(manifest).read_text())
        cf = man.get("inputs", {}).get("content_files", {}) or {}
        pph = manifest_patch_plan_hash(man)
        if bundle is not None and bundle.get("patch_plan_hash") != pph:
            raise PatchPlanError("patch_plan_hash_mismatch",
                                 f"manifest {pph} vs bundle {bundle.get('patch_plan_hash')}")
        entries: list[PlanEntry] = []
        sources: dict[str, str] = {}

        def one(key: str, path: str | Path | None, parse) -> str:
            ent = cf.get(key)
            if ent is None:
                return "absent"
            want = ent.get("sha256") if isinstance(ent, Mapping) else None
            if want in (None, "missing"):
                return "unavailable"
            want = want.split(":", 1)[-1]
            if path is None:
                return "empty" if want == EMPTY_SHA256 else "unavailable"
            data = Path(path).read_bytes()
            got = hashlib.sha256(data).hexdigest()
            if got != want:
                raise PatchPlanError("patch_plan_content_mismatch",
                                     f"{key}: {path} sha256 {got} != manifest {want}")
            sources[key] = str(path)
            for i, line in enumerate(data.decode("utf-8", "replace").splitlines()):
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                e = parse(line, i)
                if e is None:
                    raise PatchPlanError("patch_plan_parse", f"{key}:{i + 1}: {line!r}")
                entries.append(e)
            return "empty" if not data.strip() else "verified"

        ms = one("modifiers", modifiers, parse_modifier)
        vs = one("virtuals", virtuals, parse_virtual)
        fin = cf.get("finline")
        return cls(pph, ms, vs, tuple(entries), cf.get("fuzzing_schema") is not None,
                   None if fin is None else str(fin.get("sha256") if isinstance(fin, Mapping)
                                                else fin),
                   tuple(sorted(k for k in cf if k.startswith("preprocessing_artifact_"))),
                   sources)

    @classmethod
    def from_prep(cls, prep_dir: str | Path, *, manifest: Mapping[str, Any] | str | Path,
                  bundle: Mapping[str, Any] | None = None) -> "PatchPlan":
        """Plan from a ``scopt prepare`` directory (``work/virtuals/*.txt``)."""
        d = Path(prep_dir) / "work" / "virtuals"
        m = d / "modifiers.txt"
        v = d / "virtuals.txt"
        return cls.load(manifest, bundle=bundle, modifiers=m if m.is_file() else None,
                        virtuals=v if v.is_file() else None)


# ---------------------------------------------------------------------------
# join
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Provenance:
    source: str | None  # "patch" | "guest_code" | None
    reason: str | None  # None when explained
    detail: str = ""
    writer_insn: int | None = None
    writer_vaddr: int | None = None
    plan_entry: PlanEntry | None = None

    @property
    def ok(self) -> bool:
        return self.reason is None

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"source": self.source, "reason": self.reason}
        if self.detail:
            d["detail"] = self.detail
        if self.writer_insn is not None:
            d["writer_insn"] = self.writer_insn
        if self.writer_vaddr is not None:
            d["writer_vaddr"] = f"0x{self.writer_vaddr:x}"
        if self.plan_entry is not None:
            d["plan_entry"] = self.plan_entry.to_json()
        return d


def insn_vaddrs(unit: Any, page_bits: int = 10) -> list[int] | None:
    """Guest vaddr of every insn of ``unit`` from its insn_start word0 (PCREL page offsets)."""
    from ..decode.thumb import lengths_from_words

    if not unit.vaddrs or not unit.insns:
        return None
    rows = sorted(unit.insns, key=lambda r: r.index)
    lens = lengths_from_words([r.words[0] for r in rows], unit.guest_size, page_bits)
    out, v = [], unit.vaddrs[0]
    for n in lens:
        out.append(v)
        v += n
    return out


def plan_condition(plan: PatchPlan, bundle: Mapping[str, Any] | None) -> tuple[str, str] | None:
    """A reason that refuses every fact of this bundle, or None."""
    if bundle is not None and bundle.get("image_hash") != bundle.get("original_image_hash"):
        return "no_patch_bindings", "image differs from the original image; patch_bindings " \
            "/ origin map join is not supported"
    if plan.fuzzing_schema:
        return "unsupported_contract_fact_source", "fuzzing schema present: gated modifiers " \
            "are not in modifiers.txt"
    return None


def join_def(field: str, value: int, def_insn: int, *, ginsns: Sequence[GInsn] | None,
             vaddrs: Sequence[int] | None, plan: PatchPlan) -> Provenance:
    """Provenance of one defining store (see the module docstring)."""
    if vaddrs is None or def_insn is None or def_insn >= len(vaddrs):
        return Provenance(None, "provenance_unexplained", "no insn vaddrs")
    if not plan.modifiers_known:
        return Provenance(None, "provenance_unexplained", "patch_plan_unavailable")
    for j in range(def_insn, -1, -1):
        # 1. the guest instruction (runs after the modifiers at its vaddr)
        if ginsns is None:
            # a modifier at the defining insn is still checkable only if no guest
            # insn could follow it: fail closed without the bytes
            return Provenance(None, "provenance_unexplained", "guest_bytes_unavailable")
        g = ginsns[j]
        if field in g.writes:
            d = imm_defs(g)
            if d is not None and field in d:
                v = d[field]
                if v is None:
                    return Provenance(None, "provenance_unexplained",
                                      f"{g.text}: {field} is not a literal", j, g.vaddr)
                if v == value:
                    return Provenance("guest_code", None, g.text, j, g.vaddr)
                return Provenance(None, "patch_join_mismatch",
                                  f"{g.text} writes {field}=0x{v:x}, store has 0x{value:x}",
                                  j, g.vaddr)
            return Provenance(None, "provenance_unexplained",
                              f"{g.text} writes {field} (not an unconditional immediate)", j,
                              g.vaddr)
        # 2. FastDyn modifiers at this vaddr (last registered last)
        mods = [e for e in plan.at(vaddrs[j], "modifier") if e.field == field]
        if mods:
            e = mods[-1]
            if e.form == "deref":
                return Provenance(None, "unsupported_contract_fact_source",
                                  f"modifier {e.text!r} writes {field} (deref form)", j,
                                  vaddrs[j], e)
            if e.form == "reg_imm":
                if e.imm == value:
                    return Provenance("patch", None, e.text, j, vaddrs[j], e)
                return Provenance(None, "patch_join_mismatch",
                                  f"modifier {e.text!r} vs store 0x{value:x}", j, vaddrs[j], e)
            return Provenance(None, "provenance_unexplained", f"modifier {e.text!r}", j,
                              vaddrs[j], e)
    return Provenance(None, "provenance_unexplained", f"no writer of {field} in the unit")


def join_fact(field: str, value: int, defs: Sequence[tuple[int, int | None]], *,
              ginsns: Sequence[GInsn] | None, vaddrs: Sequence[int] | None, plan: PatchPlan,
              bundle: Mapping[str, Any] | None = None) -> tuple[Provenance, list[Provenance]]:
    """Provenance of a fact with defining stores ``defs`` = ``[(host_off, insn)]``.

    Returns ``(overall, per_def)``: overall is explained only when every def is,
    and its ``source`` is ``patch`` if any def comes from a modifier.
    """
    cond = plan_condition(plan, bundle)
    if cond is not None:
        p = Provenance(None, cond[0], cond[1])
        return p, [p for _ in defs]
    per = [join_def(field, value, insn, ginsns=ginsns, vaddrs=vaddrs, plan=plan)
           if insn is not None else Provenance(None, "provenance_unexplained", "store outside an insn")
           for _off, insn in defs]
    bad = next((p for p in per if not p.ok), None)
    if bad is not None:
        return bad, per
    src = "patch" if any(p.source == "patch" for p in per) else "guest_code"
    first = next(p for p in per if p.source == src)
    return Provenance(src, None, first.detail, first.writer_insn, first.writer_vaddr,
                      first.plan_entry), per


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UnitProvenance:
    """``patch-join@1`` value: per lookup-tail exit (by tail host offset), per fact field."""

    uid: str
    plan_hash: str | None
    rows: tuple[tuple[int, str, Provenance], ...]  # (tail_off, field, overall provenance)

    def of(self, tail_off: int, field: str) -> Provenance | None:
        return next((p for t, f, p in self.rows if t == tail_off and f == field), None)

    def to_json(self) -> dict[str, Any]:
        return {"uid": self.uid, "plan_hash": self.plan_hash,
                "rows": [{"tail_off": t, "field": f, **p.to_json()} for t, f, p in self.rows]}

    def content_digest(self) -> str:
        return "sha256:" + hashlib.sha256(json.dumps(self.to_json(), sort_keys=True)
                                          .encode()).hexdigest()


def unit_provenance(u: Any, facts: Any, *, guest: Any, plan: PatchPlan,
                    bundle: Mapping[str, Any] | None, page_bits: int = 10) -> UnitProvenance:
    from ..decode.guest_symbols import unit_guest_bytes
    from ..decode.thumb_sem import decode_unit

    vaddrs = insn_vaddrs(u, page_bits)
    ginsns = decode_unit(u, unit_guest_bytes(u, guest), page_bits)
    rows = []
    for es in getattr(facts, "exits", ()):
        for f in es.facts:
            p, _ = join_fact(f.field, f.value, f.defs, ginsns=ginsns, vaddrs=vaddrs, plan=plan,
                             bundle=bundle)
            rows.append((es.tail.off, f.field, p))
    return UnitProvenance(u.uid, plan.patch_plan_hash, tuple(rows))


def _compute(ctx: Any, scope: Any) -> UnitProvenance:
    from .b3_inputs import guest_image_for, patch_plan_for

    bundle = ctx.bundle
    u = ctx.unit(scope.id)
    facts = ctx.analysis("exit-facts", scope).value
    return unit_provenance(u, facts, guest=guest_image_for(ctx, bundle),
                           plan=patch_plan_for(ctx, bundle), bundle=bundle,
                           page_bits=ctx.header.target_page_bits)


def _spec():
    from .api import AnalysisSpec

    return AnalysisSpec(name="patch-join", version=1, scope="unit",
                        requires=(("exit-facts", 1),),
                        inputs=frozenset({"code", "meta", "bundle"}), compute=_compute,
                        result_schema=UnitProvenance)


PATCH_JOIN = _spec()
ANALYSES = (PATCH_JOIN,)
__all__ += ["UnitProvenance", "unit_provenance", "PATCH_JOIN", "ANALYSES"]

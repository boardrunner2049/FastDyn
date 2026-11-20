"""ProposalBuilder (design §5.5): how a pass proposes edits.

A pass never mutates a snapshot. It calls the builder's methods, and each call
records an :class:`Edit` with:

* ``reads``: the ``(uid, content_hash)`` of every unit it touches, the unit's
  layout hash for layout edits (and its annotation hash for ANNOTATE), every
  analysis result the pass read through its ``PassContext`` before the call,
  plus anything passed explicitly as ``reads=``;
* ``facts``: certified results the pass cited (``PassContext.fact`` or ``cites=``);
* ``affected_origins``: guest vaddr ranges ``"0x<start>-0x<end>"`` of the units;
* ``dependency_additions`` (B3/B4 only);
* the pass descriptor.

Capabilities are enforced at call time: a kind outside
``descriptor.capabilities`` raises :class:`~scopt.passes.base.EditCapabilityError`
(reason ``edit_capability``); a B3/B4 kind, or any kind the manager has not
enabled, raises :class:`CapabilityNotEnabled` (reason ``capability_not_enabled``).
The violation is also remembered in :attr:`ProposalBuilder.violations`, so a
pass that swallows the exception still fails.

Preconditions that need only the call itself (known unit, 64-aligned
placement, valid section, JSON annotation values) raise
:class:`EditPreconditionError`. Preconditions that depend on the working set
(relocatable, not pinned, no conflicting layout decision, reads still current)
are checked by the PassManager before it applies the edits (§5.6 step 4).

Layout edits never carry bytes; NativeLinker (WP10) derives the fixups.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from .analysis.api import AnalysisResult
from .canon import canonical_json, sha256_id
from .canon import _check as _check_json  # canonical-JSON value check
from .ids import is_unit_id
from .model.native import UDIV_KINDS, InlineCall
from .model.snapshot import ANNOT_HASH_PREFIX, LAYOUT_HASH_PREFIX, LAYOUT_SECTIONS, deep_freeze
from .passes.base import B2_EDIT_KINDS, B3_EDIT_KINDS, EditCapabilityError, EditKind, PassDescriptor

__all__ = [
    "Read", "FactRef", "Edit", "ProposalBuilder", "EditPreconditionError",
    "CapabilityNotEnabled", "REASON_EDIT_CAPABILITY", "REASON_EDIT_PRECONDITION",
    "TB_ALIGN", "edit_digest", "capability_reason", "REASON_CAPABILITY_NOT_ENABLED",
]

TB_ALIGN = 64
REASON_EDIT_CAPABILITY = "edit_capability"
REASON_EDIT_PRECONDITION = "edit_precondition"
REASON_CAPABILITY_NOT_ENABLED = "capability_not_enabled"

ReadKind = Literal["unit", "layout", "annotations", "analysis"]


class EditPreconditionError(ValueError):
    reason = REASON_EDIT_PRECONDITION


class CapabilityNotEnabled(EditCapabilityError):
    """B3/B4 edit kinds (or kinds the manager did not enable) in a B2 run."""

    reason = REASON_CAPABILITY_NOT_ENABLED


def capability_reason(exc: EditCapabilityError) -> str:
    """Stable reason code of a capability violation."""
    return getattr(exc, "reason", REASON_EDIT_CAPABILITY)


@dataclass(frozen=True)
class Read:
    """One dependency of an edit. ``hash`` is a content/layout/annotation/result hash."""

    kind: ReadKind
    key: str  # UnitId, or analysis name
    hash: str
    version: int | None = None
    scope_kind: str | None = None
    scope_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind, "key": self.key, "hash": self.hash}
        if self.kind == "analysis":
            d.update(version=self.version, scope_kind=self.scope_kind, scope_id=self.scope_id)
        return d

    @classmethod
    def of_result(cls, r: AnalysisResult, scope_kind: str | None = None) -> "Read":
        kind = scope_kind
        if kind is None:
            kind = "unit" if is_unit_id(r.scope_id) else "snapshot"
        return cls("analysis", r.name, r.result_hash, r.version, kind, r.scope_id)


@dataclass(frozen=True)
class FactRef:
    """A cited certified fact (an AnalysisResult)."""

    name: str
    version: int
    scope_id: str
    result_hash: str
    deps_digest: str  # digest of the result's dependency sets (two citations of one value
    #                   with different dependencies are different facts)
    result: AnalysisResult = field(compare=False, repr=False)

    @classmethod
    def of(cls, r: AnalysisResult) -> "FactRef":
        doc = [sorted(map(list, r.dep_units)), sorted(map(list, r.dep_results)),
               sorted(r.dep_inputs), bool(r.certified)]
        return cls(r.name, r.version, r.scope_id, r.result_hash,
                   sha256_id(canonical_json(doc)), r)

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "scope_id": self.scope_id,
                "result_hash": self.result_hash, "deps_digest": self.deps_digest}


def _jsonable(v: Any) -> Any:
    if isinstance(v, EditKind):
        return v.name
    if isinstance(v, Mapping):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return repr(v)


@dataclass(frozen=True)
class Edit:
    kind: EditKind
    pass_name: str
    pass_version: int
    seq: int  # index within the pass's proposal
    args: Mapping[str, Any]
    units: tuple[str, ...]  # UnitIds the edit touches
    reads: tuple[Read, ...] = ()
    facts: tuple[FactRef, ...] = ()
    affected_origins: tuple[str, ...] = ()
    dependency_additions: tuple[Any, ...] = ()
    descriptor: PassDescriptor | None = field(default=None, compare=False, repr=False)
    payload: Any = field(default=None, compare=False, repr=False)  # B3/B4 typed payload

    @property
    def is_layout(self) -> bool:
        return self.kind in (EditKind.PIN, EditKind.LAYOUT_ORDER, EditKind.PLACE)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.name, "pass": self.pass_name, "pass_version": self.pass_version,
            "seq": self.seq, "args": _jsonable(self.args), "units": list(self.units),
            "reads": [r.to_json() for r in self.reads],
            "facts": [f.to_json() for f in self.facts],
            "affected_origins": list(self.affected_origins),
            "dependency_additions": _jsonable(list(self.dependency_additions)),
        }


def edit_digest(edits: Iterable[Edit]) -> str | None:
    """``sha256:`` of the canonical JSON edit list; None when there are no edits."""
    rows = [e.to_json() for e in edits]
    return sha256_id(canonical_json(rows)) if rows else None


class ProposalBuilder:
    """Concrete ``ProposalBuilderAPI``. One builder per pass run.

    ``view`` is the ``WorkingSetView`` the pass runs on; ``ctx`` (optional) is
    the pass's context, whose recorded analysis reads and cited facts are
    attached to every subsequent edit. ``enabled`` is the set of kinds the
    manager allows in this run (B2 kinds at most).
    """

    def __init__(self, descriptor: PassDescriptor, view: Any, *,
                 enabled: Iterable[EditKind] = B2_EDIT_KINDS, ctx: Any = None):
        self.descriptor = descriptor
        self.view = view
        self.ctx = ctx
        self.enabled = frozenset(enabled)
        self.variants: set[str] = set()  # variant ids CLONEd by this proposal
        self._edits: list[Edit] = []
        self.violations: list[EditCapabilityError] = []

    # -- inspection ---------------------------------------------------------
    @property
    def edits(self) -> tuple[Edit, ...]:
        return tuple(self._edits)

    def __len__(self) -> int:
        return len(self._edits)

    def digest(self) -> str | None:
        return edit_digest(self._edits)

    def touched_units(self) -> frozenset[str]:
        return frozenset(u for e in self._edits for u in e.units)

    # -- checks -------------------------------------------------------------
    def _capability(self, kind: EditKind) -> None:
        if self.ctx is not None and hasattr(self.ctx, "budget"):
            self.ctx.budget()
        d = self.descriptor
        err: EditCapabilityError | None = None
        # B3/B4 kinds are admitted only when the manager enables them (WP21);
        # ADD_RECOVERY is never admitted in v1 (design §3.7).
        if kind not in self.enabled or kind == EditKind.ADD_RECOVERY:
            if kind in d.capabilities:
                err = CapabilityNotEnabled(
                    f"{d.name}@{d.version}: {kind.name} is not enabled in this run "
                    f"(enabled: {sorted(k.name for k in self.enabled)})")
            elif kind in B3_EDIT_KINDS:
                err = CapabilityNotEnabled(
                    f"{d.name}@{d.version}: {kind.name} is a B3/B4 edit kind; not enabled "
                    f"and not in the descriptor's capabilities")
        if err is None and kind not in d.capabilities:
            err = EditCapabilityError(
                f"{d.name}@{d.version}: {kind.name} is outside the descriptor's capabilities "
                f"{sorted(k.name for k in d.capabilities)}")
        if err is not None:
            self.violations.append(err)
            raise err

    def _unit(self, uid: Any):
        if not isinstance(uid, str) or not is_unit_id(uid):
            raise EditPreconditionError(f"malformed UnitId {uid!r}")
        try:
            return self.view.unit(uid)
        except KeyError:
            raise EditPreconditionError(f"unknown unit {uid}") from None

    def _origins(self, uids: Iterable[str]) -> tuple[str, ...]:
        out: list[str] = []
        for uid in uids:
            u = self.view.unit(uid)
            if u.vaddrs:
                out += [f"0x{v:x}-0x{v + u.guest_size:x}" for v in u.vaddrs]
            else:
                out.append(f"ram:0x{u.key.page_addr0:x}-0x{u.key.page_addr0 + u.guest_size:x}")
        return tuple(dict.fromkeys(out))

    def _coerce_read(self, item: Any) -> Read:
        if isinstance(item, Read):
            return item
        if isinstance(item, AnalysisResult):
            return Read.of_result(item)
        if isinstance(item, tuple) and len(item) == 2 and all(isinstance(x, str) for x in item):
            a, h = item
            if is_unit_id(a):
                if h.startswith(LAYOUT_HASH_PREFIX):
                    return Read("layout", a, h)
                if h.startswith(ANNOT_HASH_PREFIX):
                    return Read("annotations", a, h)
                return Read("unit", a, h)
            # (analysis name, result_hash): resolve the scope from the pass's reads
            for r in self._ctx_reads():
                if r.kind == "analysis" and r.key == a and r.hash == h:
                    return r
            return Read("analysis", a, h)
        raise EditPreconditionError(f"cannot interpret read {item!r}")

    def _ctx_reads(self) -> tuple[Read, ...]:
        f = getattr(self.ctx, "recorded_reads", None)
        return tuple(f()) if callable(f) else ()

    def _ctx_facts(self) -> tuple[FactRef, ...]:
        f = getattr(self.ctx, "recorded_facts", None)
        return tuple(f()) if callable(f) else ()

    def _record(self, kind: EditKind, args: Mapping[str, Any], uids: tuple[str, ...], *,
                reads: Iterable[Any] = (), cites: Iterable[AnalysisResult] = (),
                dependency_additions: Iterable[Any] = (),
                facet_reads: Iterable[str] = ("unit",)) -> Edit:
        rs: list[Read] = []
        for uid in uids:
            for fr in facet_reads:
                if fr == "unit":
                    rs.append(Read("unit", uid, self.view.content_hash(uid)))
                elif fr == "layout":
                    rs.append(Read("layout", uid, self.view.layout_hash(uid)))
                elif fr == "annotations":
                    rs.append(Read("annotations", uid, self.view.annotation_hash(uid)))
        rs += self._ctx_reads()
        rs += [self._coerce_read(x) for x in reads]
        facts = list(self._ctx_facts())
        for c in cites:
            if not isinstance(c, AnalysisResult):
                raise EditPreconditionError(f"cites= takes AnalysisResult objects, got {c!r}")
            facts.append(FactRef.of(c))
        e = Edit(kind=kind, pass_name=self.descriptor.name, pass_version=self.descriptor.version,
                 seq=len(self._edits), args=deep_freeze(args), units=uids,
                 reads=tuple(dict.fromkeys(rs)), facts=tuple(dict.fromkeys(facts)),
                 affected_origins=self._origins(uids),
                 dependency_additions=tuple(dependency_additions), descriptor=self.descriptor)
        self._edits.append(e)
        return e

    # -- B2 edits -----------------------------------------------------------
    def annotate(self, uid: str, key: str, value: Any, *, reads: Iterable[Any] = (),
                 cites: Iterable[AnalysisResult] = ()) -> None:
        """ANNOTATE: metadata only; later passes see it via the ``annotations`` analysis."""
        self._capability(EditKind.ANNOTATE)
        self._unit(uid)
        if not isinstance(key, str) or not key:
            raise EditPreconditionError(f"annotation key must be a non-empty string, got {key!r}")
        try:
            _check_json(value)
        except TypeError as exc:
            raise EditPreconditionError(f"annotation {key!r} is not JSON: {exc}") from None
        self._record(EditKind.ANNOTATE, {"uid": uid, "key": key, "value": value}, (uid,),
                     reads=reads, cites=cites, facet_reads=("unit", "annotations"))

    def pin(self, uid: str, reason: str, *, reads: Iterable[Any] = (),
            cites: Iterable[AnalysisResult] = ()) -> None:
        """PIN: keep the unit at its original offset."""
        self._capability(EditKind.PIN)
        self._unit(uid)
        if not isinstance(reason, str) or not reason:
            raise EditPreconditionError("pin reason must be a non-empty string")
        self._record(EditKind.PIN, {"uid": uid, "reason": reason}, (uid,), reads=reads,
                     cites=cites, facet_reads=("unit", "layout"))

    def layout_order(self, section: str, uids: tuple[str, ...] | list[str], *,
                     reads: Iterable[Any] = (), cites: Iterable[AnalysisResult] = ()) -> None:
        """LAYOUT_ORDER: append ``uids`` (in order) to ``section``."""
        self._capability(EditKind.LAYOUT_ORDER)
        if section not in LAYOUT_SECTIONS:
            raise EditPreconditionError(f"unknown layout section {section!r} "
                                        f"(one of {', '.join(LAYOUT_SECTIONS)})")
        if isinstance(uids, str):
            raise EditPreconditionError("layout_order takes a sequence of UnitIds")
        uids = tuple(uids)
        if not uids:
            raise EditPreconditionError("layout_order needs at least one unit")
        if len(set(uids)) != len(uids):
            raise EditPreconditionError("layout_order lists a unit twice")
        for u in uids:
            self._unit(u)
        self._record(EditKind.LAYOUT_ORDER, {"section": section, "uids": list(uids)}, uids,
                     reads=reads, cites=cites, facet_reads=("unit", "layout"))

    def place(self, uid: str, tb_offset: int, *, reads: Iterable[Any] = (),
              cites: Iterable[AnalysisResult] = ()) -> None:
        """PLACE: an explicit 64-aligned tb_offset (``move_units`` and the placer only)."""
        self._capability(EditKind.PLACE)
        self._unit(uid)
        if isinstance(tb_offset, bool) or not isinstance(tb_offset, int):
            raise EditPreconditionError(f"tb_offset must be an int, got {tb_offset!r}")
        if tb_offset < 0 or tb_offset % TB_ALIGN:
            raise EditPreconditionError(f"tb_offset {tb_offset:#x} is not a non-negative "
                                        f"multiple of {TB_ALIGN}")
        self._record(EditKind.PLACE, {"uid": uid, "tb_offset": tb_offset}, (uid,), reads=reads,
                     cites=cites, facet_reads=("unit", "layout"))

    def retire_direct_exit(self, payload: Any) -> None:
        from .ordinary_exit import OrdinaryExit

        self._capability(EditKind.RETIRE_DIRECT_EXIT)
        if (not isinstance(payload, OrdinaryExit) or type(payload.slot) is not int or
                payload.slot not in (0, 1) or type(payload.transfers) is not int or payload.transfers < 1):
            raise EditPreconditionError("RETIRE_DIRECT_EXIT requires a typed ordinary direct slot")
        source = self._unit(payload.target)
        successor = self._unit(payload.successor)
        if source.content_hash != payload.unit_hash:
            raise EditPreconditionError("ordinary direct exit preimage no longer matches")
        direct = [e for e in source.static_exits if e.direct and e.slot == payload.slot]
        if len(direct) != 1 or direct[0].dest_vaddr not in successor.vaddrs:
            raise EditPreconditionError("ordinary direct exit successor disagrees with the captured edge")
        e = self._record(EditKind.RETIRE_DIRECT_EXIT, {"payload": payload.to_json()},
                         (payload.target, payload.successor))
        self._edits[-1] = replace(e, payload=payload)

    def inline_call(self, payload: InlineCall) -> None:
        """Propose a checked native replacement for a call in the current unit."""
        self._capability(EditKind.INLINE_CALL)
        if not isinstance(payload, InlineCall):
            raise EditPreconditionError("INLINE_CALL requires an InlineCall payload")
        unit = self._unit(payload.target)
        splice = payload.splice
        custom_body = splice is not None and (splice.kind == "frame" or splice.kind in UDIV_KINDS)
        if (payload.unit_hash != unit.content_hash or (not custom_body and not payload.fragment.code)
                or payload.call_off < 0 or payload.continuation_off > unit.ldst_end
                or payload.continuation_off != payload.call_off + len(payload.call_bytes)
                or len(payload.call_bytes) not in (5, 6)
                or unit.code[payload.call_off:payload.continuation_off] != payload.call_bytes):
            raise EditPreconditionError("INLINE_CALL does not match the current call bytes")
        if splice is not None:
            start = splice.start_off
            if (not 0 <= start <= payload.call_off or (custom_body and start != payload.call_off)
                    or not splice.code or payload.growth < 0 or payload.growth % 8
                    or unit.code[start:payload.call_off] != splice.prefix):
                raise EditPreconditionError("INLINE_CALL splice does not match the argument setup")
        e = self._record(EditKind.INLINE_CALL, {"payload": payload.to_json()}, (payload.target,))
        self._edits[-1] = replace(e, payload=payload)

    # -- B3/B4 edits (typed payloads of design §3.7; enabled only by a B3 manager) --
    def _b3(self, kind: EditKind, payload: Any, uids: tuple[str, ...], *,
            reads: Iterable[Any] = (), cites: Iterable[AnalysisResult] = (),
            dependency_additions: Iterable[Any] = ()) -> Edit:
        from .passes.base import PAYLOAD_KINDS, PayloadError

        self._capability(kind)
        want = PAYLOAD_KINDS.get(kind)
        if want is None or not isinstance(payload, want):
            raise EditPreconditionError(
                f"{kind.name} takes a {getattr(want, '__name__', '?')} payload, got "
                f"{type(payload).__name__}")
        for u in uids:
            self._unit(u)
        try:
            args = payload.to_json()
        except PayloadError as e:  # pragma: no cover - validated at construction
            raise EditPreconditionError(str(e)) from None
        e = self._record(kind, {"payload": args}, uids, reads=reads, cites=cites,
                         dependency_additions=dependency_additions)
        e = replace(e, payload=payload)
        self._edits[-1] = e
        return e

    def redirect_exit(self, payload: Any = None, *, reads: Iterable[Any] = (),
                      cites: Iterable[AnalysisResult] = (),
                      dependency_additions: Iterable[Any] = (), **_legacy: Any) -> Edit:
        """REDIRECT_EXIT: a :class:`~scopt.passes.base.GuardedSlot`."""
        return self._b3(EditKind.REDIRECT_EXIT, payload, _uids(payload, "uid"), reads=reads,
                        cites=cites, dependency_additions=dependency_additions)

    def clone(self, payload: Any = None, *, reads: Iterable[Any] = (),
              cites: Iterable[AnalysisResult] = (), dependency_additions: Iterable[Any] = (),
              **_legacy: Any) -> Edit:
        """CLONE: a :class:`~scopt.passes.base.Clone` (records the variant id)."""
        e = self._b3(EditKind.CLONE, payload, _uids(payload, "src_uid"), reads=reads,
                     cites=cites, dependency_additions=dependency_additions)
        self.variants.add(payload.variant_id)
        return e

    def add_entry(self, payload: Any = None, *, reads: Iterable[Any] = (),
                  cites: Iterable[AnalysisResult] = (),
                  dependency_additions: Iterable[Any] = (), **_legacy: Any) -> Edit:
        """ADD_ENTRY: a :class:`~scopt.passes.base.SpecEntry` (variant cloned in this proposal)."""
        if payload is not None and getattr(payload, "variant_id", None) not in self.variants:
            self._capability(EditKind.ADD_ENTRY)
            raise EditPreconditionError("ADD_ENTRY for a variant this proposal did not CLONE")
        pred = getattr(payload, "pred", (None,))[0]
        return self._b3(EditKind.ADD_ENTRY, payload, (pred,) if isinstance(pred, str) else (),
                        reads=reads, cites=cites, dependency_additions=dependency_additions)

    def attach_fact(self, payload: Any = None, *, units: Iterable[str] = (),
                    reads: Iterable[Any] = (), cites: Iterable[AnalysisResult] = (),
                    dependency_additions: Iterable[Any] = (), **_legacy: Any) -> Edit:
        """ATTACH_FACT: a :class:`~scopt.passes.base.FactRef` (``units``: the R unit(s))."""
        return self._b3(EditKind.ATTACH_FACT, payload, tuple(units), reads=reads, cites=cites,
                        dependency_additions=dependency_additions)

    def pure_rewrite(self, payload: Any = None, *, reads: Iterable[Any] = (),
                     cites: Iterable[AnalysisResult] = (),
                     dependency_additions: Iterable[Any] = (), **_legacy: Any) -> Edit:
        """PURE_REWRITE: a :class:`~scopt.passes.base.Patch` on a unit or a variant
        of this proposal; the template must belong to this pass (§3.7)."""
        from .passes.base import PATCH_TEMPLATES

        self._capability(EditKind.PURE_REWRITE)
        tgt = getattr(payload, "target", None)
        tmpl = getattr(payload, "template_id", None)
        allowed = PATCH_TEMPLATES.get(self.descriptor.name, ())
        if tmpl is not None and tmpl not in allowed:
            raise EditPreconditionError(f"template {tmpl!r} is not allowed for "
                                        f"{self.descriptor.name} (allowed: {list(allowed)})")
        uids: tuple[str, ...] = ()
        if isinstance(tgt, str) and is_unit_id(tgt):
            uids = (tgt,)
        elif isinstance(tgt, str) and tgt not in self.variants and \
                tgt not in getattr(self.view, "variant_ids", lambda: ())():
            raise EditPreconditionError(f"PURE_REWRITE target {tgt!r} is neither a unit nor a "
                                        "known variant")
        return self._b3(EditKind.PURE_REWRITE, payload, uids, reads=reads, cites=cites,
                        dependency_additions=dependency_additions)

    def add_region(self, *args: Any, **kwargs: Any) -> None:
        """ADD_REGION is manager-generated (design §3.7, §3.9); passes never call it."""
        self._capability(EditKind.ADD_REGION)
        raise EditPreconditionError("ADD_REGION rows are generated by the manager")

    def add_recovery(self, *args: Any, **kwargs: Any) -> None:
        """ADD_RECOVERY is not used in v1: always ``capability_not_enabled``."""
        self._capability(EditKind.ADD_RECOVERY)


def _uids(payload: Any, attr: str) -> tuple[str, ...]:
    v = getattr(payload, attr, None)
    return (v,) if isinstance(v, str) else ()

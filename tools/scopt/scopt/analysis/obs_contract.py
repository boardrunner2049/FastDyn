"""``obs-contract@1``: the observation contract of B4 (design §7.1-7.2, §7.4). WP17.

Fail closed: a field that the contract does not list is observable at every
sync point and is never deletable.

Sync events (§7.1)::

    S1 TB entry (before the entry check)      S2 exit (goto_tb / guarded slot = direct chain;
    S3 lookup_tb_ptr call                         exit_tb / goto_ptr / exit_requested = main loop)
    S4 non-pure call (flags 0, NO_WG, plugin callback, noreturn)
    S5 LDST block                             S6 udiv/sdiv (NO_RWG but may raise; any
                                                 NO_RWG helper whose effect-table row may fault)
    P  brcond / br / set_label (pseudo; nothing observes)

Fields (env offsets from the EnvLayout, G21)::

    D1  regs[0..14], CF, VF, NF, ZF            observable at S1-S6
    D2  neg.can_do_io                          observable at S4, S5 and — when
                                               tb_start_cb_possible — at direct-chain S2 exits
                                               (the can_do_io direct-chain rule, G30, R5)
    D3  neg.plugin_mem_value_low/high          observable at S4; deletable only when the
                                               RuntimeIdentity proves no plugin calls
                                               qemu_plugin_mem_get_value outside memory
                                               callbacks (else observer_contract_unknown)
    --  regs[15]                               all; pc_store_required
    --  thumb, condexec_bits, hflags.*         all; not_bookkeeping_class
    --  neg.icount_decr.*                      all; accounting (spec:64)
    --  anything else                          all; field_not_deletable_class

Exits: ``goto_tb`` and ``guarded_slot`` are direct-chain exits; the
successor's TB-start plugin callback runs before the successor's own
``can_do_io`` store. ``exit_tb``/``exit_requested``/``exit_null`` return to
the main loop (``cpu_tb_exec`` sets ``can_do_io = true``, G15) and
``goto_ptr`` goes through ``helper_lookup_tb_ptr`` (sets it, G7), so
``can_do_io`` is not observable there.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..canon import canonical_json, sha256_id
from ..formats.bundle_v1 import mem_get_value_outside_mem_cb, tb_start_cb_possible
from ..formats.envlayout_v1 import EnvLayout
from .api import AnalysisSpec, ScopeRef

__all__ = [
    "SYNC_EVENTS", "DIRECT_CHAIN_EXITS", "MAIN_LOOP_EXITS", "D1_FIELDS", "D2_FIELDS", "D3_FIELDS",
    "ObsContract", "build_contract", "OBS_CONTRACT", "ANALYSES", "CONTRACT_NAME",
]

CONTRACT_NAME = "obs-contract@1"
SYNC_EVENTS = ("S1", "S2", "S3", "S4", "S5", "S6", "P")
DIRECT_CHAIN_EXITS = frozenset({"goto_tb", "guarded_slot"})
MAIN_LOOP_EXITS = frozenset({"exit_tb", "exit_requested", "exit_null", "goto_ptr", "noreturn"})
D1_FIELDS = tuple([f"regs[{i}]" for i in range(15)] + ["CF", "VF", "NF", "ZF"])
D2_FIELDS = ("neg.can_do_io",)
D3_FIELDS = ("neg.plugin_mem_value_low", "neg.plugin_mem_value_high")
_KEY_FIELDS = ("thumb", "condexec_bits", "hflags.flags", "hflags.flags2")
_REAL_SYNC = ("S1", "S2", "S3", "S4", "S5", "S6")


@dataclass(frozen=True)
class ObsContract:
    """The resolved contract for one EnvLayout and one RuntimeIdentity."""

    envlayout_id: str | None
    tb_start_cb_possible: bool
    mem_get_value_outside_mem_cb: bool | None
    offsets: Mapping[str, int] = field(default_factory=dict)  # field -> env offset (listed fields)
    contract_id: str = ""

    # -- classes ----------------------------------------------------------------------
    def deletable_class(self, fld: str) -> str | None:
        """``"D1"``/``"D2"``/``"D3"`` or None (not deletable)."""
        if fld in D1_FIELDS:
            return "D1"
        if fld in D2_FIELDS:
            return "D2"
        if fld in D3_FIELDS:
            return "D3" if self.mem_get_value_outside_mem_cb is False else None
        return None

    def not_deletable_reason(self, fld: str) -> str | None:
        """Stable reason code (§7.2, §7.5) when ``fld`` is not deletable, else None."""
        if self.deletable_class(fld) is not None:
            return None
        if fld == "regs[15]":
            return "pc_store_required"
        if fld in _KEY_FIELDS or fld.startswith("hflags."):
            return "not_bookkeeping_class"
        if fld in D3_FIELDS:
            return "observer_contract_unknown"
        return "field_not_deletable_class"

    # -- observability ------------------------------------------------------------------
    def observable(self, fld: str, event: str, *, exit_kind: str | None = None) -> bool:
        """Is ``fld`` observable at sync ``event`` (an ``SYNC_EVENTS`` member)?

        ``exit_kind`` qualifies S2 (``goto_tb``, ``guarded_slot``, ``exit_tb``,
        ``exit_requested``, ``exit_null``, ``goto_ptr``, ``noreturn``); an
        unknown exit kind at S2 is treated as a direct chain (fail closed).
        """
        if event not in SYNC_EVENTS:
            raise ValueError(f"unknown sync event {event!r}")
        cls = self.deletable_class(fld)
        if cls is None:
            # fail closed: observable everywhere (P included)
            return True
        if event == "P":
            return False
        if cls == "D1":
            return event in _REAL_SYNC
        if cls == "D2":
            if event in ("S4", "S5"):
                return True
            if event == "S2":
                direct = exit_kind is None or exit_kind not in MAIN_LOOP_EXITS
                return bool(direct and self.tb_start_cb_possible)
            return False
        if cls == "D3":
            return event == "S4"
        return True  # pragma: no cover

    def cdio_observed_at_exit(self, exit_kind: str) -> bool:
        """The ``can_do_io`` direct-chain rule (§7.2, R5)."""
        return self.observable("neg.can_do_io", "S2", exit_kind=exit_kind)

    def to_json(self) -> dict[str, Any]:
        return {"contract": CONTRACT_NAME, "envlayout_id": self.envlayout_id,
                "tb_start_cb_possible": self.tb_start_cb_possible,
                "mem_get_value_outside_mem_cb": self.mem_get_value_outside_mem_cb,
                "offsets": dict(sorted(self.offsets.items())),
                "classes": {"D1": list(D1_FIELDS), "D2": list(D2_FIELDS),
                            "D3": list(D3_FIELDS) if self.mem_get_value_outside_mem_cb is False
                            else []}}

    def content_digest(self) -> str:
        return self.contract_id


def build_contract(layout: EnvLayout | None, runtime: Mapping[str, Any] | None) -> ObsContract:
    """Resolve the contract against ``layout`` and the RuntimeIdentity facts.

    A v1 RuntimeIdentity (no admission block) gives ``tb_start_cb_possible =
    True`` and an unknown ``mem_get_value`` fact (D3 not deletable).
    """
    offs: dict[str, int] = {}
    if layout is not None:
        for f in D1_FIELDS + D2_FIELDS + D3_FIELDS + ("regs[15]",) + _KEY_FIELDS + \
                ("neg.icount_decr.u32",):
            if layout.has_field(f):
                offs[f] = layout.offset(f)
    tsc = tb_start_cb_possible(runtime)
    mgv = mem_get_value_outside_mem_cb(runtime)
    c = ObsContract(None if layout is None else layout.envlayout_id, tsc, mgv, offs)
    cid = sha256_id(canonical_json(c.to_json()))
    return ObsContract(c.envlayout_id, tsc, mgv, offs, cid)


def _compute(ctx: Any, scope: ScopeRef) -> ObsContract:
    lay = ctx.analysis("envlayout", ScopeRef("snapshot", ctx.snapshot_id)).value
    return build_contract(lay.get("layout"), ctx.runtime)


OBS_CONTRACT = AnalysisSpec(name="obs-contract", version=1, scope="snapshot",
                            requires=(("envlayout", 1),), inputs=frozenset({"meta"}),
                            compute=_compute, result_schema=ObsContract)
ANALYSES = (OBS_CONTRACT,)

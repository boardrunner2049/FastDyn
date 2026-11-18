"""B4 mutants on real units, packaged through the WP25 path (design §9.3, plan WP25). WP25.

Each mutant is a same-length byte change of one captured unit that a buggy
B4 pass could propose. It is packaged exactly like a real proposal
(:func:`scopt.passes.eliminate_state_sync.package_b4`: NativeLinker v2 → v5
writer → ``validate_b3`` with V12 from ``hostsym`` under the strict call
contract, V15 placement and deletable class, V20 admission) and must be
**rejected statically** (WP25 acceptance "every B4 mutant rejected
statically; no edit outside the allowed placement").

Kinds (shapes are searched in the HostOps of the snapshot; §9.3 names in
brackets; ``detector`` is the dynamic O4 fixture of §9.5 for the integration
stage):

=============================  ===========================================  ===========
kind                           change                                       detector
=============================  ===========================================  ===========
guest_store_delete             NOP the guest store of an LDST block         F-B4-7
flag_store_before_ldst         NOP a flag store live at the next LDST (S5)  F-B4-2
flag_store_before_helper       NOP a flag store live at a flags-0/plugin    F-B4-11
                               call (S4)
brcond_reload [T-B4-2b]        NOP a brcond sync store whose global is      F-B4-1
                               reloaded after the branch/label
cdio_last_insn_direct_chain    NOP the last-insn ``can_do_io = 1`` before a F-B4-9
                               goto_tb exit, packaged with
                               ``tb_start_cb_possible = true``
it_block_d4                    NOP a D1 store needed only by an LDST slow   F-B4-5
                               path (the D4 class), and the slow-path edit
                               that would "move" it there
icount_immediate               change the icount ``add`` immediate          F-B4-3 (O2b)
d1_before_exit                 NOP a D1 store live at an exit (S2)          F-B4-1
d1_before_udiv                 NOP a D1 store live at udiv/sdiv (S6)        F-B4-6
d2_before_lookup               NOP a ``can_do_io`` store whose first reader static (the
                               is the lookup helper (survey rule)          lookup sets
                                                                            it, G7)
d3_before_plugin_call          NOP a ``plugin_mem_value`` store live at a   F-B4-7
                               plugin callback
d3_without_fact                a dead D3 store deleted under an identity    static (V20)
                               without the ``mem_get_value`` fact
pc_store_delete                NOP a ``regs[15]`` store (pc_store_required) F-B4-1
prefix_icount_store            NOP the prefix ``icount_decr.u16.low`` store F-B4-3
exit_stub_edit                 NOP the exit stub's ``lea rax`` (struct ptr) F-B4-1
log_reg_ring_store [L1]        NOP the coverage LOG_REG ring store          F-B3-23 / F-B4-10
                                                                            (O6), reported
log_reg_index_store [L2]       NOP the LOG_REG ring-index store             as L1
log_reg_value_field [L7]       the LOG_REG value load reads regs[14]        as L1
=============================  ===========================================  ===========

The LOG_REG kinds (close-out, design/closeout-logreg.md §A.8) exist only on
coverage units (F-B4-10, F-B3-23), which HostOps admits since the close-out;
they are reported, not gating (§9.3's gating kinds are unchanged).
``UPDATE_MEM`` deletion and host_abs pointers inside env need FastDyn host
stores no captured snapshot has; they are covered by WP19's synthetic suite
(:func:`scopt.validate_sym.mutants.synthetic_suite`), which
:func:`synthetic_b4` re-runs and filters.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .analysis.effects import call_class
from .analysis.helper_effects import effects_for_hostops
from .analysis.hcfg import build_hcfg
from .analysis.hostops import OpKind, build_hostops
from .analysis.obs_contract import build_contract
from .analysis.syncdead import TrackedBytes, nop_bytes, syncdead_unit
from .passes.base import Outcome, PassResult, Patch
from .passes.eliminate_state_sync import ALL_CLASSES, B4Proposal, package_b4

__all__ = ["B4Mutant", "find_mutants", "check_mutant", "DETECTORS", "B4_SYNTH_KINDS",
           "synthetic_b4", "with_admission"]

DETECTORS: dict[str, str | None] = {
    "guest_store_delete": "F-B4-7",
    "flag_store_before_ldst": "F-B4-2",
    # close-out: F-B4-1 has no instance of the kind (its printreg TB starts after the bne, no
    # dirty flag); F-B4-11 (fixtures/b3 b4_11.s, b3_b411.elf) reads the flags after a flags-0
    # helper / plugin callback (design/closeout-logreg.md B.2)
    "flag_store_before_helper": "F-B4-11",
    "brcond_reload": "F-B4-1 (T-B4-2b)",
    "cdio_last_insn_direct_chain": "F-B4-9",
    "it_block_d4": "F-B4-5",
    "it_block_d4_slowpath": "F-B4-5",
    "icount_immediate": "F-B4-3 (O2b)",
    "d1_before_exit": "F-B4-1",
    "d1_before_udiv": "F-B4-6",
    "d2_before_lookup": None,
    "d3_before_plugin_call": "F-B4-7",
    "d3_without_fact": None,
    "pc_store_delete": "F-B4-1",
    "prefix_icount_store": "F-B4-3",
    "exit_stub_edit": "F-B4-1",
    "log_reg_ring_store": "F-B3-23 / F-B4-10 (O6)",
    "log_reg_index_store": "F-B3-23 / F-B4-10 (O6)",
    "log_reg_value_field": "F-B3-23 / F-B4-10 (O6)",
}
STATIC_ONLY = {
    "d2_before_lookup": "helper_lookup_tb_ptr sets can_do_io = true before any reader (G7); the "
                        "survey rule makes it a reader anyway, so only V12 under the strict "
                        "call contract can see the deletion",
    "d3_without_fact": "the deletion is equivalent under the admitted contract; what is wrong "
                       "is the missing RuntimeIdentity fact (V20/V15), not the bytes",
}
#: WP19 synthetic mutants that are B4 cases (no host store exists in the real snapshots)
B4_SYNTH_KINDS = ("guest_store_delete", "update_mem_delete", "log_reg_delete",
                  "flag_store_before_ldst", "flag_store_before_helper", "brcond_reload",
                  "cdio_last_insn_direct_chain", "it_block_d4", "icount_immediate",
                  "host_abs_in_env", "host_abs_unbound", "search_row",
                  # close-out: the coverage LOG_REG block (L1-L8)
                  "logreg_ring_store_nop", "logreg_index_store_nop", "logreg_index_add8",
                  "logreg_spin_nop", "logreg_spin_retarget", "logreg_buf_retarget",
                  "logreg_ring_aliases_env", "logreg_value_field", "logreg_d1_store_hoisted")


@dataclass
class B4Mutant:
    kind: str
    uid: str
    patches: tuple[Patch, ...]
    note: str = ""
    runtime: Mapping[str, Any] | None = None  # identity override (None: the caller's)
    detector: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "uid": self.uid, "note": self.note,
                "patches": [p.to_json() for p in self.patches],
                "runtime_override": None if self.runtime is None else "admission changed",
                "dynamic_detector": self.detector, "static_only": STATIC_ONLY.get(self.kind)}


def with_admission(runtime: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
    """A copy of a v2 RuntimeIdentity with admission fields changed (mutant identities)."""
    from .ids import runtime_identity_id

    ri = {k: v for k, v in runtime.items() if k != "runtime_identity_id"}
    ri["admission"] = {**runtime["admission"], **changes}
    ri["runtime_identity_id"] = runtime_identity_id(ri)
    return ri


def _p(uid: str, o: Any, code: bytes, new: bytes | None = None, why: str = "X") -> Patch:
    old = bytes(code[o.off:o.end])
    nb = new if new is not None else (nop_bytes(o.size) or bytes([0x90]) * o.size)
    return Patch(target=uid, host_off=o.off, old_bytes=old, new_bytes=nb,
                 template_id="b4_nop_store", justification=f"{why}:mutant")


def _first_observer(h: Any, g: Any, i: int, fld: str, *, stop_at_branch: bool = True
                    ) -> tuple[int, Any] | None:
    """Fall-through scan after op ``i``: the first op that observes or re-stores ``fld``."""
    ops = h.ops
    for j in range(i + 1, len(ops)):
        q = ops[j]
        if q.is_env_store and q.store_field == fld:
            return j, "restore"
        if j in g.exits:
            return j, "exit"
        if q.kind == OpKind.CALL and q.block != "ldst_slow":
            return j, "call:" + call_class(q.helper_name, q.helper_flags,
                                           effects_for_hostops(h))
        if q.kind == OpKind.GUEST_ACCESS:
            return j, "ldst"
        if q.kind == OpKind.ENV_LOAD and q.env and q.env[0].field == fld:
            return j, "load"
        if stop_at_branch and q.kind in (OpKind.JCC, OpKind.JMP) and \
                not {"LDST_FAST_JCC", "LDST_RET_JMP"} & set(q.sites):
            return j, "branch"
    return None


def find_mutants(snapshot: Any, layout: Any, runtime: Mapping[str, Any], *,
                 per_kind: int = 1, units: Iterable[str] | None = None) -> list[B4Mutant]:
    """Mutants of real units of ``snapshot`` (up to ``per_kind`` per kind)."""
    con = build_contract(layout, runtime)
    tb = TrackedBytes.of(layout)
    ca = snapshot.header.code_address
    flags = {"CF", "VF", "NF", "ZF"}
    d1 = {f"regs[{i}]" for i in range(15)} | flags
    out: list[B4Mutant] = []
    n: dict[str, int] = {}
    want_units = None if units is None else set(units)

    def want(k: str) -> bool:
        return n.get(k, 0) < per_kind

    def add(k: str, uid: str, ps: Iterable[Patch], note: str, ri: Any = None) -> None:
        n[k] = n.get(k, 0) + 1
        out.append(B4Mutant(k, uid, tuple(ps), note, ri, DETECTORS.get(k)))

    for u in snapshot.units():
        if want_units is not None and u.uid not in want_units:
            continue
        if u.meta_status != "ok" or not u.reloc.relocatable or u.key.cflags & 0x111FF:
            continue
        h = build_hostops(u, layout, ca)
        if not h.ok:
            continue
        g = build_hcfg(h)
        if not g.ok or g.cyclic:
            continue
        sd = syncdead_unit(u, h, g, layout, con, tracked=tb)
        if not sd.ok:
            continue
        ops, code, uid = h.ops, u.code, u.uid
        targets = {o.target for o in ops if o.target is not None}
        for lb in h.log_blocks:  # the coverage LOG_REG block (L1, L2, L7)
            for k, off, nb in (("log_reg_ring_store", lb.ring_store, None),
                               ("log_reg_index_store", lb.idx_store, None),
                               ("log_reg_value_field", lb.value_load,
                                bytes(code[lb.value_load:lb.value_load + 3]) +
                                bytes([(lb.value_off - 4) & 0xFF]))):
                o = h.op_at(off)
                if want(k) and o is not None:
                    add(k, uid, [_p(uid, o, code, nb, why="LOG")], f"{o.text} @{o.off:#x}")
        for i, o in enumerate(ops):
            fld = o.store_field if o.is_env_store else None
            if want("guest_store_delete") and o.kind == OpKind.GUEST_ACCESS and o.is_ld is False:
                add("guest_store_delete", uid, [_p(uid, o, code, why="D1")],
                    f"guest store {o.text} @{o.off:#x}")
            if o.block == "prefix" and want("icount_immediate") and o.text.startswith("add") \
                    and o.size >= 3:
                nb = bytearray(code[o.off:o.end])
                nb[-1] ^= 0x01
                add("icount_immediate", uid, [_p(uid, o, code, bytes(nb))],
                    f"{o.text} -> imm ^ 1")
            if o.block == "prefix" and fld == "neg.icount_decr.u16.low" and \
                    want("prefix_icount_store"):
                add("prefix_icount_store", uid, [_p(uid, o, code)], o.text)
            if o.block == "exit_stub" and o.kind == OpKind.CONST and "lea" in o.text and \
                    want("exit_stub_edit"):
                add("exit_stub_edit", uid, [_p(uid, o, code, why="D1")], o.text)
            if fld == "regs[15]" and o.block == "insn" and want("pc_store_delete"):
                add("pc_store_delete", uid, [_p(uid, o, code)], o.text)
            if not (fld and o.block == "insn"):
                continue
            fo = _first_observer(h, g, i, fld)
            if fo is None:
                continue
            j, what = fo
            if fld in flags and what == "ldst" and want("flag_store_before_ldst"):
                add("flag_store_before_ldst", uid, [_p(uid, o, code, why="D1")],
                    f"{o.text} then {ops[j].text}")
            if fld in flags and what in ("call:global", "call:plugin") and \
                    want("flag_store_before_helper"):
                add("flag_store_before_helper", uid, [_p(uid, o, code, why="D1")],
                    f"{o.text} then {what} {ops[j].helper_name}")
            if fld in d1 and what == "exit" and want("d1_before_exit"):
                add("d1_before_exit", uid, [_p(uid, o, code, why="D1")],
                    f"{o.text} then {g.exits[j]}")
            if fld in d1 and what == "call:raising" and want("d1_before_udiv"):
                add("d1_before_udiv", uid, [_p(uid, o, code, why="D1")],
                    f"{o.text} then {ops[j].helper_name}")
            if fld == "neg.can_do_io" and what == "call:lookup" and want("d2_before_lookup"):
                add("d2_before_lookup", uid, [_p(uid, o, code, why="D2")],
                    f"{o.text} then lookup_tb_ptr")
            if fld in ("neg.plugin_mem_value_low", "neg.plugin_mem_value_high") and \
                    what == "call:plugin" and want("d3_before_plugin_call"):
                add("d3_before_plugin_call", uid, [_p(uid, o, code, why="D3")],
                    f"{o.text} then plugin callback")
            # brcond reload (T-B4-2b): sync store, branch/label, reload of the same global
            if fld in d1 and o.origin == "global_sync" and want("brcond_reload"):
                seen_label = False
                for q in ops[i + 1:]:
                    if q.off in targets or q.kind == OpKind.JCC:
                        seen_label = True
                    if q.is_env_store and q.store_field == fld:
                        break
                    if q.kind in (OpKind.EXIT_JMP, OpKind.GOTO_PTR, OpKind.CALL,
                                  OpKind.GUEST_ACCESS) or \
                            (q.kind == OpKind.JMP and "GOTO_TB" in q.sites):
                        break
                    if seen_label and q.kind == OpKind.ENV_LOAD and q.env and \
                            q.env[0].field == fld:
                        add("brcond_reload", uid, [_p(uid, o, code, why="D1")],
                            f"{o.text}; branch; {q.text}")
                        break
            # D4: needed only at an LDST (S5), overwritten right after it
            if fld in d1 and what == "ldst" and want("it_block_d4"):
                k = j + 1
                ok = False
                while k < len(ops):
                    q = ops[k]
                    if q.is_env_store and q.store_field == fld:
                        ok = True
                        break
                    if k in g.exits or q.kind in (OpKind.CALL, OpKind.GUEST_ACCESS) or \
                            (q.kind in (OpKind.JCC, OpKind.JMP) and
                             not {"LDST_FAST_JCC", "LDST_RET_JMP"} & set(q.sites)) or \
                            (q.kind == OpKind.ENV_LOAD and q.env and q.env[0].field == fld):
                        break
                    k += 1
                if ok:
                    add("it_block_d4", uid, [_p(uid, o, code, why="D1")],
                        f"{o.text} needed only by LDST {ops[j].ldst} (D4)")
                    slow = [q for q in ops if q.block == "ldst_slow" and q.ldst == ops[j].ldst
                            and q.kind not in (OpKind.CALL, OpKind.JMP, OpKind.JCC)]
                    if slow and want("it_block_d4_slowpath"):
                        q = slow[0]
                        add("it_block_d4_slowpath", uid,
                            [_p(uid, o, code, why="D1"), _p(uid, q, code, why="D1")],
                            f"fast-path store deleted and slow path edited at {q.off:#x}")
        # D2b: last-insn can_do_io = 1 dead under tb_start_cb_possible = false, before goto_tb
        for d in sd.dead:
            if d.cls == "D2" and d.block == "insn" and d.old[-1:] == b"\x01" and \
                    want("cdio_last_insn_direct_chain"):
                fo = _first_observer(h, g, d.op, d.field, stop_at_branch=False)
                if fo and fo[1] == "exit" and g.exits[fo[0]].startswith("goto_tb"):
                    add("cdio_last_insn_direct_chain", uid,
                        [Patch(uid, d.off, d.old, nop_bytes(d.size), "b4_nop_store",
                               "D2:mutant")],
                        "dead under tb_start_cb_possible=false; packaged with true",
                        with_admission(runtime, tb_start_cb_possible=True,
                                       tb_start_cb_reasons=["mutant:forced_true"]))
            if d.cls == "D3" and want("d3_without_fact"):
                add("d3_without_fact", uid,
                    [Patch(uid, d.off, d.old, nop_bytes(d.size), "b4_nop_store",
                           "D3:mutant")],
                    "a dead D3 store, packaged without the mem_get_value fact",
                    with_admission(runtime, mem_get_value_outside_mem_cb=None))
        if all(not want(k) for k in DETECTORS):
            break
    return out


@dataclass
class MutantCheck:
    kind: str
    uid: str
    status: str
    reason: str | None
    failed_rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return self.status == "rejected"

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "uid": self.uid, "status": self.status,
                "rejected": self.rejected, "reason": self.reason,
                "failed_rows": self.failed_rows}


def check_mutant(snapshot: Any, m: B4Mutant, store: Any, *, layout: Any,
                 runtime: Mapping[str, Any], selftest: Mapping[str, Any],
                 candidate_id: str | None = None) -> MutantCheck:
    """Package mutant ``m`` through :func:`package_b4`; it must be rejected."""
    ri = dict(m.runtime) if m.runtime is not None else dict(runtime)
    con = build_contract(layout, ri)
    classes = tuple(sorted({p.justification.split(":", 1)[0] for p in m.patches
                            if p.justification.split(":", 1)[0] in ALL_CLASSES})) or ALL_CLASSES
    prop = B4Proposal(PassResult(Outcome.CANDIDATE, "mutant", m.kind), {m.uid: m.patches},
                      classes, con)
    cid = candidate_id or f"b4mut-{m.kind}-{m.uid.split(':')[-1][:12]}"
    try:
        r = package_b4(snapshot, prop, store, candidate_id=cid, layout=layout, runtime=ri,
                       selftest=selftest)
    except Exception as e:  # noqa: BLE001 - a crash is not a static rejection
        return MutantCheck(m.kind, m.uid, "error", f"{type(e).__name__}: {e}")
    rows = []
    if r.validation is not None:
        rows = [{"code": x["code"], "check": x["check"], "reason": x.get("reason"),
                 "detail": str(x.get("detail"))[:240]}
                for x in r.validation.rows if x["result"] == "fail"]
    return MutantCheck(m.kind, m.uid, r.status, r.reason, rows)


def synthetic_b4(contract: Any = None) -> dict[str, Any]:
    """WP19's synthetic suite, restricted to the B4 kinds (host stores, host_abs)."""
    from .validate_sym.mutants import synthetic_suite

    s = synthetic_suite()
    res = [r for r in s.results if r.name in B4_SYNTH_KINDS]
    return {"mutants": len(res), "rejected": sum(r.rejected for r in res),
            "all_rejected": bool(res) and all(r.rejected for r in res),
            "controls_ok": s.controls_ok, "results": [r.to_json() for r in res]}


"""``syncdead@1``: dead executor-state stores of one unit (B3/B4 design §7.1-7.5). WP25.

A backward *may-need* dataflow over the **atomic** ``hcfg@1`` view (an LDST block
is one op, slow paths are not in the graph) for every byte of the deletable
classes of ``obs-contract@1``::

    D1  regs[0..14], CF, VF, NF, ZF        needed at S1-S6 (every exit, the lookup, every
                                           non-pure call, udiv/sdiv, every LDST block)
    D2  neg.can_do_io                      needed at every call (any class, see below), every
                                           LDST block and, when ``tb_start_cb_possible``, every
                                           direct-chain exit (goto_tb / guarded slot; the
                                           can_do_io direct-chain rule, G30, R5)
    D3  neg.plugin_mem_value_low/high      needed at every S4/S6 call (flags 0, NO_WG, plugin
                                           callbacks, noreturn, udiv/sdiv; see below);
                                           deletable only when the RuntimeIdentity proves the
                                           plugins do not call qemu_plugin_mem_get_value
                                           outside memory callbacks

Transfer, per op (``need_in = (need_out & ~kill) | gen``)::

    env load overlapping a tracked byte     gen those bytes   (TCG reloads globals after
                                                               brcond/set_label; LOG_REG reads env) [R7]
    unclassified rbp access / host_abs op   gen ALL           (HostOps pins such units anyway)
      with an unknown address
    env store                               kill the bytes it writes (a read-modify-write
                                            also gens them)
    sync event                              gen the bytes observable there (table above)

LLVM-survey corrections (design/llvm-pass-applicability.md §3c B4-2, §4.3 item 5),
as adopted here (recorded in the WP25 implementation note of the design):

* **every helper or callback call is a reader of ``can_do_io``**, including
  ``NO_RWG`` helpers and the lookup helper (``call_reads_all``, default on).
  This is stricter than §7.2 (S4/S5 only) and is the rule the D2 model
  (map-state-sync-traffic §7.4) used: a NO_RWG helper promises nothing about
  non-global env fields, and ``can_do_io`` is read by the icount readers.
  For ``plugin_mem_value_*`` the survey's "every call" rule is **not** the
  default (``d3_all_calls``, default off): its only reader is
  ``qemu_plugin_mem_get_value`` (G22), which only plugin code calls, and the
  RuntimeIdentity fact (V20) says the plugins call it only inside memory
  callbacks, i.e. from a plugin call in TB code (S4 here). Named QEMU helpers
  and the lookup helper never run a memory callback without first writing
  both fields (``qemu_plugin_vcpu_mem_cb``, plugins/core.c:627-628). So D3 is
  needed at the S4/S6 calls of obs-contract@1, which includes every plugin
  callback;
* the D2a/D2b split: the prefix ``false`` store and body stores are D2a; the
  last-insn ``true`` store before a direct-chain exit is D2b and is needed there
  whenever ``tb_start_cb_possible`` (its dependency on the successor's TB-start
  callback is the RuntimeIdentity fact, re-checked by V20 against Tier 0);
* ``udiv``/``sdiv`` are S6 = S4 for all three classes (the env image there stays
  the baseline image: no store live there is ever deleted);
* the prefix ``can_do_io`` store class is allowed (design §7.3 D2 exception), the
  icount bytes stay frozen;
* env offsets come from the DWARF EnvLayout (``plugin_mem_value_high`` is −0x10, G21).

A store ``s`` is **dead** iff ``need_out(s) ∩ bytes(s) = ∅``: on every path
from ``s`` the bytes are overwritten before any observer, which is what "dead on
every path" means for a may-need analysis. Deleting any set of dead stores
together is safe: a dead store's kill only removes bytes that are not needed,
so deleting it leaves every need set unchanged.

Placement (design §7.3, V15): a candidate is a plain ``mov`` (ENV_STORE_IMM /
ENV_STORE_REG) writing only bytes of one field of a deletable class (the 32-bit
``plugin_mem_value_low`` store of an i32 access is partial; the dataflow is byte
level, so a partial store is dead iff its own bytes are not needed), inside one
guest insn's host range, not in an LDST block, an exit stub, the pool or a guard
stub, and not in the prefix ``[0, insn0_start)`` except the single ``can_do_io``
store emitted there (G13).

The coverage LOG_REG block (close-out, design/closeout-logreg.md §A.4): a block
matched by :func:`scopt.decode.logreg.match_logreg` has known ``abs_addr`` on its
two loads and two stores (IDX, CONS, the ring base BUF), so it gens/kills no
tracked byte except through its exact ``regs[15]`` load (not a tracked byte);
it has no env store, call, LDST or exit, so it is no observer in ``mode="b4"``
(in ``mode="tcg"`` its spin ``je`` and the label at its start stay P points).
Its spin edge is not in the atomic view. A store inside the block is refused
``edit_inside_log_block`` (a FAILED code; vacuous for env stores today, it is
the rule V15 checks).

Unit admission (§7.3): ``meta_status == ok`` and relocatable, else
``unit_not_relocatable``; ``cflags & (CF_COUNT_MASK|CF_NOIRQ|CF_MEMI_ONLY)`` ⇒
``special_cflags_unit``; a pinned HostOps or a malformed hcfg ⇒ its pin reason;
more than 256 paths, a cycle, or more than 256 candidate stores ⇒
``path_budget_exhausted``.

``mode="tcg"`` replaces the observer set by TCG's own sync set (S2-S5 plus the
pseudo points P: every branch and branch target; NO_RWG calls are not
observers). Under it no D1 store may be dead (map-state-sync-traffic §7.3 M0 = 0,
T-B4-1).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .api import AnalysisSpec, ScopeRef
from .effects import call_class
from .hcfg import HCfg, build_hcfg, exit_class
from .hostops import OpKind, UnitHostOps, build_hostops
from .obs_contract import D1_FIELDS, D2_FIELDS, D3_FIELDS, MAIN_LOOP_EXITS, ObsContract

__all__ = [
    "CF_COUNT_MASK", "CF_MEMI_ONLY", "CF_NOIRQ", "SPECIAL_CFLAGS", "MAX_PATHS", "MAX_CANDIDATES",
    "B4Reason", "DeadStore", "Refusal", "SyncDead", "syncdead_unit", "syncdead_of",
    "TrackedBytes", "SYNCDEAD", "ANALYSES", "NOPS", "nop_bytes", "census_syncdead",
]

CF_COUNT_MASK = 0x000001FF
CF_MEMI_ONLY = 0x00001000
CF_NOIRQ = 0x00010000
SPECIAL_CFLAGS = CF_COUNT_MASK | CF_NOIRQ | CF_MEMI_ONLY
MAX_PATHS = 256
MAX_CANDIDATES = 256

#: Intel-recommended multi-byte NOPs (SDM vol. 2B "NOP"), one instruction each.
NOPS = {
    1: "90", 2: "6690", 3: "0f1f00", 4: "0f1f4000", 5: "0f1f440000", 6: "660f1f440000",
    7: "0f1f8000000000", 8: "0f1f840000000000", 9: "660f1f840000000000",
    10: "662e0f1f840000000000", 11: "66662e0f1f840000000000",
}


def nop_bytes(n: int) -> bytes | None:
    """One ``n``-byte NOP instruction (1..11), else None (``no_same_length_encoding``)."""
    h = NOPS.get(n)
    return None if h is None else bytes.fromhex(h)


class B4Reason:
    """Stable B4 reason codes (design §7.3, §7.5). Every code keeps the baseline
    store (``skipped_unsupported``) except :data:`UNKNOWN` ones
    (``skipped_unknown``) and :data:`FAILED` ones (a candidate bug)."""

    FIELD_NOT_DELETABLE_CLASS = "field_not_deletable_class"
    PC_STORE_REQUIRED = "pc_store_required"
    NOT_BOOKKEEPING_CLASS = "not_bookkeeping_class"
    OBSERVED_AT_SYNC_POINT = "observed_at_sync_point"
    NO_DEAD_STORE = "no_dead_store"
    UNIT_NOT_RELOCATABLE = "unit_not_relocatable"
    SPECIAL_CFLAGS_UNIT = "special_cflags_unit"
    PATH_BUDGET_EXHAUSTED = "path_budget_exhausted"
    EDIT_IN_PREFIX = "edit_in_prefix"
    EDIT_INSIDE_LDST_BLOCK = "edit_inside_ldst_block"
    EDIT_IN_EXIT_STUB = "edit_in_exit_stub"
    EDIT_ON_INSN_BOUNDARY = "edit_on_insn_boundary"
    EDIT_INSIDE_LOG_BLOCK = "edit_inside_log_block"  # close-out: the coverage LOG_REG block
    HOST_ABS_ALIASES_ENV = "host_abs_aliases_env"
    OBSERVER_CONTRACT_UNKNOWN = "observer_contract_unknown"
    VALIDATION_INCONCLUSIVE = "validation_inconclusive"
    VALIDATION_MISMATCH = "validation_mismatch"
    NO_SAME_LENGTH_ENCODING = "no_same_length_encoding"
    CLASS_NOT_SELECTED = "class_not_selected"  # WP25: excluded by --param classes (ablation)
    # out of scope in v1 (§7.3 "Not in v1")
    NEEDS_SLOWPATH_COMPENSATION = "needs_slowpath_compensation"
    CROSS_TB_DSE_NEEDS_VALUE_TRANSPORT = "cross_tb_dse_needs_value_transport"
    NEEDS_REGALLOC = "needs_regalloc"
    NEEDS_SPLICER = "needs_splicer"

    FAILED = frozenset({"validation_mismatch", "host_abs_aliases_env", "edit_inside_ldst_block",
                        "recovery_rows_changed", "edit_inside_log_block"})
    UNKNOWN = frozenset({"validation_inconclusive", "path_budget_exhausted",
                         "observer_contract_unknown"})

    @classmethod
    def all(cls) -> frozenset[str]:
        return frozenset(v for k, v in vars(cls).items() if k.isupper() and isinstance(v, str))


@dataclass(frozen=True)
class TrackedBytes:
    """Bit index of every tracked env byte (the D1/D2/D3 fields of the contract)."""

    index: Mapping[int, int]  # env offset -> bit
    field_of_byte: Mapping[int, str]  # env offset -> field
    span: Mapping[str, tuple[int, int]]  # field -> (offset, width)
    cls_mask: Mapping[str, int]  # D1/D2/D3 -> mask

    @classmethod
    def of(cls, layout: Any) -> "TrackedBytes":
        idx: dict[int, int] = {}
        fob: dict[int, str] = {}
        span: dict[str, tuple[int, int]] = {}
        cm = {"D1": 0, "D2": 0, "D3": 0}
        for c, fields in (("D1", D1_FIELDS), ("D2", D2_FIELDS), ("D3", D3_FIELDS)):
            for f in fields:
                if not layout.has_field(f):
                    continue
                off = layout.offset(f)
                w = layout.field(f).size if hasattr(layout, "field") else 4
                span[f] = (off, w)
                for b in range(off, off + w):
                    if b not in idx:
                        idx[b] = len(idx)
                    fob[b] = f
                    cm[c] |= 1 << idx[b]
        return cls(idx, fob, span, cm)

    @property
    def all(self) -> int:
        return (1 << len(self.index)) - 1

    def mask(self, off: int, width: int) -> int:
        m = 0
        for b in range(off, off + width):
            i = self.index.get(b)
            if i is not None:
                m |= 1 << i
        return m


@dataclass(frozen=True)
class DeadStore:
    """A deletable store: ``old`` at ``[off, off+size)`` is replaced by one NOP."""

    op: int
    off: int
    size: int
    field: str
    cls: str  # D1 | D2 | D3
    origin: str | None
    insn: int | None
    block: str
    old: bytes
    text: str

    @property
    def d2_kind(self) -> str | None:
        """``D2a`` (a prefix/body ``can_do_io`` store) or ``D2b`` (a ``true`` store whose
        next observer would be a direct-chain exit); None for D1/D3."""
        if self.cls != "D2":
            return None
        return "D2a"

    def to_json(self) -> dict[str, Any]:
        return {"op": self.op, "off": self.off, "size": self.size, "field": self.field,
                "cls": self.cls, "origin": self.origin, "insn": self.insn, "block": self.block,
                "old": self.old.hex(), "text": self.text}


@dataclass(frozen=True)
class Refusal:
    op: int
    off: int
    field: str
    cls: str | None
    reason: str
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"op": self.op, "off": self.off, "field": self.field, "cls": self.cls,
                "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class SyncDead:
    uid: str
    status: str  # ok | skipped
    reason: str | None
    detail: str
    mode: str
    tb_start_cb_possible: bool
    call_reads_all: bool
    n_paths: int | None = None
    d3_all_calls: bool = False
    dead: tuple[DeadStore, ...] = ()
    refused: tuple[Refusal, ...] = ()
    counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def dead_of(self, classes: Iterable[str] = ("D1", "D2", "D3")) -> tuple[DeadStore, ...]:
        cs = set(classes)
        return tuple(d for d in self.dead if d.cls in cs)

    def content_digest(self) -> str:
        import hashlib
        import json

        return "sha256:" + hashlib.sha256(json.dumps(
            [self.uid, self.status, self.reason, self.mode, self.tb_start_cb_possible,
             self.call_reads_all, self.d3_all_calls, [d.to_json() for d in self.dead],
             [r.to_json() for r in self.refused]], sort_keys=True).encode()).hexdigest()

    def to_json(self) -> dict[str, Any]:
        return {"uid": self.uid, "status": self.status, "reason": self.reason,
                "detail": self.detail, "mode": self.mode,
                "tb_start_cb_possible": self.tb_start_cb_possible,
                "call_reads_all": self.call_reads_all, "d3_all_calls": self.d3_all_calls,
                "n_paths": self.n_paths,
                "dead": [d.to_json() for d in self.dead],
                "refused": [r.to_json() for r in self.refused], "counts": dict(self.counts)}


# ---------------------------------------------------------------------------
# the dataflow
# ---------------------------------------------------------------------------

_PLAIN_STORE = (OpKind.ENV_STORE_IMM, OpKind.ENV_STORE_REG)


def _skip(uid: str, reason: str, detail: str, mode: str, c: ObsContract, cra: bool
          ) -> SyncDead:
    return SyncDead(uid, "skipped", reason, detail, mode, c.tb_start_cb_possible, cra,
                    counts={f"unit:{reason}": 1})


def _observers(h: UnitHostOps, g: HCfg, tb: TrackedBytes, c: ObsContract, *, mode: str,
               call_reads_all: bool, d3_all_calls: bool = False) -> list[int]:
    """Bytes observed at each op (the sync events of §7.1 for each class)."""
    D1, D2, D3 = tb.cls_mask["D1"], tb.cls_mask["D2"], tb.cls_mask["D3"]
    tcg = mode == "tcg"
    obs = [0] * len(h.ops)
    targets = {o.target for o in h.ops if o.target is not None}
    for i, o in enumerate(h.ops):
        m = 0
        ek = g.exits.get(i)
        if o.kind == OpKind.CALL and o.block != "ldst_slow":
            cc = call_class(o.helper_name, o.helper_flags)
            if tcg:
                # TCG's own set: globals synced before every call that is not NO_RWG
                if cc not in ("pure", "raising"):
                    m |= D1
            elif cc in ("lookup", "global", "plugin", "noreturn", "raising"):
                m |= D1  # S3, S4, S6 (= S4)
            s4 = cc in ("global", "plugin", "noreturn", "raising")  # S4, S6 (= S4)
            if call_reads_all or s4:
                m |= D2  # survey §3c B4-2 (1): every call reads can_do_io
            if d3_all_calls or s4:
                m |= D3  # obs-contract@1: S4/S6 (plugin callbacks are S4)
        if ek is not None and o.kind != OpKind.CALL:
            k = exit_class(ek)
            m |= D1  # S2: every exit (main loop or chained successor's S1)
            direct = k not in MAIN_LOOP_EXITS
            if direct and c.tb_start_cb_possible:
                m |= D2  # the can_do_io direct-chain rule (R5)
        if o.kind == OpKind.GUEST_ACCESS:
            m |= D1 | D2  # S5
        if tcg and o.block in ("insn", "prefix") and ek is None:
            if o.kind in (OpKind.JCC, OpKind.JMP) and \
                    not {"LDST_FAST_JCC", "LDST_RET_JMP"} & set(o.sites):
                m |= D1  # P: brcond / br
            if o.off in targets and i != 0:
                m |= D1  # P: set_label
        obs[i] = m
    return obs


def _gen_kill(o: Any, tb: TrackedBytes) -> tuple[int, int, bool]:
    """(gen, kill, unknown) of the op's own env accesses."""
    gen = kill = 0
    unknown = False
    for a in o.env:
        if not a.classified:
            unknown = True
            continue
        m = tb.mask(a.disp, a.width)  # env-relative bytes (env, neg.* and cpu.* regions)
        if "r" in a.rw:
            gen |= m
        if "w" in a.rw:
            kill |= m
    if o.kind in (OpKind.HOST_ABS_STORE, OpKind.HOST_ABS_LOAD) and o.abs_addr is None:
        unknown = True
    return gen, kill, unknown


def syncdead_unit(u: Any, h: UnitHostOps, g: HCfg, layout: Any, contract: ObsContract, *,
                  mode: str = "b4", call_reads_all: bool = True, d3_all_calls: bool = False,
                  max_paths: int = MAX_PATHS, max_candidates: int = MAX_CANDIDATES,
                  tracked: TrackedBytes | None = None, skip_special: bool = True) -> SyncDead:
    """Dead deletable-class stores of unit ``u`` (see the module doc).

    ``skip_special=False`` analyses ``special_cflags_unit`` units too; it exists
    only to compare with the model population (map-state-sync-traffic §7, all
    units). The pass never edits such units."""
    R = B4Reason
    if mode not in ("b4", "tcg"):
        raise ValueError(f"mode {mode!r}")
    c = contract
    cra = call_reads_all
    if getattr(u, "meta_status", "ok") != "ok":
        return _skip(u.uid, R.UNIT_NOT_RELOCATABLE, f"meta_status {u.meta_status}", mode, c, cra)
    if not u.reloc.relocatable:
        return _skip(u.uid, R.UNIT_NOT_RELOCATABLE, ", ".join(u.reloc.reasons), mode, c, cra)
    if skip_special and u.key.cflags & SPECIAL_CFLAGS:
        return _skip(u.uid, R.SPECIAL_CFLAGS_UNIT, f"cflags {u.key.cflags:#x}", mode, c, cra)
    if not h.ok:
        r0 = (h.reasons[0] if h.reasons else "hostops_pinned").split("@", 1)[0]
        return _skip(u.uid, r0, ", ".join(h.reasons[:4]), mode, c, cra)
    if not g.ok:
        return _skip(u.uid, R.UNIT_NOT_RELOCATABLE, "hcfg: " + ", ".join(g.reasons[:4]), mode,
                     c, cra)
    if g.cyclic:
        return _skip(u.uid, R.PATH_BUDGET_EXHAUSTED, "cyclic host graph", mode, c, cra)
    ps = g.paths(max_paths=max_paths)
    if ps is None:
        return _skip(u.uid, R.PATH_BUDGET_EXHAUSTED, f"> {max_paths} paths", mode, c, cra)
    tb = tracked or TrackedBytes.of(layout)
    ALL = tb.all
    ops = h.ops
    n = len(ops)
    obs = _observers(h, g, tb, c, mode=mode, call_reads_all=cra, d3_all_calls=d3_all_calls)
    gk = [_gen_kill(o, tb) for o in ops]
    # backward may-need, reverse topological order on the (acyclic) atomic graph
    order: list[int] = []
    seen = [False] * n
    stack: list[tuple[int, int]] = [(0, 0)] if n else []
    if n:
        seen[0] = True
    while stack:
        i, k = stack[-1]
        succ = g.succ_atomic[i]
        if k < len(succ):
            stack[-1] = (i, k + 1)
            j = succ[k]
            if not seen[j]:
                seen[j] = True
                stack.append((j, 0))
        else:
            order.append(i)  # post-order: successors first
            stack.pop()
    need_in = [0] * n
    need_out = [0] * n
    for i in order:
        succ = g.succ_atomic[i]
        if succ:
            out = 0
            for j in succ:
                out |= need_in[j]
        elif i in g.exits:
            out = 0  # nothing runs after an exit in this unit (observer bytes are in obs)
        else:
            out = ALL  # falls off the graph: fail closed
        need_out[i] = out
        gen, kill, unknown = gk[i]
        nin = (out & ~kill) | gen | obs[i]
        if unknown:
            nin = ALL
        need_in[i] = nin
    reach = set(order)

    # -- candidates ------------------------------------------------------------------
    insn_ranges = [(x.host_start, x.host_end) for x in getattr(u, "insns", ())]
    unit_has_mem_cb = any(o.is_env_store and o.store_field == "neg.plugin_mem_cbs" for o in ops)
    stub_start = getattr(u, "stub_start", None)
    dead: list[DeadStore] = []
    refused: list[Refusal] = []
    counts: Counter[str] = Counter()
    n_cand = 0
    for i, o in enumerate(ops):
        if not o.is_env_store:
            continue
        acc = next((a for a in o.env if "w" in a.rw), None)
        fld = acc.field if acc is not None else "?"
        cls = c.deletable_class(fld)
        if cls is None:
            counts[f"store:{c.not_deletable_reason(fld) or R.FIELD_NOT_DELETABLE_CLASS}"] += 1
            if fld in D3_FIELDS:  # D3 without the RuntimeIdentity fact
                refused.append(Refusal(i, o.off, fld, "D3", R.OBSERVER_CONTRACT_UNKNOWN,
                                       "mem_get_value_outside_mem_cb is not false"))
            continue
        n_cand += 1

        def refuse(reason: str, detail: str = "") -> None:
            counts[f"{cls}:{reason}"] += 1
            refused.append(Refusal(i, o.off, fld, cls, reason, detail))

        span = tb.span.get(fld)
        if o.kind not in _PLAIN_STORE or acc is None or span is None or \
                not (span[0] <= acc.disp and acc.disp + acc.width <= span[0] + span[1]) or \
                any("w" in a.rw and a is not acc for a in o.env):
            refuse(R.FIELD_NOT_DELETABLE_CLASS, f"not a plain store inside one field: {o.text}")
            continue
        if i not in reach:
            refuse(R.NO_DEAD_STORE, "unreachable in the atomic graph")
            continue
        if o.log is not None or h.log_block_at(o.off) is not None:
            refuse(R.EDIT_INSIDE_LOG_BLOCK, "inside the coverage LOG_REG block")
            continue
        if o.block == "prefix":
            if fld != "neg.can_do_io" or o.origin != "cdio_prefix":
                refuse(R.EDIT_IN_PREFIX)
                continue
        elif o.block in ("ldst_fast", "ldst_slow"):
            refuse(R.EDIT_INSIDE_LDST_BLOCK)
            continue
        elif o.block != "insn":
            refuse(R.EDIT_IN_EXIT_STUB, o.block)
            continue
        if stub_start is not None and o.end > stub_start:
            refuse(R.EDIT_IN_EXIT_STUB, "guard stub")
            continue
        if o.block == "insn" and insn_ranges and \
                not any(a <= o.off and o.end <= b for a, b in insn_ranges):
            refuse(R.EDIT_ON_INSN_BOUNDARY)
            continue
        if cls == "D3" and unit_has_mem_cb:
            refuse(R.OBSERVER_CONTRACT_UNKNOWN, "memory callback in the unit (plugin_mem_cbs)")
            continue
        m = tb.mask(acc.disp, acc.width)
        if need_out[i] & m:
            refuse(R.OBSERVED_AT_SYNC_POINT)
            continue
        if nop_bytes(o.size) is None:
            refuse(R.NO_SAME_LENGTH_ENCODING, f"{o.size} bytes")
            continue
        old = bytes(u.code[o.off:o.end])
        dead.append(DeadStore(i, o.off, o.size, fld, cls, o.origin, o.insn, o.block, old, o.text))
        counts[f"{cls}:dead"] += 1
    if n_cand > max_candidates:
        return _skip(u.uid, R.PATH_BUDGET_EXHAUSTED, f"{n_cand} candidate stores > "
                     f"{max_candidates}", mode, c, cra)
    if not dead:
        counts[f"unit:{R.NO_DEAD_STORE}"] += 1
    return SyncDead(u.uid, "ok", None if dead else R.NO_DEAD_STORE, "", mode,
                    c.tb_start_cb_possible, cra, len(ps), d3_all_calls, tuple(dead),
                    tuple(refused), dict(sorted(counts.items())))


def syncdead_of(u: Any, layout: Any, code_address: int, contract: ObsContract, **kw: Any
                ) -> SyncDead:
    """``syncdead_unit`` with HostOps and hcfg built here (no AnalysisStore)."""
    h = build_hostops(u, layout, code_address)
    g = build_hcfg(h)
    return syncdead_unit(u, h, g, layout, contract, **kw)


def census_syncdead(units: Iterable[Any], layout: Any, code_address: int,
                    contract: ObsContract, **kw: Any) -> dict[str, Any]:
    """Static counts over a snapshot (T-B4-1): dead stores per class, D2 by
    position/value, unit skips, refusal reasons."""
    tb = TrackedBytes.of(layout)
    per: Counter[str] = Counter()
    d2: Counter[str] = Counter()
    units_with: Counter[str] = Counter()
    skips: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    n = 0
    rows = []
    for u in units:
        n += 1
        r = syncdead_of(u, layout, code_address, contract, tracked=tb, **kw)
        rows.append(r)
        if not r.ok:
            skips[r.reason or "?"] += 1
            continue
        for k, v in r.counts.items():
            if ":" in k and not k.startswith("unit:"):
                reasons[k] += v
        cls_here = set()
        for d in r.dead:
            per[d.cls] += 1
            cls_here.add(d.cls)
            if d.cls == "D2":
                d2["prefix" if d.block == "prefix" else "body"] += 1
                v = d.text.rsplit(",", 1)[-1].strip()
                d2["value:" + (v if v in ("0", "1") else "reg")] += 1
        for cl in cls_here:
            units_with[cl] += 1
    return {"units": n, "mode": kw.get("mode", "b4"),
            "call_reads_all": kw.get("call_reads_all", True),
            "d3_all_calls": kw.get("d3_all_calls", False),
            "tb_start_cb_possible": contract.tb_start_cb_possible,
            "dead": {k: per.get(k, 0) for k in ("D1", "D2", "D3")},
            "d2_split": dict(sorted(d2.items())), "units_with": dict(units_with),
            "unit_skips": dict(skips.most_common()), "store_reasons": dict(sorted(reasons.items())),
            "_rows": rows}


# ---------------------------------------------------------------------------
# analysis registration
# ---------------------------------------------------------------------------

def _compute(ctx: Any, scope: ScopeRef) -> SyncDead:
    lay = ctx.analysis("envlayout", ScopeRef("snapshot", ctx.snapshot_id)).value.get("layout")
    con = ctx.analysis("obs-contract", ScopeRef("snapshot", ctx.snapshot_id)).value
    h = ctx.analysis("hostops", scope).value
    g = ctx.analysis("hcfg", scope).value
    u = ctx.unit(scope.id)
    return syncdead_unit(u, h, g, lay, con)


SYNCDEAD = AnalysisSpec(name="syncdead", version=1, scope="unit",
                        requires=(("envlayout", 1), ("obs-contract", 1), ("hostops", 1),
                                  ("hcfg", 1)),
                        inputs=frozenset({"code", "meta"}), compute=_compute,
                        result_schema=SyncDead)
ANALYSES = (SYNCDEAD,)

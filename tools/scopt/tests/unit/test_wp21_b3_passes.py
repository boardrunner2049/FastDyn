"""WP21 unit tests: hconst@1, the six B3 passes, the B3 manager overlay, T-B3-7, T-B3-41.

Inputs (read-only, tests skip when absent):

* the WP24 fixture F-B3-1 base snapshot captured by WP20's integration stage
  (``runs/wp20-int/fx/icount/1``: R = ``adds r0,r0,#1; bx lr`` at 0x10000 with the
  patch-plan modifier ``r0 <- 0`` on ``bx lr``, C = the 1-insn ``cbz r0`` TB at 0x2f4,
  steady profile with the R -> C lookup edge);
* WP18's stand-in v6 RuntimeIdentity and WP19's hostsym self-test document for the
  packaging test (as WP19's integration stage used them).

"Synthetic" fixtures (T-B3-41) are derived from these real units: other facts, a profile
without lookup edges, records with registered slots.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scopt.analysis.hconst import (_add_flags, _logic_flags, build_hconst, jcc_condition)
from scopt.manager_b3 import (B3Env, B3State, LINK_FAILED_CODES, VariantPlan, VariantView,
                              b3_reject_outcome, materialize)
from scopt.passes.base import B3Reason, EditKind, FactRef, Outcome, Patch
from scopt.passes._b3util import mov_imm32, nops, refusal_result, xor_zero
from scopt.passes.branch_folding import BranchFolding, fold_jcc
from scopt.passes.constant_propagation import ConstantPropagation
from scopt.passes.dead_host_ops import DeadHostOps
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()
FX1 = ROOT / "runs/wp20-int/fx/icount/1"
FX1_SNAP = FX1 / "store/snapshots/7b2d553cc9c895236c5ed2f037ad120367284334cb186769478a620f4499e959"
FX1_PROFILE = FX1 / "prof/merged.profile.json"
SELFTEST = ROOT / "runs/wp19-int/rerun-3d5250f/selftest.json"
R_VADDR, C_VADDR = 0x10000, 0x2F4
SIX = set(Outcome)
B3_LINK_PASSES = ("specialize-returns", "chain-returns", "chain-static")
_CACHE: dict = {}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def fx1():
    if not (FX1_SNAP / "snapshot.json").is_file() or not FX1_PROFILE.is_file():
        pytest.skip(f"{FX1_SNAP} not present")
    if "s" not in _CACHE:
        from scopt.analysis.b3_inputs import resolve_for_snapshot
        from scopt.formats.profile_v1 import ProfileView, read_profile
        from scopt.importer.import_cache import open_snapshot

        s = open_snapshot(FX1_SNAP)
        resolve_for_snapshot(s, prep=str(FX1 / "prep"))
        _CACHE["s"] = s
        _CACHE["pv"] = ProfileView.open(read_profile(FX1_PROFILE), s)
    return _CACHE["s"], _CACHE["pv"]


def unit_at(s, vaddr):
    return next(u for u in s.units() if u.vaddrs and u.vaddrs[0] == vaddr)


def layout(s):
    from scopt.formats.envlayout_v1 import find_envlayout

    return find_envlayout(s.runtime["qemu_sha256"])


def run_b3(s, pv, *, disable=(), only=None, params=None, profiles=None, pipeline="b3"):
    from scopt.manager import PassManager

    pm = PassManager(s, pipeline, [pv] if profiles is None else profiles, params,
                     b3=B3Env(), enable=B3_LINK_PASSES, disable=disable, only=only)
    return pm, pm.execute()


def fake_factref(field="r0", value=0, tag="x"):
    h = "sha256:" + hashlib.sha256(tag.encode()).hexdigest()
    return FactRef(h, field, value, 0, "patch", h, h)


def variant_view(s, c, facts, code=None):
    plan = VariantPlan("sha256:" + "ab" * 32, c.uid, ("tb:" + "0" * 32 + "#0", 0), "x0",
                       tuple(fake_factref(k, v, f"{k}{v}") for k, v in facts.items()), (),
                       bytes(c.code if code is None else code), (), "test", 1000)
    return VariantView(plan.clone_id, plan, c, layout(s), s.header.code_address)


class _Ctx:
    """The PassContext surface the region passes use (analysis reads are recorded only)."""

    def __init__(self, params=None):
        self.params = params or {}

    def analysis(self, *a, **k):
        return None

    def budget(self):
        return None


def builder_for(descriptor, vid):
    from scopt.proposal import ProposalBuilder

    view = SimpleNamespace(variant_ids=lambda: (vid,))
    return ProposalBuilder(descriptor, view, enabled={EditKind.PURE_REWRITE})


def run_region(cls, vv, params=None):
    b = builder_for(cls.descriptor, vv.variant_id)
    res = cls().run(vv, _Ctx(params), b)
    return res, [e.payload for e in b.edits]


# ---------------------------------------------------------------------------
# encodings and the flag model
# ---------------------------------------------------------------------------

def test_encodings():
    assert xor_zero("ebx") == bytes.fromhex("31db")
    assert xor_zero("r12d") == bytes.fromhex("4531e4")
    assert mov_imm32("eax", 5) == bytes.fromhex("b805000000")
    assert mov_imm32("r9d", 1) == bytes.fromhex("41b901000000")
    assert nops(3) == bytes.fromhex("669090") and nops(6) == bytes.fromhex("669066906690")
    assert fold_jcc(bytes.fromhex("0f851c000000"), False) == (nops(6), "bf_jcc_to_nop")
    assert fold_jcc(bytes.fromhex("0f851c000000"), True) == (bytes.fromhex("e91d00000090"),
                                                              "bf_jcc_to_jmp")
    assert fold_jcc(bytes.fromhex("7405"), True) == (bytes.fromhex("eb05"), "bf_jcc_to_jmp")
    assert fold_jcc(bytes.fromhex("7405"), False) == (nops(2), "bf_jcc_to_nop")
    assert fold_jcc(bytes.fromhex("e905000000"), True) is None


def test_flag_model():
    r, f = _add_flags(0, 1, 32, True)  # cmp 0, 1
    assert r == 0xFFFFFFFF and f["CF"] and f["SF"] and not f["ZF"] and not f["OF"]
    r, f = _add_flags(0x7FFFFFFF, 1, 32, False)  # signed overflow
    assert f["OF"] and f["SF"] and not f["CF"]
    f = _logic_flags(0, 32)
    assert f["ZF"] and not f["SF"] and not f["CF"] and not f["OF"]
    assert jcc_condition("jne", {"ZF": True}) is False
    assert jcc_condition("je", {"ZF": True}) is True
    assert jcc_condition("jl", {"SF": True, "OF": False}) is True
    assert jcc_condition("jg", {"ZF": False}) is None  # SF/OF unknown
    assert jcc_condition("cmovae", {"CF": False}) is True


# ---------------------------------------------------------------------------
# hconst on the real F-B3-1 continuation
# ---------------------------------------------------------------------------

def test_hconst_fb3_1():
    from scopt.analysis.hcfg import build_hcfg
    from scopt.analysis.hostops import build_hostops

    s, _ = fx1()
    c = unit_at(s, C_VADDR)
    h = build_hostops(c, layout(s), s.header.code_address)
    g = build_hcfg(h)
    none = build_hconst(h, g, {}, code=c.code)
    assert none.ok and not none.decided and not none.foldable and not none.useful
    z = build_hconst(h, g, {"r0": 0}, code=c.code)
    assert dict(z.decided) == {8: False}  # jne after test ebx,ebx: not taken
    assert dict(z.foldable) == {6: ("r0", 0, "ebx")}
    assert 16 not in z.reachable  # the jne target (r0 != 0 path) is unreachable
    five = build_hconst(h, g, {"r0": 5}, code=c.code)
    assert dict(five.decided) == {8: True}
    other = build_hconst(h, g, {"r1": 0}, code=c.code)
    assert not other.useful  # fact_unused
    with pytest.raises(ValueError):
        build_hconst(h, g, {"pc": 0}, code=c.code)


def test_hconst_kill_rules():
    """A flags-0 call (or a plugin callback) between two loads kills the fact."""
    from scopt.analysis.hcfg import build_hcfg
    from scopt.analysis.hostops import EnvAccess, HostOp, OpKind, UnitHostOps

    code = bytes.fromhex("8b5d00" "e800000000" "8b5d00" "e900000000")
    env = (EnvAccess("regs[0]", 0, 4, "r", True),)
    rd = frozenset({"rbp"})
    ops = (HostOp(0, 3, "mov ebx, dword ptr [rbp]", OpKind.ENV_LOAD, "insn", 0, env=env,
                  dst_reg="ebx", regs_read=rd, regs_written=frozenset({"rbx"})),
           HostOp(3, 5, "call", OpKind.CALL, "insn", 0, helper_id=1, helper_name="helper_x",
                  helper_flags=0, sites=("HELPER_CALL",), site_auxs=(1,)),
           HostOp(8, 3, "mov ebx, dword ptr [rbp]", OpKind.ENV_LOAD, "insn", 0, env=env,
                  dst_reg="ebx", regs_read=rd, regs_written=frozenset({"rbx"})),
           HostOp(11, 5, "jmp", OpKind.EXIT_JMP, "exit_stub", 0, sites=("EXIT_TB_EPI_JMP",),
                  site_auxs=(0,)))
    h = UnitHostOps("tb:" + "1" * 32 + "#0", "ok", (), ops, (), 0, 16, 16, None, 16, 1, 0, None)
    g = build_hcfg(h)
    hc = build_hconst(h, g, {"r0": 0}, code=code)
    assert set(hc.foldable) == {0}  # only the load before the call
    ops2 = tuple(replace(o, helper_flags=0x2 | 0x1) if o.kind == OpKind.CALL else o for o in ops)
    hc2 = build_hconst(replace(h, ops=ops2), build_hcfg(replace(h, ops=ops2)), {"r0": 0},
                       code=code)
    assert set(hc2.foldable) == {0}  # a NO_WG helper without an effect-table row: fail closed
    from scopt.analysis.helper_effects import effects_for_layout
    from scopt.formats.envlayout_v1 import find_envlayout

    fx = effects_for_layout(find_envlayout(
        "sha256:56d8bfb32c91c970fe0a16237b962c02791ce0a7fcc52bf6246343fbef430d90"))
    ops4 = tuple(replace(o, helper_name="lookup_tb_ptr", helper_flags=0x6)
                 if o.kind == OpKind.CALL else o for o in ops)
    hc4 = build_hconst(replace(h, ops=ops4), build_hcfg(replace(h, ops=ops4)), {"r0": 0},
                       code=code, effects=fx)
    assert set(hc4.foldable) == {0, 2}  # a NO_WG helper whose row writes no global keeps it
    ops3 = tuple(replace(o, helper_name=None) if o.kind == OpKind.CALL else o for o in ops2)
    hc3 = build_hconst(replace(h, ops=ops3), build_hcfg(replace(h, ops=ops3)), {"r0": 0},
                       code=code)
    assert set(hc3.foldable) == {0}  # a plugin callback kills it whatever its flags


# ---------------------------------------------------------------------------
# the pipeline on F-B3-1 (§3.5 expected bytes) and T-B3-6 static separation
# ---------------------------------------------------------------------------

def _variant_patches(pm, cand):
    m = materialize(cand.b3_state, pm.b3ctx)
    (vid, ps), = m.composed.items()
    return m, {p.host_off: (p.old_bytes.hex(), p.new_bytes.hex()) for p in ps}


def test_fb3_1_pipeline_expected_bytes():
    s, pv = fx1()
    pm, cand = run_b3(s, pv)
    out = {p.name: p for p in cand.passes}
    for p in cand.passes:
        assert p.outcome in SIX
    assert out["specialize-returns"].outcome is Outcome.CANDIDATE
    for n in ("constant-propagation", "branch-folding", "dead-host-ops"):
        assert out[n].outcome is Outcome.CANDIDATE, (n, out[n].reason, out[n].detail)
    st = cand.b3_state
    (v,) = st.variants.values()
    r, c = unit_at(s, R_VADDR), unit_at(s, C_VADDR)
    assert v.src_uid == c.uid and v.pred == (r.uid, 0) and v.fact_map == {"r0": 0}
    assert [f.source for f in v.facts] == ["patch"]
    m, ps = _variant_patches(pm, cand)
    # CP: mov ebx,[rbp] -> xor ebx,ebx; nop, then DHO nops the dead xor; BF: jne -> nop;
    # DHO: the dead test -> nop (design §3.5 "Expected F-B3-1 deltas")
    assert ps == {0x15: ("8b5d00", "669090"), 0x18: ("85db", "6690"),
                  0x1A: ("0f851c000000", "669066906690")}
    assert m.pass_bytes == {"constant-propagation": 3, "branch-folding": 6, "dead-host-ops": 4}
    assert not m.downgraded and len(m.regions) == 1
    # ledger rows carry the per-pass counts
    rows = {r.id: r for r in cand.ledger if r.subject == "pass"}
    assert rows["constant-propagation@1"].counts["bytes_changed"] == 3
    assert rows["specialize-returns@1"].counts["edits:CLONE"] == 1


def test_t_b3_6_static_pass_separation():
    """CP, BF, DHO each disabled in turn: distinct non-zero byte deltas; all three -> GENERAL."""
    s, pv = fx1()
    res = {}
    for off in ("constant-propagation", "branch-folding", "dead-host-ops"):
        pm, cand = run_b3(s, pv, disable=(off,))
        m, ps = _variant_patches(pm, cand)
        res[off] = (ps, m.pass_bytes)
        assert off not in m.pass_bytes and all(v > 0 for v in m.pass_bytes.values())
    assert res["constant-propagation"][0] == {0x18: ("85db", "6690"),
                                              0x1A: ("0f851c000000", "669066906690")}
    assert res["branch-folding"][0] == {0x15: ("8b5d00", "31db90")}
    assert res["dead-host-ops"][0] == {0x15: ("8b5d00", "31db90"),
                                       0x1A: ("0f851c000000", "669066906690")}
    keys = [json.dumps(sorted(v[0].items())) for v in res.values()]
    assert len(set(keys)) == 3
    pm, cand = run_b3(s, pv, disable=("constant-propagation", "branch-folding",
                                      "dead-host-ops"))
    m = materialize(cand.b3_state, pm.b3ctx)
    assert [d["reason"] for d in m.downgraded] == ["no_simplification"] and not m.composed
    r = unit_at(s, R_VADDR)
    kinds = [t.kind for g in m.plan.grow[r.uid] for t in g.targets]
    assert kinds == ["GENERAL"]
    with_bf_off = {p.name: p for p in run_b3(s, pv, disable=("branch-folding",))[1].passes}
    assert with_bf_off["dead-host-ops"].reason == "flags_live"


def test_fb3_1_package_static_acceptance(tmp_path, monkeypatch):
    """T-B3-6 static part: the F-B3-1 B3 artifact is accepted (V1-V21, V23)."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests.synth import b3 as H
    from scopt.manager_b3 import optimize_b3

    s, pv = fx1()
    if not SELFTEST.is_file():
        pytest.skip("no hostsym self-test document")
    ri6 = H.v6_identity(monkeypatch)
    st = json.loads(SELFTEST.read_text())
    from scopt.validate_sym.selftest import kernel_id

    if st.get("kernel_id") != kernel_id():
        pytest.skip("stale self-test document")
    r = optimize_b3(s, "b3", tmp_path, env=B3Env(runtime=ri6, v6_exes=(H.QINT_V6_SHA,),
                                                 selftest=st), profiles=[pv], enable=B3_LINK_PASSES)
    pk = r["package"]
    assert r["path"] == "b3" and pk.status == "published", (pk.reason, pk.rejection_path)
    codes = pk.validation.codes()
    for k in [f"V{i}" for i in range(1, 22) if i not in (9, 10)] + ["V23"]:
        assert codes[k] == "pass", (k, codes)
    assert codes["V22"] == "skip"  # WP23 not approved
    side = json.loads((Path(pk.artifact_dir) / "cache.sc.scopt.json").read_text())
    assert side["loader_requirement"] == "qemu-sc-v6"
    rep = side["x_b3_manager"]
    assert {p["pass"]: p["byte_delta"] for p in rep["passes"]}["branch-folding"] == 6
    regions = [json.loads(x) for x in (Path(pk.artifact_dir) / "regions.jsonl").open()]
    assert sum(1 for x in regions if x["kind"] == "multi_tb") == 1


# ---------------------------------------------------------------------------
# T-B3-7: identity and all-no_change pipelines are sha256-identical (V9)
# ---------------------------------------------------------------------------

def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.mark.parametrize("how", ["identity", "all_disabled", "only_region_pass", "no_profile"])
def test_t_b3_7_identity(tmp_path, how):
    from scopt.manager_b3 import optimize_b3

    s, pv = fx1()
    kw = {"identity": dict(pipeline="identity"),
          "all_disabled": dict(pipeline="b3", disable=(
              "specialize-returns", "constant-propagation", "branch-folding", "dead-host-ops",
              "chain-returns", "chain-static")),
          "only_region_pass": dict(pipeline="b3", only="constant-propagation"),
          "no_profile": dict(pipeline="b3", profiles=[], enable=B3_LINK_PASSES)}[how]
    pipeline = kw.pop("pipeline")
    profiles = kw.pop("profiles", [pv])
    r = optimize_b3(s, pipeline, tmp_path, env=B3Env(), profiles=profiles, **kw)
    c = r["candidate"]
    assert all(p.outcome in (Outcome.NO_CHANGE, Outcome.SKIPPED_UNSUPPORTED,
                             Outcome.SKIPPED_UNKNOWN) for p in c.passes), \
        [(p.name, p.outcome) for p in c.passes]
    assert r["path"] == "v5_identity"
    pk = r["package"]
    assert pk.status == "published", pk.reason
    assert pk.validation.codes()["V9"] == "pass"
    assert _sha(Path(pk.artifact_dir) / "cache.sc") == _sha(Path(FX1_SNAP) / "cache.sc")


# ---------------------------------------------------------------------------
# T-B3-41: a rejected (synthetic) fixture per pass, with its stable reason
# ---------------------------------------------------------------------------

def test_t_b3_41_cp_no_same_length_encoding():
    s, _ = fx1()
    c = unit_at(s, C_VADDR)
    res, eds = run_region(ConstantPropagation, variant_view(s, c, {"r0": 5}))
    assert res.outcome is Outcome.SKIPPED_UNSUPPORTED and res.reason == "no_same_length_encoding"
    assert not eds


def test_t_b3_41_bf_no_decided_branch():
    s, _ = fx1()
    c = unit_at(s, C_VADDR)
    res, eds = run_region(BranchFolding, variant_view(s, c, {"r1": 0}))
    assert res.outcome is Outcome.SKIPPED_UNSUPPORTED and res.reason == "fact_unused"
    assert "no decided branch" in res.detail and not eds
    ok, eds = run_region(BranchFolding, variant_view(s, c, {"r0": 5}))
    assert ok.outcome is Outcome.CANDIDATE and eds[0].new_bytes.hex() == "e91d00000090"


def test_t_b3_41_dho_flags_live():
    s, _ = fx1()
    c = unit_at(s, C_VADDR)
    code = bytearray(c.code)
    code[0x15:0x18] = bytes.fromhex("31db90")  # CP applied, BF not: test's ZF feeds the jne
    res, eds = run_region(DeadHostOps, variant_view(s, c, {"r0": 0}, bytes(code)))
    assert res.outcome is Outcome.SKIPPED_UNSUPPORTED and res.reason == "flags_live"
    assert not eds


def test_t_b3_41_cp_flags_live_and_not_profitable():
    s, _ = fx1()
    c = unit_at(s, C_VADDR)
    vv = variant_view(s, c, {"r0": 0})
    vv.plan = replace(vv.plan, weight=0)
    res, eds = run_region(ConstantPropagation, vv)
    assert res.reason == "not_profitable" and not eds


def _no_lookup_profile(pv):
    q = copy.copy(pv)
    q._edges = [e for e in pv._edges if e["kind"] != "lookup_hit"]
    from collections import defaultdict

    q._edge_index = defaultdict(list)
    for e in q._edges:
        q._edge_index[(e["src"], e["dst"], e["phase"])].append(e)
    return q


def test_t_b3_41_chain_returns_target_not_profiled():
    s, pv = fx1()
    _pm, cand = run_b3(s, pv, only="chain-returns", profiles=[_no_lookup_profile(pv)])
    p = cand.outcome("chain-returns")
    assert p.outcome is Outcome.SKIPPED_UNSUPPORTED and p.reason == "target_not_profiled"
    assert cand.b3_state.empty


class _SnapProxy:
    """The real snapshot with replaced units (synthetic records)."""

    def __init__(self, s, units):
        self._s = s
        self._u = tuple(units)
        self._by = {u.uid: u for u in self._u}

    def __getattr__(self, k):
        return getattr(self._s, k)

    def units(self):
        return self._u

    def unit(self, uid):
        return self._by[uid]

    def by_key(self, key):
        return tuple(u for u in self._u if u.key == key)


def test_t_b3_41_chain_static_no_free_slot():
    from scopt.formats.sc_records import ScRecord
    from scopt.formats.profile_v1 import ProfileView, read_profile

    s, _ = fx1()
    units = []
    for u in s.units():
        r = ScRecord.from_bytes(u.record).replace(jmp_insn_offset=(0x20, 0x28),
                                                  jmp_reset_offset=(0x30, 0x38))
        units.append(replace(u, record=r.to_bytes(fix_checksum=True)))
    ps = _SnapProxy(s, units)
    pv = ProfileView.open(read_profile(FX1_PROFILE), ps)
    _pm, cand = run_b3(ps, pv, only="chain-static")
    p = cand.outcome("chain-static")
    assert p.outcome is Outcome.SKIPPED_UNSUPPORTED and p.reason == "no_free_slot", \
        (p.reason, p.detail)


def test_t_b3_41_specialize_returns_refusals():
    s, pv = fx1()
    _pm, cand = run_b3(s, pv, only="specialize-returns", profiles=[_no_lookup_profile(pv)])
    p = cand.outcome("specialize-returns")
    assert p.outcome is Outcome.SKIPPED_UNSUPPORTED
    assert p.stats.get("refused:target_not_profiled", 0) >= 1
    # budget 0 variants per C: context_budget
    _pm, cand = run_b3(s, pv, only="specialize-returns",
                       params={"specialize-returns": {"max_variants_per_c": 0}})
    p = cand.outcome("specialize-returns")
    # the most frequent refusal is the pass reason; the budget refusal is counted
    assert p.outcome is Outcome.SKIPPED_UNSUPPORTED and p.stats["refused:context_budget"] == 1
    assert p.reason == "fact_not_constant"  # 6 return exits without a certified fact


def test_indirect_multiway_is_never_guarded():
    """tbb / ldr pc,[rX] / bx rX exits are refused (§3.1 rule 2); WP21 defers promoting them."""
    s, pv = fx1()
    pm, cand = run_b3(s, pv)
    edges = pm.b3ctx.edges()
    ind = [x for x in edges.exits if x.exit_class == "indirect_multiway"]
    assert ind and all("indirect_multiway" in x.reason_codes for x in ind)
    grown = {u for u, _ in cand.b3_state.exits}
    assert not grown & {x.uid for x in ind if len(edges.exits_of(x.uid)) == 1}


# ---------------------------------------------------------------------------
# manager preconditions (§3.7) on the overlay
# ---------------------------------------------------------------------------

def _spec_edits():
    s, pv = fx1()
    pm, cand = run_b3(s, pv, only="specialize-returns")
    return pm, [e for e in cand.edits if e.pass_name == "specialize-returns"]


def test_manager_preconditions():
    pm, edits = _spec_edits()
    b3 = pm.b3ctx
    st0 = B3State()
    ok, ref = st0.apply("t", edits, b3)
    assert ref is None and len(ok.variants) == 1
    # SPEC slot without its CLONE/ADD_ENTRY
    bad = [e for e in edits if e.kind not in (EditKind.CLONE, EditKind.ADD_ENTRY)]
    _, ref = st0.apply("t", bad, b3)
    assert ref.code == "edit_precondition" and ref.outcome is Outcome.FAILED
    # stale fact (row hash not current)
    ef = [replace(e, payload=replace(e.payload, summary_row_hash="sha256:" + "0" * 64))
          if e.kind is EditKind.ATTACH_FACT else e for e in edits]
    _, ref = st0.apply("t", ef, b3)
    assert ref.code == "stale_fact" and ref.outcome is Outcome.FAILED
    # budgets
    _, ref = st0.apply("t", edits, b3, max_per_c=0)
    assert ref.code == "context_budget" and ref.outcome is Outcome.SKIPPED_UNSUPPORTED
    _, ref = st0.apply("t", edits, b3, max_growth=10)
    assert ref.code == "growth_budget"
    # the same slot twice
    general = [replace(e, payload=replace(e.payload, kind="GENERAL", variant_ref=None))
               for e in edits if e.kind is EditKind.REDIRECT_EXIT]
    _, ref = ok.apply("t", general, b3)
    assert ref.code == "no_free_slot"


def test_manager_rewrite_placement():
    pm, edits = _spec_edits()
    b3 = pm.b3ctx
    st, _ = B3State().apply("t", edits, b3)
    (vid, v) = next(iter(st.variants.items()))
    c = b3.unit(v.src_uid)

    def pr(off, n, new=None, tmpl="dho_nop", old=None):
        o = bytes(c.code[off:off + n]) if old is None else old
        p = Patch(vid, off, o, new if new is not None else nops(n), tmpl)
        return SimpleNamespace(kind=EditKind.PURE_REWRITE, payload=p, units=(),
                               affected_origins=(), dependency_additions=())

    ok, ref = st.apply("dead-host-ops", [pr(0x18, 2)], b3)
    assert ref is None and ok.variants[vid].code[0x18:0x1A] == nops(2)
    _, ref = st.apply("x", [pr(0x0, 3)], b3)
    assert ref.code == "edit_in_prefix"
    _, ref = st.apply("x", [pr(0x2B, 5)], b3)  # the goto_tb jmp in the exit stub
    assert ref.code == "edit_in_exit_stub"
    _, ref = st.apply("x", [pr(0x18, 2, old=b"\x90\x90")], b3)
    assert ref.code == "edit_precondition"
    _, ref = st.apply("x", [pr(0x16, 2)], b3)  # starts inside mov ebx,[rbp]
    assert ref.code == "edit_on_insn_boundary"
    # edits on clones compose: CP then DHO on the CP bytes -> one patch per original insn
    st2, ref = st.apply("constant-propagation", [pr(0x15, 3, bytes.fromhex("31db90"),
                                                    "cp_xor_zero")], b3)
    assert ref is None
    st3, ref = st2.apply("dead-host-ops", [pr(0x15, 2, nops(2), old=bytes.fromhex("31db"))],
                         b3)
    assert ref is None
    m = materialize(st3, b3)
    (ps,) = m.composed.values()
    assert [(p.host_off, p.old_bytes.hex(), p.new_bytes.hex(), p.template_id) for p in ps] == \
        [(0x15, "8b5d00", "669090", "dho_nop")]
    # a rewrite of the general C (e.g. B4) reaches its clone (NativeLinker v2 clones the
    # effective C); the variant's patches are then composed relative to the effective C
    unit_p = SimpleNamespace(kind=EditKind.PURE_REWRITE, units=(c.uid,), affected_origins=(),
                             dependency_additions=(),
                             payload=Patch(c.uid, 0x1A, bytes(c.code[0x1A:0x20]), nops(6),
                                           "b4_nop_store"))
    st4, ref = st3.apply("eliminate-state-sync", [unit_p], b3)
    assert ref is None and st4.variants[vid].code[0x1A:0x20] == nops(6)
    m4 = materialize(st4, b3)
    assert [p.host_off for p in m4.plan.rewrites[c.uid]] == [0x1A]
    (ps4,) = m4.composed.values()
    assert [p.host_off for p in ps4] == [0x15]
    # ... and a unit rewrite that overlaps a variant patch is a conflict (failed)
    unit_q = SimpleNamespace(kind=EditKind.PURE_REWRITE, units=(c.uid,), affected_origins=(),
                             dependency_additions=(),
                             payload=Patch(c.uid, 0x15, bytes(c.code[0x15:0x18]), nops(3),
                                           "b4_nop_store"))
    _, ref = st3.apply("eliminate-state-sync", [unit_q], b3)
    assert ref.code == "conflict" and ref.outcome is Outcome.FAILED


# ---------------------------------------------------------------------------
# outcomes: one of the six; unsupported/unknown never failed
# ---------------------------------------------------------------------------

def test_outcome_classes():
    for code in sorted(B3Reason.all()):
        o = B3Reason.outcome(code)
        assert o in SIX
        r = refusal_result(__import__("collections").Counter({code: 1}), nothing="x")
        assert r.outcome in SIX and r.reason == code
        if code not in B3Reason.FAILED:
            assert r.outcome is not Outcome.FAILED, code
    assert refusal_result(__import__("collections").Counter(), nothing="none").outcome \
        is Outcome.NO_CHANGE
    for c in LINK_FAILED_CODES:
        assert b3_reject_outcome(f"failed:{c}") is Outcome.FAILED
    assert b3_reject_outcome("certification_not_run") is Outcome.SKIPPED_UNKNOWN
    assert b3_reject_outcome("V12:validation_inconclusive") is Outcome.SKIPPED_UNKNOWN
    assert b3_reject_outcome("V12:validation_mismatch") is Outcome.FAILED
    assert b3_reject_outcome("failed:context_budget") is Outcome.SKIPPED_UNSUPPORTED
    assert b3_reject_outcome("V5:units") is Outcome.FAILED


def test_b2_manager_still_refuses_b3_kinds():
    """Without B3 mode, region passes are refused and B3 kinds stay disabled (B2 rule)."""
    from scopt.manager import PassManager

    s, pv = fx1()
    c = PassManager(s, "b3", [pv], enable=B3_LINK_PASSES).execute()
    for p in c.passes:
        assert p.outcome is Outcome.SKIPPED_UNSUPPORTED, (p.name, p.reason)
        assert p.reason in ("capability_not_enabled", "region_scope_not_enabled")


def _fold_zero_load(load, reg, live_flags):
    op = SimpleNamespace(off=0, end=len(load), size=len(load), block="insn", log=None)
    hostops = SimpleNamespace(ops=[op])
    liveness = SimpleNamespace(live_out=[live_flags])
    constants = SimpleNamespace(ok=True, foldable={0: ("CF", 0, reg)})
    vv = SimpleNamespace(variant_id="sha256:" + "ab" * 32, src=SimpleNamespace(uid="source"),
                         code=load, factrefs=(fake_factref("CF"),), weight=1,
                         analyses=lambda: (hostops, None, liveness, constants))
    return run_region(ConstantPropagation, vv)


@pytest.mark.parametrize(("encoding", "reg", "expected"), [
    ("8b4500", "eax", "31c090"),
    ("448b4d00", "r9d", "4531c990"),
])
def test_zero_load_short_encoding(encoding, reg, expected):
    load = bytes.fromhex(encoding)
    res, patches = _fold_zero_load(load, reg, {"ZF"})
    assert res.reason == "no_same_length_encoding" and not patches
    res, (patch,) = _fold_zero_load(load, reg, set())
    assert res.outcome == Outcome.CANDIDATE
    assert patch.template_id == "cp_xor_zero" and patch.new_bytes.hex() == expected


@pytest.mark.parametrize(("family", "reg"), [("rax", "eax"), ("r9", "r9d")])
def test_zero_load_with_live_flags_symbolic(family, reg):
    from scopt.validate_sym import check_equiv, patched
    from scopt.validate_sym.mutants import MINI_LAYOUT, OFF_CF, OFF_R, SynthContract, SynthUnit

    unit = SynthUnit("synth:zero-load-with-live-flags")
    unit.prefix()
    unit.a.cmp_env_imm(OFF_R[2], 1)
    off, size = unit.rec("load", lambda: unit.a.mov_r_env(family, OFF_CF))
    unit.a.mov_env_r(OFF_R[1], family)
    unit.a.jcc("ne", "nz")
    unit.goto_tb(0, 0x2000)
    unit.a.label("nz")
    unit.goto_tb(1, 0x2004)
    program = unit.build()
    res, (patch,) = _fold_zero_load(program.code[off:off + size], reg, {"ZF"})
    assert res.outcome == Outcome.CANDIDATE and patch.template_id == "cp_mov_imm"
    contract = SynthContract(True)
    zero = {(OFF_CF, 4): 0}
    folded = patched(program, [(off, patch.new_bytes)])
    assert check_equiv(program, folded, zero, None, contract, layout=MINI_LAYOUT).ok
    assert check_equiv(program, folded, None, None, contract, layout=MINI_LAYOUT).status == "mismatch"
    xor = xor_zero(reg)
    clobbered = patched(program, [(off, xor + nops(size - len(xor)))])
    assert check_equiv(program, clobbered, zero, None, contract, layout=MINI_LAYOUT).status == "mismatch"


@pytest.mark.parametrize(("load_hex", "reg", "family"), [
    ("8b8510020000", "eax", "rax"), ("448b8d10020000", "r9d", "r9"),
])
def test_zero_load_with_live_flags_native(tmp_path, load_hex, reg, family):
    import ctypes
    import platform
    import shutil
    import subprocess

    if platform.system() != "Linux" or platform.machine() != "x86_64" or not shutil.which("cc"):
        pytest.skip("requires x86-64 Linux with a C toolchain")
    load = bytes.fromhex(load_hex)
    _, (patch,) = _fold_zero_load(load, reg, {"CF", "ZF", "OF"})
    functions = []
    for name, body in (("original", load), ("folded", patch.new_bytes)):
        functions.append(f"""
            .globl {name}
        {name}:
            push %rbp
            mov %rdi, %rbp
            mov $-1, %{family}
            cmp $1, %esi
            .byte {','.join(str(byte) for byte in body)}
            pushfq
            pop %rcx
            mov %rcx, (%rdx)
            mov %{family}, %rax
            pop %rbp
            ret
        """)
    library = tmp_path / "zero.so"
    subprocess.run(["cc", "-shared", "-x", "assembler", "-o", str(library), "-"],
                   input="\n".join(functions) + '\n.section .note.GNU-stack,"",@progbits\n',
                   text=True, check=True)
    dll = ctypes.CDLL(str(library))
    word = ctypes.c_uint64
    env = ctypes.create_string_buffer(0x214)
    for value in (0, 1, 2, 0x10, 0x80000000, 0xffffffff):
        flags = []
        for fn in (dll.original, dll.folded):
            fn.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(word))
            fn.restype = word
            out = word()
            assert fn(env, value, ctypes.byref(out)) == 0  # includes cleared upper 32 bits
            flags.append(out.value & 0x8d5)  # OF SF ZF AF PF CF
        assert flags[0] == flags[1]

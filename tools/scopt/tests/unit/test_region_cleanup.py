from dataclasses import replace
from types import SimpleNamespace

import pytest

from scopt.analysis.hconst import decode_one
from scopt.analysis.hostops import EnvAccess, HostOp, OpKind
from scopt.analysis.native_region import NativeRegion
from scopt.passes.region_cleanup import _adjacent_reason, scalar_patches


def operation(raw):
    return decode_one(bytes.fromhex(raw), 0, len(bytes.fromhex(raw)))


@pytest.mark.parametrize("first,last,flags,expected", [
    ("89cb", "89cb", {"OF"}, "scalar_repeat_nop"),
    ("0fb6d9", "0fb6d9", {"CF"}, "scalar_repeat_nop"),
    ("0fbfd9", "0fbfd9", set(), "scalar_repeat_nop"),
    ("21cb", "85db", {"CF", "SF", "ZF", "OF", "PF"}, "scalar_test_nop"),
    ("21cb", "85db", {"AF"}, None),
    ("01cb", "85db", {"OF"}, None),
    ("0fb6d9", "81e3ff000000", set(), "scalar_mask_nop"),
    ("0fb6d9", "81e3ff000000", {"ZF"}, None),
    ("0fb7d9", "81e3ff000000", set(), None),
    ("0fbed9", "81e3ff000000", set(), None),
    ("6689cb", "89cb", set(), None),
    ("88cb", "88cb", set(), None),
    ("0f44d9", "0f44d9", set(), None),
    ("d3e3", "d3e3", set(), None),
])
def test_integer_rules(first, last, flags, expected):
    op = HostOp(0, 3, "", OpKind.FLAGS, "insn", 0, flags_written=frozenset({"ZF", "CF", "AF"}))
    assert _adjacent_reason(operation(first), operation(last), op, set(flags)) == expected


def test_env_forwarding_respects_byte_overlaps_and_source_barriers():
    load = HostOp(0, 3, "", OpKind.ENV_LOAD, "insn", 0,
                  env=(EnvAccess("regs[0]", 0, 4, "r", True),),
                  regs_written=frozenset({"rbx"}))
    store = HostOp(3, 3, "", OpKind.ENV_STORE_REG, "insn", 0,
                   env=(EnvAccess("regs[1]", 4, 4, "w", True),))
    code = bytes.fromhex("8b5d00 894504 8b5d00")
    graph = SimpleNamespace(ok=True, predecessors=lambda i, atomic: (i - 1,))
    live = SimpleNamespace(live_out=(set(), set(), set()))
    region = NativeRegion("test", code, "contract", (), (), (), (), (), (), frozenset(), "ok")

    def candidates(middle=store, source=region, cfg=graph):
        return list(scalar_patches("target", code, (load, middle, replace(load, off=6)),
                                   cfg, live, source, 10))
    assert [p.template_id for p in candidates()] == ["scalar_forward_nop"]
    assert not candidates(replace(store, env=(EnvAccess("regs[0]", 2, 4, "w", True),)))
    assert not candidates(replace(store, kind=OpKind.CALL))
    assert not candidates(replace(store, kind=OpKind.COPY, regs_partial=frozenset({"rbx"})))
    assert not candidates(source=replace(region, frozen_offsets=frozenset({3})))
    assert not candidates(cfg=SimpleNamespace(ok=True, predecessors=lambda i, atomic: (i - 1, 0)))


@pytest.mark.parametrize("raw, unsigned, signed", [
    ("0fb6c1", (0, 255), (0, 255)),
    ("0fbec1", (0, 0xffffffff), (-128, 127)),
    ("4863c1", (0, 0xffffffffffffffff), (-2147483648, 2147483647)),
    ("83c8ff", (0xffffffff, 0xffffffff), (-1, -1)),
    ("25f0000080", (0, 0x800000f0), (-2147483648, 240)),
])
def test_native_result_bounds(raw, unsigned, signed):
    from scopt.analysis.native_integer import result_facts
    facts = result_facts(operation(raw))
    assert facts.bounds(False) == unsigned
    assert facts.bounds(True) == signed


def test_cmp_uses_operand_width_and_signedness():
    from scopt.analysis.native_integer import compare_outcome, result_facts
    unknown_signed_byte = result_facts(operation("0fbec1"))
    assert compare_outcome(unknown_signed_byte, "jg", 127) is False
    assert compare_outcome(unknown_signed_byte, "jb", 128) is None
    mask = result_facts(operation("83e0fe"))
    assert compare_outcome(mask, "je", -1) is False
    assert compare_outcome(mask, "je", -2) is None


def test_bounded_branch_proposes_same_target_or_nops():
    # movzx eax,cl; cmp eax,256; jb +4. All comparison flags remain in place.
    code = bytes.fromhex("0fb6c1 3d00010000 7204")
    ops = (HostOp(0, 3, "", OpKind.COPY, "insn", 0),
           HostOp(3, 5, "", OpKind.FLAGS, "insn", 0),
           HostOp(8, 2, "", OpKind.JCC, "insn", 0))
    graph = SimpleNamespace(ok=True, predecessors=lambda i, atomic: (i - 1,))
    region = NativeRegion("test", code, "contract", (), (), (), (), (), (), frozenset(), "ok")
    live = SimpleNamespace(live_out=(set(), set(), set()))
    patch, = scalar_patches("target", code, ops, graph, live, region, 10)
    assert patch.template_id == "scalar_range_branch" and patch.new_bytes == bytes.fromhex("eb04")
    assert not list(scalar_patches("target", code, ops, graph, live,
                                   replace(region, frozen_offsets=frozenset({3})), 10))


@pytest.mark.parametrize("has_call,value,removed", [(False, 0, 1), (True, 0, 0), (False, 7, 0)])
def test_store_at_private_entry_needs_current_value(has_call, value, removed):
    from scopt.analysis.hcfg import build_hcfg
    from scopt.analysis.hconst import build_hconst
    from scopt.analysis.hostops import UnitHostOps
    from scopt.passes.region_cleanup import RegionStateForward

    call = bytes.fromhex("e800000000") if has_call else b""
    off = len(call)
    code = call + bytes.fromhex("c7450000000000 e900000000")
    ops = (HostOp(0, 5, "call", OpKind.CALL, "insn", 0, helper_flags=6,
                  helper_name="unlisted"),) if has_call else ()
    store = HostOp(off, 7, "mov", OpKind.ENV_STORE_IMM, "insn", 0, imm=0,
                   env=(EnvAccess("regs[0]", 0, 4, "w", True),))
    ops += (store, HostOp(off + 7, 5, "jmp", OpKind.EXIT_JMP, "exit_stub", 0,
                          sites=("EXIT_TB_EPI_JMP",)))
    h = UnitHostOps("fixture", "ok", (), ops, (), 0, len(code), len(code), None,
                    len(code), 1, 0, None)
    graph = build_hcfg(h)
    state = build_hconst(h, graph, {"r0": value}, code=code)
    region = NativeRegion("test", code, "contract", (), (), (), ops, graph.succ_full,
                          (), frozenset({0}) if has_call else frozenset(), "ok")
    ctx = SimpleNamespace(params={}, analysis=lambda *args: SimpleNamespace(value=region))
    vv = SimpleNamespace(variant_id="variant", src=SimpleNamespace(uid="source"), code=code,
                         factrefs=(), analyses=lambda: (h, graph, None, state))
    patches = []
    edits = SimpleNamespace(pure_rewrite=lambda p, **kwargs: patches.append(p))
    RegionStateForward().run(vv, ctx, edits)
    assert len(patches) == removed


@pytest.mark.parametrize("base,index,mutation,applied", [
    ("rbx", "rcx", None, True),
    ("rbx", "rcx", "rcx", False),
    ("rbx", "rcx", "rbx", False),
    ("rbx", "rcx", "rax", False),
    ("rax", "rcx", None, False),
])
def test_dominating_address_expression_versions(base, index, mutation, applied):
    # lea eax,[rbx+rcx*4+8] has neither a memory read nor a flag effect.
    code = bytes.fromhex("8d448b08 90 8d448b08")
    lea = HostOp(0, 4, "lea", OpKind.OPAQUE, "insn", 0,
                 regs_read=frozenset((base, index)), regs_written=frozenset({"rax"}))
    middle = HostOp(4, 1, "middle", OpKind.COPY, "insn", 0,
                    regs_partial=frozenset({mutation}) if mutation else frozenset())
    ops = lea, middle, replace(lea, off=5)
    graph = SimpleNamespace(ok=True, predecessors=lambda i, atomic: (i - 1,))
    live = SimpleNamespace(live_out=(set(), set(), {"OF"}))
    region = NativeRegion("test", code, "contract", (), (), (), (), (), (), frozenset(), "ok")
    assert bool(list(scalar_patches("target", code, ops, graph, live, region, 10))) is applied


@pytest.mark.parametrize("params,rounds,offsets,reason", [
    ({}, 3, [8, 12], "scalar_fixed_point"),
    ({"max_patches": 1}, 1, [8], "scalar_patch_budget"),
    ({"max_rounds": 1}, 1, [8], "scalar_round_budget"),
    ({"max_rounds": 0}, 0, [], "scalar_round_budget"),
])
def test_scalar_cleanup_reanalyzes_the_next_variant_generation(params, rounds, offsets, reason):
    from scopt.analysis.hcfg import build_hcfg
    from scopt.analysis.hlive import build_hlive
    from scopt.analysis.hostops import UnitHostOps, _flags
    from scopt.manager_b3 import VariantPlan, VariantView
    from scopt.passes.region_cleanup import NativeScalarCleanup

    # MOVZX gives [0,255], making JAE to the second copy impossible.
    # Until that edge disappears, the second copy has two predecessors.
    code = bytes.fromhex("0fb6c1 3d00010000 7302 89cb 89cb e900000000")
    analyzed = []
    kinds = {"movzx": OpKind.COPY, "cmp": OpKind.FLAGS, "jae": OpKind.JCC,
             "nop": OpKind.NOP, "mov": OpKind.COPY, "jmp": OpKind.EXIT_JMP}

    class FixtureView(VariantView):
        def analyses(self):
            analyzed.append(self.code)
            ops = []
            off = 0
            while off < len(self.code):
                ci = decode_one(self.code, off, len(self.code) - off)
                kind = kinds[ci.mnemonic]
                reads, writes = _flags(ci)
                ops.append(HostOp(off, ci.size, ci.mnemonic, kind, "insn", 0,
                                  target=ci.operands[0].imm if kind == OpKind.JCC else None,
                                  sites=("EXIT_TB_EPI_JMP",) if kind == OpKind.EXIT_JMP else (),
                                  flags_read=reads, flags_written=writes))
                off += ci.size
            host = UnitHostOps("fixture", "ok", (), tuple(ops), (), 0, len(self.code),
                               len(self.code), None, len(self.code), 1, 0, None)
            graph = build_hcfg(host)
            return host, graph, build_hlive(host, graph), None

    plan = VariantPlan("variant", "base", ("caller", 0), "x0", (), (), code, (), "test", 1)
    view = FixtureView("variant", plan, SimpleNamespace(uid="base"), None, 0)
    region = NativeRegion("test", code, "contract", (), (), (), (), (), (), frozenset(), "ok")
    ctx = SimpleNamespace(analysis=lambda *_: SimpleNamespace(value=region), params=params,
                          budget=lambda: None)
    edits = []
    result = NativeScalarCleanup().run(view, ctx, SimpleNamespace(pure_rewrite=edits.append))
    assert [p.host_off for p in edits] == offsets
    assert result.reason == reason
    assert result.stats == {"deleted": len(offsets), "analysis_rounds": rounds,
                            "rewrite_rounds": len(offsets), f"stop:{reason}": 1}
    assert view.code == code and view.plan.patches == ()
    assert len(analyzed) == rounds
    if rounds > 1:
        assert analyzed[1][8:10] != analyzed[0][8:10]
        assert analyzed[1][12:14] == analyzed[0][12:14]


@pytest.mark.parametrize(("params", "rewrites", "rounds"), [
    ({}, 2, 3), ({"max_rounds": 1}, 1, 1), ({"max_patches": 0}, 0, 0),
])
def test_ordinary_scalar_refreshes_analyses_and_applies_to_public_code(tmp_path, params,
                                                                      rewrites, rounds):
    from scopt.manager import PassManager
    from scopt.manager_b3 import B3Env
    from tests.synth.builders import simple_units
    from tests.unit.test_wp10_link import make_snapshot, pipeline

    # Fold the second MOVZX first. Once it stops writing EAX the initial MOV
    # dominates the last copy with the same input version.
    body = bytes.fromhex("0fb6c1 89c3 0fb6c1 89c3")
    snap = make_snapshot(tmp_path, simple_units(1, body_prefix=body))
    snap = replace(snap, runtime={**snap.runtime, "qemu_sha256":
                                  "sha256:56d8bfb32c91c970fe0a16237b962c02791ce0a7fcc52bf6246343fbef430d90"})
    name = "local-scalar-cleanup"
    recipe = pipeline((name, params), caps=("PURE_REWRITE",))
    candidate = PassManager(snap, recipe, b3=B3Env(), enable=(name,)).execute()
    report = candidate.outcome(name)
    assert report.n_edits == rewrites, report.to_json()
    assert report.stats["analysis_rounds"] == rounds
    assert not candidate.b3_state.variants
    unit, = snap.units()
    if rewrites:
        patches = [p for _, p in candidate.b3_state.rewrites[unit.uid]]
        assert [p.host_off for p in patches] == [unit.insn0_start + off for off in (5, 8)[:rewrites]]
        assert unit.code[unit.insn0_start:unit.insn0_start + len(body)] == body

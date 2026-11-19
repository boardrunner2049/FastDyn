from dataclasses import replace
from types import SimpleNamespace

import pytest

from scopt.analysis.hcfg import build_hcfg
from scopt.analysis.helper_effects import (
    ALL, NONE, BoundEffects, HelperEffect, HelperEffectTable, TCG_CALL_NO_RWG,
)
from scopt.analysis.hostops import EnvAccess, HostOp, OpKind, UnitHostOps
from scopt.analysis.obs_contract import build_contract
from scopt.passes import region_state_dse
from scopt.passes.base import Outcome
from scopt.validate_sym.mutants import MINI_LAYOUT


@pytest.mark.parametrize(("case", "deleted"), [
    ("none", 1),
    ("env_read", 0),
    ("pure_helper", 1),
    ("fault_before_returning_overwrite", 0),
    ("guest_access", 0),
    ("dispatcher_path", 0),
    ("partial_overwrite", 0),
])
def test_specialized_dse_observer_and_overwrite_contract(case, deleted, monkeypatch):
    old_code = bytes.fromhex("c7450005000000 740a 0f1f00 c7450007000000 e900000000")
    new_code = old_code[:7] + b"\x66\x90" + old_code[9:]
    first = HostOp(0, 7, "mov dword [rbp], 5", OpKind.ENV_STORE_IMM, "insn", 0,
                   env=(EnvAccess("regs[0]", 0, 4, "w", True),), imm=5)
    old_branch = HostOp(7, 2, "je skip", OpKind.JCC, "insn", 1, target=19)
    new_branch = replace(old_branch, kind=OpKind.NOP, target=None)
    middle = HostOp(9, 3, "nop", OpKind.NOP, "insn", 1)
    final = replace(first, off=12, text="mov dword [rbp], 7", imm=7, insn=2)
    stop = HostOp(19, 5, "jmp epilogue", OpKind.EXIT_JMP, "exit_stub", 2,
                  sites=("EXIT_TB_EPI_JMP",))
    helpers = []
    if case == "env_read":
        middle = replace(middle, kind=OpKind.ENV_LOAD,
                         env=(EnvAccess("regs[0]", 0, 4, "r", True),))
    elif case in ("pure_helper", "fault_before_returning_overwrite"):
        raising = case == "fault_before_returning_overwrite"
        flags = 0 if raising else TCG_CALL_NO_RWG
        helpers.append(HelperEffect("synthetic", flags, NONE, ALL if raising else NONE,
                                    False, False, raising, True, ("fixture.c",)))
        middle = replace(middle, kind=OpKind.CALL, helper_name="synthetic", helper_flags=flags)
    elif case == "guest_access":
        middle = replace(middle, kind=OpKind.GUEST_ACCESS)
    elif case == "dispatcher_path":
        new_branch = old_branch
        new_code = old_code
    elif case == "partial_overwrite":
        final = replace(final, env=(EnvAccess("regs[0]", 0, 2, "w", False),))
    fx = BoundEffects(HelperEffectTable.build(MINI_LAYOUT.exe_sha256, helpers), MINI_LAYOUT)
    monkeypatch.setattr(region_state_dse, "refresh_effects_for_layout", lambda _layout: fx)

    def hostops(branch):
        return UnitHostOps("fixture", "ok", (), (first, branch, middle, final, stop), (),
                           0, 24, 24, None, 24, 3, 0, MINI_LAYOUT.envlayout_id)

    old_host, new_host = hostops(old_branch), hostops(new_branch)
    old_graph, new_graph = build_hcfg(old_host), build_hcfg(new_host)
    # The original conditional observes the first store on its skip/exit arm.
    src = SimpleNamespace(uid="fixture", meta_status="ok", code=old_code, insns=(),
                          reloc=SimpleNamespace(relocatable=True), key=SimpleNamespace(cflags=0))
    unit = SimpleNamespace(**{**vars(src), "code": new_code})
    context = {
        "native-region": SimpleNamespace(status="ok", allows=lambda *_: True),
        "obs-contract": build_contract(MINI_LAYOUT, None),
        "hostops": old_host, "hcfg": old_graph,
    }
    ctx = SimpleNamespace(params={}, snapshot_id="snapshot", store=SimpleNamespace(inputs=lambda: ()),
                          analysis=lambda name, _scope: SimpleNamespace(value=context[name]))
    vv = SimpleNamespace(layout=MINI_LAYOUT, src=src, unit=unit, factrefs=(), variant_id="variant",
                         analyses=lambda: (new_host, new_graph, None, None))
    edits = []
    result = region_state_dse.RegionStateDSE().run(vv, ctx, SimpleNamespace(
        pure_rewrite=lambda p, **_kw: edits.append(p)))
    assert result.stats["deleted"] == len(edits) == deleted
    if deleted:
        assert result.outcome == Outcome.CANDIDATE
        assert edits[0].host_off == 0 and edits[0].template_id == "b4_nop_store"
    else:
        assert result.outcome == Outcome.NO_CHANGE
        assert result.stats["refused:observed_at_sync_point"] >= 2


def test_independent_symbolic_check_rejects_loss_of_pre_fault_state():
    from scopt.analysis.syncdead import nop_bytes
    from scopt.validate_sym.equiv import check_equiv
    from scopt.validate_sym.program import HelperRef, Program, patched

    code = bytes.fromhex("c7450005000000 e800000000 c7450007000000 e900000000")
    base = 0x70000000
    original = Program("fault", code, base, base - 256, base + len(code), 0, None,
                       helpers={7: HelperRef(0, "before_recovery", 0)}, epilogue=base + len(code))
    row = HelperEffect("before_recovery", 0, NONE, ALL, False, False, True, True, ("fixture.c",))
    effects = BoundEffects(HelperEffectTable.build(MINI_LAYOUT.exe_sha256, [row]), MINI_LAYOUT)
    modified = patched(original, [(0, nop_bytes(7))])
    verdict = check_equiv(original, modified, None, None, build_contract(MINI_LAYOUT, None),
                          layout=MINI_LAYOUT, effects=effects)
    assert verdict.status == "mismatch" and "regs[0]" in verdict.detail

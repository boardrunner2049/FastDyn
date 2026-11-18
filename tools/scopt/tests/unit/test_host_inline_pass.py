"""Framework checks for the standalone native inline pipeline."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scopt.analysis.api import AnalysisSpec, ScopeRef
from scopt.analysis.host_calls import HostCallSite
from scopt.analysis.native_callees import NativeCallBinding
from scopt.analysis.registry import AnalysisRegistry
from scopt.analysis.store import AnalysisInput
from scopt.canon import sha256_id
from scopt.formats.enums import Encoding, SiteKind
from scopt.manager import PassManager, PassRegistry, load_pipeline
from scopt.model.native import supports_tcg_integer_abi
from scopt.model.unit import Bindings, HelperBinding, Site
from scopt.passes import host_inline
from scopt.passes.base import Outcome, PassDescriptor, PassResult, StaleFact
from tests.synth.fake_snapshot import FakeSnapshot, fake_snapshot

_IMAGE_ID = sha256_id(b"native image e.g. validated in a separate resolver test")
_NATIVE_INPUT = sha256_id(b"native image manifest")
_CALLEE_CODE = bytes.fromhex("83c007c3")


@pytest.fixture
def world(tmp_path, monkeypatch):
    fake = fake_snapshot(2)
    units, analyses = [], {}
    for u in fake.units():
        host = fake.header.code_address + u.tc_offset
        target = host + 0x2000
        displacement = target - (host + 5)
        call_bytes = b"\xe8" + displacement.to_bytes(4, "little")
        caller = call_bytes + b"\x90\x90"
        row = Site(1, 0, 4, Encoding.REL32, SiteKind.HELPER_CALL, 7, target, 0, displacement)
        helper = HelperBinding(7, "test", "qemu", "helper_test", 5, target)
        u = replace(u, code=caller, tc_size=len(caller), ldst_end=len(caller), pool_start=None,
                    sites=(row,), bindings=Bindings((helper,), ()))
        units.append(u)
        call = HostCallSite(
            uid=u.uid, host_off=0, original_bytes=call_bytes, encoding="rel32", continuation_off=5,
            insn_idx=0, guest_pcs=u.vaddrs, opmark_off=0, helper_id=7, helper_name="test",
            helper_flags=5, target_abs=target, pool_slot_off=None, module="qemu",
            symbol="helper_test", call_class="pure", recovery_return_off=5, reason=None)
        callee = SimpleNamespace(
            path="/exact/to/qemu", elf_address=0x5000, image_offset=0x5000,
            image_id=_IMAGE_ID, code_hash=sha256_id(_CALLEE_CODE), symbol="helper_test",
            size=len(_CALLEE_CODE), code=_CALLEE_CODE, role="qemu", abi="sysv-amd64",
            runtime_address=target, tcg_typemask=2 | (2 << 3))
        analyses[u.uid] = (NativeCallBinding(call, callee, None, _NATIVE_INPUT),)
    snap = FakeSnapshot(units, fake.snapshot_id, fake.header, runtime={"supercache_flat": False})

    def native_lookup(ctx, scope):
        assert ctx.external_input("native-images").input_id == _NATIVE_INPUT
        ctx.certify()
        return analyses[scope.id]

    def code_fact(ctx, scope):
        ctx.certify()
        return ctx.unit(scope.id).content_hash

    registry = AnalysisRegistry([
        AnalysisSpec("envlayout", 1, "snapshot", (), frozenset(),
                     lambda _ctx, _scope: {"bound": True}, dict),
        AnalysisSpec("native-callees", 1, "unit", (), frozenset({"external:native-images"}),
                     native_lookup, tuple),
        AnalysisSpec("code-fact", 1, "unit", (), frozenset({"code"}), code_fact, str),
    ])
    bridge = tmp_path / "bridge"
    bridge.write_bytes(b"synthetic bridge adapter")
    monkeypatch.setenv("SCOPT_HOST_INLINE_BRIDGE", str(bridge))

    def bridge_result(argv, deadline, request=None):
        if argv[1:] == ["--version"]:
            return b"scopt-native bridge 1 (LLVM/BOLT 20.1.2)"
        assert json.loads(request)["code_sha256"] == sha256_id(_CALLEE_CODE)
        return json.dumps({
            "version": 1, "status": "ok", "code": "83c007e900000000",
            "origins": [{"source_off": 0, "source_size": 3, "output_off": 0, "output_size": 3},
                        {"source_off": 3, "source_size": 1, "output_off": 3, "output_size": 5}],
            "relocations": [{"field_off": 4, "width": 4, "target": "continuation"}],
        }).encode()

    monkeypatch.setattr(host_inline, "_bridge_call", bridge_result)
    monkeypatch.setattr(host_inline, "prepare_native_images",
                        lambda *args, **kwargs: AnalysisInput(_NATIVE_INPUT, ()))
    return snap, registry


def test_host_inline_publishes_generation_and_invalidates_old_code(world):
    snap, registry = world
    seen = []

    class Observe:
        descriptor = PassDescriptor(name="observe", version=1, requires=(("code-fact", 1),))

        def run(self, view, ctx, edits):
            u = view.units()[0]
            assert view.working_set.generation == 1
            assert len(u.native_fragments) == 1
            seen.append(ctx.fact("code-fact", ScopeRef("unit", u.uid)).value)
            return PassResult(Outcome.NO_CHANGE, "observed")

    pipe = {"name": "test", "allowed_capabilities": ["INLINE_CALL"],
            "pass": [{"name": "host-inline", "version": 1, "params": {"max_sites": 1}},
                     {"name": "observe", "version": 1}]}
    pm = PassManager(snap, pipe, passes=PassRegistry((host_inline.HostInline, Observe)),
                     analyses=registry)
    orig = snap.units()[0]
    scope = ScopeRef("unit", orig.uid)
    old_fact = pm.store.get("code-fact", scope)
    result = pm.execute()
    assert seen == [result.working_set.units()[0].content_hash] and seen != [orig.content_hash]
    assert not orig.native_fragments
    assert len(result.edits) == 1
    assert result.edits[0].payload.target == orig.uid
    assert result.passes[0].stats["sites:site_budget"] == 1
    with pytest.raises(StaleFact):
        pm.store.check_fact(old_fact)


def test_host_inline_budget_exact_growth_and_ablation(world):
    snap, registry = world
    pipeline = load_pipeline("host-inline")
    enabled = PassManager(snap, pipeline, analyses=registry,
                          params={"host-inline": {"max_total_growth": 8}}).execute()
    assert len(enabled.edits) == 1
    assert enabled.passes[0].stats["emitted_growth_bytes"] == 8
    row = next(r for r in enabled.ledger if r.id == "host-inline@1")
    assert row.counts["emitted_growth_bytes"] == 8
    provenance = enabled.pipeline_provenance()[0]
    assert provenance["x_counts"]["emitted_growth_bytes"] == 8
    assert provenance["x_diagnostics"]["sites"]
    assert enabled.passes[0].stats["sites:growth_budget"] == 1
    disabled = PassManager(snap, pipeline, analyses=registry, disable=["host-inline"]).execute()
    assert not disabled.edits and disabled.working_set.generation == 0
    assert disabled.passes[0].reason == "ablation_disabled"


@pytest.mark.parametrize(("typemask", "admitted"), [
    (2, True), (4 | (2 << 3), True), (sum(2 << (3 * i) for i in range(7)), True),
    (sum(2 << (3 * i) for i in range(8)), False), (6 | (2 << 3), False),
    (2 | (6 << 3), False), (2 | (7 << 3), False), (0 | (2 << 3), False),
    (2 | (2 << 6), False),
])
def test_register_only_tcg_argument_types(typemask, admitted):
    assert supports_tcg_integer_abi(typemask) is admitted



def test_host_inline_refuses_stale_envlayout(world):
    snap, registry = world
    registry.register(AnalysisSpec("envlayout", 1, "snapshot", (), frozenset(),
                                   lambda _ctx, _scope: {"bound": False}, dict), replace=True)
    result = PassManager(snap, "host-inline", analyses=registry).execute()
    assert not result.edits and result.passes[0].reason == "guard_layout_unbound"

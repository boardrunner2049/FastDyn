"""Pass scheduling and refusal isolation for fallthrough native splices."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scopt.analysis.api import AnalysisSpec
from scopt.analysis.host_calls import HostCallSite
from scopt.analysis.native_callees import NativeCallBinding
from scopt.analysis.registry import AnalysisRegistry
from scopt.analysis.store import AnalysisInput
from scopt.canon import sha256_id
from scopt.formats.enums import Encoding, OpMarkKind, SiteKind
from scopt.link import LinkError
from scopt.manager import PassManager
from scopt.model.unit import Bindings, HelperBinding, Insn, OpMark, Site
from scopt.passes import host_inline
from scopt.passes.base import Outcome
from tests.synth.fake_snapshot import FakeSnapshot, fake_snapshot


@pytest.fixture
def world(tmp_path, monkeypatch, request):
    from scopt import splice_layout

    fake = fake_snapshot(2)
    units, analyses = [], {}
    input_id = sha256_id(b"splice native source manifest")
    typemask, code_hex = getattr(request, "param", (0x12, "f30f1efa 89f8 01c0 31ff c3"))
    callee_code = bytes.fromhex(code_hex)
    callee_hash = sha256_id(callee_code)
    for u in fake.units():
        base = fake.header.code_address + u.tc_offset
        target = base + 0x2000
        caller, rows, calls, insns, marks = b"", [], [], [], []
        for index, load_at in enumerate((0, 9)):
            call_at = load_at + 3
            displacement = target - (base + call_at + 5)
            call_bytes = b"\xe8" + displacement.to_bytes(4, "little")
            caller += b"\x8b\x7d\x00" + call_bytes + b"\x90"
            rows.append(Site(call_at + 1, call_at, 4, Encoding.REL32, SiteKind.HELPER_CALL,
                             7, target, index, displacement))
            insns.append(Insn(index, (u.vaddrs[0] + 2 * index, 0, 0), load_at, call_at + 5))
            marks.append(OpMark(load_at, OpMarkKind.CALL, index, 7))
            call = HostCallSite(
                uid=u.uid, host_off=call_at, original_bytes=call_bytes, encoding="rel32",
                continuation_off=call_at + 5, insn_idx=index, guest_pcs=u.vaddrs,
                opmark_off=load_at, helper_id=7, helper_name="test", helper_flags=5,
                target_abs=target, pool_slot_off=None, module="qemu", symbol="helper_test",
                call_class="pure", recovery_return_off=call_at + 5, reason=None)
            callee = SimpleNamespace(
                path="/exact/to/qemu", elf_address=0x5000, image_offset=0x5000,
                image_id=sha256_id(b"qemu image"), code_hash=callee_hash, symbol="helper_test",
                size=len(callee_code), code=callee_code, role="qemu", abi="sysv-amd64",
                runtime_address=target, tcg_typemask=typemask)
            calls.append(NativeCallBinding(call, callee, None, input_id))
        helper = HelperBinding(7, "test", "qemu", "helper_test", 5, target)
        units.append(replace(u, code=caller, tc_size=len(caller), ldst_end=len(caller),
                             pool_start=None, sites=tuple(rows), insns=tuple(insns),
                             opmarks=tuple(marks), bindings=Bindings((helper,), ())))
        analyses[u.uid] = tuple(calls)
    snap = FakeSnapshot(units, fake.snapshot_id, fake.header, runtime={"supercache_flat": False})

    def native_lookup(ctx, scope):
        assert ctx.external_input("native-images").input_id == input_id
        ctx.certify()
        return analyses[scope.id]

    registry = AnalysisRegistry([
        AnalysisSpec("envlayout", 1, "snapshot", (), frozenset(),
                     lambda _ctx, _scope: {"bound": True}, dict),
        AnalysisSpec("native-callees", 1, "unit", (), frozenset({"external:native-images"}),
                     native_lookup, tuple),
    ])
    bridge = tmp_path / "bridge"
    bridge.write_bytes(b"synthetic bridge adapter")
    monkeypatch.setenv("SCOPT_HOST_INLINE_BRIDGE", str(bridge))

    def bridge_result(argv, deadline, request=None):
        if argv[1:] == ["--version"]:
            return b"scopt-native bridge 1 (LLVM/BOLT 20.1.2)"
        assert json.loads(request)["code_sha256"] == callee_hash
        from scopt.decode.x86 import sweep
        origins = [{"source_off": ins.address, "source_size": ins.size, "output_off": ins.address,
                    "output_size": 5 if ins.is_ret else ins.size} for ins in sweep(callee_code, 0)]
        return json.dumps({
            "version": 1, "status": "ok", "code": callee_code[:-1].hex() + "e900000000",
            "origins": origins,
            "relocations": [{"field_off": len(callee_code), "width": 4,
                             "target": "continuation"}],
        }).encode()

    monkeypatch.setattr(host_inline, "_bridge_call", bridge_result)
    monkeypatch.setattr(host_inline, "prepare_native_images",
                        lambda *args, **kwargs: AnalysisInput(input_id, ()))
    # Isolate pass scheduling and native correspondence from physical relocation.
    monkeypatch.setattr(splice_layout, "prepare_splice_layout", lambda _, req, __: req)
    return snap, registry


def test_splice_pass_stages_at_most_one_per_tb(world):
    snap, registry = world
    result = PassManager(snap, "host-inline-splice", analyses=registry).execute()
    assert result.passes[0].outcome is Outcome.CANDIDATE
    assert result.passes[0].reason == "native_calls_spliced"
    assert [(e.payload.target, e.payload.call_off) for e in result.edits] == [
        (u.uid, 3) for u in snap.units()]
    assert all(e.payload.splice.start_off == 0 and e.payload.tcg_typemask == 0x12
               for e in result.edits)
    assert all(e.payload.splice.code == bytes.fromhex("8b4500 01c0 31ff 90")
               for e in result.edits)
    assert result.passes[0].stats["sites:site_budget"] == 2
    assert result.working_set.generation == 1
    assert [len(u.native_fragments) for u in result.working_set.units()] == [1, 1]


def test_maximal_mode_attempts_second_call_and_keeps_proved_subset(world):
    snap, registry = world
    result = PassManager(snap, "host-inline-maximal", analyses=registry).execute()
    record, = result.passes

    assert record.name == "host-inline"
    assert record.outcome is Outcome.CANDIDATE
    assert record.reason == "native_inline_partial_coverage"
    assert [(e.payload.target, e.payload.call_off) for e in result.edits] == [
        (u.uid, 3) for u in snap.units()
    ]
    coverage = json.loads(record.detail)["coverage"]
    assert coverage["sites_seen"] == 4
    assert coverage["sites_approved"] == coverage["sites_applied"] == 2
    assert coverage["complete"] is False
    assert record.stats["sites:splice_interior_offset"] == 2
    assert "sites:site_budget" not in record.stats
    assert all(len(u.native_fragments) == 1 for u in result.working_set.units())


def test_splice_pass_keeps_going_after_an_unplaceable_splice(world, monkeypatch):
    from scopt import splice_layout

    snap, registry = world

    def check(unit, req, _):
        if req.call_off == 3:
            raise LinkError("test_layout_refusal", uid=unit.uid)
        return req

    monkeypatch.setattr(splice_layout, "prepare_splice_layout", check)
    result = PassManager(snap, "host-inline-splice", analyses=registry).execute()
    assert result.passes[0].stats["sites:test_layout_refusal"] == 2
    assert [e.payload.call_off for e in result.edits] == [12, 12]


def test_splice_pass_skips_physically_transformed_units(world):
    snap, registry = world
    rebuilt = FakeSnapshot([replace(u, splice_applied=True) for u in snap.units()],
                           snap.snapshot_id, snap.header, runtime=snap.runtime)
    result = PassManager(rebuilt, "host-inline-splice", analyses=registry).execute()
    assert not result.edits
    assert result.passes[0].stats["sites:grown_unit_unsupported"] == 4


@pytest.mark.parametrize(("typemask", "accepted"), [
    (0x12, True), (0x1A, True), (0x1B, True), (0x10, False),
    (0x22, True), (0x14, True), (0x92, True), (0x02, True), (0x32, False),
])
def test_splice_limits_the_callee_abi(typemask, accepted):
    assert host_inline.HostInlineSplice()._accept_abi(typemask) is accepted


@pytest.mark.parametrize("world", [(0x124, "4889f8 4885f6 7403 4801f0 c3")], indirect=True)
def test_pass_emits_two_argument_i64_conditional_leaf(world):
    snap, registry = world
    result = PassManager(snap, "host-inline-splice", analyses=registry).execute()
    rec = result.passes[0]
    assert rec.outcome is Outcome.CANDIDATE, (rec.reason, rec.detail)
    assert len(result.edits) == 2
    for edit in result.edits:
        req = edit.payload
        assert req.splice.start_off == req.call_off
        assert req.splice.prefix == b""
        assert req.splice.code.startswith(req.callee_code[:-1])


# The complete helper_usad8 from the 2da1d94f benchmark QEMU, at ELF 0x396b10.
# Exercise the default static scheduling path even on hosts without that ELF.
_USAD8 = (
    "f30f1efa 89f2 89f9 89f8 29f1 4189d0 29fe 4038fa 0f42f1 41c1e808 c1ef08 4489c1 "
    "4189f9 29f9 400fb6f6 4529c1 4138f8 4189d0 89c7 410f42c9 41c1e810 c1ef10 0fb6c9 "
    "4189f9 01f1 4489c6 4529c1 29fe 4138f8 410f42f1 c1e818 c1ea18 89c7 400fb6f6 "
    "29d7 01ce 89d1 29c1 38c2 89f8 0f43c1 0fb6c0 01f0 31d2 31c9 31f6 31ff 4531c0 4531c9 c3"
)


@pytest.mark.parametrize("world", [(0x92, _USAD8)], indirect=True)
def test_usad8_fits_default_splice_size_and_honors_tighter_overrides(world):
    snap, registry = world
    result = PassManager(snap, "host-inline-splice", analyses=registry).execute()
    assert len(result.edits) == 2
    for edit in result.edits:
        req = edit.payload
        assert len(req.callee_code) == 134
        assert req.splice.code.startswith(req.callee_code[4:-1])
    for key, reason in (("max_callee_bytes", "callee_size_budget"),
                        ("static_max_callee_bytes", "static_callee_size_budget")):
        capped = PassManager(snap, "host-inline-splice", analyses=registry,
                             params={"host-inline-splice": {key: 128}}).execute()
        assert not capped.edits
        assert capped.passes[0].stats[f"sites:{reason}"] == 4
    from scopt.passes.host_inline_extreme import HostInlineExtreme
    assert HostInlineExtreme.default_params == host_inline.HostInline.default_params
    assert host_inline.HostInline.default_params["max_callee_bytes"] == 128
    assert host_inline.HostInline.default_params["static_max_callee_bytes"] == 64

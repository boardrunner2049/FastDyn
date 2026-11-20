"""Exercise the exact framed callback's source and relocation checks."""

from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scopt.canon import sha256_id
from scopt.decode.x86 import sweep
from scopt.formats.enums import Encoding, OpMarkKind, SiteKind
from scopt.model.native import InlineCall, NativeFragment
from scopt.model.unit import Bindings, Fixup, HelperBinding, Insn, OpMark, RecoveryPoint, RelocInfo, Site
from scopt.validate_sym.native_inline import NativeInlineError
from scopt.validate_sym.native_virtual import (
    prepare_virtual_splice,
    verify_virtual_splice,
    verify_virtual_splice_body,
)

_BASE = 0x7FFF78000000
_PLUGIN = 0x7FFF4B08F000
_ENTRY = 0x26236
_IMAGE = "sha256:b6b9cb54ee1b3c1320299c0dfca6163f624d7876daa3389793364605db79dc0b"
_SOURCE = bytes.fromhex(
    "f30f1efa554889e54883ec10897dfc488975f048c705847af40400000000"
    "488d05657af4044889c6bf01000000e86851feffc605617af4040190c9c3"
)


def _request(unit, site, load_address, source=_SOURCE, image_id=_IMAGE, entry=_ENTRY):
    return InlineCall(
        target=unit.uid,
        unit_hash=unit.content_hash,
        call_off=site.insn_off,
        call_bytes=unit.code[site.insn_off:site.insn_off + 5],
        continuation_off=site.insn_off + 5,
        helper_id=site.aux,
        target_abs=load_address + entry,
        pool_slot_off=None,
        guest_pcs=(0x08000000,),
        image_id=image_id,
        elf_address=entry,
        image_offset=entry,
        symbol="benchmark_start",
        callee_code=source,
        native_input_id=sha256_id(b"test input"),
        fragment=NativeFragment(b"", (), ()),
        tcg_typemask=400,
    )


def _case(prefix=b"\x90", *, entry=_ENTRY, source=_SOURCE, image_id=_IMAGE):
    call_off = len(prefix)
    cont = call_off + 5
    target = _PLUGIN + entry
    disp = target - _BASE - cont
    code = (prefix + b"\xe8" + disp.to_bytes(4, "little", signed=True)).ljust(0x40, b"\x90")
    site = Site(call_off + 1, call_off, 4, Encoding.REL32,
                SiteKind.HELPER_CALL, 3, target, 0, disp & 0xFFFFFFFF)
    unit = SimpleNamespace(
        uid="tb:virtual#0",
        content_hash=sha256_id(code),
        code=code,
        tc_offset=0,
        ldst_end=len(code),
        pool_start=None,
        meta_status="ok",
        sites=(site,),
        bindings=Bindings(helpers=(HelperBinding(3, None, "libfastdyn.so", "benchmark_start", 0, target),)),
        reloc=RelocInfo(True, (), (
            Fixup(site.field_off, 4, "rel_minus_delta", int(site.kind), "ext.plugin", target),
        )),
        insns=(Insn(0, (0, 0, 0), 0, cont),),
        opmarks=(OpMark(0, OpMarkKind.CALL, 0, 3),),
        recovery=(RecoveryPoint("h", cont, 0, (0, 0, 0), "helper_call"),),
    )
    return unit, _request(unit, site, _PLUGIN, source, image_id, entry)


def test_frame_stack_phase_and_all_pc_relative_targets():
    unit, request = _case()
    splice = prepare_virtual_splice(unit, request, _BASE)
    assert splice.kind == "frame"
    assert splice.start_off == request.call_off and splice.prefix == b""
    assert splice.code[:13] == bytes.fromhex("488d6424f8 55 4889e5 4883ec10")
    assert splice.code[59:65] == bytes.fromhex("c9 488d642408")
    assert len(splice.code) - len(request.call_bytes) == 64
    refs = [(r.field_off, r.next_off, r.target_abs) for r in splice.relocations]
    assert refs == [
        (23, 31, _PLUGIN + 0x4F6DCD8),
        (34, 38, _PLUGIN + 0x4F6DCC0),
        (47, 51, _PLUGIN + 0xB3D0),
        (53, 58, _PLUGIN + 0x4F6DCD0),
    ]
    moved_pc = _BASE + request.call_off + 0x240
    physical = bytearray(splice.code)
    for field, end, target in refs:
        assert physical[field:field + 4] == bytes(4)
        physical[field:field + 4] = (target - moved_pc - end).to_bytes(4, "little", signed=True)
    destinations = [i.rip_target if i.rip_target is not None else i.branch_target
                    for i in sweep(bytes(physical), moved_pc)
                    if i.rip_target is not None or i.branch_target is not None]
    assert destinations == [target for _, _, target in refs]
    request = replace(request, splice=splice)
    verify_virtual_splice_body(request)
    verify_virtual_splice(unit, request, _BASE)


def test_canary_depth_two_only_retains_clock_call():
    unit, request = _case(
        entry=0x26267,
        source=bytes.fromhex("f30f1efa554889e54883ec10897dfc488975f0e8b7ffffff90c9c3"),
        image_id="sha256:fd9e57f674961120fbeb791dd16b5dac8fa211a7695b4cded64d27ce371677fc",
    )
    sites = []
    for depth in (1, 2):
        splice = prepare_virtual_splice(unit, request, _BASE, max_depth=depth)
        assert splice.depth == depth
        pc = _BASE + request.call_off
        code = bytearray(splice.code)
        for row in splice.relocations:
            code[row.field_off:row.field_off + 4] = (
                row.target_abs - pc - row.next_off
            ).to_bytes(4, "little", signed=True)
        insns = sweep(bytes(code), pc)
        assert [i.branch_target for i in insns if i.is_call] == [
            _PLUGIN + (0x26236 if depth == 1 else 0xB3D0)
        ]
        assert not any(i.is_ret or i.is_jump for i in insns)
        assert sum(i.raw == bytes.fromhex("488d6424f8") for i in insns) == depth
        assert sum(i.raw == bytes.fromhex("488d642408") for i in insns) == depth
        proof = replace(request, splice=splice)
        verify_virtual_splice(unit, InlineCall.from_json(proof.to_json()), _BASE)
        sites.append(splice)
    forged = replace(request, splice=replace(sites[0], depth=2))
    with pytest.raises(NativeInlineError, match="exact framed body"):
        verify_virtual_splice_body(forged)


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("callee_code", _SOURCE[:-1] + b"\xcc", "frame_image_unverified"),
    ("image_id", sha256_id(b"unverified plugin"), "frame_image_unverified"),
    ("elf_address", _ENTRY + 1, "frame_image_unverified"),
    ("target_abs", _PLUGIN + _ENTRY + 1, "frame_image_unverified"),
    ("tcg_typemask", 0x12, "frame_abi"),
    ("tcg_typemask", 400.0, "frame_abi"),
    ("symbol", "printreg", "frame_virtual_unsupported"),
    ("symbol", "benchmark_end", "frame_virtual_unsupported"),
])
def test_refuses_unverifiable_virtual_or_abi(field, value, reason):
    unit, request = _case()
    with pytest.raises(NativeInlineError) as exc:
        prepare_virtual_splice(unit, replace(request, **{field: value}), _BASE)
    assert exc.value.reason == reason


def test_requires_original_plugin_call_relocation():
    unit, request = _case()
    unit.reloc = RelocInfo(True, (), ())
    with pytest.raises(NativeInlineError) as exc:
        prepare_virtual_splice(unit, request, _BASE)
    assert exc.value.reason == "frame_origin"


def test_refuses_mutated_frame_body_or_relocation():
    unit, request = _case()
    splice = prepare_virtual_splice(unit, request, _BASE)
    bad_stack = replace(splice, code=splice.code[:4] + b"\xf0" + splice.code[5:])
    bad_anchor = replace(splice, relocations=(replace(splice.relocations[0], next_off=27),
                                             *splice.relocations[1:]))
    bad_plt = replace(splice, relocations=(*splice.relocations[:2],
                                         replace(splice.relocations[2], target_abs=_PLUGIN + 0xB590),
                                         splice.relocations[3]))
    for mutated in (bad_stack, bad_anchor, bad_plt):
        with pytest.raises(NativeInlineError) as exc:
            verify_virtual_splice_body(replace(request, splice=mutated))
        assert exc.value.reason == "frame_correspondence"


@pytest.mark.parametrize(("prefix", "reason"), [
    (bytes.fromhex("eb01"), "frame_incoming_reference"),  # branch to CALL byte 1
    (bytes.fromhex("8b05feffffff"), "frame_incoming_reference"),  # read across CALL entry
    (bytes.fromhex("4883ec08"), "unsupported_stack"),  # changes call stack phase
    (bytes.fromhex("488d4424f8"), "unsupported_stack"),  # captures return-slot address
])
def test_refuses_unsafe_caller_provenance(prefix, reason):
    unit, request = _case(prefix)
    with pytest.raises(NativeInlineError) as exc:
        prepare_virtual_splice(unit, request, _BASE)
    assert exc.value.reason == reason


def test_captured_benchmark_start_body_matches_live_plugin():
    evidence = os.environ.get("SCOPT_NATIVE_FRAME_EVIDENCE")
    if not evidence:
        pytest.skip("set SCOPT_NATIVE_FRAME_EVIDENCE to the bootstrap evidence root")
    from scopt.formats.scm_v1 import read_scm
    from scopt.importer.import_cache import open_snapshot

    root = Path(evidence)
    baseline = json.loads((root / "baseline.json").read_text())
    runtime = json.loads((root / "runtime-identity.json").read_text())
    image = Path(runtime["plugin_stat"]["path"]).read_bytes()
    source = image[_ENTRY:_ENTRY + len(_SOURCE)]
    snapshot = open_snapshot(baseline["snapshot"])
    helper_types = read_scm(baseline["capture"]).helpers
    matched = 0
    for unit in snapshot.units():
        helper_ids = {h.id for h in unit.bindings.helpers if h.symbol == "benchmark_start"}
        for site in unit.sites:
            if site.kind is not SiteKind.HELPER_CALL or site.aux not in helper_ids:
                continue
            request = _request(unit, site, site.value - _ENTRY, source, sha256_id(image))
            request = replace(request, tcg_typemask=helper_types[site.aux].typemask)
            splice = prepare_virtual_splice(unit, request, snapshot.header.code_address)
            verify_virtual_splice(unit, replace(request, splice=splice), snapshot.header.code_address)
            matched += 1
    assert matched == 1


def _wrapper(address, target):
    from scopt.model.native import NativeFrameBody

    prologue = bytes.fromhex("f30f1efa 55 4889e5")
    displacement = (target - address - len(prologue) - 5).to_bytes(4, "little", signed=True)
    return NativeFrameBody(address, prologue + b"\xe8" + displacement + bytes.fromhex("90 5d c3"))


def _deep_case(count=5):
    from scopt.model.native import NativeFrameBody

    addresses = [0x10000 + 0x100 * i for i in range(count)]
    bodies = [_wrapper(parent, child) for parent, child in zip(addresses, addresses[1:])]
    bodies.append(NativeFrameBody(addresses[-1], bytes.fromhex("89f8 c3")))
    root, *nested = bodies
    unit, request = _case(entry=root.address, source=root.code, image_id=sha256_id(b"new image"))
    return unit, request, tuple(nested)


@pytest.mark.parametrize("requested", [1, 2, 3, 4, 5, 25, 10**100])
def test_numeric_depth_removes_exact_prefix_and_keeps_descendant_evidence(requested):
    unit, request, nested = _deep_case()
    splice = prepare_virtual_splice(unit, request, _BASE, max_depth=requested, frame_chain=nested)
    depth = min(requested, 5)
    assert splice.depth == depth and splice.frame_chain == nested
    calls = [ins for ins in sweep(splice.code, 0) if ins.is_call]
    if depth < 5:
        call, = calls
        reloc, = splice.relocations
        assert call.end == reloc.next_off and reloc.target_abs == _PLUGIN + nested[depth - 1].address
    else:
        assert not calls and not splice.relocations
    assert splice.code.count(bytes.fromhex("488d6424f8")) == depth
    assert splice.code.count(bytes.fromhex("488d642408")) == depth
    proof = InlineCall.from_json(replace(request, splice=splice).to_json())
    verify_virtual_splice_body(proof)
    verify_virtual_splice(unit, proof, _BASE)


def test_large_numeric_frame_traversal_uses_explicit_stack():
    from scopt.validate_sym.native_virtual import _build_frame

    _, request, nested = _deep_case(1050)
    splice = _build_frame(request, 10000, frame_chain=nested)
    assert splice.depth == 1050 and not splice.relocations


def test_deep_frame_rejects_truncated_chain_cycle_and_unsafe_leaf_even_at_depth_one():
    unit, request, nested = _deep_case()
    for changed in (
        nested[:-1],
        (*nested[:-1], replace(nested[-1], address=nested[-2].address)),
        (*nested[:-1], replace(nested[-1], code=bytes.fromhex("4889c5 c3"))),  # RBP clobber
        (*nested[:-1], replace(nested[-1], code=bytes.fromhex("488d4508 c3"))),  # expose return slot
        (*nested[:-1], replace(nested[-1], code=bytes.fromhex("ffe0"))),  # indirect jump
        (*nested[:-1], _wrapper(nested[-1].address, nested[0].address), nested[0]),
    ):
        with pytest.raises(NativeInlineError, match="complete capture-bound chain"):
            prepare_virtual_splice(unit, request, _BASE, max_depth=1, frame_chain=changed)


def test_legacy_frame_receipts_saturate_without_new_fields():
    unit, request = _case()
    assert (prepare_virtual_splice(unit, request, _BASE, max_depth=91)
            == prepare_virtual_splice(unit, request, _BASE))
    unit, request = _case(
        entry=0x26267, source=bytes.fromhex("f30f1efa554889e54883ec10897dfc488975f0e8b7ffffff90c9c3"),
        image_id="sha256:fd9e57f674961120fbeb791dd16b5dac8fa211a7695b4cded64d27ce371677fc",
    )
    limited = prepare_virtual_splice(unit, request, _BASE, max_depth=100)
    assert limited == prepare_virtual_splice(unit, request, _BASE, max_depth=2)
    assert "frame_chain" not in limited.to_json()

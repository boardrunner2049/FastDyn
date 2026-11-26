"""Derived splice capture metadata and physical cross-checks."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from scopt.decode.x86 import sweep
from scopt.decode.search import decode_search, encode_search, is_pcrel
from scopt.formats.enums import Encoding, OpMarkKind, SiteKind
from scopt.formats.layouts import SCM_TB_CHUNK_VERSION_3, SCM_TB_CHUNK_VERSION_4
from scopt.formats.scm_v1 import ScmEncodeError, ScmInsn, TbChunk, parse_scm
from scopt.importer.crosscheck import _native_splice_error, _relocated_splice_code
from scopt.model.native import InlineCall, NativeFragment, NativeFrameBody, NativeOrigin, NativeRelocation, NativeSplice
from scopt.model.unit import Bindings, HelperBinding, Insn, OpMark, RecoveryPoint, Site
from scopt.formats.sc_records import ScRecord
from scopt.splice_layout import (materialize_splice, materialize_splices, prepare_splice_layout,
                                 splice_group_offsets, splice_offsets)
from scopt.validate_sym.native_inline import NativeInlineError
from scopt.validate_sym.native_splice import prepare_splice
from scopt.validate_sym import native_virtual as framed

from tests.synth import builders as B
from tests.synth.fake_snapshot import fake_snapshot


def _fixture():
    raw = next(iter(parse_scm(B.build_scm(B.simple_units(1))).tbs.values()))
    identity = "sha256:" + "2" * 64
    arithmetic = b"\x83\xc0\x01" * 4 + b"\x31\xff"
    callee = b"\x89\xf8" + arithmetic + b"\xc3"
    fragment = NativeFragment(callee[:-1] + b"\xe9\0\0\0\0",
                              tuple(NativeOrigin(i.address, i.size, i.address,
                                                 5 if i.is_ret else i.size)
                                    for i in sweep(callee, 0)), (len(callee),))
    prefix, new_prefix = b"\x8b\x7d\x10", b"\x8b\x45\x10"
    start, call_off, cont = 8, 11, 17
    old_pool = 24
    call = b"\xff\x15" + (old_pool - cont).to_bytes(4, "little", signed=True)
    splice = NativeSplice(start, prefix, new_prefix + arithmetic)
    helper = 0xaabb_ccdd
    request = InlineCall(target="tb:fixture#0", unit_hash=identity, call_off=call_off,
                         call_bytes=call, continuation_off=cont, helper_id=2,
                         target_abs=helper, pool_slot_off=old_pool, guest_pcs=(raw.vaddr,),
                         image_id=identity, elf_address=0x3000, image_offset=0x1000,
                         symbol="helper_fixture", callee_code=callee,
                         native_input_id=identity, fragment=fragment,
                         bridge_sha256=identity, splice=splice, tcg_typemask=18)
    assert request.growth == 8
    pool_start = old_pool + request.growth
    code = b"\x90" * start + splice.code
    code += b"\x90" * (pool_start - len(code)) + helper.to_bytes(8, "little")
    span = ScmInsn(raw.insns[0].words, 0, pool_start)
    chunk = replace(raw, insn0_start=0, insns=(span,), sites=(), pool=(), opmarks=(),
                    body_end=pool_start, ldst_end=pool_start,
                    pool_start=pool_start, code_size=len(code),
                    tb_chunk_version=SCM_TB_CHUNK_VERSION_4, reserved=start,
                    native_fragments=(request,))
    context = SimpleNamespace(helper=lambda _: SimpleNamespace(func=helper))
    return chunk, code, context


def _check(chunk, code, context):
    address = 0x1000
    physical = sweep(code[:chunk.pool_start], address)
    return _native_splice_error(code, chunk, context, address, physical, 10)


def _short_branch_fixture(shorts):
    chunk, _, _ = _fixture()
    old, = chunk.native_fragments
    old = replace(old, target_abs=0x10000000)
    snapshot = fake_snapshot(1)
    unit, = snapshot.units()
    ca = 0x1000 - unit.tc_offset
    base = 0x1000
    code = bytearray(b"\x90" * 144)
    sites = []
    for off, opcode, target in shorts:
        disp = (target - off - 2).to_bytes(1, "little", signed=True)
        code[off:off + 2] = bytes([opcode]) + disp
        kind = SiteKind.LABEL_JMP if opcode == 0xeb else SiteKind.LABEL_JCC
        sites.append(Site(off + 1, off, 1, Encoding.REL8, kind, 0,
                          base + target, 0, disp[0]))
    call_off = old.call_off
    continuation = call_off + 5
    disp = (old.target_abs - base - continuation).to_bytes(4, "little", signed=True)
    arg = old.splice.prefix
    call = b"\xe8" + disp
    code[old.splice.start_off:continuation] = arg + call
    sites.append(Site(call_off + 1, call_off, 4, Encoding.REL32, SiteKind.HELPER_CALL,
                      old.helper_id, old.target_abs, 0, int.from_bytes(disp, "little")))
    rec = ScRecord.from_bytes(unit.record)
    words = chunk.insns[0].words
    search = encode_search([(words, len(code))], pcrel=is_pcrel(rec.cflags), pc=rec.pc)
    end_offset = unit.tc_offset + len(code) + len(search)
    rec = rec.replace(icount=1, tc_size=len(code), end_offset=end_offset)
    unit = replace(unit, code=bytes(code), tc_size=len(code), end_offset=end_offset,
                   record=rec.to_bytes(fix_checksum=True), sites=tuple(sites), pool_start=None,
                   body_end=len(code), ldst_end=len(code), insn0_start=0, recovery=(
                       RecoveryPoint("r0", 138, 0, words, "raddr"),), icount=1, search=search,
                   insns=(Insn(0, words, 0, len(code)),), opmarks=(
                       OpMark(call_off, OpMarkKind.CALL, 0, old.helper_id),),
                   bindings=Bindings(helpers=(
                       HelperBinding(old.helper_id, "fixture", "qemu", old.symbol, 5,
                                     old.target_abs),)))
    request = replace(old, target=unit.uid, unit_hash=unit.content_hash, call_bytes=call,
                      continuation_off=continuation, pool_slot_off=None, splice=None)
    return chunk, unit, replace(request, splice=prepare_splice(unit, request, ca)), ca


def test_cascading_branch_widens_map_the_splice_and_capture():
    chunk, unit, request, ca = _short_branch_fixture(((0, 0x75, 112), (2, 0xeb, 122)))
    planned = prepare_splice_layout(unit, request, ca)
    assert [row.source_off for row in planned.splice.branches] == [0, 2]
    assert planned.splice.physical_start == 24 and planned.growth == request.growth + 16
    mu = splice_offsets(planned)
    materialized = materialize_splice(unit, planned, ca)
    assert materialized.code[:2] == b"\x0f\x85"
    assert materialized.code[6:10] == b"\x90" * 4
    assert materialized.code[10] == 0xe9
    assert materialized.code[15:20] == b"\x90" * 5
    decoded = {insn.address - 0x1000: insn for insn in sweep(materialized.code, 0x1000)}
    assert decoded[0].branch_target == 0x1000 + mu(112)
    assert decoded[10].branch_target == 0x1000 + mu(122)
    assert materialized.recovery[0].host_return_offset == mu(138)
    record = ScRecord.from_bytes(materialized.record)
    rows = decode_search(materialized.search, record.icount, pcrel=is_pcrel(record.cflags),
                         pc=record.pc)
    assert rows[0].host_end == mu(144)
    assert rows[0].words == chunk.insns[0].words

    from scopt.link import _scm_sites

    code = materialized.code
    chunk = replace(chunk, record=materialized.record, insns=(
        ScmInsn(rows[0].words, 0, len(code)),), sites=_scm_sites(materialized),
        pool_start=len(code), body_end=len(code), ldst_end=len(code), code_size=len(code),
        reserved=planned.splice.physical_start, native_fragments=(planned,))
    decoded_chunk, pad = TbChunk.decode(chunk.encode())
    assert not pad and decoded_chunk == chunk
    context = SimpleNamespace(helper=lambda _: SimpleNamespace(func=planned.target_abs))
    assert _check(decoded_chunk, code, context) is None
    assert "differs from the proof" in _check(decoded_chunk, b"\x0f\x84" + code[2:], context)
    assert "differs from the proof" in _check(
        decoded_chunk, code[:6] + b"\xcc" + code[7:], context)
    # A branch that enters the widened Jcc's padding must never be admitted.
    off = mu(102)
    incoming = b"\xe9" + (7 - off - 5).to_bytes(4, "little", signed=True)
    assert "reference overlaps" in _check(
        decoded_chunk, code[:off] + incoming + code[off + 5:], context)
    with pytest.raises(NativeInlineError, match="differ from the source"):
        materialize_splice(unit, request, ca)


def test_integer_conditional_leaf_materializes_and_roundtrips_through_scm():
    chunk, unit, template, ca = _short_branch_fixture(())
    helper = bytes.fromhex("f30f1efa 4889f8 4885f6 7403 4801f0 c3")
    fragment = NativeFragment(helper[:-1] + b"\xe9\0\0\0\0",
                              tuple(NativeOrigin(i.address, i.size, i.address,
                                                 5 if i.is_ret else i.size)
                                    for i in sweep(helper, 0)), (len(helper),))
    req = replace(template, callee_code=helper, fragment=fragment, tcg_typemask=0x124, splice=None)
    req = replace(req, splice=prepare_splice(unit, req, ca))
    req = prepare_splice_layout(unit, req, ca)
    assert req.splice.start_off == req.call_off and req.splice.prefix == b""
    physical = materialize_splice(unit, req, ca)
    assert physical.code[:req.call_off] == unit.code[:req.call_off]
    assert not physical.opmarks and not physical.sites
    code = physical.code
    chunk = replace(chunk, record=physical.record, reserved=req.splice.start_off,
                    native_fragments=(req,), code_size=len(code), pool_start=len(code),
                    body_end=len(code), ldst_end=len(code),
                    insns=(ScmInsn(chunk.insns[0].words, 0, len(code)),))
    parsed, padding = TbChunk.decode(chunk.encode())
    assert not padding and parsed == chunk
    ctx = SimpleNamespace(helper=lambda _: SimpleNamespace(func=req.target_abs))
    assert _check(parsed, code, ctx) is None
    branch_at = req.splice.start_off + 6
    assert code[branch_at] == 0x74
    mutated = code[:branch_at] + b"\x75" + code[branch_at + 1:]
    assert "splice bytes" in _check(parsed, mutated, ctx)


def test_backward_short_conditional_widen_preserves_taken_and_fallthrough():
    _, unit, request, ca = _short_branch_fixture(((130, 0x74, 4),))
    planned = prepare_splice_layout(unit, request, ca)
    mu = splice_offsets(planned)
    result = materialize_splice(unit, planned, ca)
    branch = next(i for i in sweep(result.code, 0x1000)
                  if i.address - 0x1000 == mu(130))
    assert branch.raw[:2] == b"\x0f\x84"
    assert branch.branch_target == 0x1000 + mu(4)
    assert result.code[mu(130) + 6:mu(132)] == b"\x90" * 4
    assert planned.splice.physical_start == request.splice.start_off


@pytest.mark.parametrize("opcode", [0xe2, 0xe3])
def test_short_loop_family_does_not_use_ordinary_relaxation(opcode):
    _, unit, request, ca = _short_branch_fixture(((0, opcode, 126),))
    with pytest.raises(NativeInlineError, match="cannot be widened"):
        prepare_splice_layout(unit, request, ca)


def _two_site_fixture(shorts=()):
    chunk, unit, first, ca = _short_branch_fixture(shorts)
    call_off = 52
    end = call_off + 5
    prefix = b"\x8b\x7d\x14"
    disp = (first.target_abs - 0x1000 - end).to_bytes(4, "little", signed=True)
    call = b"\xe8" + disp
    code = unit.code[:call_off - len(prefix)] + prefix + call + unit.code[end:]
    site = Site(call_off + 1, call_off, 4, Encoding.REL32, SiteKind.HELPER_CALL,
                first.helper_id, first.target_abs, 0, int.from_bytes(disp, "little"))
    unit = replace(unit, code=code, sites=(*unit.sites, site), opmarks=(
        *unit.opmarks, OpMark(call_off, OpMarkKind.CALL, 0, first.helper_id)))
    second = replace(first, call_off=call_off, continuation_off=end, call_bytes=call, splice=None)
    second = replace(second, splice=prepare_splice(unit, second, ca))
    return chunk, unit, (first, second), ca


def test_two_site_materialization_and_roundtrip():
    chunk, unit, requests, ca = _two_site_fixture()
    offsets = splice_group_offsets(requests)
    materialized = materialize_splices(unit, requests, ca)
    for request in requests:
        start = offsets(request.splice.start_off)
        assert materialized.code[start:start + len(request.splice.code)] == request.splice.code
    assert offsets(requests[1].splice.start_off) == requests[1].splice.start_off + requests[0].growth
    assert materialized.native_fragments == requests
    assert not materialized.sites and not materialized.opmarks
    assert materialized.recovery[0].host_return_offset == offsets(138)
    code = materialized.code
    chunk = replace(chunk, record=materialized.record, insns=(
        ScmInsn(chunk.insns[0].words, 0, len(code)),), sites=(),
        pool_start=len(code), body_end=len(code), ldst_end=len(code), code_size=len(code),
        reserved=offsets(requests[0].splice.start_off), native_fragments=requests)
    decoded, pad = TbChunk.decode(chunk.encode())
    assert not pad and decoded == chunk
    context = SimpleNamespace(helper=lambda _: SimpleNamespace(func=requests[0].target_abs))
    assert _check(decoded, code, context) is None
    second = offsets(requests[1].splice.start_off)
    bad = code[:second] + b"\x90" + code[second + 1:]
    assert "bytes or instruction boundaries differ" in _check(decoded, bad, context)
    with pytest.raises(NativeInlineError, match="spans overlap"):
        materialize_splices(unit, (requests[0], requests[0]), ca)
    wrong = replace(requests[1], unit_hash="sha256:" + "d" * 64)
    with pytest.raises(NativeInlineError, match="different inputs"):
        materialize_splices(unit, (requests[0], wrong), ca)


def test_combined_short_range_overflow_preserves_individual_choices():
    _, unit, requests, ca = _two_site_fixture(((0, 0x75, 110),))
    for request in requests:
        assert not prepare_splice_layout(unit, request, ca).splice.branches
    with pytest.raises(NativeInlineError, match="displacement"):
        materialize_splices(unit, requests, ca)



@pytest.mark.parametrize("count", [1, 2])
def test_certified_splice_sources_grow_guards_and_relayout(count):
    from scopt.guard import GuardOffsets, GuardTarget
    from scopt.link import B3Plan, GuardedExitSpec, NativeLinkerV2
    from scopt.model.snapshot import WorkingSet
    from scopt.model.unit import PoolEntry
    from tests.synth.fake_snapshot import FakeSnapshot

    _, unit, requests, ca = _two_site_fixture()
    requests = requests[:count]
    tail = 120
    pool = 144
    lookup = 0x22200000
    disp = (pool - tail - 9).to_bytes(4, "little", signed=True)
    tail_code = bytes.fromhex("48 8b fd ff 15") + disp + bytes.fromhex("ff e0")
    code = unit.code[:tail] + tail_code + unit.code[tail + len(tail_code):]
    code += lookup.to_bytes(8, "little")
    rec = ScRecord.from_bytes(unit.record).replace(
        tc_size=len(code), end_offset=unit.tc_offset + len(code) + len(unit.search))
    sites = (
        Site(tail + 5, tail + 3, 4, Encoding.POOL_INDIRECT, SiteKind.HELPER_CALL,
             3, lookup, 0, int.from_bytes(disp, "little")),
        Site(tail + 9, tail + 9, 0, Encoding.NONE, SiteKind.GOTO_PTR, 0, 0, 0, 0),
    )
    unit = replace(unit, code=code, tc_size=len(code), end_offset=rec.end_offset,
                   record=rec.to_bytes(fix_checksum=True), pool_start=pool,
                   pool=(PoolEntry(tail + 5, pool, 0, 1, 0, lookup),), sites=unit.sites + sites,
                   bindings=replace(unit.bindings, helpers=(
                       *unit.bindings.helpers, HelperBinding(3, "lookup_tb_ptr", "qemu",
                                                            "helper_lookup_tb_ptr", 0, lookup))))
    materialized = materialize_splices(unit, requests, ca)
    base = fake_snapshot(2)
    other = base.units()[1]
    header = replace(base.header, code_address=ca)
    snap = FakeSnapshot((materialized, other), base.snapshot_id, header)
    layout = WorkingSet(snap, sections={"hot": (other.uid, materialized.uid)})
    offsets = splice_group_offsets(requests)
    spec = GuardedExitSpec(offsets(tail), "static", (GuardTarget(0x200, 0, "GENERAL"),),
                           unit.key.flags, unit.key.cs_base)
    out = NativeLinkerV2(snap, B3Plan(grow={unit.uid: (spec,)}), placement=layout,
                         offsets=GuardOffsets(), tb_ret_abs=0xe00,
                         derive_captures=False).link()
    assert out.ok, out.failures
    moved = out.by_uid()[unit.uid]
    assert moved.moved
    assert out.ext.slots[0].src_tb_offset == moved.new_tb_offset
    assert moved.native_fragments == requests
    for request in requests:
        start = offsets(request.splice.start_off)
        assert moved.body[start:start + len(request.splice.code)] == request.splice.code


def test_splice_v4_preserves_certificate_and_uses_shifted_pool():
    chunk, code, context = _fixture()
    parsed, pad = TbChunk.decode(chunk.encode())
    assert not pad and parsed == chunk
    assert parsed.inline_start is None and parsed.pool_end == len(code)
    assert parsed.splice_start == 8
    assert parsed.native_fragments[0].pool_slot_off == 24
    assert _check(parsed, code, context) is None
    with pytest.raises(ScmEncodeError, match="v3_native_layout"):
        replace(chunk, tb_chunk_version=SCM_TB_CHUNK_VERSION_3).encode()
    with pytest.raises(ScmEncodeError, match="v4_native_layout"):
        replace(chunk, reserved=chunk.splice_start + 1).encode()


def test_splice_v4_rejects_interior_entry_bad_bytes_and_wrong_guest_origin():
    chunk, code, context = _fixture()
    jump_to_interior = b"\xe9\x06\0\0\0" + code[5:]
    assert "outside reference" in _check(chunk, jump_to_interior, context)
    # MOV EAX,[RIP+1] at offset 0 reads four bytes beginning one byte before the splice.
    straddling_read = b"\x8b\x05\x01\0\0\0" + code[6:]
    assert "RIP-relative access overlaps" in _check(chunk, straddling_read, context)
    bad_body = code[:8] + b"\x90" + code[9:]
    assert "bytes or instruction boundaries" in _check(chunk, bad_body, context)
    request, = chunk.native_fragments
    wrong = replace(request, guest_pcs=(request.guest_pcs[0] + 2,))
    assert "guest origin" in _check(replace(chunk, native_fragments=(wrong,)), code, context)


def _frame_fixture():
    chunk, _, _ = _fixture()
    old, = chunk.native_fragments
    base = 0x1000
    func = 0x50000000 + framed._BENCHMARK_START
    start = 8
    continuation = start + 5
    disp = (func - (base + continuation)).to_bytes(4, "little", signed=True)
    request = replace(old, call_off=start, call_bytes=b"\xe8" + disp,
                      continuation_off=continuation, target_abs=func, pool_slot_off=None,
                      image_id=framed._PLUGIN_IMAGE_ID, elf_address=framed._BENCHMARK_START,
                      image_offset=framed._BENCHMARK_START, symbol="benchmark_start",
                      callee_code=framed._BENCHMARK_START_CODE, tcg_typemask=400,
                      fragment=NativeFragment(b"", (), ()), splice=None)
    splice = framed._build_frame(request)
    request = replace(request, splice=splice)
    code = b"\x90" * start + _relocated_splice_code(splice, base) + b"\x90"
    span = ScmInsn(chunk.insns[0].words, 0, len(code))
    chunk = replace(chunk, insns=(span,), body_end=len(code), ldst_end=len(code),
                    pool_start=len(code), code_size=len(code), native_fragments=(request,))
    ctx = SimpleNamespace(helper=lambda _: SimpleNamespace(func=func))
    return chunk, code, ctx


def test_frame_v4_roundtrip_and_checks_physical_relocations():
    chunk, code, context = _frame_fixture()
    parsed, pad = TbChunk.decode(chunk.encode())
    assert not pad and parsed == chunk
    request, = parsed.native_fragments
    splice = request.splice
    assert splice.kind == "frame"
    assert len(splice.relocations) == 4
    for row in splice.relocations:
        field = splice.start_off + row.field_off
        displacement = int.from_bytes(code[field:field + 4], "little", signed=True)
        assert 0x1000 + splice.start_off + row.next_off + displacement == row.target_abs
    assert _check(parsed, code, context) is None
    field = splice.start_off + splice.relocations[0].field_off
    altered = code[:field] + bytes([code[field] ^ 1]) + code[field + 1:]
    assert "bytes or instruction boundaries" in _check(parsed, altered, context)


def test_frame_v4_rejects_overlapping_or_noncanonical_relocations():
    chunk, _, _ = _frame_fixture()
    request, = chunk.native_fragments
    splice = request.splice
    first, *rest = splice.relocations
    for changed in (
        replace(splice, relocations=(first, *splice.relocations)),
        replace(splice, code=splice.code[:first.field_off] + b"\x01" +
                splice.code[first.field_off + 1:]),
    ):
        with pytest.raises(ScmEncodeError, match="v4_native_layout"):
            replace(chunk, native_fragments=(replace(request, splice=changed),)).encode()
    with pytest.raises(ScmEncodeError, match="v4_native_layout"):
        replace(chunk, native_fragments=(replace(request, tcg_typemask=0),)).encode()
    outside = NativeRelocation(first.field_off, first.next_off, 1 << 63)
    with pytest.raises(ValueError, match="signed rel32"):
        _relocated_splice_code(replace(splice, relocations=(outside, *rest)), 0x1000)


def test_v4_depth_is_canonical_and_limited_by_splice_kind():
    chunk, _, _ = _frame_fixture()
    request, = chunk.native_fragments
    assert "depth" not in request.splice.to_json()
    for depth in (2, 3, 4, 17):
        chain = () if depth == 2 else tuple(NativeFrameBody(16*i, b"\xc3") for i in range(1, depth))
        deep = replace(request, splice=replace(request.splice, depth=depth, frame_chain=chain))
        changed = replace(chunk, native_fragments=(deep,))
        assert TbChunk.decode(changed.encode())[0] == changed
        assert deep.splice.to_json()["depth"] == depth
    for depth in (True, 0, -1, 3, 1 << 64):
        changed = replace(chunk, native_fragments=(replace(
            request, splice=replace(request.splice, depth=depth)),))
        with pytest.raises(ScmEncodeError, match="v4_native_(data|layout)"):
            changed.encode()

    integer, _, _ = _fixture()
    request, = integer.native_fragments
    for splice in (replace(request.splice, depth=2),
                   replace(request.splice, frame_chain=(NativeFrameBody(16, b"\xc3"),))):
        with pytest.raises(ScmEncodeError, match="v4_native_(data|layout)"):
            replace(integer, native_fragments=(replace(request, splice=splice),)).encode()


def test_splice_followed_by_guard_keeps_both_boundaries():
    chunk, code, _ = _fixture()
    grown = replace(chunk, tb_chunk_version=5, reserved=len(code), code_size=len(code) + 32)
    decoded, padding = TbChunk.decode(grown.encode())
    assert not padding and decoded == grown
    assert decoded.pool_end == decoded.stub_start == len(code)
    assert decoded.splice_start == chunk.splice_start
    with pytest.raises(ScmEncodeError):
        replace(grown, reserved=chunk.splice_start + 1).encode()


def test_frame_cleanup_proof_rejects_mutated_targets_and_stack_observers():
    from scopt.validate_sym.frame_cleanup import cleanup_frame, verify_cleanup
    from scopt.validate_sym.native_inline import NativeInlineError

    code = bytes.fromhex("e800000000 90 488d642408 90 c9")
    refs = [NativeRelocation(1, 5, 0x12345000)]
    compact, newrefs = cleanup_frame(code, refs)
    assert compact == bytes.fromhex("e800000000 c9")
    verify_cleanup(code, refs, compact, newrefs)
    with pytest.raises(NativeInlineError, match="target"):
        verify_cleanup(code, refs, compact, [replace(newrefs[0], target_abs=0)])
    for observer in ("488b0424", "50", "e800000000"):
        original = bytes.fromhex("488d642408" + observer + "c9")
        with pytest.raises(NativeInlineError, match="RSP"):
            verify_cleanup(original, (), original[5:], ())

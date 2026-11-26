"""Versioned capture metadata for appended native code."""

from dataclasses import replace

import pytest

from scopt.formats import scm_v1 as S
from scopt.formats.layouts import SCM_TB_CHUNK_VERSION_3
from scopt.model.native import InlineCall, NativeFragment, NativeOrigin

from tests.synth import builders as B


def _request():
    identity = "sha256:" + "9" * 64
    fragment = NativeFragment(b"\x8d\x47\x01\xe9\0\0\0\0",
                              (NativeOrigin(0, 3, 0, 3), NativeOrigin(3, 1, 3, 5)), (4,))
    return InlineCall(target="tb:fixture#0", unit_hash=identity, call_off=1,
                      call_bytes=b"\xe8\0\0\0\0", continuation_off=6, helper_id=2,
                      target_abs=0x3000, pool_slot_off=None, guest_pcs=(0x8000000,),
                      image_id=identity, elf_address=0x3000, image_offset=0x1000,
                      symbol="helper_fixture", callee_code=b"\x8d\x47\x01\xc3",
                      native_input_id=identity, fragment=fragment, bridge_sha256=identity)


def _chunk():
    original = next(iter(S.parse_scm(B.build_scm(B.simple_units(1))).tbs.values()))
    request = _request()
    return replace(original, code_size=original.code_size + request.growth,
                   tb_chunk_version=SCM_TB_CHUNK_VERSION_3, reserved=original.code_size,
                   native_fragments=(request,))


def test_native_capture_preserves_span_origin_and_exact_bytes():
    old = _chunk()
    new, pad = S.TbChunk.decode(old.encode())
    assert not pad
    assert new == old
    assert new.inline_start == new.pool_end == old.reserved
    assert new.stub_start is None
    assert new.native_fragments[0].fragment.origins[1].output_size == 5
    assert new.encode() == old.encode()


def test_native_capture_rejects_bad_geometry_and_corrupted_certificate():
    chunk = _chunk()
    with pytest.raises(S.ScmEncodeError, match="native_layout"):
        replace(chunk, reserved=chunk.code_size).encode()
    with pytest.raises(S.ScmEncodeError, match="native_layout"):
        replace(chunk, native_fragments=()).encode()
    payload = bytearray(chunk.encode())
    marker = payload.index(b"NZ1\0")
    payload[marker + 8] ^= 0x80
    with pytest.raises(S.ScmDecodeError, match="native_data"):
        S.TbChunk.decode(bytes(payload))


@pytest.mark.parametrize("indirect", [False, True])
def test_native_materialization_preserves_offsets_and_search(indirect):
    from scopt.formats.enums import Encoding, OpMarkKind, SiteKind
    from scopt.formats.sc_records import ScRecord
    from scopt.link import transplant_native
    from scopt.model.unit import Fixup, OpMark, PoolEntry, RecoveryPoint, RelocInfo, Site

    from tests.synth.fake_snapshot import fake_snapshot

    snapshot = fake_snapshot(1)
    unit = snapshot.units()[0]
    site_address = snapshot.header.code_address + unit.tc_offset
    target = site_address + 0x1000
    if indirect:
        raw = b"\xff\x15\x02\0\0\0"
        code = raw + b"\x90\x90" + target.to_bytes(8, "little")
        encoding, field, pool_start = Encoding.POOL_INDIRECT, 2, 8
        pool = (PoolEntry(field, pool_start, 2, 1, -4, target),)
        fixups = ()
    else:
        raw = b"\xe8" + (target - site_address - 5).to_bytes(4, "little")
        code = raw + b"\x90\x90\x90"
        encoding, field, pool_start = Encoding.REL32, 1, None
        pool = ()
        fixups = (Fixup(field, 4, "rel_minus_delta", int(SiteKind.HELPER_CALL), "helper", target),)
    request = replace(_request(), target=unit.uid, unit_hash=unit.content_hash, call_off=0,
                      call_bytes=raw, continuation_off=len(raw), target_abs=target,
                      pool_slot_off=pool_start)
    old = replace(unit, code=code, tc_size=len(code), ldst_end=8, pool_start=pool_start,
                  sites=(Site(field, 0, 4, encoding, SiteKind.HELPER_CALL, request.helper_id,
                              target, 0, int.from_bytes(raw[field:], "little")),),
                  pool=pool, reloc=RelocInfo(True, (), fixups),
                  opmarks=(OpMark(0, OpMarkKind.CALL, 0, request.helper_id),),
                  recovery=(RecoveryPoint("r0", len(raw), 0, (0, 0, 0), "helper_call"),),
                  native_fragments=(request,))
    new = transplant_native(old, code_address=snapshot.header.code_address)
    assert new.code[0] == 0xE9
    assert 5 + int.from_bytes(new.code[1:5], "little", signed=True) == len(code)
    assert new.code[5:len(raw)] == b"\x90" * (len(raw) - 5)
    assert new.code[len(raw):len(code)] == code[len(raw):]
    fragment = new.code[len(code):]
    assert len(code) + len(fragment) + int.from_bytes(fragment[-4:], "little", signed=True) == len(raw)
    assert new.inline_start == new.pool_end == len(code)
    assert new.search == old.search
    assert not (new.pool or new.reloc.fixups or new.recovery or new.opmarks)
    assert len(new.sites) == 1 and new.sites[0].kind is SiteKind.LABEL_JMP
    record = ScRecord.from_bytes(new.record)
    assert record.checksum_ok
    assert record.tc_size == len(new.code)
    assert record.end_offset == unit.tc_offset + len(new.code) + len(unit.search)

"""Check a v5 direct-exit retirement on the output bytes and recorded metadata."""
from collections import Counter
from dataclasses import replace

from .canon import fnv1a64
from .formats.enums import Encoding, OpMarkKind, SiteKind
from .formats.sc_records import ScRecord


def _check(condition, message):
    if not condition:
        raise ValueError(f"ordinary exit verification: {message}")


def _one(items, message):
    values = tuple(items)
    _check(len(values) == 1, message)
    return values[0]


def _site(rows, body, position, displacement, kind, encoding, aux, index, target, tc):
    """Check a signed 32-bit x86 operand and the SCM record describing it."""
    site = _one((s for s in rows if s.insn_off == position), "missing or ambiguous relocation")
    encoded = body[displacement:displacement + 4]
    address = tc + displacement + 4 + int.from_bytes(encoded, "little", signed=True)
    _check(len(encoded) == 4 and address == target, "incorrect native target")
    _check((site.field_off, site.width, site.kind, site.enc, site.aux, site.insn_idx,
            site.value, site.final_field) == (displacement, 4, kind, encoding, aux, index,
            target, int.from_bytes(encoded, "little")), "incorrect relocation metadata")
    return site


def verify_boundary(unit, original, body, record, metadata, slots, code_address, epilogue, tb_ret):
    """Prove both entry paths and account for every byte and metadata change."""
    old = ScRecord.from_bytes(unit.record)
    new = ScRecord.from_bytes(record)
    _check(tb_ret == epilogue + 2, "epilogue must clear EAX and fall through to the original return")
    _check(original.record == unit.record and metadata.record == record, "SCM record identity")
    _check(new.to_bytes(fix_checksum=True) == record and metadata.code_fnv64 == fnv1a64(body),
           "output record checksum or native/search hash")
    _check(len(body) == len(unit.code) + len(unit.search) and len(unit.code) == old.tc_size,
           "code or search length changed")
    _check(bool(slots) and len(set(slots)) == len(slots) and set(slots) <= {0, 1}, "invalid retired slots")

    # Reverse only authorized edits. The comparisons below cover the entire
    # native body, search data, record and three SCM arrays, including entries
    # unrelated to this exit or attached to the same guest instruction.
    restored = bytearray(body)
    before_sites, after_sites = Counter(original.sites), Counter(metadata.sites)
    before_marks, after_marks = Counter(original.opmarks), Counter(metadata.opmarks)
    before_exits, after_exits = Counter(original.static_exits), Counter(metadata.static_exits)
    fields = list(new.jmp_insn_offset)
    resets = list(new.jmp_reset_offset)
    spans = []
    tc = code_address + old.tc_offset
    for slot in slots:
        start = old.jmp_insn_offset[slot] - 1
        end = start + 17
        _check(0 <= start < end <= len(unit.code) and all(end <= a or b <= start for a, b in spans),
               "overlap or out-of-range native exit")
        _check(old.jmp_reset_offset[slot] == start + 5 and fields[slot] == resets[slot] == 0xffff,
               "slot reset offsets")
        fields[slot], resets[slot] = old.jmp_insn_offset[slot], old.jmp_reset_offset[slot]
        spans.append((start, end))
        _check(unit.code[start:start+8] == bytes.fromhex("e900000000488d05") and unit.code[start+12] == 0xe9,
               "input must be reset JMP, LEA RAX and direct JMP")
        _check(body[start] == body[start+5] == 0xe9 and body[start+10:end] == b"\x90"*7,
               "new entry paths must each jump directly to the common null return")
        guest = _one((i for i in unit.insns if i.host_start <= start and end <= i.host_end),
                     "exit spans guest instructions").index
        old_operands = ((start,start+1,SiteKind.GOTO_TB,Encoding.GOTO_TB_REL32,tc+start+5),
                        (start+5,start+8,SiteKind.MOVI_EXIT_TB_PTR,Encoding.RIPREL32,code_address+old.tb_offset+slot),
                        (start+12,start+13,SiteKind.EXIT_TB_RET_JMP,Encoding.REL32,tb_ret))
        for pos, field, kind, encoding, target in old_operands:
            before_sites[_site(original.sites, unit.code, pos, field, kind, encoding, slot, guest, target, tc)] -= 1
        for pos in (start, start+5):
            after_sites[_site(metadata.sites, body, pos, pos+1, SiteKind.EXIT_TB_EPI_JMP,
                              Encoding.REL32, 0, guest, epilogue, tc)] -= 1
        reset_mark = _one((m for m in original.opmarks if m.kind == OpMarkKind.GOTO_TB and
                           m.insn_idx == guest and start-8 <= m.host_off <= start), "original GOTO_TB marker")
        exit_mark = _one((m for m in original.opmarks if m.kind == OpMarkKind.EXIT_TB and
                          m.insn_idx == guest and m.host_off == start+5), "original EXIT_TB marker")
        before_marks[reset_mark] -= 1
        after_marks[replace(exit_mark, host_off=start)] -= 1
        direct = _one((x for x in original.static_exits if x.slot == slot and x.direct == 1
                       and x.insn_idx == guest), "original static exit")
        before_exits[direct] -= 1
        after_exits[replace(direct, direct=0, slot=0xff)] -= 1
        restored[start:end] = unit.code[start:end]

    _check(restored == unit.code + unit.search, "other code or search bytes changed")
    _check(new.replace(jmp_insn_offset=tuple(fields), jmp_reset_offset=tuple(resets),
                       checksum=old.checksum) == old, "unrelated v5 record field changed")
    _check(before_sites == after_sites and before_marks == after_marks and before_exits == after_exits,
           "unaccounted relocation, operation marker or static exit")
    restored_metadata = replace(metadata, record=original.record, code_fnv64=original.code_fnv64,
                                sites=original.sites, opmarks=original.opmarks, static_exits=original.static_exits)
    _check(restored_metadata == original, "unrelated SCM contents changed")

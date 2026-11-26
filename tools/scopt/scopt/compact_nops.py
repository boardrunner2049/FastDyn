"""Compact a captured x86-64 direct-exit TB without moving its mutable rel32 fields off alignment."""
from __future__ import annotations

from dataclasses import dataclass, replace

from .canon import fnv1a64
from .decode.search import decode_search, encode_search, is_pcrel, unwind
from .decode.x86 import sweep, tiles
from .formats.enums import Encoding, NONE_U32, OpMarkKind, SiteKind, TbFlag
from .formats.sc_records import ScRecord


class Ineligible(ValueError):
    pass


@dataclass(frozen=True)
class Compaction:
    branch: int
    store: int

    def offset(self, old: int) -> int:
        """Map surviving instruction boundaries and operands in unchanged instructions."""
        if self.branch < old < self.branch + 6 or self.store < old < self.store + 6:
            raise Ineligible("metadata enters a replaced instruction span")
        return old - (4 if old >= self.branch + 6 else 0)


def choose(insns, code, chunk, record, tc):
    if (chunk.tb_chunk_version != 1 or chunk.flags & (TbFlag.OPS_PRESENT | TbFlag.PLUGIN_CBS_PRESENT)
            or chunk.ldst or chunk.pool or chunk.pool_start != NONE_U32
            or chunk.body_end != chunk.ldst_end or chunk.body_end != len(code)):
        raise Ineligible("requires ordinary pool-free code without slow paths")
    # A public goto_tb patch field must stay aligned and contain the reset displacement.
    # A disp32 form of the preceding PC store consumes the entire three-byte padding.
    for store, nop, jump in zip(insns, insns[1:], insns[2:]):
        field = jump.address - tc + 1
        if (store.raw != b"\x89\x5d\x3c" or nop.mnemonic != "nop" or nop.size != 3
                or jump.raw != b"\xe9\x00\x00\x00\x00" or field not in record.jmp_insn_offset
                or (tc + field) % 4):
            continue
        for insn in insns:
            if (insn.address >= store.address or insn.size != 6 or insn.raw[0] != 0x0f
                    or not 0x80 <= insn.raw[1] <= 0x8f or insn.branch_target is None):
                continue
            plan = Compaction(insn.address - tc, store.address - tc)
            try:
                delta = plan.offset(insn.branch_target - tc) - (plan.branch + 2)
            except Ineligible:
                continue
            if -128 <= delta <= 127:
                return plan
    raise Ineligible("no eligible short branch and three-byte exit padding")


def verify_reencoding(old, new, tc, plan, old_rows, new_rows, code_size):
    """Decode the emitted stream independently and compare meanings and search states."""
    retained = [ins for ins in old if ins.address != tc + plan.store + 3]
    if not tiles(new, tc, tc + code_size - 4) or len(new) != len(retained):
        raise Ineligible("output instruction boundaries changed")
    for original, emitted in zip(retained, new, strict=True):
        if (emitted.address != tc + plan.offset(original.address - tc)
                or original.mnemonic != emitted.mnemonic):
            raise Ineligible("output mnemonic or instruction address changed")
        if original.branch_target is None and original.rip_target is None:
            if original.op_str != emitted.op_str:
                raise Ineligible("output operands changed")
        for source, actual in ((original.branch_target, emitted.branch_target),
                               (original.rip_target, emitted.rip_target)):
            expected = (tc + plan.offset(source - tc)
                        if source is not None and tc <= source < tc + code_size else source)
            if actual != expected:
                raise Ineligible("output reference target changed")
        if unwind(old_rows, original.end - tc) != unwind(new_rows, emitted.end - tc):
            raise Ineligible("output search state changed")


def compact(unit, chunk, arena):
    """Return checked physical bytes, derived SCM and the two changed source offsets."""
    rec, old = unit.record, unit.code
    tc = arena + rec.tc_offset
    insns = sweep(old, tc)
    if not tiles(insns, tc, tc + len(old)):
        raise Ineligible("input code does not tile")
    plan = choose(insns, old, chunk, rec, tc)
    branch, store = plan.branch, plan.store
    nop = store + 3
    code = bytearray(old[:branch] + bytes([old[branch + 1] - 0x10, 0])
                     + old[branch + 6:store] + b"\x89\x9d\x3c\x00\x00\x00" + old[store + 6:])
    starts = {i.address for i in insns}
    site_map = {s.insn_off: s for s in chunk.sites}
    if len(site_map) != len(chunk.sites):
        raise Ineligible("overlapping site descriptions")
    seen, sites = set(), []

    for ins in insns:
        off = ins.address - tc
        if off in (store, nop):
            continue
        target = ins.branch_target if ins.branch_target is not None else ins.rip_target
        if target is None:
            if ins.is_jump or ins.is_ret or ins.is_call or off in site_map:
                raise Ineligible("unclassified indirect control transfer or operand")
            continue
        site = site_map.get(off)
        if site is None or site.value != target or ins.is_call:
            raise Ineligible("reference lacks a matching captured site")
        if site.kind in (SiteKind.LABEL_JCC, SiteKind.LABEL_JMP, SiteKind.GOTO_TB):
            if target not in starts or target == tc + nop or ins.branch_target is None:
                raise Ineligible("control transfer targets removed padding or non-instruction")
            new_target = tc + plan.offset(target - tc)
        elif site.kind == SiteKind.MOVI_EXIT_TB_PTR and ins.mnemonic == "lea":
            if not arena + rec.tb_offset <= target < tc:
                raise Ineligible("exit pointer outside its TB structure")
            new_target = target
        elif site.kind in (SiteKind.EXIT_TB_RET_JMP, SiteKind.EXIT_TB_EPI_JMP) and ins.mnemonic == "jmp":
            if arena + rec.tb_offset <= target < arena + rec.end_offset:
                raise Ineligible("external epilogue target inside TB")
            new_target = target
        else:
            raise Ineligible("unsupported relocation")
        pos = plan.offset(off)
        if off == branch:
            if site.kind != SiteKind.LABEL_JCC or site.enc != Encoding.REL32:
                raise Ineligible("conditional branch has unexpected capture")
            size, field, width, enc = 2, pos + 1, 1, Encoding.REL8
        else:
            field = plan.offset(site.field_off)
            size, width, enc = ins.size, site.width, site.enc
            relative_field = ins.imm_offset if ins.branch_target is not None else ins.disp_offset
            relative_width = ins.imm_size if ins.branch_target is not None else ins.disp_size
            if (field != pos + relative_field or width != relative_width or width not in (1, 4)):
                raise Ineligible("displacement fields disagree")
        try:
            operand = (new_target - (tc + pos + size)).to_bytes(width, "little", signed=True)
        except OverflowError as exc:
            raise Ineligible("relocated branch out of range") from exc
        code[field:field + width] = operand
        seen.add(off)
        sites.append(replace(site, insn_off=pos, field_off=field, width=width, enc=int(enc),
                             value=new_target, final_field=int.from_bytes(operand, "little")))
    if seen != site_map.keys():
        raise Ineligible("unaccounted site in replacement spans")

    rows = decode_search(unit.search, rec.icount, is_pcrel(rec.cflags), pc=rec.pc)
    if any(unit.search[rows.nbytes:]):
        raise Ineligible("opaque trailing search bytes")
    search = encode_search([(r.words, plan.offset(r.host_end)) for r in rows],
                           is_pcrel(rec.cflags), pc=rec.pc)
    search += bytes((-(rec.tc_offset + len(code) + len(search))) % 16)

    def slots(items):
        return tuple(v if v == 0xffff else plan.offset(v) for v in items)

    record = rec.replace(tc_size=len(code), end_offset=rec.tc_offset + len(code) + len(search),
                         jmp_insn_offset=slots(rec.jmp_insn_offset), jmp_reset_offset=slots(rec.jmp_reset_offset))
    raw = record.to_bytes(fix_checksum=True)
    for field in record.jmp_insn_offset:
        if field != 0xffff and ((tc + field) % 4 or code[field - 1:field + 4] != b"\xe9\0\0\0\0"):
            raise Ineligible("unpatchable goto_tb field")
    marks = []
    for mark in chunk.opmarks:
        if mark.host_off == nop and mark.kind == OpMarkKind.GOTO_TB:
            new_off = plan.offset(store + 6)
        else:
            new_off = plan.offset(mark.host_off)
        marks.append(replace(mark, host_off=new_off))
    if chunk.reserved or chunk.native_fragments:
        raise Ineligible("extended capture")
    mapped = replace(chunk, record=raw, code_size=len(code), code_fnv64=fnv1a64(code + search),
                     sites=tuple(sites), insns=tuple(replace(i, host_start=plan.offset(i.host_start),
                         host_end=plan.offset(i.host_end)) for i in chunk.insns), opmarks=tuple(marks),
                     insn0_start=plan.offset(chunk.insn0_start), body_end=plan.offset(chunk.body_end),
                     ldst_end=plan.offset(chunk.ldst_end), source=None)
    output = replace(unit, record=ScRecord.from_bytes(raw), record_bytes=raw, body=bytes(code) + search)
    verify_reencoding(insns, sweep(bytes(code), tc), tc, plan, rows,
                      decode_search(search, rec.icount, is_pcrel(rec.cflags), pc=rec.pc), len(old))
    return output, mapped, plan

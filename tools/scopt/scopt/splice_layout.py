"""Offset and relocation reconstruction for one fall-through splice per TB."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .canon import sha256_id
from .decode.search import decode_search, encode_search, is_pcrel
from .decode.x86 import sweep, tiles
from .formats.enums import Encoding, OpMarkKind, SiteKind
from .formats.sc_v5 import ScRecord
from .model.native import InlineCall
from .model.unit import Fixup, TBUnit
from .validate_sym.native_inline import NativeInlineError


@dataclass(frozen=True, slots=True)
class SpliceOffsets:
    start: int
    end: int
    growth: int

    def __call__(self, offset: int) -> int:
        if offset <= self.start:
            return offset
        if offset >= self.end:
            return offset + self.growth
        raise NativeInlineError("splice_interior_offset", f"original offset {offset:#x} is replaced")


def splice_offsets(request: InlineCall) -> SpliceOffsets:
    splice = request.splice
    if splice is None:
        raise NativeInlineError("splice_missing", "missing fall-through replacement")
    return SpliceOffsets(splice.start_off, request.continuation_off, request.growth)


def _signed(value: int, width: int) -> bytes:
    try:
        return value.to_bytes(width, "little", signed=True)
    except OverflowError as exc:
        raise NativeInlineError("splice_reference_range",
                                f"displacement {value:#x} does not fit in {width} bytes") from exc


def _native_layout(unit: TBUnit, request: InlineCall, code_address: int) -> TBUnit:
    """Move every surviving TCG offset and preserve the original search words."""
    from .validate_sym.native_splice import verify_splice
    from .validate_sym.native_virtual import verify_virtual_splice

    splice = request.splice
    mu = splice_offsets(request)
    if (unit.splice_applied or unit.inline_start is not None or unit.stub_start is not None
            or unit.meta_status != "ok" or not unit.reloc.relocatable or mu.growth < 0
            or mu.growth % 8):
        raise NativeInlineError("splice_layout_unsupported", "needs an original relocatable TB and aligned growth")
    assert splice is not None
    verify = verify_virtual_splice if splice.kind == "frame" else verify_splice
    verify(unit, request, code_address)
    start, end = mu.start, mu.end
    tc = code_address + unit.tc_offset
    pool_start = unit.pool_start
    exec_end = unit.ldst_end if pool_start is None else pool_start
    code = bytearray(unit.code[:start] + splice.code + unit.code[end:])
    insns = sweep(unit.code[:exec_end], tc)
    if not tiles(insns, tc, tc + exec_end):
        raise NativeInlineError("splice_decode", "original TCG code does not tile")

    def address(value: int) -> int:
        return tc + mu(value - tc) if tc <= value < tc + unit.tc_size else value

    for insn in insns:
        off = insn.address - tc
        if start <= off < end:
            continue
        if insn.branch_target is not None:
            target, width, field = insn.branch_target, insn.imm_size, off + insn.imm_offset
        elif insn.rip_target is not None:
            target, width, field = insn.rip_target, insn.disp_size, off + insn.disp_offset
        else:
            continue
        if width not in (1, 4):
            raise NativeInlineError("splice_reference_width", f"unsupported reference at {off:#x}")
        new_field = mu(field)
        code[new_field:new_field + width] = _signed(address(target) - (tc + mu(off) + insn.size), width)

    chosen = [s for s in unit.sites if s.kind is SiteKind.HELPER_CALL and s.insn_off == request.call_off]
    if len(chosen) != 1:
        raise NativeInlineError("splice_call_binding", "expected one original helper site")
    call = chosen[0]
    drop = call.field_off
    old_marks = [m for m in unit.opmarks if m.kind is OpMarkKind.CALL and m.host_off <= call.insn_off]
    call_mark = max(old_marks, key=lambda m: m.host_off, default=None)
    if call_mark is None or call_mark.helper_id != request.helper_id or call_mark.insn_idx != call.insn_idx:
        raise NativeInlineError("splice_call_binding", "helper provenance has no matching CALL opmark")

    fixups = []
    for f in unit.reloc.fixups:
        if f.field_off == drop:
            continue
        new_field = mu(f.field_off)
        target = f.target_abs
        new_target = None if target is None else address(target)
        if f.rule == "abs_plus_delta" and target is not None and target != new_target:
            if int.from_bytes(unit.code[f.field_off:f.field_off + f.width], "little") != target:
                raise NativeInlineError("splice_reference_value", "absolute fixup does not match its operand")
            try:
                code[new_field:new_field + f.width] = new_target.to_bytes(f.width, "little")
            except OverflowError as exc:
                raise NativeInlineError("splice_reference_range", "absolute fixup out of range") from exc
        fixups.append(replace(f, field_off=new_field, target_abs=new_target))
    for reloc in splice.relocations:
        field = start + reloc.field_off
        disp = reloc.target_abs - (tc + start + reloc.next_off)
        code[field:field + 4] = _signed(disp, 4)
        fixups.append(Fixup(field, 4, "rel_minus_delta", int(SiteKind.MOVI_CONST),
                            "ext.native", reloc.target_abs))

    pool, pool_index = [], {}
    for i, p in enumerate(unit.pool):
        if p.user_field_off == drop:
            continue
        pool_index[i] = len(pool)
        slot, new_value = mu(p.slot_off), address(p.data0)
        if new_value != p.data0:
            if p.nlong != 1 or int.from_bytes(code[slot:slot + 8], "little") not in (p.data0, new_value):
                raise NativeInlineError("splice_pool_value", "ambiguous position-dependent pool pointer")
            code[slot:slot + 8] = new_value.to_bytes(8, "little")
        pool.append(replace(p, user_field_off=mu(p.user_field_off), slot_off=slot, data0=new_value))

    sites, site_index = [], {}
    for i, site in enumerate(unit.sites):
        if site == call or (site.kind is SiteKind.POOL_REF and site.field_off == drop):
            continue
        if start <= site.insn_off < end or start <= site.field_off < end:
            raise NativeInlineError("splice_unexpected_site", f"another captured site at {site.insn_off:#x}")
        new_field = mu(site.field_off)
        new_value = address(site.value)
        if (site.enc in (Encoding.IMM64, Encoding.IMM32_ZX, Encoding.IMM32_SX)
                and new_value != site.value):
            try:
                code[new_field:new_field + site.width] = new_value.to_bytes(site.width, "little")
            except OverflowError as exc:
                raise NativeInlineError("splice_reference_range", "absolute site out of range") from exc
        aux = site.aux
        if site.kind is SiteKind.POOL_REF:
            if aux not in pool_index:
                raise NativeInlineError("splice_pool_user", "surviving site depends on removed pool user")
            aux = pool_index[aux]
        site_index[i] = len(sites)
        sites.append(replace(site, field_off=new_field, insn_off=mu(site.insn_off), aux=aux,
                             value=new_value, final_field=int.from_bytes(
                                 code[new_field:new_field + min(site.width, 4)], "little")))

    remapped_insns = tuple(replace(x, host_start=mu(x.host_start), host_end=mu(x.host_end))
                           for x in unit.insns)
    marks = tuple(replace(m, host_off=mu(m.host_off)) for m in unit.opmarks if m != call_mark)
    ldst = tuple(replace(row,
                         jcc_fields=tuple(None if f is None else mu(f) for f in row.jcc_fields),
                         slow_start=mu(row.slow_start), slow_end=mu(row.slow_end),
                         raddr_off=mu(row.raddr_off)) for row in unit.ldst)
    keep_recovery = [p for p in unit.recovery if p.source != "helper_call" or p.host_return_offset != end]
    recovery = tuple(replace(p, id=f"r{i}", host_return_offset=mu(p.host_return_offset))
                     for i, p in enumerate(keep_recovery))
    pointers = tuple(replace(p, site=site_index[p.site]) for p in unit.bindings.unchecked_host_pointers
                     if p.site in site_index)

    old_record = ScRecord.from_bytes(unit.record)
    pcrel = is_pcrel(old_record.cflags)
    old_rows = decode_search(unit.search, unit.icount, pcrel=pcrel, pc=old_record.pc)
    search = encode_search([(row.words, mu(row.host_end)) for row in old_rows], pcrel=pcrel,
                           pc=old_record.pc) + unit.search[old_rows.nbytes:]

    def jump_offsets(offsets: tuple[int, ...]) -> tuple[int, ...]:
        return tuple(x if x == 0xFFFF else mu(x) for x in offsets)

    end_offset = unit.tc_offset + len(code) + len(search)
    record = old_record.replace(tc_size=len(code), end_offset=end_offset,
                                jmp_insn_offset=jump_offsets(old_record.jmp_insn_offset),
                                jmp_reset_offset=jump_offsets(old_record.jmp_reset_offset))
    raw_record = record.to_bytes(fix_checksum=True)
    code = bytes(code)
    return replace(unit, record=raw_record, code=code, search=search, tc_size=len(code),
                   end_offset=end_offset, insn0_start=mu(unit.insn0_start), body_end=mu(unit.body_end),
                   ldst_end=mu(unit.ldst_end), pool_start=None if pool_start is None else mu(pool_start),
                   insns=remapped_insns, sites=tuple(sites), pool=tuple(pool), opmarks=marks,
                   ldst=ldst, recovery=recovery, bindings=replace(unit.bindings, unchecked_host_pointers=pointers),
                   reloc=replace(unit.reloc, fixups=tuple(sorted(fixups, key=lambda f: f.field_off))),
                   native_fragments=(request,),
                   splice_applied=True,
                   content_hash=sha256_id(raw_record + code + search + request.certificate_id.encode()))


def materialize_splice(unit: TBUnit, request: InlineCall, code_address: int) -> TBUnit:
    try:
        return _native_layout(unit, request, code_address)
    except (OverflowError, ValueError) as exc:
        if isinstance(exc, NativeInlineError):
            raise
        raise NativeInlineError("splice_metadata_range", str(exc)) from exc


def check_splice_layout(unit: TBUnit, request: InlineCall, code_address: int) -> None:
    materialize_splice(unit, request, code_address)

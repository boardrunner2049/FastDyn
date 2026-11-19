"""Offset and relocation reconstruction for certified fall-through splices."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .canon import sha256_id
from .decode.search import decode_search, encode_search, is_pcrel
from .decode.x86 import sweep, tiles
from .formats.enums import Encoding, OpMarkKind, SiteKind
from .formats.sc_v5 import ScRecord
from .model.native import (BRANCH_RELAXATION_GROWTH, MAX_BRANCH_RELAXATIONS,
                           MAX_INTEGER_SPLICES, InlineCall, NativeBranchRelaxation)
from .model.unit import Fixup, TBUnit
from .validate_sym.native_inline import NativeInlineError


@dataclass(frozen=True, slots=True)
class SpliceOffsets:
    start: int
    end: int
    growth: int
    branches: tuple[NativeBranchRelaxation, ...] = ()

    def __call__(self, offset: int) -> int:
        if self.start < offset < self.end:
            raise NativeInlineError("splice_interior_offset", f"original offset {offset:#x} is replaced")
        if any(offset == row.source_off + 1 for row in self.branches):
            raise NativeInlineError("splice_interior_offset", f"offset {offset:#x} enters a widened branch")
        return (offset + (self.growth if offset >= self.end else 0)
                + BRANCH_RELAXATION_GROWTH * sum(
                    row.source_off + 2 <= offset for row in self.branches))

    def field(self, offset: int) -> int:
        for row in self.branches:
            if offset == row.source_off + 1:
                return self(row.source_off) + len(row.near_opcode)
        return self(offset)


def splice_offsets(request: InlineCall) -> SpliceOffsets:
    splice = request.splice
    if splice is None:
        raise NativeInlineError("splice_missing", "missing fall-through replacement")
    return SpliceOffsets(splice.start_off, request.continuation_off,
                         len(splice.code) - (request.continuation_off - splice.start_off),
                         splice.branches)


@dataclass(frozen=True, slots=True)
class SpliceGroupOffsets:
    spans: tuple[SpliceOffsets, ...]

    def __call__(self, offset: int) -> int:
        return offset + sum(span(offset) - offset for span in self.spans)

    def field(self, offset: int) -> int:
        return offset + sum(span.field(offset) - offset for span in self.spans)

    def contains(self, offset: int) -> bool:
        return any(span.start <= offset < span.end for span in self.spans)


def splice_group_offsets(requests: tuple[InlineCall, ...]) -> SpliceGroupOffsets:
    if not 1 <= len(requests) <= MAX_INTEGER_SPLICES:
        raise NativeInlineError("splice_site_budget", "invalid number of splices for one TB")
    if len({(req.target, req.unit_hash, req.native_input_id, req.bridge_sha256)
            for req in requests}) != 1:
        raise NativeInlineError("splice_group_provenance", "splice certificates have different inputs")
    spans = []
    previous_end = 0
    for request in requests:
        span = splice_offsets(request)
        splice = request.splice
        if len(requests) > 1 and (splice.kind != "integer" or splice.branches or splice.relocations):
            raise NativeInlineError("splice_group_unsupported",
                                    "multiple sites require integer splices without branch widening")
        if not previous_end <= span.start < span.end or span.growth < 0 or span.growth % 8:
            raise NativeInlineError("splice_group_geometry",
                                    "splice spans overlap, differ in order, or have unaligned growth")
        spans.append(span)
        previous_end = span.end
    return SpliceGroupOffsets(tuple(spans))


def _address(mu, tc: int, size: int, value: int) -> int:
    return tc + mu(value - tc) if tc <= value < tc + size else value


def _required_relaxations(unit, request, tc, instructions):
    """Find the first fixed point of required widens in original host order."""
    splice = request.splice
    mu = replace(splice_offsets(request), branches=())
    candidates = [insn for insn in instructions if insn.imm_size == 1
                  and insn.branch_target is not None
                  and not mu.start <= insn.address - tc < mu.end]
    selected = {}
    starts = {insn.address for insn in instructions}
    sites = {}
    for site in unit.sites:
        sites.setdefault(site.insn_off, []).append(site)
    for _ in range(MAX_BRANCH_RELAXATIONS + 1):
        new = []
        for insn in candidates:
            off = insn.address - tc
            if off in selected:
                continue
            target = insn.branch_target
            delta = _address(mu, tc, unit.tc_size, target) - (tc + mu(off) + insn.size)
            if -128 <= delta <= 127:
                continue
            raw = unit.code[off:off + insn.size]
            matches = sites.get(off, ())
            site = matches[0] if len(matches) == 1 else None
            if (splice.kind != "integer" or len(raw) != 2
                    or not (raw[0] == 0xeb or 0x70 <= raw[0] <= 0x7f)
                    or target not in starts or site is None or site.field_off != off + 1
                    or site.width != 1 or site.enc != Encoding.REL8
                    or site.kind != (SiteKind.LABEL_JMP if raw[0] == 0xeb else SiteKind.LABEL_JCC)
                    or site.value != target or site.final_field != raw[1]):
                raise NativeInlineError("splice_reference_range",
                                        f"short branch at {off:#x} cannot be widened")
            new.append(NativeBranchRelaxation(off, raw))
        if not new:
            return mu.branches
        selected.update((row.source_off, row) for row in new)
        if len(selected) > MAX_BRANCH_RELAXATIONS:
            break
        mu = replace(mu, branches=tuple(selected[off] for off in sorted(selected)))
    raise NativeInlineError("splice_branch_budget", "too many short branches need widening")


def _signed(value: int, width: int) -> bytes:
    try:
        return value.to_bytes(width, "little", signed=True)
    except OverflowError as exc:
        raise NativeInlineError("splice_reference_range",
                                f"displacement {value:#x} does not fit in {width} bytes") from exc


def _native_layout(unit: TBUnit, requests: tuple[InlineCall, ...], code_address: int, *,
                   prepare: bool = False) -> TBUnit:
    """Move every surviving TCG offset and preserve the original search words."""
    from .validate_sym.native_splice import verify_splice
    from .validate_sym.native_virtual import verify_virtual_splice

    if (unit.splice_applied or unit.inline_start is not None or unit.stub_start is not None
            or unit.meta_status != "ok" or not unit.reloc.relocatable):
        raise NativeInlineError("splice_layout_unsupported", "needs an original relocatable TB and aligned growth")
    mu = splice_group_offsets(requests)
    for request in requests:
        verify = verify_virtual_splice if request.splice.kind == "frame" else verify_splice
        verify(unit, request, code_address)
    tc = code_address + unit.tc_offset
    pool_start = unit.pool_start
    exec_end = unit.ldst_end if pool_start is None else pool_start
    insns = sweep(unit.code[:exec_end], tc)
    if not tiles(insns, tc, tc + exec_end):
        raise NativeInlineError("splice_decode", "original TCG code does not tile")

    branches = ()
    if len(requests) == 1:
        request, = requests
        splice = request.splice
        branches = _required_relaxations(unit, request, tc, insns)
        if prepare:
            splice = replace(splice, branches=branches)
            requests = (replace(request, splice=splice),)
            mu = splice_group_offsets(requests)
        elif branches != splice.branches:
            raise NativeInlineError("splice_branch_correspondence", "widened branches differ from the source")
    widened = {row.source_off: row for row in branches}
    pieces = [(request.splice.start_off, request.continuation_off, request.splice.code)
              for request in requests]
    for row in branches:
        opcode = row.near_opcode
        replacement = opcode + bytes(4) + b"\x90" * (6 - len(opcode))
        pieces.append((row.source_off, row.source_off + 2, replacement))
    before = 0
    code = bytearray()
    for lo, hi, data in sorted(pieces):
        code.extend(unit.code[before:lo])
        code.extend(data)
        before = hi
    code.extend(unit.code[before:])

    def address(value: int) -> int:
        return _address(mu, tc, unit.tc_size, value)

    for insn in insns:
        off = insn.address - tc
        if mu.contains(off):
            continue
        if insn.branch_target is not None:
            target, width, field = insn.branch_target, insn.imm_size, off + insn.imm_offset
        elif insn.rip_target is not None:
            target, width, field = insn.rip_target, insn.disp_size, off + insn.disp_offset
        else:
            continue
        size = insn.size
        if off in widened:
            width = 4
            size = len(widened[off].near_opcode) + width
        if width not in (1, 4):
            raise NativeInlineError("splice_reference_width", f"unsupported reference at {off:#x}")
        new_field = mu.field(field)
        code[new_field:new_field + width] = _signed(address(target) - (tc + mu(off) + size), width)

    drop_sites, drop_fields, drop_marks = set(), set(), set()
    for request in requests:
        chosen = [s for s in unit.sites if s.kind is SiteKind.HELPER_CALL and s.insn_off == request.call_off]
        if len(chosen) != 1:
            raise NativeInlineError("splice_call_binding", "expected one original helper site per splice")
        call, = chosen
        old_marks = [m for m in unit.opmarks if m.kind is OpMarkKind.CALL and m.host_off <= call.insn_off]
        mark = max(old_marks, key=lambda m: m.host_off, default=None)
        if (mark is None or mark.helper_id != request.helper_id or mark.insn_idx != call.insn_idx
                or mark in drop_marks or call.field_off in drop_fields):
            raise NativeInlineError("splice_call_binding", "helper provenance has no distinct CALL opmark")
        drop_sites.add(call)
        drop_fields.add(call.field_off)
        drop_marks.add(mark)

    fixups = []
    for f in unit.reloc.fixups:
        if f.field_off in drop_fields:
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
    for request in requests:
        splice = request.splice
        start = mu(splice.start_off)
        for reloc in splice.relocations:
            field = start + reloc.field_off
            disp = reloc.target_abs - (tc + start + reloc.next_off)
            code[field:field + 4] = _signed(disp, 4)
            fixups.append(Fixup(field, 4, "rel_minus_delta", int(SiteKind.MOVI_CONST),
                                "ext.native", reloc.target_abs))

    pool, pool_index = [], {}
    for i, p in enumerate(unit.pool):
        if p.user_field_off in drop_fields:
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
        if site in drop_sites or (site.kind is SiteKind.POOL_REF and site.field_off in drop_fields):
            continue
        if mu.contains(site.insn_off) or mu.contains(site.field_off):
            raise NativeInlineError("splice_unexpected_site", f"another captured site at {site.insn_off:#x}")
        branch = widened.get(site.insn_off)
        new_field = mu.field(site.field_off) if branch else mu(site.field_off)
        width = 4 if branch else site.width
        enc = Encoding.REL32 if branch else site.enc
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
                             width=width, enc=enc,
                             value=new_value, final_field=int.from_bytes(
                                 code[new_field:new_field + min(width, 4)], "little")))

    remapped_insns = tuple(replace(x, host_start=mu(x.host_start), host_end=mu(x.host_end))
                           for x in unit.insns)
    marks = tuple(replace(m, host_off=mu(m.host_off)) for m in unit.opmarks if m not in drop_marks)
    ldst = tuple(replace(row,
                         jcc_fields=tuple(None if f is None else mu(f) for f in row.jcc_fields),
                         slow_start=mu(row.slow_start), slow_end=mu(row.slow_end),
                         raddr_off=mu(row.raddr_off)) for row in unit.ldst)
    returns = {req.continuation_off for req in requests}
    keep_recovery = [p for p in unit.recovery
                     if p.source != "helper_call" or p.host_return_offset not in returns]
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
    if branches:
        bounds = [value for item in remapped_insns for value in (item.host_start, item.host_end)]
        bounds.extend(mark.host_off for mark in marks)
        bounds.extend(point.host_return_offset for point in recovery)
        verify_relaxed_branches(requests[0], code, tc, sites, sweep(code[:mu(exec_end)], tc), bounds)
    return replace(unit, record=raw_record, code=code, search=search, tc_size=len(code),
                   end_offset=end_offset, insn0_start=mu(unit.insn0_start), body_end=mu(unit.body_end),
                   ldst_end=mu(unit.ldst_end), pool_start=None if pool_start is None else mu(pool_start),
                   insns=remapped_insns, sites=tuple(sites), pool=tuple(pool), opmarks=marks,
                   ldst=ldst, recovery=recovery, bindings=replace(unit.bindings, unchecked_host_pointers=pointers),
                   reloc=replace(unit.reloc, fixups=tuple(sorted(fixups, key=lambda f: f.field_off))),
                   native_fragments=requests,
                   splice_applied=True,
                   content_hash=sha256_id(raw_record + code + search +
                                          b"".join(req.certificate_id.encode() for req in requests)))


def materialize_splice(unit: TBUnit, request: InlineCall, code_address: int) -> TBUnit:
    return materialize_splices(unit, (request,), code_address)


def materialize_splices(unit: TBUnit, requests: tuple[InlineCall, ...], code_address: int) -> TBUnit:
    try:
        return _native_layout(unit, requests, code_address)
    except (OverflowError, ValueError) as exc:
        if isinstance(exc, NativeInlineError):
            raise
        raise NativeInlineError("splice_metadata_range", str(exc)) from exc


def verify_relaxed_branches(request, code, tc, sites, physical, boundaries=()):
    """Verify the near instructions and their inert padding against the certificate."""
    splice = request.splice
    if not splice.branches:
        return
    if not splice.valid_branch_spans(request.continuation_off, len(code) - request.growth):
        raise NativeInlineError("splice_branch_correspondence", "invalid widened branch geometry")
    mu = splice_offsets(request)
    by_off = {insn.address - tc: insn for insn in physical}
    for row in splice.branches:
        off = mu(row.source_off)
        target = mu(row.target_off)
        after = mu(row.source_off + 2)
        near = row.near_opcode
        field = off + len(near)
        width = len(near) + 4
        short_delta = target - off - 2
        if row.target_off >= row.source_off + 2:
            short_delta -= BRANCH_RELAXATION_GROWTH
        exact = near + _signed(target - off - width, 4) + b"\x90" * (after - off - width)
        branch = by_off.get(off)
        kind = SiteKind.LABEL_JMP if near == b"\xe9" else SiteKind.LABEL_JCC
        matches = [s for s in sites if s.insn_off == off]
        if (not branch or branch.size != width or branch.branch_target != tc + target
                or target not in by_off or after not in by_off
                or code[off:after] != exact or -128 <= short_delta <= 127
                or len(matches) != 1 or matches[0].kind != kind
                or matches[0].enc != Encoding.REL32 or matches[0].field_off != field
                or matches[0].width != 4 or matches[0].value != tc + target
                or matches[0].final_field != int.from_bytes(code[field:field + 4], "little")
                or any(off < boundary < after for boundary in boundaries)):
            raise NativeInlineError("splice_branch_correspondence",
                                    f"widened branch at {off:#x} differs from the proof")
        for insn in physical:
            target_abs, rip, operand = insn.branch_target, insn.rip_target, insn.rip_mem
            if (target_abs is not None and tc + off < target_abs < tc + after
                    or rip is not None and operand is not None and rip < tc + after
                    and tc + off < rip + max(operand.size, 1)):
                raise NativeInlineError("splice_branch_entry", "reference overlaps a widened branch")
        for site in sites:
            insn = by_off.get(site.insn_off)
            if (off < site.insn_off < after or site is not matches[0] and off < site.field_off < after
                    or tc + off <= site.value < tc + after and
                    (insn is None or insn.branch_target != tc + off)):
                raise NativeInlineError("splice_branch_entry", "captured reference overlaps a widened branch")


def prepare_splice_layout(unit: TBUnit, request: InlineCall, code_address: int) -> InlineCall:
    try:
        return _native_layout(unit, (request,), code_address, prepare=True).native_fragments[0]
    except (OverflowError, ValueError) as exc:
        if isinstance(exc, NativeInlineError):
            raise
        raise NativeInlineError("splice_metadata_range", str(exc)) from exc

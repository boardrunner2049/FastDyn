"""V5 x86-64 ordinary direct exit retirement on the reviewed QEMU runtime."""
from dataclasses import dataclass, replace
import struct

from .canon import fnv1a64, sha256_id
from .decode.search import CF_PCREL
from .decode.x86 import sweep
from .formats.enums import Encoding, OpMarkKind, SiteKind
from .formats.scm_v1 import ScmSite
from .formats.sc_v5 import ScRecord

RUNTIME = "sha256:2da1d94f82da8f182282877b47959507b1282e11469e7b418fdf7913e99f28b6"
PASS = "retire-ordinary-exits"
SPAN = 17
NO_SLOT = 0xffff
# The high byte is the QEMU CPU cluster, not a translation behavior flag.
LOCAL_CFLAGS = 0x00ffffff


@dataclass(frozen=True, slots=True)
class OrdinaryExit:
    target: str
    unit_hash: str
    slot: int
    successor: str
    transfers: int
    trace_sha256: str

    def to_json(self):
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class NativeEntries:
    branches: tuple[tuple[int, int], ...]
    data: tuple[tuple[int, int], ...]


def require(condition, description):
    if not condition:
        raise ValueError(description)


def one(rows, description):
    found = list(rows)
    require(len(found) == 1, description)
    return found[0]


def check_runtime(snapshot):
    require(snapshot.header.version == 5 and
            (snapshot.runtime or {}).get("qemu_sha256") == RUNTIME,
            "ordinary exits require the reviewed x86-64 QEMU v5 runtime")


def capture(snapshot, join):
    from .formats.scm_v1 import RunInfo

    check_runtime(snapshot)
    require(join is not None and bool(join.captures), "ordinary exits require a bound SCM capture")
    header = snapshot.header
    pairs = set()
    for f in join.captures:
        ri = one((c.body for c in f.chunks if isinstance(c.body, RunInfo)), "capture needs one RUN_INFO")
        require((ri.code_address, ri.prologue_end, ri.runtime_signature) ==
                (header.code_address, header.prologue_end, header.runtime_signature) and
                len(ri.prologue) == header.prologue_end and
                sha256_id(ri.prologue) == snapshot.runtime.get("prologue_sha256"),
                "captured TCG prologue does not bind the runtime identity")
        start, tail = ri.epilogue_off, ri.tb_ret_off
        require(0 <= start and tail == start + 2 < header.prologue_end and
                ri.prologue[start:tail] in (b"\x33\xc0", b"\x31\xc0"),
                "common return must clear EAX and fall through to the original TB return")
        pairs.add((start, tail))
    epilogue, tb_ret = one(pairs, "inconsistent epilogue and TB return across captures")
    return header.code_address + epilogue, header.code_address + tb_ret


def targets(units, code_address):
    branches, data = [], []
    for u in units:
        end = u.pool_start if u.pool_start is not None else u.ldst_end
        for insn in sweep(u.code[:end], code_address + u.tc_offset):
            if insn.branch_target is not None:
                branches.append((insn.address, insn.branch_target))
            if insn.rip_target is not None:
                length = 1 if insn.is_lea else max(1, insn.rip_mem.size)
                data.append((insn.rip_target, insn.rip_target + length))
        data.extend((pool.data0, pool.data0 + 1) for pool in u.pool)
    return NativeEntries(tuple(branches),tuple(data))


def retire(unit, chunk, selected, code_address, epilogue, tb_ret, entries):
    """Construct same-placement replacement bytes and a standard v5 capture row."""
    require(unit.origin == "elf_verified" and unit.reloc.relocatable and not
            (unit.stub_start is not None or unit.native_fragments), "unverified exit source unit")
    require(chunk.record == unit.record and chunk.code_fnv64 == fnv1a64(unit.code + unit.search),
            "exit source capture does not match original record and bytes")
    record = ScRecord.from_bytes(unit.record)
    require(record.cflags & LOCAL_CFLAGS == CF_PCREL and record.pc == 0,
            "ordinary exits require a normal non-icount CF_PCREL translation")
    body = bytearray(unit.code)
    sites, marks, exits = list(chunk.sites), list(chunk.opmarks), list(chunk.static_exits)
    reset, fields = list(record.jmp_reset_offset), list(record.jmp_insn_offset)
    spans = []
    tc = code_address + unit.tc_offset
    for slot in selected:
        require(type(slot) is int and slot in (0, 1), "invalid ordinary exit slot")
        goto = one((s for s in sites if s.kind == SiteKind.GOTO_TB and s.aux == slot),
                   "missing or duplicate GOTO_TB site")
        at = goto.insn_off
        end = at + SPAN
        lea = one((s for s in sites if s.kind == SiteKind.MOVI_EXIT_TB_PTR and s.insn_off == at+5),
                  "missing or duplicate exit-TB pointer site")
        ret = one((s for s in sites if s.kind == SiteKind.EXIT_TB_RET_JMP and s.insn_off == at+12),
                  "missing or duplicate TB return site")
        require((goto.field_off, fields[slot], reset[slot]) == (at+1, at+1, at+5) and
                (tc + at + 1) % 4 == 0 and lea.field_off == at+8 and ret.field_off == at+13 and
                (goto.enc, lea.enc, ret.enc) == (Encoding.GOTO_TB_REL32, Encoding.RIPREL32, Encoding.REL32) and
                all(s.width == 4 and s.aux == slot and
                    s.insn_idx == goto.insn_idx for s in (goto, lea, ret)), "inconsistent exit slot fields")
        require(0 <= at < end <= unit.body_end and all(end <= a or b <= at for a,b in spans) and
                any(i.index == goto.insn_idx and i.host_start <= at < end <= i.host_end for i in unit.insns),
                "edited exit must fit one guest instruction")
        require(not any(tc+at < t < tc+end and (a,t) != (tc+at,tc+at+5)
                        for a,t in entries.branches) and
                not any(tc+at < t and a < tc+end for a,t in entries.data) and
                not any(at <= p.host_return_offset < end for p in unit.recovery) and
                not any(at <= p.user_field_off < end or at <= p.slot_off < end for p in unit.pool) and
                {s for s in sites if at <= s.insn_off < end or at <= s.field_off < end} == {goto,lea,ret},
                "an interior observer or reference reaches the exit")
        old = bytes(body[at:end])
        require(old[:8] == bytes.fromhex("e900000000488d05") and old[12] == 0xe9,
                "uncanonical original ordinary exit")
        orig = sweep(old, tc + at)
        require(len(orig) == 3 and orig[0].branch_target == goto.value == tc+at+5 and
                orig[1].rip_target == lea.value == code_address+unit.tb_offset+slot and
                orig[2].branch_target == ret.value == tb_ret, "old exit has unexpected decoded targets")
        gm = one((m for m in marks if m.kind == OpMarkKind.GOTO_TB and
                  m.insn_idx == goto.insn_idx and at-8 <= m.host_off <= at), "original GOTO_TB opmark")
        em = one((m for m in marks if m.kind == OpMarkKind.EXIT_TB and m.host_off == at+5 and
                  m.insn_idx == goto.insn_idx), "original EXIT_TB opmark")
        require(not any(at <= m.host_off < end and m not in (gm,em) for m in marks),
                "unrelated opmark inside the exit")
        x = one((x for x in exits if x.slot == slot and x.direct and x.insn_idx == goto.insn_idx),
                "original direct StaticExit")
        sites = [s for s in sites if s not in (goto,lea,ret)]
        for addr in (at,at+5):
            disp = epilogue - (tc + addr + 5)
            body[addr:addr+5] = b"\xe9" + struct.pack("<i",disp)
            sites.append(ScmSite(addr+1, addr, 4, int(Encoding.REL32),
                                 int(SiteKind.EXIT_TB_EPI_JMP),0,epilogue,goto.insn_idx,disp & 0xffffffff))
        body[at+10:end] = b"\x90"*7
        dst = sweep(bytes(body[at:end]), tc+at)
        require(len(dst) == 9 and all(i.branch_target == epilogue for i in dst[:2]) and
                all(i.raw == b"\x90" for i in dst[2:]), "uncanonical replacement epilogue")
        marks = [m for m in marks if m not in (gm,em)] + [replace(em,host_off=at),em]
        exits = [replace(z,direct=0,slot=255) if z == x else z for z in exits]
        reset[slot] = fields[slot] = NO_SLOT
        spans.append((at,end))
    new_record = record.replace(jmp_reset_offset=tuple(reset),jmp_insn_offset=tuple(fields)).to_bytes(fix_checksum=True)
    payload = bytes(body) + unit.search
    return payload, replace(chunk, record=new_record, code_fnv64=fnv1a64(payload),
                            sites=tuple(sorted(sites,key=lambda s:s.field_off)),
                            opmarks=tuple(sorted(marks,key=lambda m:m.host_off)), static_exits=tuple(exits), source=None)


def retired_slots(before, after):
    a, b = map(ScRecord.from_bytes, (before, after))
    return tuple(n for n in (0,1) if a.jmp_insn_offset[n] != NO_SLOT and
                 b.jmp_insn_offset[n] == b.jmp_reset_offset[n] == NO_SLOT)

"""SuperCache v7 firmware-region directory and sparse writer.

V7 leaves all public v5 records intact.  A small fixed header at file offset
0x100 points to an arena-resident, checksummed directory; therefore the number
of regions and members is limited only by the actual TCG arena, not the legacy
64 KiB metadata gap.
"""

from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..canon import canonical_json, fnv1a64
from .layouts import SC_CODE_OFFSET
from .sc_v5 import ScFormatError, ScHeader, ScRecord, read_sc, write_sc

__all__ = ["SC7_VERSION", "SC7_MAGIC", "SC7_DIRECTORY_VERSION",
           "SC7_TRUSTED_DIRECTORY_VERSION", "SC7_REGION_COMPOSITE",
           "SC7_REGION_TRUSTED", "SC7_EDGE_TRUSTED_SKIP_POLL",
           "SC7_TRUSTED_ENTRY_OFFSET", "Sc7Directory",
           "directory_size", "loader_requirement",
           "build_directory", "write_sc7", "read_sc7", "decode_directory",
           "sc7_snapshot_id", "LOADER_V7", "LOADER_V7_TRUSTED"]

SC7_VERSION = 7
SC7_OFFSET = 0x100
SC7_MAGIC = b"QSC7FWRG"
SC7_FEATURE_FIRMWARE_REGIONS = 1
SC7_DIRECTORY_VERSION = 2
SC7_TRUSTED_DIRECTORY_VERSION = 3
SC7_REGION_COMPOSITE = 1
SC7_REGION_TRUSTED = 2
SC7_EDGE_TRUSTED_SKIP_POLL = 1
SC7_TRUSTED_ENTRY_OFFSET = 15
LOADER_V7 = "qemu-sc-v7-composite-firmware-regions"
LOADER_V7_TRUSTED = "qemu-sc-v7-trusted-firmware-regions"

_HEADER = struct.Struct("<8sIIIIIIQQQ8sQ8s")
_REGION = struct.Struct("<8s8sIIIIQQQII")
_MEMBER = struct.Struct("<QQQQQQQQ8sII")
_EDGE = struct.Struct("<IIIIBBHIQ")


@dataclass(frozen=True, slots=True)
class Sc7Directory:
    header: bytes
    table: bytes
    table_offset: int
    max_end: int
    n_regions: int
    n_members: int
    n_edges: int
    artifact_id8: bytes
    directory_version: int = SC7_DIRECTORY_VERSION

    @property
    def trusted(self) -> bool:
        return self.directory_version == SC7_TRUSTED_DIRECTORY_VERSION


def loader_requirement(directory: Sc7Directory) -> str:
    return LOADER_V7_TRUSTED if directory.trusted else LOADER_V7


def directory_size(regions: Sequence[Any]) -> int:
    members = sum(len(r.members) for r in regions)
    edges = sum(len(m.patched_edges) for r in regions for m in r.members)
    return _REGION.size * len(regions) + _MEMBER.size * members + _EDGE.size * edges


def _id8(value: str) -> bytes:
    raw = value.split(":", 1)[-1]
    try:
        return bytes.fromhex(raw)[:8]
    except ValueError:
        return hashlib.sha256(value.encode()).digest()[:8]


def _artifact_id8(table: bytes, payloads: Sequence[tuple[bytes, bytes, bytes]],
                  directory_version: int) -> bytes:
    return hashlib.sha256(f"scopt.sc7/{directory_version}".encode() + table + b"".join(
        hashlib.sha256(descriptor + code + search).digest()
        for descriptor, code, search in payloads)).digest()[:8]


def build_directory(link: Any, source_tb: Mapping[str, int]) -> Sc7Directory:
    trust_modes = {bool(region.trusted) for region in link.firmware_regions}
    if len(trust_modes) != 1:
        raise ScFormatError("v7 artifact cannot mix safe and trusted firmware regions")
    trusted = trust_modes == {True}
    directory_version = (SC7_TRUSTED_DIRECTORY_VERSION if trusted else
                         SC7_DIRECTORY_VERSION)
    region_rows = bytearray()
    member_rows = bytearray()
    edge_rows = bytearray()
    first_member = first_edge = 0
    for ri, region in enumerate(link.firmware_regions):
        if not region.members or region.entry_member != 0:
            raise ScFormatError(f"firmware region {ri}: invalid entry member")
        descriptor_size = len(region.members[0].descriptor)
        next_descriptor = region.start_offset
        next_code = region.members[0].tc_offset
        code_end = next_code + sum(len(m.code) for m in region.members)
        next_search = code_end
        for mi, member in enumerate(region.members):
            expected_descriptor = region.start_offset if trusted else next_descriptor
            if (len(member.descriptor) != descriptor_size or
                    member.tb_offset != expected_descriptor):
                raise ScFormatError(f"firmware region {ri} member {mi}: descriptors are "
                                    "not contiguous in member order")
            if member.tc_offset != next_code:
                raise ScFormatError(f"firmware region {ri} member {mi}: code is not "
                                    "contiguous in member order")
            expected_search = code_end if trusted else next_search
            if member.search_offset != expected_search:
                raise ScFormatError(f"firmware region {ri} member {mi}: search data is not "
                                    "contiguous and out of line")
            if not trusted:
                next_descriptor += descriptor_size
            next_code += len(member.code)
            next_search += len(member.search)
        descriptor_end = region.start_offset + descriptor_size if trusted else next_descriptor
        if (not descriptor_size or descriptor_end > region.members[0].tc_offset or
                region.end_offset != next_search):
            raise ScFormatError(f"firmware region {ri}: invalid composite geometry")
        region_member_base = first_member
        region_edge_base = first_edge
        n_edges = sum(len(m.patched_edges) for m in region.members)
        entry = region.members[region.entry_member]
        try:
            entry_source_tb = source_tb[entry.source_uid]
        except KeyError:
            raise ScFormatError(f"firmware region {ri}: unknown entry source {entry.source_uid}") \
                from None
        region_rows += _REGION.pack(
            _id8(region.region_id), _id8(region.callsite_id), region.entry_member,
            region_member_base, len(region.members), region_edge_base,
            entry_source_tb, region.start_offset, region.end_offset,
            n_edges, SC7_REGION_COMPOSITE | (SC7_REGION_TRUSTED if trusted else 0))
        for local_member, member in enumerate(region.members):
            if len(member.code) != member.tc_size or len(member.search) != member.search_size:
                raise ScFormatError(f"firmware region {ri} member {local_member}: "
                                    "code/search size differs from its descriptor")
            try:
                member_source_tb = source_tb[member.source_uid]
            except KeyError:
                raise ScFormatError(f"firmware region {ri} member {local_member}: unknown "
                                    f"source {member.source_uid}") from None
            context = hashlib.sha256(canonical_json(list(member.inline_context))).digest()[:8]
            member_rows += _MEMBER.pack(
                member_source_tb, member.tb_offset, member.tc_offset,
                member.tc_size, member.search_offset,
                fnv1a64(member.code), fnv1a64(member.search), member.guest_vaddr,
                context, ri, 0)
            global_source = region_member_base + local_member
            for exit_id, field, target in member.patched_edges:
                if not 0 <= target < len(region.members):
                    raise ScFormatError(f"firmware region {ri} member {local_member}: edge "
                                        f"target {target} is outside the region")
                if not 0 <= field <= member.tc_size - 4:
                    raise ScFormatError(f"firmware region {ri} member {local_member}: edge "
                                        f"field {field:#x} is outside the code")
                kind = 1
                edge_flags = SC7_EDGE_TRUSTED_SKIP_POLL if trusted else 0
                edge_rows += _EDGE.pack(ri, global_source, region_member_base + target,
                                        field, kind, edge_flags, 0, 0,
                                        fnv1a64(exit_id.encode()))
            first_member += 1
        first_edge += n_edges
    table = bytes(region_rows + member_rows + edge_rows)
    if len(table) != link.firmware_table_size:
        raise ScFormatError(f"firmware directory size {len(table)} != reserved "
                            f"{link.firmware_table_size}")
    payloads = [(m.descriptor, m.code, m.search)
                for r in link.firmware_regions for m in r.members]
    aid = _artifact_id8(table, payloads, directory_version)
    max_end = link.firmware_table_offset + len(table)
    prefix = _HEADER.pack(SC7_MAGIC, directory_version, _HEADER.size,
                          len(link.firmware_regions),
                          first_member, first_edge, _REGION.size, link.firmware_table_offset,
                          len(table), max_end, aid, 0, b"\0" * 8)
    checksum = fnv1a64(table, fnv1a64(prefix[:64]))
    header = prefix[:64] + struct.pack("<Q", checksum) + prefix[72:]
    return Sc7Directory(header, table, link.firmware_table_offset, max_end,
                        len(link.firmware_regions), first_member, first_edge, aid,
                        directory_version)


def write_sc7(path: str | os.PathLike, header: ScHeader, units: Iterable,
              link: Any, *, check: bool = True, fsync: bool = True) -> ScHeader:
    parts = list(units)
    source_tb = {u.uid: u.new_tb_offset for u in link.units}
    directory = build_directory(link, source_tb)
    extra = [(SC7_OFFSET, directory.header),
             (SC_CODE_OFFSET + directory.table_offset, directory.table)]
    for region in link.firmware_regions:
        for mi, member in enumerate(region.members):
            if (len(member.descriptor) != header.tb_size or
                    member.descriptor != b"\0" * header.tb_size):
                raise ScFormatError("private firmware descriptor must be canonical zero bytes")
            if not directory.trusted or mi == 0:
                extra.append((SC_CODE_OFFSET + member.tb_offset, member.descriptor))
            extra.append((SC_CODE_OFFSET + member.tc_offset, member.code))
            extra.append((SC_CODE_OFFSET + member.search_offset, member.search))
    hdr = header.replace(version=SC7_VERSION, reserved=SC7_FEATURE_FIRMWARE_REGIONS)
    if check:
        arena_spans: list[tuple[int, int, str]] = []
        for i, item in enumerate(parts):
            rec = ScRecord.from_bytes(item[0] if isinstance(item, tuple) else item.record)
            arena_spans.append((rec.tb_offset, rec.end_offset, f"public record {i}"))
        arena_spans.append((directory.table_offset, directory.max_end, "firmware directory"))
        for ri, region in enumerate(link.firmware_regions):
            for mi, member in enumerate(region.members):
                if not directory.trusted or mi == 0:
                    arena_spans.append(
                        (member.tb_offset, member.tb_offset + len(member.descriptor),
                         f"region {ri} member {mi} descriptor"))
                arena_spans.extend((
                    (member.tc_offset, member.tc_offset + len(member.code),
                     f"region {ri} member {mi} code"),
                    (member.search_offset, member.search_offset + len(member.search),
                     f"region {ri} member {mi} search"),
                ))
        for start, end, label in arena_spans:
            if not 0 <= start <= end <= hdr.code_size:
                raise ScFormatError(f"{label} [0x{start:x},0x{end:x}) outside the arena")
        ordered = sorted(arena_spans)
        for (a, b, al), (c, d, cl) in zip(ordered, ordered[1:]):
            if c < b:
                raise ScFormatError(f"{al} [0x{a:x},0x{b:x}) overlaps {cl} "
                                    f"[0x{c:x},0x{d:x})")
    out = write_sc(path, hdr, parts, check=check, fsync=fsync,
                   versions=(5, 6, SC7_VERSION), extra=extra)
    if check:
        parsed = read_sc7(path)
        if parsed.artifact_id8 != directory.artifact_id8:
            raise ScFormatError("v7 directory artifact identity changed after write")
    return out


def read_sc7(path: str | os.PathLike) -> Sc7Directory:
    sc = read_sc(path, versions=(SC7_VERSION,))
    if not sc.loadable or sc.header.version != SC7_VERSION:
        raise ScFormatError("not a loadable v7 cache")
    if sc.header.reserved != SC7_FEATURE_FIRMWARE_REGIONS:
        raise ScFormatError(f"invalid v7 feature bits 0x{sc.header.reserved:x}")
    if sc.problems or sc.skipped or sc.count != sc.header.record_count:
        raise ScFormatError("invalid v7 public record table")
    with open(path, "rb") as stream:
        stream.seek(SC7_OFFSET)
        raw = stream.read(_HEADER.size)
        if len(raw) != _HEADER.size:
            raise ScFormatError("truncated v7 directory header")
        magic, version, size, nr, nm, ne, rsz, off, n, max_end, aid, checksum, reserved = \
            _HEADER.unpack(raw)
        if (magic != SC7_MAGIC or
                version not in (SC7_DIRECTORY_VERSION, SC7_TRUSTED_DIRECTORY_VERSION) or
                size != _HEADER.size or rsz != _REGION.size):
            raise ScFormatError("invalid v7 directory header")
        expected_size = nr * _REGION.size + nm * _MEMBER.size + ne * _EDGE.size
        if reserved != b"\0" * 8 or off + n != max_end:
            raise ScFormatError("invalid v7 directory bounds")
        if (off % 8 or n != expected_size or off > sc.header.code_size or
                n > sc.header.code_size - off):
            raise ScFormatError("v7 directory count/size mismatch")
        stream.seek(SC_CODE_OFFSET + off)
        table = stream.read(n)
    if len(table) != n or fnv1a64(table, fnv1a64(raw[:64])) != checksum:
        raise ScFormatError("v7 directory checksum mismatch")
    directory = Sc7Directory(raw, table, off, max_end, nr, nm, ne, aid, version)
    rows = decode_directory(directory)
    by_tb = {sc.records[i].tb_offset: sc.records[i] for i in sc.valid_slots}
    public_spans = [(r.tb_offset, r.end_offset, f"public record {i}")
                    for i, r in enumerate(sc.records) if r is not None]
    private_spans: list[tuple[int, int, str]] = []
    expected_member = expected_edge = 0
    region_spans: list[tuple[int, int, str]] = []
    for i, region in enumerate(rows["regions"]):
        expected_region_flags = SC7_REGION_COMPOSITE | (
            SC7_REGION_TRUSTED if directory.trusted else 0)
        if (region["flags"] != expected_region_flags or
                region["first_member"] != expected_member or
                region["first_edge"] != expected_edge or not region["n_members"] or
                region["entry_member"] >= region["n_members"] or
                region["start_offset"] % 64 or
                not region["start_offset"] < region["end_offset"] <= off):
            raise ScFormatError(f"v7 region {i}: invalid bounds or indexes")
        region_spans.append((region["start_offset"], region["end_offset"],
                             f"region {i}"))
        entry_index = region["first_member"] + region["entry_member"]
        if rows["members"][entry_index]["source_tb_offset"] != region["source_tb_offset"]:
            raise ScFormatError(f"v7 region {i}: entry source record mismatch")
        expected_member += region["n_members"]
        expected_edge += region["n_edges"]
    if expected_member != nm or expected_edge != ne:
        raise ScFormatError("v7 directory arrays are not partitioned by the regions")
    for (a, b, al), (c, d, cl) in zip(sorted(region_spans), sorted(region_spans)[1:]):
        if c < b:
            raise ScFormatError(f"v7 layout overlap: {al} and {cl}")
    payloads: list[tuple[bytes, bytes, bytes]] = []
    with open(path, "rb") as stream:
        for i, member in enumerate(rows["members"]):
            source = by_tb.get(member["source_tb_offset"])
            if source is None:
                raise ScFormatError(f"v7 member {i}: unknown source record")
            search_size = (0 if directory.trusted else
                           source.end_offset - source.tc_offset - source.tc_size)
            region_index = member["region_index"]
            if region_index >= nr or member["flags"]:
                raise ScFormatError(f"v7 member {i}: invalid region or flags")
            region = rows["regions"][region_index]
            if not region["first_member"] <= i < region["first_member"] + region["n_members"]:
                raise ScFormatError(f"v7 member {i}: outside its declared region")
            code_end = member["tc_offset"] + member["tc_size"]
            search_end = member["search_offset"] + search_size
            descriptor_end = member["tb_offset"] + sc.header.tb_size
            if (member["tc_size"] == 0 or not region["start_offset"] <= member["tb_offset"]
                    or descriptor_end > region["end_offset"]
                    or not region["start_offset"] <= member["tc_offset"] < code_end
                    or code_end > region["end_offset"]
                    or not region["start_offset"] <= member["search_offset"] <= search_end
                    or search_end > region["end_offset"]):
                raise ScFormatError(f"v7 member {i}: invalid code/search bounds")
            region_first = region["first_member"]
            if not directory.trusted or i == region_first:
                private_spans.append(
                    (member["tb_offset"], descriptor_end, f"member {i} descriptor"))
            private_spans.extend((
                (member["tc_offset"], code_end, f"member {i} code"),
                (member["search_offset"], search_end, f"member {i} search"),
            ))
            stream.seek(SC_CODE_OFFSET + member["tb_offset"])
            descriptor = stream.read(sc.header.tb_size)
            stream.seek(SC_CODE_OFFSET + member["tc_offset"])
            code = stream.read(member["tc_size"])
            stream.seek(SC_CODE_OFFSET + member["search_offset"])
            search = stream.read(search_size)
            if descriptor != b"\0" * sc.header.tb_size:
                raise ScFormatError(f"v7 member {i}: non-canonical private descriptor")
            if fnv1a64(code) != member["code_fnv64"] or \
                    fnv1a64(search) != member["search_fnv64"]:
                raise ScFormatError(f"v7 member {i}: code/search hash mismatch")
            payloads.append((descriptor, code, search))
    for i, region in enumerate(rows["regions"]):
        lo = region["first_member"]
        members = rows["members"][lo:lo + region["n_members"]]
        next_descriptor = region["start_offset"]
        code_start = members[0]["tc_offset"]
        next_code = code_start
        code_end = code_start + sum(member["tc_size"] for member in members)
        next_search = code_end
        for member in members:
            source = by_tb[member["source_tb_offset"]]
            search_size = (0 if directory.trusted else
                           source.end_offset - source.tc_offset - source.tc_size)
            expected_descriptor = region["start_offset"] if directory.trusted \
                else next_descriptor
            if (member["tb_offset"] != expected_descriptor or
                    member["tc_offset"] != next_code):
                raise ScFormatError(f"v7 region {i}: non-contiguous composite members")
            expected_search = code_end if directory.trusted else next_search
            if member["search_offset"] != expected_search:
                raise ScFormatError(f"v7 region {i}: search data is not contiguous and "
                                    "out of line")
            if not directory.trusted:
                next_descriptor += sc.header.tb_size
            next_code += member["tc_size"]
            next_search += search_size
        descriptor_end = (region["start_offset"] + sc.header.tb_size
                          if directory.trusted else next_descriptor)
        if (region["entry_member"] != 0 or descriptor_end > code_start or
                next_code != code_end or next_search != region["end_offset"]):
            raise ScFormatError(f"v7 region {i}: invalid composite geometry")
    occupied = sorted(public_spans + private_spans +
                      [(off, max_end, "firmware directory")])
    for (a, b, al), (c, d, cl) in zip(occupied, occupied[1:]):
        if c < b:
            raise ScFormatError(f"v7 layout overlap: {al} and {cl}")
    for i, edge in enumerate(rows["edges"]):
        ri = edge["region_index"]
        expected_edge_flags = SC7_EDGE_TRUSTED_SKIP_POLL if directory.trusted else 0
        if (ri >= nr or edge["kind"] != 1 or edge["flags"] != expected_edge_flags or
                edge["reserved"]):
            raise ScFormatError(f"v7 edge {i}: invalid kind, flags, or region")
        region = rows["regions"][ri]
        if not region["first_edge"] <= i < region["first_edge"] + region["n_edges"]:
            raise ScFormatError(f"v7 edge {i}: outside its declared region")
        lo, hi = region["first_member"], region["first_member"] + region["n_members"]
        if not lo <= edge["source_member"] < hi or not lo <= edge["target_member"] < hi:
            raise ScFormatError(f"v7 edge {i}: member outside its region")
        source = rows["members"][edge["source_member"]]
        target = rows["members"][edge["target_member"]]
        field = edge["field_offset"]
        if field > source["tc_size"] - 4:
            raise ScFormatError(f"v7 edge {i}: field outside source code")
        code = payloads[edge["source_member"]][1]
        disp = int.from_bytes(code[field:field + 4], "little", signed=True)
        target_entry = target["tc_offset"] + (
            SC7_TRUSTED_ENTRY_OFFSET if directory.trusted else 0)
        if source["tc_offset"] + field + 4 + disp != target_entry:
            raise ScFormatError(f"v7 edge {i}: rel32 target mismatch")
        if directory.trusted:
            target_code = payloads[edge["target_member"]][1]
            if (len(target_code) < 15 or target_code[:5] != bytes.fromhex("8b5df885db") or
                    target_code[5:7] != b"\x0f\x8c" or
                    target_code[11:15] not in
                    (bytes.fromhex("c645fc00"), bytes.fromhex("c645fc01"))):
                raise ScFormatError(f"v7 edge {i}: trusted target prefix is not proven")
    if aid == b"\0" * 8 or aid != _artifact_id8(table, payloads, version):
        raise ScFormatError("v7 directory artifact identity mismatch")
    return directory


def decode_directory(directory: Sc7Directory) -> dict[str, Any]:
    """Decode the typed directory arrays for receipts, reimport and validation."""
    table = directory.table
    expected = (_REGION.size * directory.n_regions + _MEMBER.size * directory.n_members +
                _EDGE.size * directory.n_edges)
    if len(table) != expected:
        raise ScFormatError(f"v7 directory table is {len(table)} bytes, expected {expected}")
    roff = 0
    moff = _REGION.size * directory.n_regions
    eoff = moff + _MEMBER.size * directory.n_members
    regions = []
    for i in range(directory.n_regions):
        (rid, cid, entry, first_member, n_members, first_edge, source_tb,
         start, end, n_edges, flags) = _REGION.unpack_from(table, roff + i * _REGION.size)
        regions.append({"region_id8": rid.hex(), "callsite_id8": cid.hex(),
                        "entry_member": entry, "first_member": first_member,
                        "n_members": n_members, "first_edge": first_edge,
                        "n_edges": n_edges, "source_tb_offset": source_tb,
                        "start_offset": start, "end_offset": end, "flags": flags})
    members = []
    for i in range(directory.n_members):
        row = _MEMBER.unpack_from(table, moff + i * _MEMBER.size)
        members.append({"source_tb_offset": row[0], "tb_offset": row[1],
                        "tc_offset": row[2], "tc_size": row[3],
                        "search_offset": row[4], "code_fnv64": row[5],
                        "search_fnv64": row[6], "guest_vaddr": row[7],
                        "context_id8": row[8].hex(), "region_index": row[9],
                        "flags": row[10]})
    edges = []
    for i in range(directory.n_edges):
        row = _EDGE.unpack_from(table, eoff + i * _EDGE.size)
        edges.append({"region_index": row[0], "source_member": row[1],
                      "target_member": row[2], "field_offset": row[3],
                      "kind": row[4], "flags": row[5], "reserved": row[6],
                      "guest_target": row[7], "exit_id_fnv64": row[8]})
    # Each member is an affine copy of its source TB's complete native span.
    # Keeping one row per member (rather than keying only by source offset)
    # preserves the one-to-many lineage created by call-site-specific copies.
    origins = [{"source_tb_offset": member["source_tb_offset"],
                "context_id8": member["context_id8"], "source_offset": 0,
                "private_offset": member["tc_offset"], "size": member["tc_size"],
                "member_index": i, "region_index": member["region_index"]}
               for i, member in enumerate(members)]
    for region in regions:
        first = region["first_member"]
        subset = members[first:first + region["n_members"]]
        if not subset:
            region["composite"] = None
            continue
        tc_offset = subset[0]["tc_offset"]
        tc_size = sum(member["tc_size"] for member in subset)
        region["composite"] = {
            "descriptor_offset": subset[0]["tb_offset"],
            "tc_offset": tc_offset, "tc_size": tc_size,
            "search_start": tc_offset + tc_size,
            "search_end": region["end_offset"],
            "trusted": directory.trusted,
            "member_recovery": not directory.trusted,
        }
    return {"artifact_id8": directory.artifact_id8.hex(), "regions": regions,
            "members": members, "edges": edges, "origins": origins,
            "max_end": directory.max_end,
            "directory_version": directory.directory_version,
            "trusted": directory.trusted}


def sc7_snapshot_id(path: str | os.PathLike) -> str:
    """Identity of the records, directory and every private member byte."""
    sc = read_sc(path, versions=(SC7_VERSION,))
    directory = read_sc7(path)
    digest = hashlib.sha256(
        f"scopt.snapshot_id.v7/{directory.directory_version}".encode())
    digest.update(sc.snapshot_id().encode())
    digest.update(directory.header)
    digest.update(directory.table)
    rows = decode_directory(directory)
    by_tb = {sc.records[i].tb_offset: sc.records[i] for i in sc.valid_slots}
    with open(path, "rb") as stream:
        for member in rows["members"]:
            source = by_tb[member["source_tb_offset"]]
            search_size = (0 if directory.trusted else
                           source.end_offset - source.tc_offset - source.tc_size)
            stream.seek(SC_CODE_OFFSET + member["tc_offset"])
            code = stream.read(member["tc_size"])
            stream.seek(SC_CODE_OFFSET + member["search_offset"])
            search = stream.read(search_size)
            stream.seek(SC_CODE_OFFSET + member["tb_offset"])
            descriptor = stream.read(sc.header.tb_size)
            digest.update(hashlib.sha256(descriptor).digest())
            digest.update(hashlib.sha256(code).digest())
            digest.update(hashlib.sha256(search).digest())
    return "sha256:" + digest.hexdigest()

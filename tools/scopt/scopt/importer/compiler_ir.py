"""Versioned, trusted serialization of the importer IR used by ``optimize``.

The proof-oriented importer remains the producer and auditor of this file.
Normal compilation trusts it, just as a compiler trusts an object file, and
does not reconstruct the IR from captures on every invocation.
"""

from __future__ import annotations

import marshal
from dataclasses import fields
from pathlib import Path
from types import MappingProxyType

from ..formats.enums import Encoding, OpMarkKind, SiteKind
from ..formats.sc_v5 import ScHeader
from ..formats.scm_v1 import RunInfo
from ..ids import TBKey
from ..model.cache_snapshot import CacheSnapshot
from ..model.unit import (AnchoredPointer, Bindings, Fixup, HelperBinding, Insn, Ldst,
                          OpMark, PoolEntry, RecoveryPoint, RelocInfo, Site, StaticExit,
                          TBUnit)

NAME = "compiler-ir.v1"
MAGIC = "scopt.compiler-ir/1"


def _values(value):
    return tuple(getattr(value, field.name) for field in fields(value))


def _unit(unit: TBUnit) -> tuple:
    if unit.native_fragments:
        raise ValueError("compiler IR v1 does not serialize native-fragment certificates")
    return (
        unit.uid, _values(unit.key), unit.slot, unit.guest_size, unit.icount,
        unit.guest_hash, unit.vaddrs, unit.origin, unit.tb_offset, unit.tc_offset,
        unit.tc_size, unit.end_offset, unit.code, unit.search, unit.record,
        unit.insn0_start, unit.body_end, unit.ldst_end, unit.pool_start,
        tuple(_values(row) for row in unit.insns),
        tuple((row.field_off, row.insn_off, row.width, int(row.enc), int(row.kind),
               row.aux, row.value, row.insn_idx, row.final_field) for row in unit.sites),
        tuple(_values(row) for row in unit.pool),
        tuple(_values(row) for row in unit.ldst),
        tuple((row.host_off, int(row.kind), row.insn_idx, row.helper_id)
              for row in unit.opmarks),
        tuple(_values(row) for row in unit.static_exits),
        (unit.reloc.relocatable, unit.reloc.reasons,
         tuple(_values(row) for row in unit.reloc.fixups)),
        tuple(_values(row) for row in unit.recovery),
        (tuple(_values(row) for row in unit.bindings.helpers),
         tuple(_values(row) for row in unit.bindings.unchecked_host_pointers)),
        unit.meta_status, unit.content_hash, dict(unit.annotations), unit.stub_start,
        unit.inline_start, unit.splice_applied, unit.retired_exit_slots,
    )


def _restore_unit(row: tuple) -> TBUnit:
    (uid, key, slot, guest_size, icount, guest_hash, vaddrs, origin, tb_offset,
     tc_offset, tc_size, end_offset, code, search, record, insn0_start, body_end,
     ldst_end, pool_start, insns, sites, pool, ldst, opmarks, static_exits,
     reloc, recovery, bindings, meta_status, content_hash, annotations, stub_start,
     inline_start, splice_applied, retired_exit_slots) = row
    return TBUnit(
        uid, TBKey(*key), slot, guest_size, icount, guest_hash, tuple(vaddrs), origin,
        tb_offset, tc_offset, tc_size, end_offset, code, search, record, insn0_start,
        body_end, ldst_end, pool_start,
        tuple(Insn(*item) for item in insns),
        tuple(Site(item[0], item[1], item[2], Encoding(item[3]), SiteKind(item[4]),
                   *item[5:]) for item in sites),
        tuple(PoolEntry(*item) for item in pool),
        tuple(Ldst(*item) for item in ldst),
        tuple(OpMark(item[0], OpMarkKind(item[1]), item[2], item[3]) for item in opmarks),
        tuple(StaticExit(*item) for item in static_exits),
        RelocInfo(reloc[0], tuple(reloc[1]), tuple(Fixup(*item) for item in reloc[2])),
        tuple(RecoveryPoint(*item) for item in recovery),
        Bindings(tuple(HelperBinding(*item) for item in bindings[0]),
                 tuple(AnchoredPointer(*item) for item in bindings[1])),
        meta_status, content_hash, MappingProxyType(dict(annotations)), stub_start,
        (), inline_start, splice_applied, tuple(retired_exit_slots))


def write_compiler_ir(path: str | Path, snapshot: CacheSnapshot) -> None:
    run_info = None if snapshot.run_info is None else _values(snapshot.run_info)
    doc = {"magic": MAGIC, "snapshot_id": snapshot.snapshot_id,
           "header": snapshot.header_bytes, "capture_level": snapshot.capture_level,
           "run_info": run_info, "units": tuple(_unit(unit) for unit in snapshot.units()),
           "firmware_regions": tuple(dict(row) for row in snapshot.firmware_regions)}
    data = marshal.dumps(doc, 4)
    target = Path(path)
    temporary = target.with_name(target.name + ".tmp")
    with open(temporary, "wb") as stream:
        stream.write(data)
        stream.flush()
    temporary.replace(target)


def read_compiler_ir(path: str | Path) -> CacheSnapshot:
    source = Path(path)
    doc = marshal.loads(source.read_bytes())
    if not isinstance(doc, dict) or doc.get("magic") != MAGIC:
        raise ValueError("unsupported compiler IR")
    directory = source.parent
    bundle_path = directory / "bundle.json"
    runtime_path = directory / "runtime_identity.json"
    import json

    bundle = json.loads(bundle_path.read_text()) if bundle_path.is_file() else None
    runtime = json.loads(runtime_path.read_text()) if runtime_path.is_file() else None
    header_bytes = doc["header"]
    run_info = None if doc["run_info"] is None else RunInfo(*doc["run_info"])
    return CacheSnapshot(
        snapshot_id=doc["snapshot_id"], header=ScHeader.from_bytes(header_bytes),
        header_bytes=header_bytes, _units=tuple(_restore_unit(row) for row in doc["units"]),
        runtime=runtime, bundle=bundle, capture_level=doc["capture_level"],
        run_info=run_info, regions={}, details={},
        firmware_regions=tuple(doc.get("firmware_regions", ())), path=str(directory))

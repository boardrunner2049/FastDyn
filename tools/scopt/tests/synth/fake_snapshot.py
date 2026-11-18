"""A fake ``SnapshotView`` built from the WP4 synthetic builders (WP9 tests).

This module turns a ``build_sc``/``build_scm`` pair into ``TBUnit`` objects
directly, without WP7's importer (``CacheSnapshot``), so the WP9 unit tests
control relocatability and meta status exactly: the ``.sc`` is
parsed by ``scopt.formats.sc_v5`` and the ``.scm`` by ``scopt.formats.scm_v1``,
and each valid slot becomes a unit with its UnitId, key, bytes and captured
arrays. There is no cross-check: relocatability and meta status are whatever
the test asks for (``not_relocatable`` / ``no_meta`` index sets).
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from scopt.formats import scm_v1 as S
from scopt.formats.sc_v5 import ScHeader, read_sc
from scopt.ids import TBKey
from scopt.model.unit import Bindings, RelocInfo, TBUnit

from . import builders as B


class FakeSnapshot:
    """Minimal ``SnapshotView`` (+ ``capture_level``) over synthetic units."""

    def __init__(self, units: Sequence[TBUnit], snapshot_id: str, header: ScHeader | None = None,
                 *, runtime: Any = None, bundle: Any = None, capture_level: int | None = 1):
        self._units = tuple(units)
        self._by_uid = {u.uid: u for u in self._units}
        self.snapshot_id = snapshot_id
        self.header = header
        self.runtime = runtime
        self.bundle = bundle
        self.capture_level = capture_level

    def units(self) -> tuple[TBUnit, ...]:
        return self._units

    def unit(self, uid: str) -> TBUnit:
        return self._by_uid[uid]

    def by_key(self, key: TBKey) -> tuple[TBUnit, ...]:
        return tuple(u for u in self._units if u.key == key)

    def layout(self) -> Mapping[str, int]:
        return MappingProxyType({u.uid: u.tb_offset for u in self._units})

    def uids(self) -> list[str]:
        return [u.uid for u in self._units]


def _content_hash(record: bytes, body: bytes, chunk: S.TbChunk | None) -> str:
    h = hashlib.sha256()
    h.update(record)
    h.update(body)
    h.update(chunk.encode() if chunk is not None else b"no_meta")
    return "sha256:" + h.hexdigest()


def fake_snapshot(units: Sequence[B.SynthUnit] | int = 4, *, directory: str | None = None,
                  not_relocatable: Iterable[int] = (), no_meta: Iterable[int] = (),
                  capture_level: int | None = 1) -> FakeSnapshot:
    """Build ``.sc``+``.scm`` with the WP4 builders and model them as a snapshot.

    ``units`` is a list of ``SynthUnit`` or a count for ``builders.simple_units``.
    Indices in ``not_relocatable`` get ``RelocInfo(False, ("unexplained_reference",))``;
    indices in ``no_meta`` get ``meta_status="no_meta"`` (and are not relocatable).
    """
    if isinstance(units, int):
        units = B.simple_units(units)
    own = directory is None
    d = tempfile.mkdtemp(prefix="wp9-fake-") if own else directory
    try:
        sc_path, scm_path = B.build_pair(list(units), d)
        sc = read_sc(sc_path, strict=True)
        scm = S.read_scm(scm_path)
    finally:
        if own:
            for f in os.listdir(d):
                os.unlink(os.path.join(d, f))
            os.rmdir(d)
    nr = set(not_relocatable)
    nm = set(no_meta)
    out = []
    uids = sc.unit_ids()
    for idx, (su, uid) in enumerate(zip(sc.units(), uids)):
        rec = su.record
        ch = None if idx in nm else scm.tbs.get(su.slot)
        arrays = ch.model_arrays() if ch is not None else {}
        meta_status = "ok" if ch is not None else "no_meta"
        if idx in nm:
            reloc = RelocInfo(False, ("no_meta",), ())
        elif idx in nr:
            reloc = RelocInfo(False, ("unexplained_reference",), ())
        else:
            reloc = RelocInfo(True, (), ())
        out.append(TBUnit(
            uid=uid, key=rec.key, slot=su.slot, guest_size=rec.guest_size, icount=rec.icount,
            guest_hash=rec.guest_hash, vaddrs=(ch.vaddr,) if ch is not None else (),
            origin="unknown", tb_offset=rec.tb_offset, tc_offset=rec.tc_offset,
            tc_size=rec.tc_size, end_offset=rec.end_offset, code=su.code, search=su.search,
            record=su.record_bytes,
            insn0_start=ch.insn0_start if ch else 0, body_end=ch.body_end if ch else 0,
            ldst_end=ch.ldst_end if ch else 0,
            pool_start=ch.pool_start_or_none if ch else None,
            insns=arrays.get("insns", ()), sites=arrays.get("sites", ()),
            pool=arrays.get("pool", ()), ldst=arrays.get("ldst", ()),
            opmarks=arrays.get("opmarks", ()), static_exits=arrays.get("static_exits", ()),
            reloc=reloc, recovery=(), bindings=Bindings(), meta_status=meta_status,
            content_hash=_content_hash(su.record_bytes, su.body, ch)))
    return FakeSnapshot(out, sc.snapshot_id(), sc.header, capture_level=capture_level)

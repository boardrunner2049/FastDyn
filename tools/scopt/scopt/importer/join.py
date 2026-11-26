"""Bind ``.scm`` TB chunks to ``.sc`` slots (design §2.5.8, §2.7 step 1, §4.3 step 2). WP7.

A snapshot is one ``.sc`` v5 file plus zero or more ``.scm`` v1 captures (one
per prep run that added records to that ``.sc``). This module decides, for
every *valid* slot of the ``.sc`` (validity mirrors QEMU's ``load_records``,
:mod:`scopt.formats.sc_records`), which TB chunk describes it.

Snapshot-level rejections (:class:`ImportRejected`, the import fails, exit 2):

* any ``.sc`` header problem (``magic``, ``version``, ``header_size``,
  ``header_checksum``, ``truncated_header``): QEMU would not reuse the file;
* ``unsupported_arena_below_4g``: ``code_address < 4 GiB`` (§9);
* captures whose RUN_INFO disagrees with the ``.sc`` header
  (``run_info_mismatch:<field>`` for ``code_address``, ``code_size``,
  ``prologue_end``, ``runtime_signature``, ``tb_struct_size``,
  ``host_page_size``, ``target_page_bits``) when **no** given capture
  agrees (§4.3 step 2). When at least one capture agrees, each disagreeing
  one is a *foreign* capture (a leftover from a deleted cache or another
  binary): it cannot bind (§2.5.8), is left out of
  :attr:`JoinResult.captures` and is reported as a capture problem with the
  same ``run_info_mismatch:<field>`` code;
* any capture whose RUN_INFO says ``flat`` (``unsupported_flat``);
* two captures with different TB chunks that both bind to one slot
  (``snapshot_merge_conflict``). Chunks that do not bind (e.g. a capture of
  another ``.sc`` made by the same binary, whose RUN_INFO therefore agrees)
  never conflict; a capture none of whose chunks binds is reported as
  ``no_chunk_binds``.

Per-slot results (:class:`SlotBinding`): a chunk binds to slot ``i`` only if
``chunk.slot == i``, its 104 record bytes equal the ``.sc`` record and its
``code_fnv64`` equals FNV-1a-64 of the ``.sc`` bytes ``[tc_offset,
end_offset)``. Otherwise the slot is ``no_meta`` with reason ``no_chunk``,
``record_mismatch`` or ``code_fnv_mismatch``; the importer pins it.

File-level problems that do not reject the snapshot (a damaged ``.scm``
header, rejected chunks, a missing END) are reported in
:attr:`JoinResult.capture_problems` with stable codes; ``bad_trailer`` from
the WP4 reader is reported as ``chunk_checksum`` (the T-B1-9 name).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..canon import fnv1a64
from ..formats.enums import RunInfoFlag
from ..formats.sc_records import ScFile, read_sc
from ..formats.scm_v1 import ScmFile, ScmHeaderError, TbChunk, read_scm

__all__ = [
    "ImportRejected", "SlotBinding", "CaptureProblem", "JoinResult", "join", "load_captures",
    "ARENA_MIN", "RUN_INFO_BIND_FIELDS",
]

#: ``code_address`` must be at least 4 GiB (imm32 arena pointers impossible), §9.
ARENA_MIN = 1 << 32

#: RUN_INFO field -> ``.sc`` header field that must be equal for a capture to bind.
RUN_INFO_BIND_FIELDS: tuple[tuple[str, str], ...] = (
    ("code_address", "code_address"),
    ("code_size", "code_size"),
    ("prologue_end", "prologue_end"),
    ("runtime_signature", "runtime_signature"),
    ("tb_struct_size", "tb_size"),
    ("host_page_size", "host_page_size"),
    ("target_page_bits", "target_page_bits"),
)

# WP4 reader reasons -> T-B1-9 names
_CHUNK_REASON = {"bad_trailer": "chunk_checksum"}


class ImportRejected(Exception):
    """The whole snapshot is rejected; ``reason`` is a stable code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True, slots=True)
class CaptureProblem:
    """A ``.scm`` problem that degrades coverage but does not reject the snapshot."""

    file: str  # capture name (basename) for reports
    code: str  # scm_header | chunk_checksum | truncated | seq_gap | after_end | ...
    detail: str = ""
    seq: int | None = None
    prefix: bool = False


@dataclass(frozen=True, slots=True)
class SlotBinding:
    slot: int
    chunk: TbChunk | None
    source: ScmFile | None
    reason: str | None  # None when bound; else no_chunk | record_mismatch | code_fnv_mismatch
    detail: str = ""

    @property
    def bound(self) -> bool:
        return self.chunk is not None and self.reason is None


@dataclass
class JoinResult:
    sc: ScFile
    #: the captures used: foreign ones (RUN_INFO disagrees, or no chunk binds) are
    #: left out and reported in ``capture_problems``
    captures: tuple[ScmFile, ...]
    slots: dict[int, SlotBinding]
    capture_problems: list[CaptureProblem] = field(default_factory=list)

    def bound(self) -> list[int]:
        return [s for s, b in self.slots.items() if b.bound]

    def histogram(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for b in self.slots.values():
            k = "bound" if b.bound else b.reason or "?"
            out[k] = out.get(k, 0) + 1
        return dict(sorted(out.items()))


def _name(scm: ScmFile) -> str:
    return os.path.basename(scm.path) if scm.path else "<memory>"


def load_captures(paths: Iterable[str | os.PathLike | ScmFile]
                  ) -> tuple[list[ScmFile], list[CaptureProblem]]:
    """Read every capture; a wrong file header drops that file (``scm_header``)."""
    out: list[ScmFile] = []
    probs: list[CaptureProblem] = []
    for p in paths:
        if isinstance(p, ScmFile):
            out.append(p)
            continue
        try:
            out.append(read_scm(p))
        except ScmHeaderError as e:
            probs.append(CaptureProblem(os.path.basename(os.fspath(p)), "scm_header", str(e)))
    return out, probs


def _check_sc(sc: ScFile) -> None:
    if sc.header_problems:
        raise ImportRejected(sc.header_problems[0], ", ".join(sc.header_problems))
    if sc.header.code_address < ARENA_MIN:
        raise ImportRejected("unsupported_arena_below_4g",
                             f"code_address 0x{sc.header.code_address:x} < 4 GiB")


def _check_run_info(sc: ScFile, scm: ScmFile) -> ImportRejected | None:
    """The first RUN_INFO/header disagreement of ``scm`` (None if it agrees)."""
    ri = scm.run_info
    if ri is None:
        return None
    if ri.flags & RunInfoFlag.FLAT:
        raise ImportRejected("unsupported_flat", f"{_name(scm)}: RUN_INFO says supercache-flat")
    for rf, hf in RUN_INFO_BIND_FIELDS:
        a, b = getattr(ri, rf), getattr(sc.header, hf)
        if a != b:
            return ImportRejected(f"run_info_mismatch:{rf}",
                                  f"{_name(scm)}: RUN_INFO {rf}=0x{a:x} but .sc header "
                                  f"{hf}=0x{b:x}")
    return None


def _bind(u: Any, tb: TbChunk, src: ScmFile) -> SlotBinding:
    """Whether ``tb`` binds to the ``.sc`` unit ``u`` (record bytes, code FNV)."""
    if tb.record != u.record_bytes:
        diff = [i for i in range(104) if tb.record[i] != u.record_bytes[i]]
        return SlotBinding(u.slot, tb, src, "record_mismatch",
                           f"{len(diff)} record bytes differ, first at {diff[0]}")
    fnv = fnv1a64(u.body)
    if fnv != tb.code_fnv64:
        return SlotBinding(u.slot, tb, src, "code_fnv_mismatch",
                           f"capture 0x{tb.code_fnv64:x} != .sc 0x{fnv:x}")
    return SlotBinding(u.slot, tb, src, None)


def join(sc: ScFile | str | os.PathLike, captures: Sequence[str | os.PathLike | ScmFile] = ()
         ) -> JoinResult:
    """Bind the TB chunks of ``captures`` to the valid slots of ``sc``."""
    if not isinstance(sc, ScFile):
        sc = read_sc(sc)
    _check_sc(sc)
    files, probs = load_captures(captures)
    for f in files:
        for r in f.rejected_chunks:
            code = r.reason.split(":", 1)[0]
            probs.append(CaptureProblem(_name(f), _CHUNK_REASON.get(code, code), r.reason,
                                        r.seq, r.prefix))
        if f.incomplete:
            probs.append(CaptureProblem(_name(f), "incomplete", "no END chunk"))
        elif not f.clean:
            probs.append(CaptureProblem(_name(f), "unclean_end", "END.clean != 1"))
        if f.run_info is None and f.tbs:
            probs.append(CaptureProblem(_name(f), "no_run_info",
                                        "TB chunks without RUN_INFO cannot bind"))
    mism = {id(f): _check_run_info(sc, f) for f in files}
    with_ri = [f for f in files if f.run_info is not None]
    usable = [f for f in with_ri if mism[id(f)] is None]
    if with_ri and not usable:
        raise mism[id(with_ri[0])]  # type: ignore[misc]
    for f in with_ri:
        e = mism[id(f)]
        if e is not None:
            probs.append(CaptureProblem(_name(f), e.reason, "foreign capture, not bound: " +
                                        e.detail))
    # union by slot over the binding chunks only (§2.5.8): a chunk of another
    # .sc (same binary, so same RUN_INFO) simply fails to bind; two chunks
    # that both bind to one slot must be byte-equal.
    cands: dict[int, list[tuple[TbChunk, ScmFile]]] = {}
    for f in usable:
        for slot, tb in f.tbs.items():
            cands.setdefault(slot, []).append((tb, f))
    slots: dict[int, SlotBinding] = {}
    bound_files: set[int] = set()
    for u in sc.units():
        tried = [_bind(u, tb, src) for tb, src in cands.get(u.slot, ())]
        if not tried:
            slots[u.slot] = SlotBinding(u.slot, None, None, "no_chunk")
            continue
        ok = [t for t in tried if t.reason is None]
        if not ok:
            slots[u.slot] = tried[0]
            continue
        for t in ok[1:]:
            if t.chunk.encode() != ok[0].chunk.encode():  # type: ignore[union-attr]
                raise ImportRejected(
                    "snapshot_merge_conflict",
                    f"slot {u.slot}: binding chunks differ between {ok[0].source.path} and "  # type: ignore[union-attr]
                    f"{t.source.path}")  # type: ignore[union-attr]
        bound_files.update(id(t.source) for t in ok)
        slots[u.slot] = ok[0]
    unbound = {id(f) for f in usable if f.tbs and id(f) not in bound_files}
    for f in usable:
        if id(f) in unbound:
            probs.append(CaptureProblem(_name(f), "no_chunk_binds",
                                        f"foreign capture, not used: none of its {len(f.tbs)} "
                                        f"TB chunks binds to this .sc"))
    used = tuple(f for f in files if mism[id(f)] is None and id(f) not in unbound)
    return JoinResult(sc, used, slots, probs)


def describe(j: JoinResult) -> dict[str, Any]:
    """Small JSON summary for reports."""
    return {
        "captures": [_name(f) for f in j.captures],
        "slots": len(j.slots),
        "binding": j.histogram(),
        "capture_problems": [{"file": p.file, "code": p.code, "detail": p.detail,
                              "seq": p.seq, "prefix": p.prefix} for p in j.capture_problems],
    }

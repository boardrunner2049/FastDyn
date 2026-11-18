"""ValidateAndPackage: V1-V9 static checks, packaging and V10 (design §4.6, §5.6 step 6, §5.8). WP10.

:class:`ValidateAndPackage` takes a :class:`~scopt.manager.Candidate`, links it
(:class:`~scopt.link.NativeLinker`), writes the artifact files into a staging
directory under ``store/artifacts/``, runs every static check on the written
files and either publishes the directory atomically
(:func:`scopt.formats.artifact.publish`) or writes
``store/rejections/<candidate>.json`` and publishes nothing.
:func:`optimize` runs the PassManager first.

Every check is one row of ``validation.json``::

    {"code": "V1", "check": "second_parser", "result": "pass|fail|skip", "detail": "..."}

Any ``fail`` row rejects the candidate. ``skip`` is used only where the check
does not apply (V9 for a candidate that moves units) or cannot be recorded in
the file it describes (V8 ``manifest``: MANIFEST.sha256 covers
validation.json, so the manifest is verified by :func:`publish` right before
the rename, and again by :func:`validate_artifact`).

Checks (§5.8; ``in`` = the input snapshot, ``out`` = the written artifact):

* **V1 structural** — ``out`` header equals ``in`` except ``checksum`` (and
  ``record_count`` when ``in`` had invalid records); QEMU would load it
  (``valid_header``/``load_records`` replicated by :mod:`scopt.formats.sc_v5`);
  :func:`~scopt.formats.sc_v5.structural_findings` is empty (``tb%64``,
  ``tc-tb``, disjoint intervals above ``ROUND_UP(prologue_end, 64)``, zero
  bytes outside the records, exact file size); ``max_end < code_size -
  host_page - 1024``; slot order (UnitId per slot) unchanged; the
  independent ``independent/scparse.py`` agrees on every header and
  record field.
* **V2 byte diff** — per unit, ``out[tc':end']`` differs from ``in[tc:end]``
  only inside the unit's declared fixup fields (unmoved units are identical).
* **V3 re-decode** — per moved unit, a capstone sweep of ``[0, pool_start)``
  at the new address gives the same tiling and mnemonics, and every
  reference resolves to the same semantic target (offset inside the code,
  own-struct offset, or the same absolute address). Two-sided: the re-import
  of ``out`` with the derived capture (V8) classifies every unit with the same
  sites/classes/fixups and the same status and reasons as ``in`` (0
  unexplained, 0 missing).
* **V4 range** — every fixup field of a moved unit decodes to its original
  absolute target (so the rel32/disp32 fits int32 and did not wrap); every
  live ``jmp_insn_offset`` field is 4-aligned in absolute address and zero;
  every pool start is 8-aligned in absolute address.
* **V5 identity** — the multiset of ``(UnitId, key, guest_hash, guest_size,
  icount, jmp_reset_offset, jmp_insn_offset)`` is unchanged and every unit's
  search data is byte-identical.
* **V6 recovery** — every recovery point unwinds (``GETPC_ADJ``) on the
  search data read back from ``out`` to its ``(insn, words)``; the re-import
  reports no ``recovery_mismatch``.
* **V7 compat** — runtime header fields copied; the input bundle and
  RuntimeIdentity are present, self-consistent (content ids), equal to the
  artifact's ``bundle.json``/``runtime_identity.json`` and agree with the
  header; ``supercache_flat`` is false; the loader requirement is v5.
* **V8 provenance** — the sidecar validates against
  ``artifact_sidecar.v1`` and is complete (ids, move map = the moved units,
  pinned list, one pipeline row per pass); the derived capture re-imports
  (:class:`~scopt.importer.import_cache.ImportCache`) to
  ``output_snapshot_id`` with every input-bound slot bound again; every
  CacheRegion validates.
* **V9 identity** — for a candidate that moves nothing:
  ``sha256(out cache.sc) == sha256(in cache.sc)`` when ``in`` is canonical;
  otherwise the row passes as ``identity_canonicalized`` with the dropped
  records.

V10 (:func:`validate_dynamic`) is a separate ledger stage that delegates to
the WP8 run harness (deploy oracle, launch, post-run proof).
"""

from __future__ import annotations

import json
import os
import pickle
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .analysis.locality import locality_sidecar
from .canon import canonical_json, content_id, hexaddr, sha256_id
from .decode.search import decode_search, is_pcrel, unwind
from .decode.x86 import Arena, extract_refs, sweep, tiles
from .formats import artifact as A
from .formats.region_v1 import build_region, validate_region
from .formats.sc_v5 import (ScFile, ScFormatError, ScRecord, read_sc, sha256_sparse,
                            structural_findings, write_sc)
from .ids import bundle_id as _bundle_id
from .ledger import Ledger, LedgerRow, utc_now
from .paths import fastdyn_root
from .link import LinkResult, NativeLinker, UnitLink, arena_limit

__all__ = [
    "Validation", "ValidateAndPackage", "PackageResult", "optimize", "validate_static",
    "validate_artifact", "validate_dynamic", "scparse_compare", "reimport", "SCPARSE",
    "VALIDATION_SCHEMA", "artifact_link", "Inspection",
]

VALIDATION_SCHEMA = "scopt.validation"
SCPARSE = Path(os.environ.get(
    "SCOPT_SCPARSE", Path(__file__).resolve().parents[1] / "independent/scparse.py"))
NONE16 = 0xFFFF
_MAX_DETAIL = 8


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------

class Validation:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.data: dict[str, Any] = {}

    def add(self, code: str, check: str, ok: bool | None, detail: str = "",
            reason: str | None = None, **extra: Any) -> bool:
        """Append one row. ``extra`` adds structured fields (e.g. V9 ``status`` and
        ``dropped``) next to the human ``detail``."""
        row = {"code": code, "check": check,
               "result": "skip" if ok is None else ("pass" if ok else "fail"),
               "detail": detail, **extra}
        if ok is False:
            row["reason"] = reason or check
        self.rows.append(row)
        return bool(ok)

    def fails(self) -> list[dict[str, Any]]:
        return [r for r in self.rows if r["result"] == "fail"]

    @property
    def accepted(self) -> bool:
        return not self.fails()

    def first_reason(self) -> str | None:
        f = self.fails()
        return None if not f else f"{f[0]['code']}:{f[0].get('reason', f[0]['check'])}"

    def codes(self) -> dict[str, str]:
        """``V1..V9`` -> pass | fail | skip (fail wins, then pass)."""
        out: dict[str, str] = {}
        for r in self.rows:
            cur = out.get(r["code"])
            res = r["result"]
            if cur == "fail" or (cur == "pass" and res == "skip"):
                continue
            out[r["code"]] = res
        return dict(sorted(out.items(), key=lambda kv: int(kv[0][1:])))

    def to_json(self, **extra: Any) -> dict[str, Any]:
        return {"schema": VALIDATION_SCHEMA, "version": 1,
                "verdict": "accepted" if self.accepted else "rejected",
                "codes": self.codes(), **extra, "rows": self.rows}


def _few(xs: Sequence[Any]) -> str:
    xs = list(xs)
    s = ", ".join(str(x) for x in xs[:_MAX_DETAIL])
    return s + (f" (+{len(xs) - _MAX_DETAIL} more)" if len(xs) > _MAX_DETAIL else "")


# ---------------------------------------------------------------------------
# second parser (V1)
# ---------------------------------------------------------------------------

class _NoGlobals(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:  # pragma: no cover - fail closed
        raise pickle.UnpicklingError(f"refusing global {module}.{name}")


def scparse_compare(path: str | os.PathLike, sc: ScFile, *,
                    script: str | os.PathLike | None = None, timeout_s: float = 600.0
                    ) -> tuple[bool, str]:
    """Run the independent ``scparse.py`` on ``path`` and compare with ``sc``.

    The script prints the header on its first line and pickles ``(code_addr,
    prologue_end, records)`` (plain dicts/ints; loaded with no globals allowed).
    """
    script = Path(script or SCPARSE)
    if not script.is_file():
        return False, f"second parser {script} not found"
    with tempfile.TemporaryDirectory(prefix="scopt-scparse-") as td:
        out = Path(td) / "recs.pickle"
        p = subprocess.run([sys.executable, "-I", str(script), os.fspath(path), str(out)],
                           cwd=td, capture_output=True, text=True, timeout=timeout_s)
        if p.returncode != 0 or not out.is_file():
            return False, f"scparse exit {p.returncode}: {p.stderr.strip()[-300:]}"
        with open(out, "rb") as f:
            code_addr, prol_end, recs = _NoGlobals(f).load()
    first = p.stdout.splitlines()[0].split() if p.stdout else []
    kv = dict(zip(first[0::2], first[1::2]))
    h = sc.header
    probs = []
    want = {"ver": str(h.version), "hsz": str(h.header_size), "code_size": hex(h.code_size),
            "code_addr": hex(h.code_address), "prologue_end": hex(h.prologue_end),
            "records": str(h.record_count), "sig": hex(h.runtime_signature),
            "host_page": str(h.host_page_size), "target_page_bits": str(h.target_page_bits),
            "tb_size": str(h.tb_size), "hdr_ck_ok": "True"}
    for k, v in want.items():
        if kv.get(k) != v:
            probs.append(f"header {k}: scparse {kv.get(k)} vs {v}")
    if code_addr != h.code_address or prol_end != h.prologue_end:
        probs.append("pickled header fields differ")
    if len(recs) != len(sc.record_bytes):
        probs.append(f"record count: scparse {len(recs)} vs {len(sc.record_bytes)}")
    for i, (r, raw) in enumerate(zip(recs, sc.record_bytes)):
        o = ScRecord.from_bytes(raw)
        mine = dict(i=i, tb=o.tb_offset, end=o.end_offset, tc=o.tc_offset, tcs=o.tc_size,
                    pc=o.pc, cs=o.cs_base, pa0=o.page_addr0, pa1=o.page_addr1, fl=o.flags,
                    cf=o.cflags, gs=o.guest_size, ic=o.icount, jr=tuple(o.jmp_reset_offset),
                    ji=tuple(o.jmp_insn_offset),
                    ok=sc.records[i] is not None if i < len(sc.records) else False)
        theirs = {k: (tuple(v) if isinstance(v, list) else v) for k, v in r.items()}
        if theirs != mine:
            diff = sorted(k for k in mine if theirs.get(k) != mine[k])
            probs.append(f"slot {i}: {diff}")
            if len(probs) > _MAX_DETAIL:
                break
    return (not probs), ("agrees on header and "
                         f"{len(recs)} records" if not probs else _few(probs))


# ---------------------------------------------------------------------------
# re-import (V3 two-sided, V6, V8)
# ---------------------------------------------------------------------------

def _snapshot_json(snapshot: Any) -> dict[str, Any]:
    p = getattr(snapshot, "path", None)
    if p and (Path(p) / "snapshot.json").is_file():
        return json.loads((Path(p) / "snapshot.json").read_text())
    return {}


def reimport(art_dir: str | os.PathLike, snapshot: Any, scms: Sequence[str] | None = None):
    """Import ``art_dir/cache.sc`` with its derived captures like the input was
    imported (same firmware/plugin options, the input's manifest, bundle and
    RuntimeIdentity; native files are not re-hashed). Returns the
    :class:`~scopt.importer.import_cache.ImportResult` (no store written).
    """
    from .importer.import_cache import ImportCache

    d = Path(art_dir)
    if scms is None:
        scms = [str(d / n) for n in A.derived_captures(d)]  # derive order, not name order
    meta = _snapshot_json(snapshot)
    opts = meta.get("options", {})
    sp = getattr(snapshot, "path", None)
    man = Path(sp) / "manifest.json" if sp and (Path(sp) / "manifest.json").is_file() else None
    bundle = dict(snapshot.bundle) if snapshot.bundle is not None else None
    ri = dict(snapshot.runtime) if snapshot.runtime is not None else None
    res = ImportCache(d / "cache.sc", list(scms), manifest=man,
                      bundle=bundle if man is not None else None, runtime_identity=ri,
                      firmware=opts.get("firmware"), plugin=opts.get("plugin"),
                      use_manifest_firmware=False, verify_files=False).run()
    if man is None and bundle is not None:
        # no manifest to verify the bundle against (synthetic inputs): the
        # regions still carry the input's bundle; V7 checks the bundle itself.
        res.regions = _regions(res, bundle, None if ri is None else ri.get("runtime_identity_id"))
    return res


def _regions(res: Any, bundle: Mapping[str, Any] | None, ri_id: str | None) -> list[dict]:
    snap = res.snapshot
    out = []
    for u in snap.units():
        uc = res.checks.get(u.slot)
        sb = {c.site.field_off: c.binding for c in uc.classified} if uc else {}
        out.append(build_region(
            u, snapshot_id=snap.snapshot_id, tb_size=snap.header.tb_size,
            page_bits=snap.header.target_page_bits, bundle=bundle, runtime_identity_id=ri_id,
            search_len=uc.search_len if uc else None, site_bindings=sb,
            helper_returns={c.ref: c.host_return_offset for c in uc.recovery_checks
                            if c.source == "helper_call"} if uc else None))
    return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _canonical_sha256(in_path: str | os.PathLike) -> str | None:
    """sha256 of ``write(read(in))`` (sc_v5's identity writer, into a scratch
    directory; sparse). None when the input is not loadable at all."""
    from .formats.sc_v5 import identity_copy

    with tempfile.TemporaryDirectory(prefix="scopt-v9-") as td:
        try:
            return identity_copy(in_path, Path(td) / "canonical.sc").out_sha256
        except ScFormatError:
            return None

def _status(u: Any) -> str:
    if u.meta_status == "no_meta":
        return "no_meta"
    return "ok_relocatable" if u.reloc.relocatable else "pinned"


def _fixup_rows(u: Any) -> list[tuple]:
    return [(f.field_off, f.width, f.rule, int(f.site_kind), f.target_class, f.target_abs)
            for f in u.reloc.fixups]


def _classes(uc: Any) -> list[tuple[int, str]] | None:
    if uc is None:
        return None
    return sorted((c.site.field_off, c.cls) for c in uc.classified)


def _sem(v: int | None, tb_abs: int, tc_abs: int, code_len: int) -> tuple:
    if v is None:
        return ("none",)
    if tc_abs <= v < tc_abs + code_len:
        return ("code", v - tc_abs)
    if tb_abs <= v < tc_abs:
        return ("struct", v - tb_abs)
    return ("abs", v)


def _decode(u: Any, tc_abs: int, code: bytes, arena: Arena):
    end = u.pool_start if u.pool_start is not None else len(code)
    insns = sweep(code[:end], tc_abs)
    return insns, extract_refs(insns, arena)


def decode_compare(u: Any, link: UnitLink, code_address: int, code_size: int
                   ) -> tuple[list[str], dict[int, tuple[int, int]]]:
    """V3/V4 decode comparison of one moved unit. Returns ``(problems,
    {field_off: (old target, new target)})`` for reference fields."""
    arena = Arena(code_address, code_size)
    d = link.delta
    old_tc, new_tc = code_address + u.tc_offset, code_address + u.tc_offset + d
    old_tb, new_tb = code_address + u.tb_offset, code_address + u.tb_offset + d
    new_code = link.body[:u.tc_size]
    oi, orf = _decode(u, old_tc, u.code, arena)
    ni, nrf = _decode(u, new_tc, new_code, arena)
    probs = []
    if len(oi) != len(ni):
        probs.append(f"tiling: {len(oi)} vs {len(ni)} insns")
    for a, b in zip(oi, ni):
        if (a.address - old_tc, a.size, a.mnemonic, a.bad) != \
                (b.address - new_tc, b.size, b.mnemonic, b.bad):
            probs.append(f"insn @{a.address - old_tc}: {a.mnemonic}/{a.size} vs "
                         f"{b.mnemonic}/{b.size}")
            break
    if any(x.bad for x in ni):
        probs.append("bad instruction in the new code")
    om = {(r.field_addr - old_tc, r.kind): r for r in orf}
    nm = {(r.field_addr - new_tc, r.kind): r for r in nrf}
    if set(om) != set(nm):
        probs.append(f"reference sets differ: {_few(sorted(set(om) ^ set(nm)))}")
    targets: dict[int, tuple[int, int]] = {}
    for k in sorted(set(om) & set(nm)):
        a, b = om[k], nm[k]
        sa = _sem(a.value, old_tb, old_tc, u.tc_size)
        sb = _sem(b.value, new_tb, new_tc, u.tc_size)
        if sa != sb:
            probs.append(f"ref @{k[0]} ({k[1]}): {sa} vs {sb}")
        if a.value is not None and b.value is not None:
            targets[k[0]] = (a.value, b.value)
    return probs, targets


# ---------------------------------------------------------------------------
# static validation
# ---------------------------------------------------------------------------

def _native_projection(snapshot: Any, link: LinkResult, validation: Validation
                       ) -> tuple[dict[str, UnitLink], dict[str, Any]]:
    """Recheck native code, then project the equivalent original helper events for V2–V4."""
    extended = [unit for unit in link.units if unit.native_fragments and
                snapshot.unit(unit.uid).inline_start is None and
                not snapshot.unit(unit.uid).splice_applied]
    if not extended:
        return {}, {}
    from .analysis.api import ScopeRef
    from .analysis.native_callees import prepare_native_images
    from .analysis.store import AnalysisStore
    from .importer.join import ImportRejected
    from .link import LinkError, relocate_unit
    from .model.native import supports_tcg_integer_abi
    from .splice_layout import materialize_splice
    from .validate_sym.native_inline import NativeInlineError, verify_fragment, verify_site
    from .validate_sym.program import certified_native_calls, program_of, world_of

    projected = {}
    spliced = {}
    errors = []
    stores = {}
    code_address = snapshot.header.code_address
    for lu in extended:
        original = snapshot.unit(lu.uid)
        try:
            plain = relocate_unit(original, lu.new_tb_offset)
            body = bytearray(lu.body[:original.tc_size])
            start = original.tc_size
            last = -1
            for call in lu.native_fragments:
                if call.call_off < last:
                    raise ValueError("inline requests overlap or are out of order")
                last = call.continuation_off
                framed = call.splice is not None and call.splice.kind == "frame"
                if not framed:
                    verify_site(original, call, code_address)
                if not call.bridge_sha256:
                    raise ValueError("native bridge digest is missing")
                if call.bridge_sha256 not in stores:
                    native = prepare_native_images(
                        snapshot, backend_revision="58df0ef89dd64126512e4ee27b4ac3fd8ddf6247",
                        bridge_version=1, policy_version=1, bridge_sha256=call.bridge_sha256)
                    analysis = AnalysisStore(snapshot, external_inputs={"native-images": native})
                    layout = analysis.get("envlayout", ScopeRef("snapshot", snapshot.snapshot_id)).value
                    if not layout["bound"]:
                        raise ValueError("native validation needs the exact QEMU EnvLayout")
                    stores[call.bridge_sha256] = analysis
                analysis = stores[call.bridge_sha256]
                rows = analysis.get("native-callees", ScopeRef("unit", lu.uid)).value
                certified = next((row for row in rows if row.call.host_off == call.call_off), None)
                if (certified is None or certified.callee is None or
                        certified.input_id != call.native_input_id or
                        certified.call.guest_pcs != call.guest_pcs or
                        certified.call.pool_slot_off != call.pool_slot_off or
                        certified.callee.image_id != call.image_id or
                        certified.callee.elf_address != call.elf_address or
                        certified.callee.image_offset != call.image_offset or
                        certified.callee.code != call.callee_code or
                        certified.callee.runtime_address != call.target_abs or
                        certified.callee.symbol != call.symbol):
                    raise ValueError("linked callee does not match its capture-bound native image")
                if framed:
                    if (certified.callee.role != "plugin" or certified.callee.abi != "sysv-amd64" or
                            certified.call.call_class != "plugin" or certified.call.helper_flags != 0 or
                            certified.callee.tcg_typemask != 400):
                        raise ValueError("framed splice does not match the captured plugin ABI")
                elif (certified.callee.role != "qemu" or
                      not supports_tcg_integer_abi(certified.callee.tcg_typemask)):
                    raise ValueError("linked callee does not match the captured integer helper ABI")
                if call.splice is not None:
                    if len(lu.native_fragments) != 1 or call.tcg_typemask != certified.callee.tcg_typemask:
                        raise ValueError("splice must have one call and the captured signature")
                    materialized = materialize_splice(original, call, code_address)
                    placed = relocate_unit(materialized, lu.new_tb_offset)
                    if (placed.body, placed.record, tuple(sorted(placed.patched))) != (
                            lu.body, lu.record, tuple(sorted(lu.patched))):
                        raise ValueError("linked splice bytes, record or fixup fields differ from the proof")
                    abstract = certified_native_calls(program_of(original, world_of(snapshot)),
                                                      plain.body[:original.tc_size], lu.delta, (call,))
                    projected[lu.uid] = replace(plain, body=abstract.code + plain.body[original.tc_size:])
                    spliced[lu.uid] = materialized
                    break
                expected_call = (b"\xe9" + (start - call.call_off - 5).to_bytes(4, "little", signed=True)
                                 + b"\x90" * (len(call.call_bytes) - 5))
                if body[call.call_off:call.continuation_off] != expected_call:
                    raise ValueError("linked call does not jump to its certified island")
                end = start + len(call.fragment.code)
                verify_fragment(call, lu.body[start:end], island_start=start)
                start = end
                body[call.call_off:call.continuation_off] = plain.body[
                    call.call_off:call.continuation_off]
            if lu.uid in spliced:
                continue
            out_record = ScRecord.from_bytes(lu.record)
            if start != out_record.tc_size:
                raise ValueError("certified islands do not cover the enlarged code")
            expected_fields = tuple((f.field_off, f.width) for f in original.reloc.fixups
                                    if not any(c.call_off <= f.field_off < c.continuation_off
                                               for c in lu.native_fragments))
            if tuple(sorted(lu.patched)) != tuple(sorted(expected_fields if lu.moved else ())):
                raise ValueError("remaining linked fixup fields differ")
            projected_program = certified_native_calls(
                program_of(original, world_of(snapshot)), bytes(body), lu.delta, lu.native_fragments)
            body = projected_program.code + lu.body[start:]
            projected[lu.uid] = replace(lu, body=bytes(body), patched=plain.patched)
        except (NativeInlineError, ImportRejected, LinkError, ValueError, OSError, OverflowError) as exc:
            errors.append(f"{lu.uid}: {getattr(exc, 'reason', 'native_correspondence')}: {exc}")
    validation.add("V24", "native_correspondence", not errors, _few(errors) or
                   f"verified native correspondence in {len(extended)} units ({len(spliced)} spliced)")
    return projected, spliced


def validate_static(snapshot: Any, link: LinkResult, art_dir: str | os.PathLike, *,
                    sidecar: Mapping[str, Any] | None,
                    expected_pinned: Sequence[Mapping[str, Any]] | None = None,
                    expected_passes: int | None = None,
                    scparse: str | os.PathLike | None = None
                    ) -> tuple[Validation, Any]:
    """Run V1-V9 on the files in ``art_dir``. Returns ``(validation, reimport result)``."""
    v = Validation()
    art = Path(art_dir)
    in_units = {u.uid: u for u in snapshot.units()}
    h_in = snapshot.header
    out_path = art / "cache.sc"
    t0 = time.monotonic()

    # ---- V1 structural ------------------------------------------------------
    out = read_sc(out_path)
    hdr_fields = [k for k in h_in.__dataclass_fields__ if k not in ("checksum", "record_count")]
    diff = [k for k in hdr_fields if getattr(out.header, k) != getattr(h_in, k)]
    v.add("V1", "header_equal", not diff,
          "all header fields except checksum/record_count equal" if not diff else _few(diff))
    canonical_in = len(snapshot.units()) == h_in.record_count
    rc_ok = out.header.record_count == len(link.units) and (
        not canonical_in or out.header.record_count == h_in.record_count)
    v.add("V1", "record_count", rc_ok,
          f"{out.header.record_count} records (input header {h_in.record_count}, "
          f"{len(snapshot.units())} valid)")
    v.add("V1", "header_valid", out.loadable, _few(out.header_problems) or "valid_header ok")
    v.add("V1", "records_valid", out.loadable and not out.skipped and
          len(out.valid_slots) == len(link.units),
          f"{len(out.valid_slots)}/{len(link.units)} records pass load_records" +
          (f"; skipped {_few([(s.slot, s.reason) for s in out.skipped])}" if out.skipped else ""))
    sf = structural_findings(out) if out.loadable else []
    v.add("V1", "canonical", out.loadable and not sf,
          "tb%64, tc-tb, disjoint, >= prologue, zero outside records, exact size"
          if not sf else _few([f"{f.code} {f.detail}" for f in sf]))
    lim = arena_limit(h_in)
    v.add("V1", "max_end_bound", out.max_end < lim,
          f"max_end {out.max_end:#x} < code_size - host_page - 1024 = {lim:#x}")
    out_uids = out.unit_ids() if out.loadable else []
    in_uids = [u.uid for u in snapshot.units()]
    v.add("V1", "slot_order", out_uids == in_uids,
          f"{len(out_uids)} units in input slot order" if out_uids == in_uids else
          "UnitId order differs")
    rec_eq = out.loadable and [out.record_bytes[i] for i in out.valid_slots] == \
        [u.record for u in link.units] and \
        [out.bodies[i] for i in out.valid_slots] == [u.body for u in link.units]
    v.add("V1", "written_equals_linked", rec_eq, "records and arena bytes read back equal "
          "the linker output")
    ok2, det2 = scparse_compare(out_path, out, script=scparse)
    v.add("V1", "second_parser", ok2, det2)

    projections, spliced = _native_projection(snapshot, link, v)

    # ---- V2 byte diff -------------------------------------------------------
    bad2 = []
    for linked in link.units:
        lu = projections.get(linked.uid, linked)
        u = in_units[lu.uid]
        old = u.code + u.search
        if len(old) != len(lu.body):
            bad2.append(f"{lu.uid}: length")
            continue
        if not lu.moved:
            if old != lu.body:
                bad2.append(f"{lu.uid}: unmoved unit changed")
            continue
        declared = sorted((f.field_off, f.width) for f in u.reloc.fixups)
        if sorted(lu.patched) != declared:
            bad2.append(f"{lu.uid}: patched fields != declared fixups")
        allowed = bytearray(len(old))
        for off, w in declared:
            allowed[off:off + w] = b"\x01" * w
        diffs = [i for i, (a, b) in enumerate(zip(old, lu.body)) if a != b and not allowed[i]]
        if diffs:
            bad2.append(f"{lu.uid}: bytes differ outside fixups at {diffs[:4]}")
    nmoved = sum(1 for lu in link.units if lu.moved)
    v.add("V2", "byte_diff", not bad2,
          f"{nmoved} moved units differ only in declared fixup fields; "
          f"{len(link.units) - nmoved} unmoved units byte-identical" if not bad2 else _few(bad2))

    # ---- V3 / V4 decode -----------------------------------------------------
    ca, cs = h_in.code_address, h_in.code_size
    bad3, bad4 = [], []
    nfix = 0
    for linked in link.units:
        lu = projections.get(linked.uid, linked)
        if not lu.moved:
            continue
        u = in_units[lu.uid]
        probs, targets = decode_compare(u, lu, ca, cs)
        bad3 += [f"{lu.uid} {p}" for p in probs]
        for f in u.reloc.fixups:
            nfix += 1
            t = targets.get(f.field_off)
            if t is None:
                bad4.append(f"{lu.uid} fixup @{f.field_off}: no decoded reference")
            elif f.rule == "rel_minus_delta" and t[0] != t[1]:
                bad4.append(f"{lu.uid} fixup @{f.field_off}: target {t[0]:#x} -> {t[1]:#x}")
            elif f.rule == "abs_plus_delta" and t[1] != t[0] + lu.delta:
                bad4.append(f"{lu.uid} fixup @{f.field_off}: abs {t[0]:#x} -> {t[1]:#x}")
    v.add("V3", "redecode", not bad3,
          f"{nmoved} moved units: same tiling, mnemonics and semantic targets" if not bad3
          else _few(bad3))
    v.add("V4", "fixup_targets", not bad4,
          f"{nfix} fixup fields of moved units decode to their original targets (int32)"
          if not bad4 else _few(bad4))
    bad4b = []
    for lu in link.units:
        u = spliced.get(lu.uid, in_units[lu.uid])
        rec = ScRecord.from_bytes(lu.record)
        tc_abs = ca + rec.tc_offset
        for off in rec.jmp_insn_offset:
            if off == NONE16:
                continue
            if (tc_abs + off) % 4 or lu.body[off:off + 4] != b"\0\0\0\0":
                bad4b.append(f"{lu.uid} goto_tb field @{off}")
        if u.pool_start is not None and (tc_abs + u.pool_start) % 8:
            bad4b.append(f"{lu.uid} pool @{u.pool_start} not 8-aligned")
    v.add("V4", "goto_tb_and_pool_alignment", not bad4b,
          "goto_tb fields 4-aligned and zero; pools 8-aligned" if not bad4b else _few(bad4b))

    # ---- V5 identity --------------------------------------------------------
    def ident(uid: str, rec: ScRecord) -> tuple:
        return (uid, rec.page_addr0, rec.page_addr1, rec.flags, rec.cs_base, rec.cflags,
                rec.guest_hash, rec.guest_size, rec.icount, tuple(rec.jmp_reset_offset),
                tuple(rec.jmp_insn_offset))
    mi = Counter(ident(u.uid, ScRecord.from_bytes(spliced.get(u.uid, u).record))
                 for u in snapshot.units())
    mo = Counter(ident(uid, out.records[i]) for uid, i in zip(out_uids, out.valid_slots))
    v.add("V5", "unit_multiset", mi == mo,
          f"{sum(mo.values())} units, no add or drop" if mi == mo else
          f"added {_few(list((mo - mi).elements())[:3])} dropped "
          f"{_few(list((mi - mo).elements())[:3])}")
    bad5 = [lu.uid for lu in link.units
            if lu.body[ScRecord.from_bytes(lu.record).tc_size:] !=
            spliced.get(lu.uid, in_units[lu.uid]).search]
    search_evidence = ("search data matches checked offsets and original guest words" if spliced
                       else "search data byte-identical for every unit")
    v.add("V5", "search_data", not bad5, _few(bad5) if bad5 else search_evidence)

    # ---- V6 recovery --------------------------------------------------------
    bad6 = []
    npts = 0
    for lu in link.units:
        u = spliced.get(lu.uid, in_units[lu.uid])
        if not u.recovery:
            continue
        rec = ScRecord.from_bytes(lu.record)
        removed = {call.continuation_off for call in lu.native_fragments if call.splice is None}
        try:
            rows = decode_search(lu.body[rec.tc_size:], u.icount, is_pcrel(rec.cflags))
        except ValueError as e:
            bad6.append(f"{lu.uid}: search {e}")
            continue
        for p in u.recovery:
            if p.source == "helper_call" and p.host_return_offset in removed:
                continue
            npts += 1
            got = unwind(rows, p.host_return_offset)
            if got is None or got[0] != p.insn or tuple(got[1]) != tuple(p.words):
                bad6.append(f"{lu.uid} {p.id} @{p.host_return_offset}: {got} != "
                            f"({p.insn}, {tuple(p.words)})")
    v.add("V6", "unwind", not bad6,
          f"{npts} recovery points unwind to their insn at the new placement" if not bad6
          else _few(bad6))

    # ---- V7 compat ----------------------------------------------------------
    rt = ("runtime_signature", "code_address", "code_size", "prologue_end", "tb_size",
          "target_page_bits", "host_page_size")
    d7 = [k for k in rt if getattr(out.header, k) != getattr(h_in, k)]
    v.add("V7", "runtime_fields_copied", not d7, _few(d7) or ", ".join(rt))
    b, ri = snapshot.bundle, snapshot.runtime
    b_ok = b is not None and b.get("bundle_id") == _bundle_id(b)
    v.add("V7", "bundle", b_ok, f"bundle_id {None if b is None else b.get('bundle_id')}"
          if b is not None else "the input snapshot has no bundle", "identity_missing:bundle")
    ri_ok = ri is not None and ri.get("runtime_identity_id") == content_id(
        ri, "runtime_identity_id")
    v.add("V7", "runtime_identity", ri_ok,
          f"runtime_identity_id {None if ri is None else ri.get('runtime_identity_id')}"
          if ri is not None else "the input snapshot has no RuntimeIdentity",
          "identity_missing:runtime_identity")
    if ri is not None:
        hd = []
        for k in ("runtime_signature", "code_address", "code_size", "prologue_end"):
            if ri.get(k) is not None and ri.get(k) != hexaddr(getattr(out.header, k)):
                hd.append(k)
        for k in ("tb_size", "host_page_size", "target_page_bits"):
            if ri.get(k) is not None and ri.get(k) != getattr(out.header, k):
                hd.append(k)
        v.add("V7", "runtime_identity_matches_header", not hd, _few(hd) or "agree")
        v.add("V7", "supercache_flat", ri.get("supercache_flat") is False,
              f"supercache_flat={ri.get('supercache_flat')!r}", "unsupported_flat")
    copies = []
    for name, doc in (("bundle.json", b), ("runtime_identity.json", ri)):
        p = art / name
        if doc is None:
            continue
        if not p.is_file() or p.read_bytes() != canonical_json(dict(doc)) + b"\n":
            copies.append(name)
    v.add("V7", "identity_files", not copies and b is not None and ri is not None,
          "bundle.json and runtime_identity.json equal the input's" if not copies
          else _few(copies))
    lr = None if sidecar is None else sidecar.get("loader_requirement")
    v.add("V7", "loader_requirement", sidecar is None or lr == A.LOADER_V5, str(lr))

    # ---- V8 provenance ------------------------------------------------------
    out_sid = out.snapshot_id() if out.loadable else None
    res = None
    try:
        res = reimport(art, snapshot)
    except Exception as e:  # ImportRejected or a parse error: fail closed
        v.add("V8", "reimport", False, f"{type(e).__name__}: {e}", "reimport_failed")
    if res is not None:
        sid_ok = res.snapshot.snapshot_id == out_sid
        v.add("V8", "reimport_snapshot_id", sid_ok, f"{res.snapshot.snapshot_id}")
        in_meta = _snapshot_json(snapshot)
        in_bound = in_meta.get("capture", {}).get("binding", {}).get("bound")
        if in_bound is None:
            in_bound = sum(1 for u in snapshot.units() if u.meta_status != "no_meta")
        out_bound = res.join.histogram().get("bound", 0)
        v.add("V8", "reimport_binding", out_bound == in_bound,
              f"{out_bound} slots bound by the derived capture (input {in_bound})")
        bad8 = []
        bad3b = []
        bad6b = []
        details = getattr(snapshot, "details", {}) or {}
        by_link_uid = link.by_uid()
        for u2 in res.snapshot.units():
            u = in_units.get(u2.uid)
            if u is None:
                bad8.append(f"{u2.uid}: not in input")
                continue
            if (_status(u2), u2.reloc.reasons, u2.origin) != (_status(u), u.reloc.reasons,
                                                              u.origin):
                bad8.append(f"{u2.uid}: {_status(u)}{list(u.reloc.reasons)} -> "
                            f"{_status(u2)}{list(u2.reloc.reasons)}")
            expected_inline = by_link_uid[u.uid].native_fragments
            if u2.native_fragments != expected_inline:
                bad8.append(f"{u2.uid}: reimported native provenance differs")

            if u.uid in spliced:
                from .splice_layout import splice_offsets

                expected = spliced[u.uid]
                linked = by_link_uid[u.uid]
                mu = splice_offsets(expected_inline[0])
                if not u2.splice_applied or any(getattr(u2, k) != getattr(expected, k) for k in
                        ("insns", "ldst", "opmarks", "pool_start", "body_end", "ldst_end",
                         "recovery", "static_exits")):
                    bad8.append(f"{u2.uid}: reimported splice coordinates differ")
                expected_fixups = [(*row[:5], row[5] + linked.delta
                                     if row[2] == "abs_plus_delta" and row[5] is not None
                                     else row[5]) for row in _fixup_rows(expected)]
                if _fixup_rows(u2) != expected_fixups:
                    bad3b.append(f"{u2.uid}: remapped fixups differ")
                ci, co = _classes(details.get(u.uid)), _classes(res.checks.get(u2.slot))
                if ci is not None and sorted((mu(off), cls) for off, cls in ci
                                             if not mu.start <= off < mu.end) != (co or []):
                    bad3b.append(f"{u2.uid}: remapped site classes differ")
                continue

            added_inline = expected_inline if u.inline_start is None and not u.splice_applied else ()

            def original_field(off: int) -> bool:
                return off < u.tc_size and not any(
                    call.call_off <= off < call.continuation_off for call in added_inline)

            if _fixup_rows(u2) != [row for row in _fixup_rows(u) if original_field(row[0])]:
                bad3b.append(f"{u2.uid}: fixups differ")
            ci, co = _classes(details.get(u.uid)), _classes(res.checks.get(u2.slot))
            if ci is not None and ([row for row in ci if original_field(row[0])] !=
                                   [row for row in co or () if original_field(row[0])]):
                bad3b.append(f"{u2.uid}: retained site classes differ")
            if any(r.startswith("recovery_mismatch") for r in u2.reloc.reasons):
                bad6b.append(u2.uid)
        v.add("V8", "reimport_status", not bad8,
              f"{len(res.snapshot.units())} units keep status, reasons and origin"
              if not bad8 else _few(bad8))
        v.add("V3", "two_sided_reimport", not bad3b and not bad8,
              "re-import of the new placement: same classified sites and fixups, 0 new "
              "unexplained/missing references" if not (bad3b or bad8) else _few(bad3b + bad8))
        v.add("V6", "reimport_recovery", not bad6b,
              "no recovery_mismatch on re-import" if not bad6b else _few(bad6b))
        rbad = [r["members"][0]["unit"] for r in res.regions if validate_region(r)]
        v.add("V8", "regions_valid", not rbad,
              f"{len(res.regions)} CacheRegion v1 rows validate" if not rbad else _few(rbad))
    if sidecar is not None:
        errs = A.sidecar_errors(sidecar)
        v.add("V8", "sidecar_schema", not errs, _few(errs) or "artifact_sidecar.v1")
        comp = []
        if sidecar.get("input_snapshot_id") != snapshot.snapshot_id:
            comp.append("input_snapshot_id")
        if sidecar.get("output_snapshot_id") != out_sid:
            comp.append("output_snapshot_id")
        if b is not None and sidecar.get("bundle_id") != b.get("bundle_id"):
            comp.append("bundle_id")
        if ri is not None and sidecar.get("runtime_identity_id") != ri.get("runtime_identity_id"):
            comp.append("runtime_identity_id")
        if sidecar.get("move_map") != link.move_map():
            comp.append("move_map")
        if expected_pinned is not None and sidecar.get("pinned") != list(expected_pinned):
            comp.append("pinned")
        if expected_passes is not None and len(sidecar.get("pipeline", ())) != expected_passes:
            comp.append("pipeline")
        v.add("V8", "sidecar_complete", not comp, _few(comp) or
              "ids, move map, pinned units and pipeline rows present and consistent")
    v.add("V8", "manifest", None, "MANIFEST.sha256 covers validation.json; it is verified at "
          "publish (before the rename) and by `scopt validate`")

    # ---- V9 identity --------------------------------------------------------
    out_sha = sha256_sparse(out_path)
    v.data["out_sha256"] = out_sha
    if nmoved:
        v.add("V9", "identity_sha256", None, f"not an identity candidate ({nmoved} units moved)")
    else:
        sp = getattr(snapshot, "path", None)
        in_path = Path(sp) / "cache.sc" if sp else None
        if in_path is None or not in_path.is_file():
            v.add("V9", "identity_sha256", False, "input cache.sc not available",
                  "input_unavailable")
        else:
            sc_in = read_sc(in_path)
            fin = structural_findings(sc_in)
            in_sha = sha256_sparse(in_path)
            v.data["in_sha256"] = in_sha
            if not fin:
                v.add("V9", "identity_sha256", in_sha == out_sha,
                      f"sha256 {out_sha} {'==' if in_sha == out_sha else '!='} input {in_sha}",
                      "identity_sha256_mismatch", status="identity" if in_sha == out_sha
                      else "fail", in_sha256=in_sha, out_sha256=out_sha)
            else:
                # Not canonical: the identity output is the input as QEMU loads it
                # (invalid records dropped, the rest packed in slot order). Compare
                # with an independent canonical rewrite of the input.
                dropped = [{"slot": s.slot, "reason": s.reason} for s in sc_in.skipped]
                canon_sha = _canonical_sha256(in_path)
                ok9 = canon_sha is not None and canon_sha == out_sha
                v.add("V9", "identity_sha256", ok9,
                      "identity_canonicalized: input not canonical "
                      f"({_few(sorted({f.code for f in fin}))}); dropped "
                      f"{[(d['slot'], d['reason']) for d in dropped]}; output "
                      f"{'==' if ok9 else '!='} the canonical rewrite of the input "
                      f"({canon_sha})", "identity_canonicalized_mismatch",
                      status="identity_canonicalized" if ok9 else "fail", dropped=dropped,
                      findings=sorted({f.code for f in fin}), in_sha256=in_sha,
                      out_sha256=out_sha, canonical_sha256=canon_sha)
    v.data["seconds"] = round(time.monotonic() - t0, 3)
    v.data["output_snapshot_id"] = out_sid
    return v, res


# ---------------------------------------------------------------------------
# package
# ---------------------------------------------------------------------------

@dataclass
class PackageResult:
    status: str  # published | rejected
    candidate_id: str
    reason: str | None = None
    artifact_id: str | None = None
    artifact_dir: str | None = None
    publish_status: str | None = None  # created | exists
    rejection_path: str | None = None
    validation: Validation | None = None
    link: LinkResult | None = None
    output_snapshot_id: str | None = None
    ledger: tuple[LedgerRow, ...] = ()
    candidate: Any = field(default=None, repr=False)

    @property
    def published(self) -> bool:
        return self.status == "published"

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status, "candidate_id": self.candidate_id, "reason": self.reason,
                "artifact_id": self.artifact_id, "artifact_dir": self.artifact_dir,
                "publish_status": self.publish_status, "rejection_path": self.rejection_path,
                "output_snapshot_id": self.output_snapshot_id,
                "validation": None if self.validation is None else self.validation.codes(),
                "link": None if self.link is None else self.link.to_json()}


def _pinned_rows(candidate: Any, link: LinkResult) -> list[dict[str, Any]]:
    rows = {r["unit"]: list(r["reasons"]) for r in candidate.pinned()}
    for f in link.rollbacks:
        rows.setdefault(f.uid, []).append(f"failed:{f.reason}")
    order = {u.uid: i for i, u in enumerate(candidate.working_set.units())}
    return [{"unit": k, "reasons": v} for k, v in sorted(rows.items(), key=lambda kv: order[kv[0]])]


class Inspection:
    """The ``inspected`` stage (§5.9) of one input snapshot, for one optimize invocation.

    The snapshot's units as imported and classified by the WP7 cross-check
    (re-run when the snapshot was opened). The per-unit rows (verdict = the
    unit status, reason = its pin reasons, evidence_ref = the SnapshotId) are
    emitted **once** per invocation: into the ledger of the first candidate
    packaged with this object. Every candidate gets its own artifact-level row,
    whose ``counts`` field holds the status tallies::

        {"units": n, "ok_relocatable": a, "pinned": b, "no_meta": c}

    and whose verdict is ``pass`` (every unit ``ok_relocatable``), ``partial``
    (some units are pinned or ``no_meta``: they stay put) or ``fail`` (no unit).
    Pass one object to every :class:`ValidateAndPackage` of the same snapshot
    in one invocation (:func:`optimize` does this for its candidate).
    """

    STATUSES = ("ok_relocatable", "pinned", "no_meta")

    def __init__(self, snap: Any):
        self.snapshot_id = snap.snapshot_id
        self.units: list[tuple[str, str, str]] = []
        status = getattr(snap, "status", None)
        if status is not None and hasattr(snap, "units"):
            for u in snap.units():
                reasons = getattr(getattr(u, "reloc", None), "reasons", ()) or ()
                self.units.append((u.uid, status(u.uid), ",".join(reasons)))
        self.available = status is not None
        self.emitted_into: str | None = None  # candidate id whose ledger has the unit rows

    def counts(self) -> dict[str, int]:
        c = Counter(st for _, st, _ in self.units)
        out = {"units": len(self.units)}
        out.update({k: c.get(k, 0) for k in self.STATUSES})
        out.update({k: v for k, v in sorted(c.items()) if k not in self.STATUSES})
        return out

    def verdict(self) -> str:
        c = self.counts()
        if not c["units"]:
            return "fail"
        return "pass" if c["ok_relocatable"] == c["units"] else "partial"

    def emit(self, ledger: Ledger, candidate_id: str) -> None:
        if not self.available:
            return
        if self.emitted_into is None:
            for uid, st, reasons in self.units:
                ledger.emit("unit", uid, "inspected", st, reasons, self.snapshot_id)
            self.emitted_into = candidate_id
        c = self.counts()
        units_note = ("unit rows in this ledger" if self.emitted_into == candidate_id else
                      f"unit rows in the ledger of {self.emitted_into}")
        ledger.emit("artifact", candidate_id, "inspected", self.verdict(),
                    f"input {self.snapshot_id}: {c['units']} units, " + ", ".join(
                        f"{k} {v}" for k, v in sorted(c.items()) if k != "units")
                    + f"; {units_note}", self.snapshot_id, counts=c)


class ValidateAndPackage:
    """Link, write, validate, then publish or reject (see the module docstring)."""

    def __init__(self, candidate: Any, store: str | os.PathLike, *,
                 scparse: str | os.PathLike | None = None, clock=utc_now,
                 join_result: Any = None, inspection: "Inspection | None" = None,
                 profiles: Iterable[Any] = ()):
        self.candidate = candidate
        self.profiles = tuple(profiles)  # for the x_locality model (WP11)
        self.store = Path(store)
        self.scparse = scparse
        self.ledger = Ledger(candidate.ledger, clock=clock)
        self.join_result = join_result
        snap = candidate.working_set.base
        if inspection is not None and inspection.snapshot_id != snap.snapshot_id:
            raise ValueError(f"inspection of {inspection.snapshot_id} given for a candidate "
                             f"of {snap.snapshot_id}")
        self.inspection = inspection

    def _reject(self, reason: str, *, link: LinkResult | None = None,
                validation: Validation | None = None, extra: Mapping[str, Any] | None = None,
                supersedes: LedgerRow | None = None) -> PackageResult:
        c = self.candidate
        ev = None if validation is None else sha256_id(canonical_json(validation.to_json()))
        row_reason = reason
        if supersedes is not None:
            # V1-V9 passed and an ``accepted: pass`` row was written, then publishing
            # failed: this row explicitly supersedes it (same stage, later ts).
            row_reason = (f"{reason}; supersedes accepted:pass@{supersedes.ts} "
                          "(publish failed after V1-V9 passed)")
        self.ledger.emit("artifact", c.candidate_id, "accepted", "fail", row_reason, ev)
        doc = {"candidate_id": c.candidate_id, "input_snapshot_id": c.input_snapshot_id,
               "reason": reason, "pipeline_name": c.pipeline.name,
               "pipeline": c.pipeline_provenance(),
               "ablation": dict(c.ablation), "profile_ids": c.profile_rows(),
               "link": None if link is None else link.to_json(),
               "validation": None if validation is None else validation.to_json(),
               "ledger": [r.to_json() for r in self.ledger.rows], **(extra or {})}
        p = A.write_rejection(self.store, c.candidate_id, doc)
        return PackageResult("rejected", c.candidate_id, reason, rejection_path=str(p),
                             validation=validation, link=link, ledger=self.ledger.rows,
                             candidate=c)

    def _inspected(self, snap: Any) -> None:
        """``inspected`` rows (§5.9) through the shared :class:`Inspection`."""
        if self.inspection is None:
            self.inspection = Inspection(snap)
        self.inspection.emit(self.ledger, self.candidate.candidate_id)

    def run(self) -> PackageResult:
        c = self.candidate
        snap = c.working_set.base
        self._inspected(snap)
        from .passes.base import Outcome
        from .passes.host_inline_extreme import EXTREME_PASS

        extreme = c.outcome(EXTREME_PASS)
        if extreme is not None and extreme.outcome not in (Outcome.CANDIDATE, Outcome.NO_CHANGE):
            return self._reject(extreme.reason)
        link = NativeLinker(c, join_result=self.join_result).link()
        if not link.ok:
            f = link.failures[0]
            failed = {x.uid for x in link.failures}
            for x in link.rollbacks:  # first-attempt failures of a failed second attempt
                if x.uid not in failed:
                    self.ledger.emit("unit", x.uid, "compiled", "fail", x.reason,
                                     "rolled_back")
            for x in link.failures:
                self.ledger.emit("unit", x.uid, "compiled", "fail", x.reason)
            self.ledger.emit("artifact", c.candidate_id, "compiled", "fail", f.reason)
            return self._reject(f"failed:{f.reason}", link=link)
        stage = A.staging_dir(self.store)
        try:
            return self._package(stage, snap, link)
        finally:
            if stage.exists():
                A.remove_tree(stage)

    def _package(self, stage: Path, snap: Any, link: LinkResult) -> PackageResult:
        c = self.candidate
        try:
            write_sc(stage / "cache.sc", snap.header, [(u.record, u.body) for u in link.units])
        except ScFormatError as e:
            self.ledger.emit("artifact", c.candidate_id, "compiled", "fail", "write_sc")
            return self._reject("write_sc", link=link, extra={"detail": str(e)})
        for name, data in link.derived_scms:
            (stage / name).write_bytes(data)
        if snap.bundle is not None:
            A.write_json_file(stage / "bundle.json", dict(snap.bundle))
        if snap.runtime is not None:
            A.write_json_file(stage / "runtime_identity.json", dict(snap.runtime))
        A.write_json_file(stage / A.PROFILES, {"profiles": c.profile_rows()})
        for f in link.rollbacks:
            # the requested move failed and the unit went back to its original
            # offset (§5.6 step 6): its own compile failed, the candidate did not
            self.ledger.emit("unit", f.uid, "compiled", "fail", f.reason, "rolled_back")
        for lu in link.units:
            if lu.moved:
                self.ledger.emit("unit", lu.uid, "compiled", "pass", "moved",
                                 f"delta:{lu.delta}")
        self.ledger.emit("artifact", c.candidate_id, "compiled", "pass",
                         f"{sum(1 for u in link.units if u.moved)} moved")
        out = read_sc(stage / "cache.sc")
        pinned = _pinned_rows(c, link)
        sidecar: dict[str, Any] = {
            "schema": "fastdyn.artifact_sidecar", "version": 1,
            "input_snapshot_id": snap.snapshot_id, "output_snapshot_id": out.snapshot_id(),
            "bundle_id": None if snap.bundle is None else snap.bundle.get("bundle_id"),
            "runtime_identity_id": None if snap.runtime is None else
            snap.runtime.get("runtime_identity_id"),
            "profile_ids": c.profile_rows(), "pipeline": c.pipeline_provenance(),
            "move_map": link.move_map(), "pinned": pinned,
            "validation_sha256": "sha256:" + "0" * 64,  # placeholder until validation.json exists
            "tool_versions": A.tool_versions(), "loader_requirement": A.LOADER_V5,
            "x_candidate_id": c.candidate_id, "x_pipeline_name": c.pipeline.name,
            "x_ablation": dict(c.ablation),
            "x_link": {"attempts": link.attempts,
                       "rollbacks": [f.to_json() for f in link.rollbacks],
                       "capture_sources": list(link.capture_sources),
                       "derived_captures": [n for n, _ in link.derived_scms]},
        }
        if self.profiles:  # static locality MODEL, input vs candidate layout (WP11)
            try:
                sidecar["x_locality"] = locality_sidecar(snap, link, self.profiles,
                                                         phase=_layout_phase(c))
            except Exception as e:  # noqa: BLE001 - a report model never fails packaging
                sidecar["x_locality"] = {"status": "not_run", "phase": _layout_phase(c),
                                         "reason": f"error:{type(e).__name__}: {e}"[:300]}
        val, res = validate_static(snap, link, stage, sidecar=sidecar, expected_pinned=pinned,
                                   expected_passes=len(c.passes), scparse=self.scparse)
        if not val.accepted:
            return self._reject(val.first_reason() or "validation", link=link, validation=val)
        vdoc = val.to_json(candidate_id=c.candidate_id, input_snapshot_id=snap.snapshot_id,
                           output_snapshot_id=val.data.get("output_snapshot_id"))
        vbytes = A.write_json_file(stage / A.VALIDATION, vdoc)
        sidecar["validation_sha256"] = sha256_id(vbytes)
        errs = A.sidecar_errors(sidecar)
        if errs:  # pragma: no cover - validated above with the placeholder
            return self._reject("V8:sidecar_schema", link=link, validation=val)
        A.write_json_file(stage / A.SIDECAR, sidecar)
        (stage / A.REGIONS).write_bytes(b"".join(canonical_json(r) + b"\n" for r in res.regions))
        accepted = self.ledger.emit("artifact", c.candidate_id, "accepted", "pass", "V1-V9",
                                    sidecar["validation_sha256"])
        self.ledger.write(stage / A.LEDGER)
        try:
            aid, final, st = A.publish(stage, self.store,
                                       known={"cache.sc": val.data["out_sha256"]})
        except A.ArtifactError as e:
            return self._reject(f"V8:{e.code}", link=link, validation=val,
                                extra={"detail": e.detail}, supersedes=accepted)
        return PackageResult("published", c.candidate_id, None, aid, str(final), st,
                             validation=val, link=link,
                             output_snapshot_id=val.data.get("output_snapshot_id"),
                             ledger=self.ledger.rows, candidate=c)


def optimize(snapshot: Any, pipeline: Any, store: str | os.PathLike, *,
             profiles: Iterable[Any] = (), params: Mapping[str, Mapping[str, Any]] | None = None,
             disable: Iterable[str] = (), only: str | None = None,
             scparse: str | os.PathLike | None = None, inspection: Inspection | None = None,
             **manager_kw: Any) -> PackageResult:
    """PassManager -> NativeLinker -> ValidateAndPackage (§5.6).

    ``inspection`` (an :class:`Inspection` of ``snapshot``) is shared by callers
    that package several candidates of one snapshot in one invocation, so the
    per-unit ``inspected`` rows are written once; by default each call makes its
    own."""
    from .manager import PassManager

    profiles = tuple(profiles)
    cand = PassManager(snapshot, pipeline, profiles, params, disable=disable, only=only,
                       **manager_kw).execute()
    return ValidateAndPackage(cand, store, scparse=scparse, inspection=inspection,
                              profiles=profiles).run()


def _layout_phase(candidate: Any) -> str:
    """The profile phase the layout passes used (hotcold-partition's ``phase``)."""
    for p in candidate.passes:
        if p.name == "hotcold-partition":
            return str(p.params.get("phase", "steady"))
    return "steady"


# ---------------------------------------------------------------------------
# re-validation of a published artifact (`scopt validate A`)
# ---------------------------------------------------------------------------

def artifact_link(snapshot: Any, art_dir: str | os.PathLike) -> LinkResult:
    """A :class:`LinkResult` describing what is in ``art_dir/cache.sc`` (for
    re-validation): units matched to the input by slot order."""
    from .importer.join import join

    root = Path(art_dir)
    out = read_sc(root / "cache.sc")
    captures = sorted(root.glob("*.scm"))
    binding = join(out, captures).slots if captures and out.loadable else {}
    if not out.loadable:
        raise ScFormatError(f"{art_dir}/cache.sc is not loadable: {out.header_problems}")
    units = []
    in_units = list(snapshot.units())
    uids = out.unit_ids()
    if len(uids) != len(in_units):
        raise ScFormatError(f"artifact has {len(uids)} units, input {len(in_units)}")
    for u, uid, i in zip(in_units, uids, out.valid_slots):
        if u.uid != uid:
            raise ScFormatError(f"slot {i}: {uid} != input {u.uid}")
        rec = out.records[i]
        d = rec.tb_offset - u.tb_offset
        chunk = binding[i].chunk if i in binding else None
        fragments = chunk.native_fragments if chunk is not None else ()
        if d and fragments and fragments[0].splice is not None and not u.splice_applied:
            from .splice_layout import splice_offsets

            call = fragments[0]
            mu = splice_offsets(call)
            patched = tuple((mu(f.field_off), f.width) for f in u.reloc.fixups
                            if not mu.start <= f.field_off < mu.end)
            patched += tuple((mu.start + rel.field_off, 4) for rel in call.splice.relocations)
        else:
            patched = tuple((f.field_off, f.width) for f in u.reloc.fixups if not any(
                c.call_off <= f.field_off < c.continuation_off for c in fragments)) if d else ()
        units.append(UnitLink(u.uid, i, u.tb_offset, rec.tb_offset, out.record_bytes[i],
                              out.bodies[i], patched, input_slot=u.slot, native_fragments=fragments))
    return LinkResult(True, out.header, tuple(units))


#: codes of the move-only static validator (:func:`validate_static`)
STATIC_CODES = frozenset(f"V{i}" for i in range(1, 10))


def static_not_applicable(info: Any) -> str | None:
    """Why :func:`validate_artifact` (V1-V9) cannot re-check the artifact ``info``
    (:func:`scopt.formats.artifact.read_artifact`), or ``None`` when it can.

    V1-V9 re-derive a move-only output (B2 layout): every unit is the input's
    bytes, moved. Artifacts that change unit bytes were packaged by another
    validator, recorded in their ``validation.json``: B3 and B4 by
    :func:`validate_b3` (V1-V23; B3 also writes ``.sc`` v6), the icount-entry
    fix by its SFB1-SFB9 rows. For those, V2 ("unmoved unit changed") would
    reject every edit. They are re-checked by ``scopt publish``
    (:func:`certification_problems`, manifest, stored validation, output
    SnapshotId) and by ``scopt validate --dynamic``."""
    side = info.sidecar or {}
    if side.get("loader_requirement") not in (None, A.LOADER_V5):
        return (f"loader_requirement {side.get('loader_requirement')!r}: static "
                "re-validation reads .sc v5 only")
    val = info.validation or {}
    codes = set(val.get("codes") or {})
    codes |= {r.get("code") for r in val.get("rows") or () if isinstance(r, Mapping)}
    codes.discard(None)
    other = sorted(codes - STATIC_CODES - {"V10"})
    if other:
        return (f"packaged by another validator (validation.json codes "
                f"{', '.join(other[:6])}{' ...' if len(other) > 6 else ''}); "
                "V1-V9 re-check move-only artifacts")
    return None


def validate_artifact(art_dir: str | os.PathLike, snapshot: Any, *,
                      scparse: str | os.PathLike | None = None) -> dict[str, Any]:
    """Re-run V1-V9 on a published artifact against its input snapshot, verify
    MANIFEST.sha256, that the directory name is the ArtifactId the manifest
    gives, and the sidecar's ``validation_sha256``, and compare with the stored
    ``validation.json`` verdicts. Read-only."""
    info = A.read_artifact(art_dir)
    probs = A.verify_manifest(art_dir)
    if info.input_snapshot_id != snapshot.snapshot_id:
        raise ValueError(f"artifact input {info.input_snapshot_id} != snapshot "
                         f"{snapshot.snapshot_id}")
    link = artifact_link(snapshot, art_dir)
    vb = (Path(art_dir) / A.VALIDATION).read_bytes()
    val, _ = validate_static(snapshot, link, art_dir, sidecar=info.sidecar,
                             expected_pinned=info.sidecar.get("pinned"), scparse=scparse)
    val.rows = [r for r in val.rows if not (r["code"] == "V8" and r["check"] == "manifest")]
    val.add("V8", "manifest", not probs, _few(probs) or "MANIFEST.sha256 verifies")
    # The directory is named by its ArtifactId (§4.1, §4.6): a renamed or copied
    # directory, or a manifest edited in place, no longer names itself.
    name = Path(art_dir).resolve().name
    want = None if info.artifact_id is None else A.id_hex(info.artifact_id)
    val.add("V8", "artifact_dir_name", want is not None and name == want,
            f"directory {name} {'==' if name == want else '!='} ArtifactId hex {want} "
            "(sha256 of MANIFEST.sha256)", "artifact_dir_name")
    val.add("V8", "validation_sha256", info.sidecar.get("validation_sha256") == sha256_id(vb),
            "sidecar validation_sha256 matches validation.json")
    stored = (info.validation or {}).get("codes", {})
    now = val.codes()
    same = bool(stored) and stored == now
    return {"artifact_id": info.artifact_id, "artifact_dir": str(art_dir),
            "input_snapshot_id": info.input_snapshot_id,
            "output_snapshot_id": info.output_snapshot_id,
            "verdict": "accepted" if val.accepted else "rejected",
            "codes": now, "stored_codes": stored, "stored_codes_agree": same,
            "rows": val.rows}


# ---------------------------------------------------------------------------
# V10 dynamic (delegates to the WP8 run harness)
# ---------------------------------------------------------------------------

def validate_dynamic(art_dir: str | os.PathLike, configs: Sequence[str | os.PathLike],
                     out_dir: str | os.PathLike, *, baseline: str | os.PathLike | None = None,
                     reps: int = 1, qemu: str | os.PathLike | None = None,
                     timeout_s: float = 300.0, quiet: bool = False, clock=utc_now,
                     inputs: Sequence[str | os.PathLike] = (), input_bank: str = "input"
                     ) -> dict[str, Any]:
    """V10 (§5.8): for each config (held-out input) and repetition, deploy the
    artifact and the baseline (default: the input snapshot's store directory)
    into fresh run directories with the WP8 deploy oracle, launch both
    (interleaved) and require: proof PASS on both, equal exit codes, equal
    normalized output (``norm_b0``) and guest output (``norm_guest``). The
    appended counts are reported. ``inputs`` (held-out input files) are written
    into the ``input_bank`` RAM bank of both arms; every config runs once per
    input. Writes ``out_dir/dynamic.json`` and ``out_dir/ledger.jsonl`` (one
    ``validated_dynamic`` row)."""
    from .run.deploy import DeployError, deploy_new
    from .run.launcher import launch

    art = Path(art_dir).resolve()
    info = A.read_artifact(art)
    if baseline is None:
        baseline = art.parent.parent / "snapshots" / A.id_hex(info.input_snapshot_id)
    baseline = Path(baseline).resolve()
    if qemu is None and info.runtime_identity:
        qemu = (info.runtime_identity.get("qemu_exe_stat") or {}).get("path")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    runs: list[dict[str, Any]] = []
    fails: list[str] = []
    plan = [(ci, cfg, ii, inp) for ci, cfg in enumerate(configs)
            for ii, inp in enumerate([str(Path(x).resolve()) for x in inputs] or [None])]
    for ci, cfg, ii, inp in plan:
        tag = f"c{ci}" if inp is None else f"c{ci}-i{ii}"
        for rep in range(reps):
            pair: dict[str, Any] = {"config": str(cfg), "rep": rep, "input": inp}
            for arm, src in (("artifact", art), ("baseline", baseline)):
                rd_path = out / f"{tag}-r{rep}-{arm}"
                try:
                    rd, dep = deploy_new(src, cfg, rd_path, qemu=qemu, quiet=quiet,
                                         timeout_s=min(timeout_s, 300.0))
                except DeployError as e:
                    pair[arm] = {"run_dir": str(rd_path), "deploy_refused": e.code,
                                 "detail": e.detail}
                    fails.append(f"{tag} r{rep} {arm}: deploy refused {e.code}")
                    continue
                doc = launch(rd, timeout_s=timeout_s,
                             inputs={input_bank: inp} if inp else None)
                pair[arm] = {"run_dir": str(rd_path), "verdict": doc["verdict"],
                             "exit_code": doc["exit_code"], "appended": doc["appended"],
                             "output_norm_sha256": doc["output_norm_sha256"],
                             "guest_norm": doc["guest_norm"], "bench": doc["bench"]}
                if doc["verdict"] != "PASS":
                    fails.append(f"{tag} r{rep} {arm}: {doc['verdict']}")
            a, bl = pair.get("artifact", {}), pair.get("baseline", {})
            if "verdict" in a and "verdict" in bl:
                for k in ("exit_code", "output_norm_sha256", "guest_norm"):
                    if a[k] != bl[k]:
                        fails.append(f"{tag} r{rep}: {k} differs")
            runs.append(pair)
    verdict = "pass" if not fails else "fail"
    doc = {"schema": "scopt.validate_dynamic", "version": 1, "artifact_id": info.artifact_id,
           "artifact_dir": str(art), "baseline": str(baseline), "qemu": None if qemu is None
           else str(qemu), "configs": [str(c) for c in configs], "reps": reps,
           "inputs": [str(Path(x).resolve()) for x in inputs], "input_bank": input_bank,
           "verdict": verdict, "failures": fails, "runs": runs,
           "appended": {"artifact": [r.get("artifact", {}).get("appended") for r in runs],
                        "baseline": [r.get("baseline", {}).get("appended") for r in runs]}}
    data = A.write_json_file(out / "dynamic.json", doc)
    led = Ledger(clock=clock)
    led.emit("artifact", info.artifact_id or str(art), "validated_dynamic", verdict,
             "V10" if not fails else fails[0], sha256_id(data))
    led.write(out / "ledger.jsonl")
    return doc


# ---------------------------------------------------------------------------
# V20 admission identity (B3/B4 design §9.1; WP17). WP18 owns the V-row
# registry and decides when V20 runs (every candidate with a B3/B4 edit);
# this is the row body.
# ---------------------------------------------------------------------------

def v20_rows(v: Validation, runtime: Mapping[str, Any] | None, *,
             units: Iterable[Any] | None = None, need_d3: bool = False,
             need_d2: bool = True) -> bool:
    """Append the V20 rows for ``runtime`` (a RuntimeIdentity document); True if all pass.

    * ``admission``: the identity is v2 with a well-formed admission block,
      else ``observer_contract_unknown``;
    * ``cpu_model``: ``cortex-m3`` (``ADMITTED_CPU_MODELS``), else ``unsupported_guest``;
    * ``envlayout_bound``: a packaged/searchable EnvLayout exists for
      ``qemu_sha256`` and its id equals ``admission.envlayout_id``, else
      ``guard_layout_unbound``;
    * ``tb_start_cb_possible``: recorded; a ``false`` claim is cross-checked
      against the Tier-0 capture of ``units`` (no plugin call in any prefix),
      else ``tb_start_cb_contradicted`` (skipped when ``need_d2`` is false);
    * ``mem_get_value``: only with ``need_d3``: the fact is ``false``, else
      ``observer_contract_unknown``;
    * ``ac4_virtuals``: no AC4-excluded virtual present, else ``ac4_excluded_virtual``;
    * ``exec_vaddr_aliases``: informational (skip row with the recorded aliases).
    """
    from .analysis.hostops import tier0_prefix_plugin_calls
    from .formats.bundle_v1 import ADMITTED_CPU_MODELS, admission_errors, admission_of
    from .formats.envlayout_v1 import check_binding, find_envlayout

    adm = admission_of(runtime)
    errs = admission_errors(adm) if adm is not None else ["not a v2 RuntimeIdentity"]
    ok = v.add("V20", "admission", not errs, _few(errs) or "scopt.admission/1",
               "observer_contract_unknown")
    if errs:
        return False
    cpu = adm.get("cpu_model")
    ok &= v.add("V20", "cpu_model", cpu in ADMITTED_CPU_MODELS, f"{cpu}", "unsupported_guest")
    lay = find_envlayout(runtime.get("qemu_sha256"))
    reason = check_binding(lay, runtime, adm.get("envlayout_id"))
    ok &= v.add("V20", "envlayout_bound", reason is None,
                f"exe {runtime.get('qemu_sha256')} envlayout {adm.get('envlayout_id')}",
                reason or "guard_layout_unbound")
    claim = adm.get("tb_start_cb_possible")
    if not need_d2:
        v.add("V20", "tb_start_cb_possible", None, f"claim {claim}; not needed")
    elif claim is True:
        ok &= v.add("V20", "tb_start_cb_possible", True,
                    "true (conservative): " + ", ".join(adm.get("tb_start_cb_reasons", [])))
    elif units is None:
        ok &= v.add("V20", "tb_start_cb_possible", False,
                    "false claim cannot be cross-checked without the Tier-0 units",
                    "tb_start_cb_unchecked")
    else:
        hits = tier0_prefix_plugin_calls(units)
        ok &= v.add("V20", "tb_start_cb_possible", not hits,
                    _few([f"{u}@{o:#x}" for u, o, _ in hits]) or
                    "false; no prefix plugin call in the Tier-0 capture",
                    "tb_start_cb_contradicted")
    if need_d3:
        mgv = adm.get("mem_get_value_outside_mem_cb")
        ok &= v.add("V20", "mem_get_value", mgv is False,
                    f"{mgv} {adm.get('mem_get_value_evidence')}", "observer_contract_unknown")
    else:
        v.add("V20", "mem_get_value", None, f"{adm.get('mem_get_value_outside_mem_cb')}; "
              "not needed (no D3 edit)")
    present = adm.get("ac4_excluded_virtuals_present") or []
    ok &= v.add("V20", "ac4_virtuals", not present,
                _few(present) or "none (inspected: " + ", ".join(adm.get("ac4_inputs", [])) + ")",
                "ac4_excluded_virtual")
    v.add("V20", "exec_vaddr_aliases", None, f"{adm.get('exec_vaddr_aliases')} (informational; "
          "g_pc covers aliases)")
    return bool(ok)


__all__ += ["v20_rows"]


# ===========================================================================
# B3/B4 V-row registry and the WP18 row bodies (design §9.1; plan "Shared files
# and owners": WP18 owns the registry; WP17 V20, WP19 V12-V14/V18, WP21 V21
# budgets, WP23 V22 fill their bodies).
# ===========================================================================

#: Every V-row id of design §9.1 -> (check, owner WP, status of the body here).
V_ROWS: dict[str, tuple[str, str, str]] = {
    "V1": ("structural (B1 §5.8; v6-aware)", "WP10/WP18", "implemented"),
    "V2": ("byte diff, relaxed for declared B3/B4 edits", "WP10/WP18", "implemented"),
    "V3": ("re-decode", "WP10/WP18", "implemented"),
    "V4": ("range / fixup targets", "WP10/WP18", "implemented"),
    "V5": ("identity: every original unit plus declared variants", "WP10/WP18", "implemented"),
    "V6": ("recovery", "WP10/WP18", "implemented"),
    "V7": ("compat: v6 clause (v6-capable RuntimeIdentity)", "WP10/WP18", "implemented"),
    "V8": ("provenance and re-import", "WP10/WP18", "implemented"),
    "V9": ("byte identity (identity pipeline; V9' identity-v6)", "WP10/WP18", "implemented"),
    "V10": ("dynamic differential (run harness)", "WP10/WP26", "separate stage"),
    "V11": ("ext table rules, two parsers, max_end, artifact id", "WP18", "implemented"),
    "V12": ("hostsym trace equivalence", "WP19",
            "implemented (v12_rows + coverage_rows: every changed unit of the link)"),
    "V13": ("guarded-chain contract (structural part here; hostsym part WP19)", "WP18/WP19",
            "implemented (structural; entailment: v13_entailment_rows + coverage_rows)"),
    "V14": ("fact certification", "WP19/WP20",
            "implemented (v14_rows + coverage_rows: every fact of every SPEC link)"),
    "V15": ("edit placement", "WP18", "implemented"),
    "V16": ("recovery and clone pointer bindings", "WP18", "implemented"),
    "V17": ("reachability", "WP18", "implemented"),
    "V18": ("hostsym self-test and mutation suite", "WP19",
            "implemented (v18_rows; run when validate_b3 gets the self-test document)"),
    "V19": ("summary/region consistency, derived .scm, graph round trip", "WP18/WP20",
            "implemented (CalleeSummary rows checked when given: WP20 format)"),
    "V20": ("admission identity", "WP17", "implemented (v20_rows)"),
    "V21": ("budgets and growth accounting", "WP18/WP21", "implemented (pass budgets: WP21)"),
    "V22": ("ops-xcheck (Tier 1)", "WP23", "not_run: WP23 not approved"),
    "V23": ("observation contract unchanged", "WP18/WP25", "implemented"),
    "V24": ("native call correspondence", "host-inline", "implemented"),
}

#: the WP22 gate-binary record in the maintainer workspace (optional: absent
#: elsewhere, then only ``extra`` and ``$SCOPT_V6_EXES`` count)
V6_GATE_BINARY_JSON = fastdyn_root() / "runs/wp22/gate-binary.json"


def v6_capable_exes(extra: Iterable[str] = ()) -> frozenset[str]:
    """Exe sha256s (hex, no prefix) of QEMU binaries whose loader reads ``.sc`` v6:
    ``extra``, ``$SCOPT_V6_EXES`` (comma separated) and the frozen gate binary
    recorded by WP22 in ``runs/wp22/gate-binary.json`` (``exe_sha256``)."""
    out = {x.removeprefix("sha256:") for x in extra if x}
    out |= {x.strip().removeprefix("sha256:") for x in
            os.environ.get("SCOPT_V6_EXES", "").split(",") if x.strip()}
    try:
        doc = json.loads(V6_GATE_BINARY_JSON.read_text())
        v = doc.get("exe_sha256")
        if isinstance(v, str):
            out.add(v.removeprefix("sha256:"))
    except (OSError, ValueError):
        pass
    return frozenset(out)


def _ri_exe(ri: Mapping[str, Any] | None) -> str | None:
    if not ri:
        return None
    v = ri.get("qemu_sha256") or (ri.get("admission") or {}).get("exe_sha256")
    return None if v is None else str(v).removeprefix("sha256:")


def stub_rows(v: Validation, codes: Iterable[str], reason: str) -> None:
    """``skip`` rows for V-rows whose body is not run here (stable detail)."""
    for c in codes:
        v.add(c, "not_run", None, reason)


def _must_stores(u: Any, layout: Any, code_address: int, fields: Sequence[str],
                 at: int, *, max_rounds: int = 64) -> tuple[bool, str]:
    """Do stores to every field of ``fields`` precede host offset ``at`` on every
    path of the unit's atomic hcfg (forward must-analysis)? (V13) Fails closed
    when the fixpoint is not reached within ``max_rounds`` rounds."""
    from .analysis.hcfg import build_hcfg
    from .analysis.hostops import build_hostops

    h = build_hostops(u, layout, code_address)
    if not h.ok:
        return False, f"hostops pinned: {list(h.reasons)[:3]}"
    g = build_hcfg(h)
    try:
        ti = h.index_of(at)
    except KeyError:
        return False, f"no op at +{at:#x}"
    full = frozenset(fields)
    IN: list[frozenset[str] | None] = [None] * g.n
    IN[g.entry] = frozenset()
    changed = True
    rounds = 0
    while changed:
        if rounds >= max_rounds:
            return False, f"must-store analysis did not converge in {max_rounds} rounds"
        changed = False
        rounds += 1
        for i in range(g.n):
            if i != g.entry:
                ps = [p for p in g.predecessors(i) if IN[p] is not None]
                if not ps:
                    continue
                acc = None
                for p in ps:
                    sf = h.ops[p].store_field
                    o = IN[p] | ({sf} & full if sf else frozenset())  # type: ignore[operator]
                    acc = o if acc is None else acc & o
                if acc != IN[i]:
                    IN[i] = acc
                    changed = True
    got = IN[ti]
    if got is None:
        return False, f"tail +{at:#x} unreachable in hcfg"
    miss = sorted(full - got)
    return not miss, ("stores " + ", ".join(sorted(full)) + " on every path" if not miss
                      else f"not stored on some path: {miss}")


def _b4_deletion_problems(h: Any, p: Any, contract: Any) -> list[str]:
    """V15 for a ``b4_nop_store`` patch (WP25): the old bytes are exactly one
    plain env store (``mov [rbp+d], imm/reg``) whose written bytes lie in one
    field of a deletable class of ``contract`` (D1/D2, D3 only with the
    RuntimeIdentity fact), and the new bytes are NOPs only."""
    from .analysis.hostops import OpKind

    a, b = p.host_off, p.host_off + len(p.old_bytes)
    out: list[str] = []
    ops = [o for o in h.ops if o.off == a and o.end == b]
    if len(ops) != 1:
        return ["b4 deletion is not exactly one host instruction"]
    o = ops[0]
    w = [x for x in o.env if "w" in x.rw]
    if not o.is_env_store or o.kind not in (OpKind.ENV_STORE_IMM, OpKind.ENV_STORE_REG) or \
            len(w) != 1 or any("r" in x.rw for x in o.env):
        out.append(f"field_not_deletable_class (not a plain env store: {o.text})")
    else:
        fld = w[0].field
        if not w[0].classified or contract.deletable_class(fld) is None:
            out.append(f"{contract.not_deletable_reason(fld) or 'field_not_deletable_class'} "
                       f"({fld})")
    xs = sweep(bytes(p.new_bytes), 0)
    if not xs or any(x.bad or not x.mnemonic.startswith("nop") for x in xs) or \
            sum(x.size for x in xs) != len(p.new_bytes):
        out.append("b4 replacement is not NOPs only")
    return out


def validate_b3(snapshot: Any, link: Any, art_dir: str | os.PathLike, *,
                plan: Any = None, sidecar: Mapping[str, Any] | None = None,
                layout: Any = None, v6_exes: Iterable[str] = (),
                runtime: Mapping[str, Any] | None = None, scparse: Any = None,
                sc6parse: Any = None, regions: Sequence[Mapping[str, Any]] | None = None,
                hot_set_bytes: int | None = None, growth_fraction: float = 0.10,
                need_d2: bool = False, need_d3: bool = False,
                passes_byte_delta: Mapping[str, int] | None = None,
                skip_reimport: bool = False,
                summaries: Sequence[Mapping[str, Any]] | None = None,
                hostsym: Mapping[str, Any] | Any | None = None
                ) -> tuple[Validation, Any]:
    """V1-V9 (relaxed for declared B3/B4 edits), V11, V13 (structural), V15-V17,
    V19, V21, V23 on the files in ``art_dir`` written from ``link`` (a
    :class:`~scopt.link.LinkResultV2`), plus V20 (WP17 body) and skip rows for
    the bodies other WPs own (V12, V14, V18: WP19/WP20; V22: WP23).

    ``runtime`` overrides the snapshot's RuntimeIdentity (the identity the
    artifact targets; V7/V20). ``layout`` is the EnvLayout (default: found for
    the identity's exe, else the snapshot's). ``regions`` are the output
    CacheRegion rows (tb_singleton + multi_tb) to check (V19); default: built
    from the re-import.

    ``hostsym`` carries the WP19 inputs (the caller runs the kernel): ``equiv``
    ((label, Verdict) per changed unit, V12), ``entailment`` ((label, Verdict)
    per guarded R', V13 path part), ``facts`` (V14 items) and ``selftest`` (the
    V18 self-test document). Without it, V12/V14/V18 (and V13 when the link has a
    guarded slot) are ``not_run`` rows and :func:`package_b3` refuses an edited plan
    (``certification_not_run``). With it, the V12/V13/V14 ``coverage`` rows
    (:func:`coverage_rows`) require a certifying verdict for every label the link
    needs (:func:`scopt.validate_sym.link_check.required_hostsym`); verdicts for
    labels the link does not need never count.
    ``hostsym`` may also be a callable ``(snapshot, link) -> mapping``
    (:func:`scopt.validate_sym.link_check.hostsym_inputs`), which runs the
    kernel on this ``link``; :func:`package_b3` passes it through.
    """
    from .formats.envlayout_v1 import find_envlayout
    from .formats.sc_v6 import (KIND_SPEC, LOADER_V6, read_sc6, sc6_snapshot_id,
                                sc6parse_compare)
    from .guard import GuardError, GuardOffsets, decode_guard, guard_key
    from .link import lookup_tails, tb_ret_abs_of

    v = Validation()
    art = Path(art_dir)
    out_path = art / "cache.sc"
    t0 = time.monotonic()
    h_in = snapshot.header
    in_units = {u.uid: u for u in snapshot.units()}
    eff = dict(getattr(link, "effective", {}) or {})
    grown = dict(getattr(link, "grown", {}) or {})
    rewritten = dict(getattr(link, "rewritten", {}) or {})
    ri = dict(runtime) if runtime is not None else (
        dict(snapshot.runtime) if snapshot.runtime is not None else None)
    if layout is None:
        layout = find_envlayout((ri or {}).get("qemu_sha256")) or \
            find_envlayout((snapshot.runtime or {}).get("qemu_sha256"))
    ca, cs = h_in.code_address, h_in.code_size

    # ---- V1 structural (v6-aware) -------------------------------------------------
    f6 = read_sc6(out_path)
    out = f6.sc
    is_v6 = f6.is_v6
    want_v = 6 if getattr(link, "version", 5) == 6 else 5
    hdr_fields = [k for k in h_in.__dataclass_fields__
                  if k not in ("checksum", "record_count", "version", "reserved")]
    diff = [k for k in hdr_fields if getattr(out.header, k) != getattr(h_in, k)]
    v.add("V1", "header_equal", not diff,
          "header equal except checksum/record_count (and version/feature_flags for v6)"
          if not diff else _few(diff))
    v.add("V1", "version", out.header.version == want_v and (want_v == 6 or
                                                            out.header.reserved == h_in.reserved),
          f"version {out.header.version} (expected {want_v}), feature_flags "
          f"{out.header.reserved:#x}")
    v.add("V1", "record_count", out.header.record_count == len(link.units),
          f"{out.header.record_count} records for {len(link.units)} units")
    v.add("V1", "header_valid", out.loadable, _few(out.header_problems) or "valid_header ok")
    v.add("V1", "records_valid", out.loadable and not out.skipped and
          len(out.valid_slots) == len(link.units),
          f"{len(out.valid_slots)}/{len(link.units)} records pass load_records")
    sf = f6.structural_findings() if out.loadable else []
    v.add("V1", "canonical", out.loadable and not sf,
          "tb%64, tc-tb, disjoint, >= prologue, zero outside records/ext/variants, exact size"
          if not sf else _few([f"{x.code} {x.detail}" for x in sf]))
    lim = arena_limit(h_in)
    v.add("V1", "max_end_bound", f6.max_end < lim,
          f"max_end (records and variants) {f6.max_end:#x} < {lim:#x}")
    out_uids = out.unit_ids() if out.loadable else []
    in_uids = [u.uid for u in snapshot.units()]
    v.add("V1", "slot_order", out_uids == in_uids, f"{len(out_uids)} units in input slot order"
          if out_uids == in_uids else "UnitId order differs")
    rec_eq = out.loadable and [out.record_bytes[i] for i in out.valid_slots] == \
        [u.record for u in link.units] and \
        [out.bodies[i] for i in out.valid_slots] == [u.body for u in link.units] and \
        list(f6.variant_bodies) == [x.body for x in getattr(link, "variants", ())]
    v.add("V1", "written_equals_linked", rec_eq, "records, arena bytes and variant bodies read "
          "back equal the linker output")
    ok2, det2 = scparse_compare(out_path, out, script=scparse)
    v.add("V1", "second_parser", ok2, det2)

    # ---- V2 byte diff, relaxed ---------------------------------------------------------
    bad2 = []
    for lu in link.units:
        u = in_units[lu.uid]
        if lu.uid in grown:
            g = grown[lu.uid]
            allowed = bytearray(len(lu.body))
            for f in g.unit.reloc.fixups:
                allowed[f.field_off:f.field_off + f.width] = b"\1" * f.width
            for spec, _st in g.stubs:
                allowed[spec.tail_off:spec.tail_off + 11] = b"\1" * 11
            allowed[u.tc_size:g.unit.tc_size] = b"\1" * (g.unit.tc_size - u.tc_size)
            for p in rewritten.get(lu.uid, ()):
                allowed[p.host_off:p.host_off + len(p.old_bytes)] = b"\1" * len(p.old_bytes)
            new_code = lu.body[:g.unit.tc_size]
            d = [i for i in range(u.tc_size) if new_code[i] != u.code[i] and not allowed[i]]
            if d:
                bad2.append(f"{lu.uid}: grown unit differs outside declared ranges at {d[:4]}")
            if lu.body[g.unit.tc_size:] != u.search:
                bad2.append(f"{lu.uid}: search bytes changed")
            continue
        old = u.code + u.search
        if len(old) != len(lu.body):
            bad2.append(f"{lu.uid}: length")
            continue
        allowed = bytearray(len(old))
        if lu.moved:
            for f in u.reloc.fixups:
                allowed[f.field_off:f.field_off + f.width] = b"\1" * f.width
        for p in rewritten.get(lu.uid, ()):
            allowed[p.host_off:p.host_off + len(p.old_bytes)] = b"\1" * len(p.old_bytes)
        d = [i for i, (a, b) in enumerate(zip(old, lu.body)) if a != b and not allowed[i]]
        if d:
            bad2.append(f"{lu.uid}: bytes differ outside declared edits at {d[:4]}")
    v.add("V2", "byte_diff", not bad2,
          f"{len(grown)} grown, {len(rewritten)} rewritten units differ only in declared ranges "
          "(fixups, tails, stubs, patches); every other unit byte-identical" if not bad2
          else _few(bad2))

    # ---- V3 / V4 --------------------------------------------------------------------------
    bad3, bad4 = [], []
    nfix = 0
    for lu in link.units:
        if not lu.moved:
            continue
        e = eff.get(lu.uid, in_units[lu.uid])
        probs, targets = decode_compare(e, lu, ca, cs)
        bad3 += [f"{lu.uid} {p}" for p in probs]
        for f in e.reloc.fixups:
            if e.pool_start is not None and f.field_off >= e.pool_start:
                continue  # stub fixups: checked by the stub decode below
            nfix += 1
            t = targets.get(f.field_off)
            if t is None:
                bad4.append(f"{lu.uid} fixup @{f.field_off}: no decoded reference")
            elif f.rule == "rel_minus_delta" and t[0] != t[1]:
                bad4.append(f"{lu.uid} fixup @{f.field_off}: target {t[0]:#x} -> {t[1]:#x}")
            elif f.rule == "abs_plus_delta" and t[1] != t[0] + lu.delta:
                bad4.append(f"{lu.uid} fixup @{f.field_off}: abs {t[0]:#x} -> {t[1]:#x}")
        if lu.uid in grown:  # the stubs decode to the same guard after the move
            g = grown[lu.uid]
            for _spec, st in g.stubs:
                try:
                    r0 = ScRecord.from_bytes(g.unit.record)
                    rec = ScRecord.from_bytes(lu.record)
                    reg0 = {n: (r0.jmp_insn_offset[n], r0.jmp_reset_offset[n]) for n in (0, 1)
                            if r0.jmp_insn_offset[n] != NONE16}
                    reg1 = {n: (rec.jmp_insn_offset[n], rec.jmp_reset_offset[n])
                            for n in (0, 1) if rec.jmp_insn_offset[n] != NONE16}
                    a = decode_guard(g.unit.code, st.off, tc_abs=ca + g.unit.tc_offset,
                                     struct_off=g.unit.tb_offset - g.unit.tc_offset,
                                     registered=reg0)
                    b = decode_guard(lu.body[:g.unit.tc_size], st.off,
                                     tc_abs=ca + rec.tc_offset,
                                     struct_off=rec.tb_offset - rec.tc_offset, registered=reg1)
                    if (a.atoms, a.slots, a.pool_slot_off, a.tb_ret_abs, a.end) != \
                            (b.atoms, b.slots, b.pool_slot_off, b.tb_ret_abs, b.end):
                        bad4.append(f"{lu.uid} stub @{st.off}: differs after the move")
                except GuardError as ge:
                    bad3.append(f"{lu.uid} stub @{st.off}: {ge}")
    v.add("V3", "redecode", not bad3,
          "moved units: same tiling, mnemonics and semantic targets; stubs decode" if not bad3
          else _few(bad3))
    v.add("V4", "fixup_targets", not bad4, f"{nfix} fixup fields keep their targets; stubs keep "
          "tb_ret and pool targets" if not bad4 else _few(bad4))
    bad4b = []
    for lu in link.units:
        e = eff.get(lu.uid, in_units[lu.uid])
        rec = ScRecord.from_bytes(lu.record)
        tc_abs = ca + rec.tc_offset
        for off in rec.jmp_insn_offset:
            if off != NONE16 and ((tc_abs + off) % 4 or lu.body[off:off + 4] != b"\0\0\0\0"):
                bad4b.append(f"{lu.uid} jump field @{off}")
        if e.pool_start is not None and (tc_abs + e.pool_start) % 8:
            bad4b.append(f"{lu.uid} pool @{e.pool_start} not 8-aligned")
    v.add("V4", "goto_tb_and_pool_alignment", not bad4b,
          "registered jump fields 4-aligned and zero; pools 8-aligned" if not bad4b
          else _few(bad4b))

    # ---- V5 identity (relaxed) ----------------------------------------------------------
    def ident(uid: str, rec: ScRecord) -> tuple:
        return (uid, rec.page_addr0, rec.page_addr1, rec.flags, rec.cs_base, rec.cflags,
                rec.guest_hash, rec.guest_size, rec.icount)
    mi = Counter(ident(u.uid, ScRecord.from_bytes(u.record)) for u in snapshot.units())
    mo = Counter(ident(uid, out.records[i]) for uid, i in zip(out_uids, out.valid_slots))
    v.add("V5", "unit_multiset", mi == mo, f"{sum(mo.values())} units, none added or dropped"
          if mi == mo else "unit multiset differs")
    bad5 = []
    for uid, i in zip(out_uids, out.valid_slots):
        if uid not in in_units:  # e.g. a duplicated record (the unit multiset fails too)
            bad5.append(f"{uid}: not an input unit")
            continue
        r_in = ScRecord.from_bytes(in_units[uid].record)
        r_out = out.records[i]
        declared = set()
        if uid in grown and grown[uid].registered:
            declared = {si.slot_n for _, st in grown[uid].stubs for si in st.slots}
        for n in (0, 1):
            if n in declared:
                if r_in.jmp_insn_offset[n] != NONE16:
                    bad5.append(f"{uid}: declared slot {n} was registered in the input")
                continue
            if (r_out.jmp_insn_offset[n], r_out.jmp_reset_offset[n]) != \
                    (r_in.jmp_insn_offset[n], r_in.jmp_reset_offset[n]):
                bad5.append(f"{uid}: jump offsets of slot {n} changed")
        if out.bodies[i][r_out.tc_size:] != in_units[uid].search:
            bad5.append(f"{uid}: search data")
    vb = list(getattr(link, "variants", ()))
    nvar_ext = 0 if f6.ext is None else len(f6.ext.variants)
    if nvar_ext != len(vb) or (plan is not None and len(vb) != len(plan.variants)):
        bad5.append(f"variants: ext {nvar_ext}, linked {len(vb)}, declared "
                    f"{None if plan is None else len(plan.variants)}")
    v.add("V5", "identity_relaxed", not bad5,
          f"every original unit present (key, guest hash/size, icount, jump offsets except "
          f"declared guarded slots, search data); {len(vb)} declared variants" if not bad5
          else _few(bad5))

    # ---- V6 recovery ------------------------------------------------------------------------
    bad6, npts = [], 0
    for lu in link.units:
        e = eff.get(lu.uid, in_units[lu.uid])
        if not e.recovery:
            continue
        rec = ScRecord.from_bytes(lu.record)
        try:
            rows = decode_search(lu.body[rec.tc_size:], e.icount, is_pcrel(rec.cflags))
        except ValueError as ex:
            bad6.append(f"{lu.uid}: search {ex}")
            continue
        for p in e.recovery:
            npts += 1
            got = unwind(rows, p.host_return_offset)
            if got is None or got[0] != p.insn or tuple(got[1]) != tuple(p.words):
                bad6.append(f"{lu.uid} {p.id}")
    v.add("V6", "unwind", not bad6, f"{npts} recovery points unwind at the new placement"
          if not bad6 else _few(bad6))

    # ---- V7 compat (v6 clause) ---------------------------------------------------------------
    rt = ("runtime_signature", "code_address", "code_size", "prologue_end", "tb_size",
          "target_page_bits", "host_page_size")
    d7 = [k for k in rt if getattr(out.header, k) != getattr(h_in, k)]
    v.add("V7", "runtime_fields_copied", not d7, _few(d7) or ", ".join(rt))
    b = snapshot.bundle
    v.add("V7", "bundle", b is not None and b.get("bundle_id") == _bundle_id(b),
          f"bundle_id {None if b is None else b.get('bundle_id')}", "identity_missing:bundle")
    v.add("V7", "runtime_identity", ri is not None and ri.get("runtime_identity_id") ==
          content_id(ri, "runtime_identity_id"), f"{None if ri is None else ri.get('runtime_identity_id')}",
          "identity_missing:runtime_identity")
    if ri is not None:
        v.add("V7", "supercache_flat", ri.get("supercache_flat") is False,
              f"supercache_flat={ri.get('supercache_flat')!r}", "unsupported_flat")
    lr = None if sidecar is None else sidecar.get("loader_requirement")
    want_lr = LOADER_V6 if is_v6 else A.LOADER_V5
    v.add("V7", "loader_requirement", None if sidecar is None else lr == want_lr,
          f"{lr} (expected {want_lr})" if sidecar is not None
          else f"no sidecar given (checked by package_b3; expected {want_lr})")
    if is_v6:
        exe = _ri_exe(ri)
        caps = v6_capable_exes(v6_exes)
        v.add("V7", "v6_capable_runtime", exe is not None and exe in caps,
              f"RuntimeIdentity exe {exe} {'is' if exe in caps else 'is not'} a v6-capable "
              f"(qint-v6) binary ({len(caps)} known)", "v6_runtime_required")

    # ---- V8 provenance / re-import ------------------------------------------------------------
    out_sid = sc6_snapshot_id(f6) if out.loadable else None
    res = None
    if not skip_reimport:
        try:
            res = reimport(art, snapshot)
        except Exception as ex:  # noqa: BLE001 - fail closed
            v.add("V8", "reimport", False, f"{type(ex).__name__}: {ex}", "reimport_failed")
    if res is not None:
        v.add("V8", "reimport_snapshot_id", res.snapshot.snapshot_id == out_sid,
              f"{res.snapshot.snapshot_id}")
        bad8, bad3b = [], []
        for u2 in res.snapshot.units():
            e = eff.get(u2.uid) or in_units.get(u2.uid)
            if e is None:
                bad8.append(f"{u2.uid}: not in input")
                continue
            if (_status(u2), u2.reloc.reasons, u2.origin) != (_status(e), e.reloc.reasons,
                                                              e.origin):
                bad8.append(f"{u2.uid}: {_status(e)}{list(e.reloc.reasons)} -> "
                            f"{_status(u2)}{list(u2.reloc.reasons)}")
            if _fixup_rows(u2) != _fixup_rows(e):
                bad3b.append(f"{u2.uid}: fixups differ")
            if (u2.stub_start, [(p.host_return_offset, p.insn) for p in u2.recovery]) != \
                    (e.stub_start, [(p.host_return_offset, p.insn) for p in e.recovery]):
                bad3b.append(f"{u2.uid}: stub_start {e.stub_start}->{u2.stub_start} or recovery "
                             f"points {len(e.recovery)}->{len(u2.recovery)} differ")
        v.add("V8", "reimport_status", not bad8,
              f"{len(res.snapshot.units())} units keep status, reasons and origin" if not bad8
              else _few(bad8))
        v.add("V3", "two_sided_reimport", not bad3b and not bad8,
              "re-import: same fixups, stub starts and recovery points; 0 unexplained/missing "
              "references" if not (bad3b or bad8) else _few(bad3b + bad8))
        vbad = [r for r in res.variants if not r.get("ok")]
        v.add("V8", "reimport_variants", not vbad and len(res.variants) == nvar_ext,
              f"{len(res.variants)} variants re-imported (VARIANT chunks: bytes == relocate(C) "
              "+ patches)" if not vbad else _few([f"{r['variant_index']}: {r['problems']}"
                                                  for r in vbad]))
    if sidecar is not None:
        errs = A.sidecar_errors(sidecar)
        v.add("V8", "sidecar_schema", not errs, _few(errs) or "artifact_sidecar.v1")

    # ---- V9 / V9' ---------------------------------------------------------------------------
    no_edit = plan is not None and plan.empty
    if no_edit and not is_v6:
        sp = getattr(snapshot, "path", None)
        ok9 = sp is not None and sha256_sparse(Path(sp) / "cache.sc") == sha256_sparse(out_path)
        v.add("V9", "identity_sha256", ok9, "identity pipeline: sha256 equal to the input",
              "identity_sha256_mismatch")
    elif no_edit and is_v6:
        same = out.loadable and [out.record_bytes[i] for i in out.valid_slots] == \
            [u.record for u in snapshot.units()] and \
            [out.bodies[i] for i in out.valid_slots] == [u.code + u.search
                                                        for u in snapshot.units()] and \
            f6.ext is not None and not (f6.ext.variants or f6.ext.slots or f6.ext.links)
        v.add("V9", "identity_v6", same, "V9': identity-v6: every record and code byte equal, "
              "empty extension table" if same else "identity-v6 differs", "identity_v6_mismatch")
    else:
        v.add("V9", "identity_sha256", None, "not an identity candidate")

    # ---- V11 ext table -----------------------------------------------------------------------
    if is_v6:
        v.add("V11", "ext_rules", f6.problem is None,
              "every §5.3/§5.4 rule (+ WP22 additions) holds offline" if f6.problem is None
              else f"loader would die: {f6.problem[0]}: {f6.problem[1]}",
              None if f6.problem is None else f6.problem[0])
        ok11, det11, _doc = sc6parse_compare(out_path, f6, script=sc6parse)
        v.add("V11", "independent_parser", ok11, det11)
        v.add("V11", "max_end_bound", f6.max_end < lim,
              f"max_end' {f6.max_end:#x} < code_size - host_page - 1024 = {lim:#x}")
        aid = None if f6.ext is None else bytes(f6.ext.artifact_id8).hex()
        side_aid = None if sidecar is None else sidecar.get("x_ext_artifact_id8")
        v.add("V11", "artifact_id", aid not in (None, "0" * 16) and
              (sidecar is None or side_aid == aid),
              f"ext artifact_id8 {aid}; sidecar x_ext_artifact_id8 {side_aid}")
    else:
        v.add("V11", "ext_rules", None, "v5 artifact: no extension table")

    # ---- V13 guarded-chain contract (structural) ------------------------------------------------
    off = None
    try:
        off = GuardOffsets.from_layout(layout)
    except GuardError as ge:
        v.add("V13", "envlayout", False if grown else None, str(ge), "guard_layout_unbound")
    if off is not None:
        adm_lay = ((ri or {}).get("admission") or {}).get("envlayout_id")
        v.add("V13", "envlayout_id", adm_lay is None or adm_lay == off.envlayout_id,
              f"stub EnvLayout {off.envlayout_id}; RuntimeIdentity {adm_lay}",
              "guard_layout_unbound")
    bad13, nst = [], 0
    tb_ret = tb_ret_abs_of(getattr(getattr(snapshot, "working_set", snapshot), "base", snapshot))
    ext_by_src: dict[tuple[int, int], Any] = {}
    if f6.ext is not None:
        ext_by_src = {(s.src_tb_offset, s.slot_n): s for s in f6.ext.slots}
    out_by_uid = {lu.uid: lu for lu in link.units}
    for uid, g in grown.items():
        lu = out_by_uid[uid]
        rec = ScRecord.from_bytes(lu.record)
        src = g.source
        reg = {n: (rec.jmp_insn_offset[n], rec.jmp_reset_offset[n]) for n in (0, 1)
               if rec.jmp_insn_offset[n] != NONE16}
        tails = {t.off: t for t in lookup_tails(src)}
        for spec, st in g.stubs:
            nst += 1
            try:
                dg = decode_guard(lu.body[:rec.tc_size], st.off, tc_abs=ca + rec.tc_offset,
                                  struct_off=rec.tb_offset - rec.tc_offset, offsets=off,
                                  registered=reg)
            except GuardError as ge:
                bad13.append(f"{uid} stub @{st.off}: {ge.detail}")
                continue
            names = [a for a, _, _ in dg.atoms]
            if names[:5] != ["g_bp", "g_thumb", "g_cond", "g_flags", "g_flags2"] or \
                    names[5:] != ["g_pc"] * len(spec.targets):
                bad13.append(f"{uid}: atoms {names}")
            try:
                kf, kf2 = guard_key(spec.key_flags, spec.key_cs_base)
            except GuardError as ge:
                bad13.append(f"{uid}: key_C {ge}")
                kf = kf2 = None
            if (dg.kf, dg.kf2) != (kf, kf2):
                bad13.append(f"{uid}: Kf/Kf2 {dg.kf:#x}/{dg.kf2:#x} != key_C")
            if [x.vaddr for x in dg.slots] != [x.vaddr for x in spec.targets]:
                bad13.append(f"{uid}: g_pc constants {dg.vaddrs}")
            for si, ra in zip(dg.slots, dg.tb_ret_abs):
                if si.reset_kind == "exit_tb" and (tb_ret is None or ra != tb_ret):
                    bad13.append(f"{uid} slot {si.slot_n}: reset jmp {ra} != the snapshot's "
                                 f"tb_ret_addr {tb_ret}")
            t = tails.get(spec.tail_off)
            if t is None:
                bad13.append(f"{uid}: no original tail at +{spec.tail_off:#x}")
                continue
            if dg.f_bytes[:5] != t.raw[:5] or dg.f_bytes[9:] != t.raw[9:] or \
                    dg.pool_slot_off != t.pool_slot_off:
                bad13.append(f"{uid}: F != the original tail (same pool slot)")
            if lu.body[spec.tail_off:spec.tail_off + 11] != b"\xe9" + \
                    (st.off - spec.tail_off - 5).to_bytes(4, "little", signed=True) + b"\xcc" * 6:
                bad13.append(f"{uid}: tail is not jmp S; int3 x6")
            for si, tg in zip(dg.slots, spec.targets):
                want_reset = "exit_tb" if (tg.kind == "GENERAL" and g.registered) else "jmp_F"
                if si.reset_kind != want_reset:
                    bad13.append(f"{uid} slot {tg.slot_n}: reset {si.reset_kind} != {want_reset}")
                if g.registered:
                    if reg.get(tg.slot_n) != (si.field_off, si.reset_off):
                        bad13.append(f"{uid} slot {tg.slot_n}: record jump offsets")
                    se = ext_by_src.get((rec.tb_offset, tg.slot_n))
                    if se is None or se.target_vaddr != tg.vaddr or se.guard_off != dg.off or \
                            se.guard_len != dg.end - dg.off or \
                            (se.kind == KIND_SPEC) != (tg.kind == "SPEC"):
                        bad13.append(f"{uid} slot {tg.slot_n}: ext slot entry disagrees")
            xs = sweep(lu.body[dg.off:dg.end], ca + rec.tc_offset + dg.off)
            calls = [x for x in xs if x.is_call]
            if [x.address - (ca + rec.tc_offset) for x in calls] != [dg.f_off + 3] or \
                    any(x.mnemonic not in ("cmp", "jne", "je", "jmp", "nop", "lea", "movabs",
                                           "mov", "call") for x in xs):
                bad13.append(f"{uid}: stub has a store/call/LDST beyond F's lookup call")
            if layout is not None:
                need = ["regs[15]", "thumb"] if spec.exit_class == "return" else ["regs[15]"]
                ok, det = _must_stores(src, layout, ca, need, spec.tail_off)
                if not ok:
                    bad13.append(f"{uid}: {det}")
    # SPEC key binding, from the written file only (not the plan): for every link,
    # the guard bytes of the SPEC slot must test exactly the lookup key of the
    # general C whose clone it enters (invariant 2). The loader checks only Vc's
    # page offset and the arm-time probe only Vc's ram address and C's MMU index.
    bad13k, nkey = [], 0
    if f6.ext is not None and f6.ext.links and out.loadable and f6.problem is not None:
        # the loader refuses the table (V11 fails with its reason): the entries
        # cannot be trusted to index each other
        v.add("V13", "spec_key_binding", False, f"ext table refused by the loader: "
              f"{f6.problem[0]}", "guard_contract_violation")
    elif f6.ext is not None and f6.ext.links and out.loadable:
        rec_of = {}
        for i in out.valid_slots:
            rec_of[out.records[i].tb_offset] = (out.records[i], out.bodies[i])
        uid_at = {ScRecord.from_bytes(lu.record).tb_offset: lu.uid for lu in link.units}
        for li, lk in enumerate(f6.ext.links):
            nkey += 1
            if not (0 <= lk.slot_index < len(f6.ext.slots)
                    and 0 <= lk.variant_index < len(f6.ext.variants)):
                bad13k.append(f"link {li}: slot/variant index out of range")
                continue
            se = f6.ext.slots[lk.slot_index]
            ve = f6.ext.variants[lk.variant_index]
            p13 = f"link {li} (slot {lk.slot_index} -> variant {lk.variant_index})"
            rr, cr = rec_of.get(se.src_tb_offset), rec_of.get(ve.general_tb_offset)
            if rr is None or cr is None:
                bad13k.append(f"{p13}: R or C record missing")
                continue
            rrec, rbody = rr
            crec = cr[0]
            regr = {n: (rrec.jmp_insn_offset[n], rrec.jmp_reset_offset[n]) for n in (0, 1)
                    if rrec.jmp_insn_offset[n] != NONE16}
            try:
                dk = decode_guard(rbody[:rrec.tc_size], se.guard_off,
                                  tc_abs=ca + rrec.tc_offset,
                                  struct_off=rrec.tb_offset - rrec.tc_offset, offsets=off,
                                  registered=regr)
            except GuardError as ge:
                bad13k.append(f"{p13}: guard does not decode: {ge.detail}")
                continue
            si = next((x for x in dk.slots if x.slot_n == se.slot_n), None)
            if si is None or si.kind != "SPEC" or si.vaddr != se.target_vaddr:
                bad13k.append(f"{p13}: slot {se.slot_n} not a SPEC compare of "
                              f"{se.target_vaddr:#x} in the guard")
                continue
            try:
                want = guard_key(crec.flags, crec.cs_base)
            except GuardError as ge:
                bad13k.append(f"{p13}: C's key not admissible: {ge.detail}")
                continue
            if (dk.kf, dk.kf2) != want:
                bad13k.append(f"{p13}: guard Kf/Kf2 {dk.kf:#x}/{dk.kf2:#x} != C's key "
                              f"{want[0]:#x}/{want[1]:#x}")
            cu = in_units.get(uid_at.get(crec.tb_offset, ""))
            vas = {x & ~1 for x in (getattr(cu, "vaddrs", None) or ())}
            if not vas:
                bad13k.append(f"{p13}: C has no known virtual address")
            elif si.vaddr not in vas:  # exact: a Vc with bit 0 set is not C's pc
                bad13k.append(f"{p13}: Vc {si.vaddr:#x} is not a virtual address of C")
    if nkey:
        v.add("V13", "spec_key_binding", not bad13k,
              f"{nkey} SPEC links: the guard bytes test C's own key (Kf/Kf2 == "
              "guard_key(C.flags, C.cs_base)) and Vc is one of C's virtual addresses"
              if not bad13k else _few(bad13k), "guard_contract_violation")
    if grown or nst:
        v.add("V13", "guard_template", not bad13,
              f"{nst} stubs decode to {('guard-v1')} with every atom; constants equal the slot "
              "entries and key_C; F equals the tail; resets by kind; R15/thumb stored on every "
              "path; no store/call/LDST but F's call. Rule 6 (no DISAS_EXIT R) holds by "
              "construction: such TBs end in exit_tb(NULL,0) and have no lookup tail"
              if not bad13 else _few(bad13), "guard_contract_violation")
    else:
        v.add("V13", "guard_template", None, "no guarded slot")

    # ---- V15 edit placement -------------------------------------------------------------------
    bad15, npat = [], 0
    from .analysis.hostops import build_hostops
    from .analysis.obs_contract import build_contract

    contract15 = build_contract(layout, ri)
    # close-out (design/closeout-logreg.md §A.2 rule 7): one coverage ring per process, so
    # every matched LOG_REG block of the input snapshot must share one (IDX, CONS, BUF)
    # triple; otherwise no unit with a block may be edited (fail closed)
    from .decode.logreg import blocks_by_uid

    log15 = blocks_by_uid(in_units.values(), layout, ca) if layout is not None else {}
    triples15 = {lb.triple for lb in log15.values()}
    edited15 = set(rewritten) | set(grown) | {vl.src_uid for vl in getattr(link, "variants", ())}
    if len(triples15) > 1:
        for uid in sorted(edited15 & set(log15)):
            bad15.append(f"{uid} (rule 7, {len(triples15)} distinct LOG_REG triples in the "
                         "snapshot): edit_inside_log_block")

    def placement(u: Any, patches: Sequence[Any], who: str) -> None:
        nonlocal npat
        if not patches:
            return
        h = build_hostops(u, layout, ca)
        blocks = h.blocks
        for p in patches:
            npat += 1
            a, b = p.host_off, p.host_off + len(p.old_bytes)
            if len(p.old_bytes) != len(p.new_bytes):
                bad15.append(f"{who} @{a}: not same-length")
            if any(lb.overlaps(a, b) for lb in h.log_blocks):
                bad15.append(f"{who} @{a}: edit_inside_log_block")
            kinds = {bl.kind for bl in blocks if bl.start < b and a < bl.end}
            if "prefix" in kinds:
                ok_d2 = p.template_id == "b4_nop_store" and any(
                    o.off == a and o.end == b and o.is_env_store and
                    o.store_field == "neg.can_do_io" for o in h.ops)
                if not ok_d2:
                    bad15.append(f"{who} @{a}: edit_in_prefix")
            for k, code in (("ldst_fast", "edit_inside_ldst_block"),
                            ("ldst_slow", "edit_inside_ldst_block"),
                            ("exit_stub", "edit_in_exit_stub"), ("pool", "edit_in_exit_stub")):
                if k in kinds:
                    bad15.append(f"{who} @{a}: {code}")
            if u.stub_start is not None and b > u.stub_start:
                bad15.append(f"{who} @{a}: edit_in_exit_stub (guard stub)")
            ins = [i for i in u.insns if i.host_start < b and a < i.host_end]
            if len(ins) > 1 or (ins and not (ins[0].host_start <= a and b <= ins[0].host_end)):
                bad15.append(f"{who} @{a}: edit_on_insn_boundary")
            if not any(o.off == a for o in h.ops) or not any(o.end == b for o in h.ops):
                bad15.append(f"{who} @{a}: patch does not cover whole host instructions")
            if p.template_id == "b4_nop_store":
                # WP25: "deletions only of deletable classes" (§9.1 V15, §7.2, §7.5)
                bad15.extend(f"{who} @{a}: {e}" for e in _b4_deletion_problems(h, p, contract15))
        # re-decode tiles
        code = bytearray(u.code)
        for p in patches:
            code[p.host_off:p.host_off + len(p.new_bytes)] = p.new_bytes
        end = u.pool_start if u.pool_start is not None else len(code)
        xs = sweep(bytes(code[:end]), 0)
        if not tiles(xs, 0, end):
            bad15.append(f"{who}: patched code does not re-decode")
        else:
            old = {x.address for x in sweep(u.code[:end], 0)}
            new = {x.address for x in xs}
            spans = [(p.host_off, p.host_off + len(p.new_bytes)) for p in patches]
            outside = {o for o in old ^ new if not any(s <= o < e for s, e in spans)}
            if outside:
                bad15.append(f"{who}: insn boundaries changed outside the patches")
    for uid, ps in rewritten.items():
        placement(in_units[uid], ps, uid)
    for vl in getattr(link, "variants", ()):
        placement(eff.get(vl.src_uid, in_units[vl.src_uid]), vl.patches, f"variant {vl.index}")
    v.add("V15", "placement", not bad15,
          f"{npat} patches: same-length, outside prefix (D2 store excepted), LDST blocks, "
          "LOG_REG blocks, exit stubs, pool and guard stubs, inside one insn, whole "
          "instructions, re-decode tiles"
          if not bad15 else _few(bad15), (bad15[0].split(": ")[-1] if bad15 else None))

    # ---- V16 recovery and clone bindings ---------------------------------------------------------
    bad16, nv16 = [], 0
    for vl in getattr(link, "variants", ()):
        nv16 += 1
        c = eff.get(vl.src_uid, in_units[vl.src_uid])
        cl = out_by_uid[vl.src_uid]
        crec = ScRecord.from_bytes(cl.record)
        if vl.body[vl.tc_size:] != cl.body[crec.tc_size:] or vl.tc_size != crec.tc_size:
            bad16.append(f"variant {vl.index}: rows not byte-identical to C")
        try:
            rows = decode_search(vl.body[vl.tc_size:], c.icount, is_pcrel(crec.cflags))
            if len(rows) != c.icount:
                bad16.append(f"variant {vl.index}: {len(rows)} rows != icount {c.icount}")
            crows = decode_search(cl.body[crec.tc_size:], c.icount, is_pcrel(crec.cflags))
            for p in c.recovery:
                if unwind(rows, p.host_return_offset) != unwind(crows, p.host_return_offset):
                    bad16.append(f"variant {vl.index} {p.id}: unwind differs")
        except ValueError as ex:
            bad16.append(f"variant {vl.index}: search {ex}")
        want = bytearray(relocate_unit_body(c, vl.tb_offset))
        for p in vl.patches:
            want[p.host_off:p.host_off + len(p.new_bytes)] = p.new_bytes
        if bytes(want) != vl.body:
            bad16.append(f"variant {vl.index}: bytes != relocate(C) + declared patches")
        # embedded pointers: decode-compare the variant with C carrying the same
        # declared patches in place, so every finding counts (none is discarded)
        cp = c
        if vl.patches:
            pc_ = bytearray(c.code)
            for p in vl.patches:
                pc_[p.host_off:p.host_off + len(p.new_bytes)] = p.new_bytes
            from dataclasses import replace as _dc_replace
            cp = _dc_replace(c, code=bytes(pc_))
        fake = UnitLink(vl.src_uid, 0, c.tb_offset, vl.tb_offset, b"", vl.body)
        probs, _t = decode_compare(cp, fake, ca, cs)
        if probs:
            bad16.append(f"variant {vl.index}: {probs[:2]}")
    v.add("V16", "variants", not bad16,
          f"{nv16} variants: rows byte-identical, icount rows, unwind equal, bytes == "
          "relocate(C) + declared patches, embedded pointers equal C's bindings" if not bad16
          else _few(bad16))

    # ---- V17 reachability ------------------------------------------------------------------------
    bad17 = []
    if f6.ext is not None and f6.ext.variants:
        vr = [(x.tb_offset, x.end_offset) for x in f6.ext.variants]
        lk_per_var = Counter(lk.variant_index for lk in f6.ext.links)
        for i, _x in enumerate(f6.ext.variants):
            if lk_per_var.get(i, 0) != 1:
                bad17.append(f"variant {i}: {lk_per_var.get(i, 0)} links")
        for s_ in f6.ext.slots:
            if any(a <= s_.src_tb_offset < b_ for a, b_ in vr):
                bad17.append(f"slot names variant at {s_.src_tb_offset:#x} as R")
        for i in out.valid_slots:
            r_ = out.records[i]
            if any(r_.tb_offset < b_ and a < r_.end_offset for a, b_ in vr):
                bad17.append(f"record {i} overlaps a variant")
        targets = []
        for lu in link.units:
            e = eff.get(lu.uid, in_units[lu.uid])
            d = lu.delta
            for s_ in e.sites:
                tv = s_.value + d if ca + e.tb_offset <= s_.value < ca + e.end_offset else s_.value
                targets.append((lu.uid, tv))
        for uid, tv in targets:
            if any(ca + a <= tv < ca + b_ for a, b_ in vr):
                bad17.append(f"{uid}: a reference targets a variant")
        v.add("V17", "reachability", not bad17,
              f"{len(vr)} variants: one link each, no slot/record/reference targets C' (its "
              "only entry is the armed SPEC slot), no variant is a record" if not bad17
              else _few(bad17))
    else:
        bad17 = []
        if f6.ext is not None:
            recs = {out.records[i].tb_offset for i in out.valid_slots}
            bad17 = [f"slot names {s_.src_tb_offset:#x}, not a record" for s_ in f6.ext.slots
                     if s_.src_tb_offset not in recs]
        v.add("V17", "reachability", not bad17,
              "no variant: every slot names a record; nothing else is reachable"
              if not bad17 else _few(bad17))

    # ---- V19 summary / region consistency, derived .scm, graph --------------------------------
    if res is not None:
        regs = list(regions) if regions is not None else \
            list(res.regions) + multi_tb_rows(res, link, layout=layout, runtime=ri)
        rbad = [str(r.get("region_id")) for r in regs if validate_region(r)]
        multi = [r for r in regs if r.get("kind") == "multi_tb"]
        bad19 = []
        if f6.ext is not None:
            for li, lk in enumerate(f6.ext.links):
                se = f6.ext.slots[lk.slot_index]
                ve = f6.ext.variants[lk.variant_index]
                rows = [r for r in multi if int(r["members"][0]["origin"]["tb_offset"], 16) ==
                        se.src_tb_offset and r.get("x_slot") == se.slot_n and
                        int(r["members"][1]["origin"]["tb_offset"], 16) == ve.tb_offset]
                if len(rows) != 1:
                    bad19.append(f"link {li}: {len(rows)} multi_tb rows")
                    continue
                r = rows[0]
                cur = {u.uid for u in res.snapshot.units()}
                if any(x not in cur for x in r["dependencies"]["units"]):
                    bad19.append(f"link {li}: stale dependency units")
        v.add("V19", "regions", not rbad and not bad19,
              f"{len(regs)} CacheRegion rows validate; {len(multi)} multi_tb rows, one per SPEC "
              "link, with current dependency units" if not (rbad or bad19)
              else _few(rbad + bad19))
        from .graph import b3_edges, check_graph, export_graph, graph_bytes, load_graph

        gdoc = export_graph(res.snapshot, variants=res.variants)
        data = graph_bytes(gdoc)
        bad_g = []
        try:
            back = load_graph(data)
            if graph_bytes(back) != data:
                bad_g.append("export -> import -> export not byte-identical")
            errs = check_graph(back, res.snapshot, variants=res.variants)
            bad_g += errs[:3]
            e3 = b3_edges(back)
            want_gs = 0 if f6.ext is None else len(f6.ext.slots)
            want_gs += sum(len(st.slots) for g in grown.values() if not g.registered
                           for _, st in g.stubs)
            if len(e3["guarded_slot"]) != want_gs:
                bad_g.append(f"{len(e3['guarded_slot'])} guarded_slot edges != {want_gs}")
            nl = 0 if f6.ext is None else len(f6.ext.links)
            if len(e3["variant_link"]) != nl:
                bad_g.append(f"{len(e3['variant_link'])} variant_link edges != {nl}")
        except Exception as ex:  # noqa: BLE001
            bad_g.append(f"{type(ex).__name__}: {ex}")
        # a return exit's fallback lookup (F) has targets outside the guarded set:
        # the region must say so; a static exit's F has its one static target
        unk_bad = [uid for uid, g in grown.items()
                   if any(spec.exit_class == "return" for spec, _ in g.stubs) and
                   not any(x.startswith("goto_ptr") for x in
                           res.snapshot.regions[uid].get("unknowns", []))]
        v.add("V19", "graph_roundtrip", not bad_g and not unk_bad,
              "graph export -> import reproduces the guarded_slot and variant_link edges; the "
              "fallback lookup of every guarded exit is an explicit unknown"
              if not (bad_g or unk_bad) else _few(bad_g + [f"{u}: no explicit unknown"
                                                           for u in unk_bad]))
        cited = [f for vl in getattr(link, "variants", ()) for f in vl.facts]
        if summaries is None and not cited:
            v.add("V19", "callee_summaries", None, "no fact cited and no CalleeSummary rows "
                  "given (WP20 builds them; WP21's passes cite them)")
        else:
            from .formats.callee_summary_v1 import check_factref, validate_row

            bad_s = []
            rows_s = list(summaries or ())
            for row in rows_s:
                try:
                    validate_row(row)
                except Exception as ex:  # noqa: BLE001
                    bad_s.append(f"row {row.get('return_unit')}: {ex}"[:200])
            for f in cited:
                ref = f.to_json() if hasattr(f, "to_json") else dict(f)
                try:
                    check_factref(ref, rows_s)
                except Exception as ex:  # noqa: BLE001 - stale_fact
                    bad_s.append(f"fact {ref.get('fact_id')}: {ex}"[:200])
            v.add("V19", "callee_summaries", not bad_s,
                  f"{len(rows_s)} CalleeSummary rows validate; {len(cited)} cited facts are "
                  "current" if not bad_s else _few(bad_s), "stale_fact")
        v.data["graph_sha256"] = sha256_id(data)
    else:
        v.add("V19", "regions", None, "no re-import")

    # ---- V21 budgets and growth ------------------------------------------------------------------
    stub_bytes = sum(g.stub_bytes for g in grown.values())
    var_bytes = sum(x.end_offset - x.tc_offset for x in getattr(link, "variants", ()))
    per_c = Counter(x.src_uid for x in getattr(link, "variants", ()))
    bad21 = []
    max_c = getattr(plan, "max_variants_per_c", 2) if plan is not None else 2
    max_all = getattr(plan, "max_variants", 64) if plan is not None else 64
    if len(per_c) and max(per_c.values()) > max_c:
        bad21.append(f"context_budget: > {max_c} variants of one C")
    if sum(per_c.values()) > max_all:
        bad21.append(f"context_budget: > {max_all} variants")
    # §3.4 max_code_growth_bytes budgets specialization (variants + the stubs of exits
    # with a SPEC slot); GENERAL-only stubs have their own budget (≤2 slots/unit, 256
    # units, §3.6) and are reported, not budgeted (WP21)
    spec_stub = sum(len(st.code) for g in grown.values() for _sp, st in g.stubs
                    if any(si.kind == "SPEC" for si in st.slots))
    if hot_set_bytes is not None and spec_stub + var_bytes > growth_fraction * hot_set_bytes:
        bad21.append(f"growth_budget: {spec_stub + var_bytes} > {growth_fraction:.0%} of "
                     f"{hot_set_bytes}")
    v.add("V21", "budgets", not bad21,
          f"stub bytes {stub_bytes} (SPEC exits {spec_stub}), variant bytes {var_bytes}, "
          f"variants {sum(per_c.values())} "
          f"(budgets: {max_c}/C, {max_all}/artifact)"
          + (f", hot set {hot_set_bytes}" if hot_set_bytes is not None else
             " (hot-set bytes not given: growth reported only)") if not bad21 else _few(bad21),
          (bad21[0].split(":")[0] if bad21 else None), stub_bytes=stub_bytes,
          spec_stub_bytes=spec_stub, variant_bytes=var_bytes,
          pass_byte_delta=dict(passes_byte_delta or {}), hot_set_bytes=hot_set_bytes)

    # ---- V23 observation contract ------------------------------------------------------------------
    bad23 = []
    if res is not None:
        for u2 in res.snapshot.units():
            u = in_units.get(u2.uid)
            ra = snapshot.regions.get(u2.uid) if hasattr(snapshot, "regions") else None
            rb = res.snapshot.regions.get(u2.uid)
            if u is None or ra is None or rb is None:
                continue
            if u2.uid in grown:
                # the tail's lookup call moved to F: compare helper calls as a multiset
                def norm(evs: Sequence[Mapping[str, Any]]) -> tuple:
                    exact = [(e["kind"], e["insn"], e["host_offset"]) for e in evs
                             if e["kind"] != "helper_call"]
                    calls = Counter((e["kind"], e["insn"]) for e in evs
                                    if e["kind"] == "helper_call")
                    return exact, calls
                if norm(ra["maps"]["events"]) != norm(rb["maps"]["events"]):
                    bad23.append(f"{u2.uid}: observation events differ")
            elif [(e["kind"], e["insn"], e["host_offset"]) for e in ra["maps"]["events"]] != \
                    [(e["kind"], e["insn"], e["host_offset"]) for e in rb["maps"]["events"]]:
                bad23.append(f"{u2.uid}: observation events differ")
    b_out = None
    bp = art / "bundle.json"
    if bp.is_file():
        b_out = json.loads(bp.read_text())
    if b is not None and b_out is not None:
        for k in ("contract_id", "instrumentation_hash"):
            if b_out.get(k) != b.get(k):
                bad23.append(f"bundle {k} changed")
    v.add("V23", "observation_contract", not bad23 if res is not None else None,
          "contract_id/instrumentation_hash equal; no guest/plugin event added, dropped or "
          "moved in any rewritten unit" if not bad23 else _few(bad23))

    # ---- rows other WPs own --------------------------------------------------------------------------
    if callable(hostsym):
        # WP19 integration: a callable computes the inputs from this link
        # (scopt.validate_sym.link_check.hostsym_inputs), so package_b3 can
        # certify the artifact it linked itself
        hostsym = hostsym(snapshot, link)
    from .validate_sym.link_check import required_hostsym

    req = required_hostsym(link, plan)
    if hostsym is None:
        stub_rows(v, ["V12", "V14"], "not_run: no hostsym verdicts given (V12 hostsym, "
                  "V14 fact certification: WP19/WP20 kernels, run by the caller)")
        if req.entailment:
            stub_rows(v, ["V13"], "not_run: no hostsym guard entailment verdicts given "
                      f"({len(req.entailment)} grown R' with a guarded slot)")
        stub_rows(v, ["V18"], "not_run: no hostsym self-test document given")
    else:
        v12_rows(v, hostsym.get("equiv", ()), required=req.equiv)
        v13_entailment_rows(v, hostsym.get("entailment", ()), required=req.entailment)
        v14_rows(v, hostsym.get("facts", ()), required=req.links)
        v18_rows(v, hostsym.get("selftest"))
        coverage_rows(v, req)
    stub_rows(v, ["V22"], "not_run: WP23 not approved (Tier 1 ops-xcheck)")
    b3_edit = bool(grown or getattr(link, "variants", ()) or getattr(link, "rewritten", None)
                   or need_d2 or need_d3)
    if b3_edit and ri is not None:
        v20_rows(v, ri, units=list(snapshot.units()), need_d3=need_d3, need_d2=need_d2)
    elif b3_edit:
        v.add("V20", "admission", False, "B3/B4 edits without a RuntimeIdentity: no admission "
              "facts (fail closed)", "guard_layout_unbound")
    else:
        v.add("V20", "admission", None, "no B3/B4 edit")
    v.data["seconds"] = round(time.monotonic() - t0, 3)
    v.data["output_snapshot_id"] = out_sid
    return v, res


def multi_tb_rows(res: Any, link: Any, *, layout: Any = None,
                  runtime: Mapping[str, Any] | None = None,
                  passes: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """The manager-generated ``multi_tb`` CacheRegion rows (ADD_REGION, design §3.9)
    of a v6 artifact: one per (R, SPEC slot, C') link, from the re-import ``res``
    (its tb_singleton rows and variant rows) and the link result."""
    from .formats.region_v1 import build_multi_tb_region
    from .guard import GuardOffsets, guard_atoms, guard_key

    ext = getattr(res, "ext", None)
    if ext is None or not ext.links:
        return []
    regions = res.snapshot.regions
    by_tb = {u.tb_offset: u for u in res.snapshot.units()}
    vrow = {r["variant_index"]: r for r in res.variants}
    grown = dict(getattr(link, "grown", {}) or {})
    off = GuardOffsets.from_layout(layout)  # GuardError (guard_layout_unbound): no fallback
    exe = _ri_exe(runtime)
    rows = []
    for lk in ext.links:
        se = ext.slots[lk.slot_index]
        ve = ext.variants[lk.variant_index]
        r = by_tb.get(se.src_tb_offset)
        c = by_tb.get(ve.general_tb_offset)
        vr = vrow.get(lk.variant_index, {})
        if r is None or c is None:
            raise ValueError(f"multi_tb: link {lk.slot_index}->{lk.variant_index}: R or C "
                             "not in the re-import")
        g = grown.get(r.uid)
        kf = kf2 = None
        facts: tuple = ()
        if g is not None:
            for spec, st in g.stubs:
                if any(si.slot_n == se.slot_n for si in st.slots):
                    kf, kf2 = guard_key(spec.key_flags, spec.key_cs_base)
        vl = next((x for x in getattr(link, "variants", ()) if x.index == lk.variant_index),
                  None)
        if vl is not None:
            facts = tuple(f.to_json() if hasattr(f, "to_json") else dict(f) for f in vl.facts)
        if kf is None or kf2 is None:
            raise ValueError(f"multi_tb: R {r.uid} slot {se.slot_n} is not a grown SPEC slot "
                             "of this link result (guard key unknown)")
        atoms = guard_atoms(off, kf, kf2, [se.target_vaddr])
        body = vl.body if vl is not None else None
        rows.append(build_multi_tb_region(
            regions[r.uid], regions[c.uid], slot_n=se.slot_n,
            variant={"variant_id": vr.get("variant_key") or (vl.key if vl else None),
                     "tb_offset": ve.tb_offset, "tc_offset": ve.tc_offset,
                     "end_offset": ve.end_offset,
                     "code_sha256": sha256_id(body) if body is not None else
                     regions[c.uid]["native"]["code_sha256"],
                     "snapshot_id": res.snapshot.snapshot_id},
            r_orig_uid=r.uid, guard_atoms=atoms, facts=facts, passes=passes,
            envlayout_id=off.envlayout_id, runtime_exe_sha256=exe))
    return rows


def relocate_unit_body(u: Any, new_tb: int) -> bytes:
    """``[tc', end')`` of ``u`` moved to ``new_tb`` (NativeLinker v1 rules)."""
    from .link import relocate_unit

    return relocate_unit(u, new_tb).body


__all__ += ["STATIC_CODES", "static_not_applicable", "V_ROWS", "validate_b3", "v6_capable_exes", "stub_rows", "relocate_unit_body",
            "multi_tb_rows"]


# ---------------------------------------------------------------------------
# B3/B4 packaging (NativeLinker v2 -> files -> validate_b3 -> publish/reject)
# ---------------------------------------------------------------------------

def write_b3_files(stage: str | os.PathLike, snapshot: Any, link: Any) -> None:
    """Write ``cache.sc`` (v5 or v6), the derived captures and the identity
    files of a :class:`~scopt.link.LinkResultV2` into ``stage``."""
    from .formats.sc_v6 import write_sc6

    st = Path(stage)
    st.mkdir(parents=True, exist_ok=True)
    parts = [(u.record, u.body) for u in link.units]
    if getattr(link, "version", 5) == 6:
        write_sc6(st / "cache.sc", snapshot.header, parts, link.ext,
                  [x.body for x in link.variants], rewritten=bool(link.grown or link.rewritten))
    else:
        write_sc(st / "cache.sc", snapshot.header, parts)
    for name, data in link.derived_scms:
        (st / name).write_bytes(data)
    if snapshot.bundle is not None:
        A.write_json_file(st / "bundle.json", dict(snapshot.bundle))
    if snapshot.runtime is not None:
        A.write_json_file(st / "runtime_identity.json", dict(snapshot.runtime))


@dataclass
class B3PackageResult:
    status: str  # published | rejected
    candidate_id: str
    reason: str | None = None
    artifact_id: str | None = None
    artifact_dir: str | None = None
    rejection_path: str | None = None
    validation: Validation | None = None
    link: Any = None
    reimport: Any = None


#: V-rows whose body certifies B3/B4 edits (hostsym, facts, mutation suite). They
#: run when validate_b3 is given ``hostsym`` inputs (WP19 bodies v12_rows,
#: v14_rows, v18_rows); without them they are ``not_run`` stubs and package_b3
#: refuses to publish an edited artifact unless the caller passes
#: ``allow_uncertified=True`` (structural tests only); V22 (WP23, not approved)
#: never blocks. V13's path part (guard entailment) is certifying too whenever the
#: link has a grown R' with a guarded slot (:func:`certifying_rows`).
CERTIFYING_ROWS = ("V12", "V14", "V18")

#: The row check names of one hostsym label per certifying code.
_COVER_CHECKS = {"V12": ("equiv",), "V13": ("guard_entailment",),
                 "V14": ("certified", "proposer_equal", "provenance")}


def certifying_rows(link: Any = None, plan: Any = None) -> tuple[str, ...]:
    """:data:`CERTIFYING_ROWS` plus V13 when ``link`` has a guarded slot (``link``
    unknown: V13 too, fail closed)."""
    if link is None:
        return ("V12", "V13", "V14", "V18")
    from .validate_sym.link_check import required_hostsym

    return ("V12", "V13", "V14", "V18") if required_hostsym(link, plan).entailment \
        else CERTIFYING_ROWS


def hostsym_coverage(val: Validation, req: Any) -> dict[str, list[tuple[str, str]]]:
    """Per certifying code, the required labels (``req``, a
    :class:`~scopt.validate_sym.link_check.HostsymRequired`) without a certifying
    verdict in ``val``: ``(label, "missing" | "uncertified")``. A label is certified
    when every check of :data:`_COVER_CHECKS` for it has a ``pass`` row and none a
    ``fail`` row; rows for labels the link does not require never count."""
    res: dict[tuple[str, str], set[str]] = {}
    for r in val.rows:
        res.setdefault((r["code"], r["check"]), set()).add(r["result"])

    def state(code: str, label: str) -> str | None:
        seen = [res.get((code, f"{c}:{label}")) for c in _COVER_CHECKS[code]]
        if all(x is None for x in seen):
            return "missing"
        if any(x is None or "fail" in x or "pass" not in x for x in seen):
            return "uncertified"
        return None

    out: dict[str, list[tuple[str, str]]] = {}
    for code, labels in (("V12", req.equiv), ("V13", req.entailment), ("V14", req.facts)):
        bad = [(lab, st) for lab in labels if (st := state(code, lab)) is not None]
        out[code] = bad
    return out


def coverage_rows(v: Validation, req: Any) -> bool:
    """V12/V13/V14 ``coverage`` rows: every label the link requires has a certifying
    verdict (B3/B4 design §9.1: V12 every changed unit, V13 every guarded R', V14
    every SPEC link). A missing label fails with ``hostsym_coverage_missing:<label>``,
    a present but not certified one with ``hostsym_coverage_uncertified:<label>``."""
    ok = True
    gaps = hostsym_coverage(v, req)
    for code, labels in (("V12", req.equiv), ("V13", req.entailment), ("V14", req.facts)):
        for lab, st in gaps[code]:
            ok &= v.add(code, f"coverage:{lab}", False,
                        f"required hostsym verdict for {lab} is {st}",
                        f"hostsym_coverage_{st}:{lab}")
        if not gaps[code]:
            what = {"V12": "changed units", "V13": "grown R' with a guarded slot",
                    "V14": "SPEC-link facts"}[code]
            v.add(code, "coverage", True if labels else None,
                  f"{len(labels)} required {what} certified" if labels
                  else f"no {what}", required=list(labels))
    return bool(ok)


def uncertified_rows(val: Validation, link: Any = None, plan: Any = None) -> list[str]:
    """Certifying rows that did not certify the artifact (fail closed).

    Coverage is computed from ``link`` (and ``plan``) — never from what hostsym
    happened to return: V12 needs a passing ``equiv`` verdict for every changed unit
    (grown R', rewritten U', every variant C' per entering link), V13 a passing
    ``guard_entailment`` for every grown R' with a guarded slot, V14 a certified,
    proposer-equal, provenanced fact for every fact of every SPEC link, V18 a pass.
    A row is also uncertified when it is missing or has a ``not_run`` stub. With
    ``link`` unknown, V12/V13/V14 are uncertified (coverage cannot be decided)."""
    codes = certifying_rows(link, plan)
    gaps: dict[str, list[tuple[str, str]]] = {}
    req = None
    if link is not None:
        from .validate_sym.link_check import required_hostsym

        req = required_hostsym(link, plan)
        gaps = hostsym_coverage(val, req)
    out = []
    for c in codes:
        rows = [r for r in val.rows if r["code"] == c]
        if c == "V13":
            rows = [r for r in rows if r["check"] == "not_run" or
                    r["check"].split(":")[0] in ("guard_entailment", "coverage")]
        if not rows or any(r["check"] == "not_run" for r in rows):
            out.append(c)
        elif c == "V18":
            if not any(r["result"] == "pass" for r in rows) or \
                    any(r["result"] == "fail" for r in rows):
                out.append(c)
        elif req is None or gaps.get(c):
            out.append(c)
        elif c == "V12" and not req.equiv:
            out.append(c)  # an edited plan always changes a unit
    return out


def certification_problems(sidecar: Mapping[str, Any], validation: Mapping[str, Any]
                           ) -> list[str]:
    """Publish-time re-check of a B3/B4 artifact's certification from its sidecar
    (``x_b3``) and ``validation.json`` rows (``scopt publish``): every grown and
    rewritten unit and every variant needs its own certifying verdict, and the
    ``coverage`` rows (written by :func:`validate_b3` from the link) must pass and
    name them. Empty for an artifact without B3/B4 edits."""
    xb = sidecar.get("x_b3")
    if not isinstance(xb, Mapping):
        return []
    grown = list(xb.get("grown") or ())
    rewritten = [u for u in (xb.get("rewritten") or ()) if u not in grown]
    variants = list(xb.get("variants") or ())
    if not (grown or rewritten or variants):
        return []
    rows = [r for r in validation.get("rows", ()) if isinstance(r, Mapping)]
    passed = {(r.get("code"), r.get("check")) for r in rows if r.get("result") == "pass"}
    failed = {(r.get("code"), r.get("check")) for r in rows if r.get("result") == "fail"}
    probs: list[str] = []
    if failed:
        probs.append(f"{len(failed)} failing rows")
    if any(r.get("check") == "not_run" and r.get("code") in ("V12", "V13", "V14", "V18")
           for r in rows):
        probs.append("certifying row not_run")
    cov = {r.get("code"): r for r in rows if r.get("check") == "coverage"}

    def need(code: str, labels: Sequence[str]) -> None:
        row = cov.get(code)
        if row is None or row.get("result") != "pass":
            probs.append(f"{code}: no passing coverage row")
            return
        req = set(row.get("required") or ())
        for lab in labels:
            if lab not in req:
                probs.append(f"{code}: coverage does not name {lab}")
            elif (code, f"{_COVER_CHECKS[code][0]}:{lab}") not in passed:
                probs.append(f"{code}: no passing verdict for {lab}")

    from .validate_sym.link_check import r_label, u_label

    need("V12", [r_label(u) for u in grown] + [u_label(u) for u in rewritten])
    n_c = sum(1 for x in (cov.get("V12") or {}).get("required") or () if x.startswith("C':"))
    if n_c < len(variants):
        probs.append(f"V12: coverage names {n_c} C' verdicts for {len(variants)} variants")
    if grown:
        need("V13", grown)
    if variants and (cov.get("V14") or {}).get("result") not in ("pass", "skip"):
        probs.append("V14: no coverage row")
    if not any(r.get("code") == "V18" and r.get("result") == "pass" for r in rows):
        probs.append("V18: no passing self-test row")
    return probs


def package_b3(snapshot: Any, plan: Any, store: str | os.PathLike, *, candidate_id: str,
               pipeline: Sequence[Mapping[str, Any]] | None = None,
               profile_ids: Sequence[Mapping[str, Any]] = (), layout: Any = None,
               v6_exes: Iterable[str] = (), runtime: Mapping[str, Any] | None = None,
               scparse: Any = None, sc6parse: Any = None, allow_uncertified: bool = False,
               ledger_rows: Iterable[LedgerRow] = (),
               sidecar_extra: Mapping[str, Any] | None = None,
               **vkw: Any) -> B3PackageResult:
    """Link ``plan`` (NativeLinker v2), write, validate (:func:`validate_b3`) and
    publish into ``store`` or write ``store/rejections/<candidate>.json``.

    ``pipeline`` are the sidecar pipeline rows (default: one row describing the
    plan). The sidecar's ``loader_requirement`` is ``qemu-sc-v6`` for a v6
    output; ``x_ext_artifact_id8`` carries the ext table id; ``move_map``
    lists the grown (moved) units; ``regions.jsonl`` has the tb_singleton rows
    of the re-import plus the manager-generated multi_tb rows.

    Fail closed: an edited plan is published only when every
    :func:`certifying_rows` row certified it with full coverage of the link
    (:func:`uncertified_rows`: a verdict for every changed unit, guarded R' and
    SPEC-link fact, computed from the link), or with ``allow_uncertified=True``
    (structural tests; the sidecar then lists them in ``x_uncertified``).
    A table the loader would refuse is rejected with its ``ext_problem`` code.

    ``ledger_rows`` (WP21) start the artifact ledger (the PassManager's pass rows);
    ``sidecar_extra`` adds ``x_`` keys to the sidecar (the manager's B3 report)."""
    from .formats.envlayout_v1 import find_envlayout
    from .formats.sc_v6 import LOADER_V6, ScExtRefused
    from .link import NativeLinkerV2

    if vkw.get("skip_reimport"):
        # V8/V19 and the regions need the re-import; packaging never skips it
        raise ValueError("package_b3 always re-imports the artifact; skip_reimport is a "
                         "validate_b3-only test option")
    led = Ledger(ledger_rows)
    ri = dict(runtime) if runtime is not None else (
        dict(snapshot.runtime) if snapshot.runtime is not None else None)
    if layout is None:
        layout = find_envlayout((ri or {}).get("qemu_sha256"))
    from .guard import GuardError, GuardOffsets

    try:
        off = GuardOffsets.from_layout(layout)
    except GuardError as e:
        if plan.grow:
            return _reject_b3(store, candidate_id, f"{e.reason}", led, None, None)
        off = GuardOffsets()
    link = NativeLinkerV2(snapshot, plan, offsets=off).link()
    if not link.ok:
        f = link.failures[0]
        led.emit("artifact", candidate_id, "compiled", "fail", f.reason, f.uid)
        return _reject_b3(store, candidate_id, f"failed:{f.reason}", led, link, None)
    stage = A.staging_dir(store)
    try:
        try:
            write_b3_files(stage, snapshot, link)
        except ScExtRefused as e:
            return _reject_b3(store, candidate_id, f"failed:{e.code}", led, link, None, str(e))
        except ScFormatError as e:
            return _reject_b3(store, candidate_id, "write_sc", led, link, None, str(e))
        if ri is not None and runtime is not None:
            A.write_json_file(stage / "runtime_identity.json", ri)
        from .formats.sc_v6 import read_sc6, sc6_snapshot_id

        out_sid = sc6_snapshot_id(read_sc6(stage / "cache.sc"))
        if pipeline is None:
            pdoc = {"grow": sorted(plan.grow), "variants": len(plan.variants),
                    "rewrites": sorted(plan.rewrites)}
            pipeline = [{"pass": "b3-plan", "version": 1,
                         "params_sha256": sha256_id(canonical_json(pdoc)),
                         "outcome": "candidate" if not plan.empty else "no_change",
                         "reason": "", "affected": sorted(set(plan.grow) | set(plan.rewrites)),
                         "edit_digest": None, "duration_s": 0.0}]
        sidecar: dict[str, Any] = {
            "schema": "fastdyn.artifact_sidecar", "version": 1,
            "input_snapshot_id": snapshot.snapshot_id, "output_snapshot_id": out_sid,
            "bundle_id": None if snapshot.bundle is None else snapshot.bundle.get("bundle_id"),
            "runtime_identity_id": None if ri is None else ri.get("runtime_identity_id"),
            "profile_ids": list(profile_ids), "pipeline": list(pipeline),
            "move_map": link.move_map(), "pinned": [],
            "validation_sha256": "sha256:" + "0" * 64, "tool_versions": A.tool_versions(),
            "loader_requirement": LOADER_V6 if link.version == 6 else A.LOADER_V5,
            "x_candidate_id": candidate_id,
            "x_b3": {"grown": sorted(link.grown), "rewritten": sorted(link.rewritten),
                     "variants": [x.key for x in link.variants]},
        }
        if link.ext is not None:
            sidecar["x_ext_artifact_id8"] = bytes(link.ext.artifact_id8).hex()
        for k, x in (sidecar_extra or {}).items():
            if not k.startswith("x_"):
                raise ValueError(f"sidecar_extra key {k!r} must start with 'x_'")
            sidecar[k] = x
        val, res = validate_b3(snapshot, link, stage, plan=plan, sidecar=sidecar, layout=layout,
                               v6_exes=v6_exes, runtime=ri, scparse=scparse,
                               sc6parse=sc6parse, **vkw)
        if not val.accepted:
            return _reject_b3(store, candidate_id, val.first_reason() or "validation", led,
                              link, val)
        if res is None:  # cannot happen when V8 passed; fail closed anyway
            return _reject_b3(store, candidate_id, "reimport_failed", led, link, val)
        unc = uncertified_rows(val, link, plan) if not plan.empty else []
        if unc and not allow_uncertified:
            return _reject_b3(store, candidate_id, "certification_not_run", led, link, val,
                              f"edited artifact without {', '.join(unc)}")
        if unc:
            sidecar["x_uncertified"] = unc
            led.emit("artifact", candidate_id, "accepted", "skip",
                     "allow_uncertified: " + ",".join(unc))
        try:
            regions = list(res.regions) + multi_tb_rows(res, link, layout=layout, runtime=ri)
        except (ValueError, GuardError) as e:
            return _reject_b3(store, candidate_id, "failed:multi_tb_region", led, link, val,
                              str(e))
        vb = A.write_json_file(stage / A.VALIDATION, val.to_json(
            candidate_id=candidate_id, input_snapshot_id=snapshot.snapshot_id,
            output_snapshot_id=out_sid))
        sidecar["validation_sha256"] = sha256_id(vb)
        A.write_json_file(stage / A.SIDECAR, sidecar)
        (stage / A.REGIONS).write_bytes(b"".join(canonical_json(r) + b"\n" for r in regions))
        A.write_json_file(stage / A.PROFILES, {"profiles": list(profile_ids)})
        led.emit("artifact", candidate_id, "accepted", "pass", "V1-V23 (static)",
                 sidecar["validation_sha256"])
        led.write(stage / A.LEDGER)
        aid, final, _st = A.publish(stage, store)
        return B3PackageResult("published", candidate_id, None, aid, str(final), None, val,
                               link, res)
    finally:
        if stage.exists():
            A.remove_tree(stage)


def _reject_b3(store: Any, cid: str, reason: str, led: Ledger, link: Any, val: Any,
               detail: str = "") -> B3PackageResult:
    led.emit("artifact", cid, "accepted", "fail", reason)
    doc = {"candidate_id": cid, "reason": reason, "detail": detail,
           "link": None if link is None else link.to_json(),
           "validation": None if val is None else val.to_json(),
           "ledger": [r.to_json() for r in led.rows]}
    p = A.write_rejection(store, cid, doc)
    return B3PackageResult("rejected", cid, reason, rejection_path=str(p), validation=val,
                           link=link)


__all__ += ["write_b3_files", "package_b3", "B3PackageResult", "CERTIFYING_ROWS",
            "uncertified_rows", "certifying_rows", "hostsym_coverage", "coverage_rows",
            "certification_problems"]


# ---------------------------------------------------------------------------
# V12, V13 (entailment), V14, V18: hostsym rows (B3/B4 design §9.1-9.3, WP19)
# ---------------------------------------------------------------------------

def _verdict_row(v: Validation, code: str, check: str, verdict: Any) -> bool:
    if verdict.status == "equivalent":
        return v.add(code, check, True, verdict.detail, paths=[verdict.o_paths, verdict.n_paths])
    return v.add(code, check, False, verdict.detail, verdict.reason or "validation_mismatch",
                 verdict=verdict.status, paths=[verdict.o_paths, verdict.n_paths])


def v12_rows(v: Validation, checks: Iterable[tuple[str, Any]], *,
             required: Sequence[str] | None = None) -> bool:
    """V12: one row per changed unit, from :func:`scopt.validate_sym.check_equiv` (or
    :func:`~scopt.validate_sym.check_guarded_chain`) verdicts ``(label, Verdict)``.

    ``equivalent`` passes; ``mismatch`` fails with its failed code
    (``validation_mismatch``, ``edit_inside_ldst_block``, ``recovery_rows_changed``,
    ``host_abs_aliases_env``, ``guard_contract_violation``); ``inconclusive``
    fails with ``validation_inconclusive`` (the candidate is skipped_unknown).

    ``required`` (:func:`scopt.validate_sym.link_check.required_hostsym`) are the
    labels the link needs; the "no changed unit" skip is only written when it is
    empty (coverage itself: :func:`coverage_rows`).
    """
    ok = True
    n = 0
    for label, verdict in checks:
        n += 1
        ok &= _verdict_row(v, "V12", f"equiv:{label}", verdict)
    if n == 0 and not required:
        v.add("V12", "equiv", None, "no changed unit")
    return bool(ok)


def v13_entailment_rows(v: Validation, checks: Iterable[tuple[str, Any]], *,
                        required: Sequence[str] | None = None) -> bool:
    """V13, path-condition part (WP19): for every grown R', every path reaching a
    guarded slot entails each declared guard atom and the stub has no store,
    call or LDST (:func:`~scopt.validate_sym.check_guarded_chain`). The
    structural V13 checks (template bytes, reset paths, offsets) are WP18's."""
    ok = True
    n = 0
    for label, verdict in checks:
        n += 1
        ok &= _verdict_row(v, "V13", f"guard_entailment:{label}", verdict)
    if n == 0 and not required:
        v.add("V13", "guard_entailment", None, "no guarded slot")
    return bool(ok)


def v14_rows(v: Validation, facts: Iterable[Mapping[str, Any]], *,
             required: Sequence[tuple[str, Sequence[str]]] | None = None) -> bool:
    """V14: fact certification per SPEC link.

    Each item: ``label``, ``cert`` (:class:`~scopt.validate_sym.FactCert` from
    hostsym on R'), ``proposed`` (the ``exit-facts@1`` value) and ``provenance``
    (the ``patch-join@1`` row, or None). Rows: ``certified`` (else
    ``fact_not_constant`` / ``validation_inconclusive``), ``proposer_equal``
    (else ``fact_derivation_mismatch``, a failed tool bug), ``provenance``
    (else ``provenance_unexplained``).

    ``required`` are the link's SPEC links ``(prefix, fact labels)``: a link whose
    variant declares no fact gets a ``no_fact:<prefix>`` skip row (its ``P`` is the
    guard atoms, certified by V13 entailment and V12 on the C' context); the "no
    SPEC link" skip is only written when there is no SPEC link.
    """
    ok = True
    n = 0
    for f in facts:
        n += 1
        lab = f["label"]
        cert = f["cert"]
        reason = {"not_constant": "fact_not_constant", "unreached": "fact_not_constant",
                  "inconclusive": "validation_inconclusive"}.get(cert.status, "fact_not_constant")
        ok &= v.add("V14", f"certified:{lab}", cert.certified,
                    f"{cert.status} value {cert.value} on {cert.paths} paths: {cert.detail}",
                    reason, cert_hash=cert.cert_hash)
        if cert.certified:
            ok &= v.add("V14", f"proposer_equal:{lab}", cert.value == f.get("proposed"),
                        f"certifier {cert.value} proposer {f.get('proposed')}",
                        "fact_derivation_mismatch")
        prov = f.get("provenance")
        ok &= v.add("V14", f"provenance:{lab}", prov is not None,
                    str(prov) if prov is not None else "no patch-join row",
                    "provenance_unexplained")
    for prefix, labels in (required or ()):
        if not labels:
            v.add("V14", f"no_fact:{prefix}", None, "the variant declares no fact: P = the "
                  "guard atoms (V13 guard_entailment on R', V12 on the C' context)")
    if n == 0 and not required:
        v.add("V14", "facts", None, "no SPEC link")
    return bool(ok)


def v18_rows(v: Validation, selftest: Mapping[str, Any] | None) -> bool:
    """V18: the hostsym self-test document (:func:`scopt.validate_sym.selftest.run_selftest`)
    passed for this kernel (``kernel_id`` = hash of the validate_sym sources) and
    capstone version: native ``ctypes``+``mmap(PROT_EXEC)`` check of every concrete
    form and the synthetic mutation suite 100 % rejected."""
    import capstone

    from .validate_sym.selftest import kernel_id

    if selftest is None:
        return v.add("V18", "selftest", False, "no hostsym self-test document",
                     "hostsym_selftest_missing")
    cur = kernel_id()
    if selftest.get("kernel_id") != cur or \
            selftest.get("capstone_version") != capstone.__version__:
        return v.add("V18", "selftest", False,
                     f"self-test of kernel {selftest.get('kernel_id')} / capstone "
                     f"{selftest.get('capstone_version')}; current {cur} / {capstone.__version__}",
                     "hostsym_selftest_stale")
    nat = selftest.get("native", {})
    mut = selftest.get("mutants", {})
    ok = v.add("V18", "native_semantics", nat.get("status") == "pass",
               f"{nat.get('forms')} forms, {nat.get('trials')} trials, "
               f"{nat.get('n_failures')} failures ({nat.get('status')})", "hostsym_selftest_failed")
    ok &= v.add("V18", "mutation_suite", bool(mut.get("all_rejected")) and
                bool(mut.get("controls_ok")),
                f"{mut.get('rejected')}/{mut.get('mutants')} rejected, controls_ok "
                f"{mut.get('controls_ok')}", "hostsym_selftest_failed")
    return bool(ok)


__all__ += ["v12_rows", "v13_entailment_rows", "v14_rows", "v18_rows"]

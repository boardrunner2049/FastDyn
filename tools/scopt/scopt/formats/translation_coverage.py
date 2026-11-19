"""Versioned translation coverage derived from QEMU fixture pretranslation.

The v1 producer covers the immutable Thumb fixture region in exactly two TCG
contexts. Every covered entry retains its general entry semantics; outside
addresses, additional contexts, and unresolved transfers remain residual.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Literal

from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile

from ..canon import canonical_json, content_id, is_sha256_id, sha256_id
from ..ids import TBKey

CoverageMode = Literal["auto", "complete", "partial"]
_SCHEMA = "scopt.translation_coverage"
_PRODUCER = "scopt-tcg-coverage-v1"
_FILENAME = "translation_coverage.json"
_COUNT_MASK = 0x1FF


class CoverageError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class CoverageSelection:
    mode: Literal["complete", "partial"]
    reason: str
    coverage_id: str | None = None
    covered_units: tuple[str, ...] = ()
    roots: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Probe:
    variant: int
    pc: int
    key: TBKey
    guest_size: int
    icount: int


def _sha_file(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def _checked_metadata(snapshot: Any) -> tuple[dict, Path]:
    base = Path(snapshot.path) if getattr(snapshot, "path", None) else None
    bundle = getattr(snapshot, "bundle", None)
    runtime = getattr(snapshot, "runtime", None)
    if base is None or bundle is None or runtime is None:
        raise CoverageError("unbound_coverage", "snapshot, bundle, and runtime identity are required")
    try:
        metadata = json.loads((base / "snapshot.json").read_text())
        cap = metadata["capture"]
        if not cap["complete"] or cap["problems"] or not cap["files"]:
            raise CoverageError("incomplete_capture", "producer requires clean level-1 captured TBs")
        files = {}
        for row in cap["files"]:
            name = row["name"]
            if Path(name).name != name or name in files:
                raise CoverageError("capture_mismatch", "unexpected capture file name")
            if _sha_file(base / name) != row["sha256"]:
                raise CoverageError("capture_mismatch", f"{name} changed since import")
            files[name] = row["sha256"]
        firmware = Path(metadata["options"]["firmware"])
        if _sha_file(firmware) != bundle["image_hash"]:
            raise CoverageError("image_mismatch", "firmware bytes differ from the checked bundle")
        manifest = json.loads((base / "manifest.json").read_text())
        nf = manifest["inputs"]["native_files"]
        if _sha_file(Path(nf["qemu"]["path"])) != runtime["qemu_sha256"]:
            raise CoverageError("runtime_mismatch", "QEMU bytes differ from the runtime identity")
        if _sha_file(Path(nf["plugin"]["path"])) != runtime["plugin_sha256"]:
            raise CoverageError("runtime_mismatch", "plugin bytes differ from the runtime identity")
        settings = manifest["inputs"]["settings"]
        cpus = settings["cpus"]
        if (settings["platform"] != "generic-cortexm" or len(cpus) != 1
                or cpus[0]["cpu"] != "cortex-m3" or cpus[0]["arch"] != "arm"):
            raise CoverageError("unsupported_domain", "producer requires the fixed Cortex-M3 fixture")
        identities = {
            "snapshot_id": snapshot.snapshot_id,
            "bundle_id": bundle["bundle_id"],
            "contract_id": bundle["contract_id"],
            "image_hash": bundle["image_hash"],
            "patch_plan_hash": bundle["patch_plan_hash"],
            "memory_map_hash": bundle["memory_map_hash"],
            "runtime_identity_id": runtime["runtime_identity_id"],
            "qemu_sha256": runtime["qemu_sha256"],
            "plugin_sha256": runtime["plugin_sha256"],
            "capture_files": files,
        }
        for name, value in identities.items():
            if name != "capture_files" and not is_sha256_id(value):
                raise CoverageError("unbound_coverage", f"missing checked identity {name}")
        return identities, firmware
    except (OSError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, CoverageError):
            raise
        raise CoverageError("unbound_coverage", str(error)) from error


def _trace_rows(trace: str) -> tuple[int, int, int, tuple[_Probe, ...]]:
    if not isinstance(trace, str) or len(trace) > 65536:
        raise CoverageError("bad_producer_trace", "expected a bounded QEMU probe trace")
    try:
        head, *rows, tail = trace.splitlines()
        producer, *bounds = head.split()
        start, end, trigger = map(lambda v: int(v, 16), bounds)
        if (producer != _PRODUCER or tail != "end" or not 0 <= start < end < 1 << 32
                or (start | end | trigger) & 1 or end - start > 256):
            raise ValueError("invalid QEMU probe version or bounds")
        probes = []
        for row in rows:
            variant, pc, p0, p1, flags, cs_base, cflags, size, icount = row.split()
            pc, p0, p1, flags, cs_base, cflags = map(
                lambda v: int(v, 16), (pc, p0, p1, flags, cs_base, cflags))
            probes.append(_Probe(int(variant), pc, TBKey.from_raw(p0, p1, flags, cs_base, cflags),
                                 int(size), int(icount)))
    except ValueError as error:
        raise CoverageError("bad_producer_trace", str(error)) from error
    positions = {(probe.variant, probe.pc) for probe in probes}
    expected = {(variant, pc) for variant in (0, 1) for pc in range(start, end, 2)}
    if positions != expected or len(probes) != len(expected):
        raise CoverageError("incomplete_context_inventory", "missing or duplicate Thumb entry/context")
    contexts = {(p.key.flags, p.key.cs_base, p.key.cflags & ~_COUNT_MASK) for p in probes}
    if len(contexts) != 1 or any(p.key.cflags & _COUNT_MASK != p.variant for p in probes):
        raise CoverageError("context_mismatch", "probe used unexpected CPU or TCG settings")
    return start, end, trigger, tuple(probes)


def _check_fixture(firmware: Path, start: int, end: int, trigger: int) -> tuple[int, tuple[int, ...]]:
    """Check the exact fixture entry domain, including its explicit UDF terminal."""
    try:
        elf = ELFFile(io.BytesIO(firmware.read_bytes()))
        symtab = elf.get_section_by_name(".symtab")
        text = elf.get_section_by_name(".text")
        vectors = elf.get_section_by_name(".vectors").data()
        entries = tuple(int.from_bytes(vectors[i:i + 4], "little")
                        for i in range(4, len(vectors), 4))
        if (len(vectors) < 8 or len(vectors) % 4 or entries[0] != (trigger | 1)
                or elf["e_entry"] != entries[0] or any(v and not v & 1 for v in entries)):
            raise CoverageError("external_entry_mismatch", "fixture vector table or reset entry differs")
        exception_roots = tuple(sorted({v & ~1 for v in entries[1:] if v and v != entries[0]}))
        syms = {symbol.name: int(symbol["st_value"]) for symbol in symtab.iter_symbols()}
        off = start - int(text["sh_addr"])
        blob = text.data()[off:off + end - start]
        # MOVW r3,#0x123, an indirect branch, then UDF #0.
        if (elf["e_machine"] != "EM_ARM" or text["sh_flags"] & 0x5 != 0x4
                or syms["start"] & ~1 != trigger or syms["coverage_start"] != start
                or syms["coverage_end"] != end or syms["coverage_invalid"] != start + 6
                or off < 0 or len(blob) != 8 or blob[:4] != b"\x40\xf2\x23\x13"
                or blob[4:] != b"\x20\x47\x00\xde"):
            raise CoverageError("unsupported_domain", "ELF does not contain the fixture's Thumb domain")
    except (AttributeError, ELFError, KeyError, OSError, ValueError) as error:
        if isinstance(error, CoverageError):
            raise
        raise CoverageError("unsupported_domain", str(error)) from error
    return start + 6, exception_roots


def _producer_inventory(snapshot: Any, probes: tuple[_Probe, ...], trigger: int,
                        start: int, end: int, exception_roots: tuple[int, ...] = (),
                        ) -> tuple[list[dict], list[str]]:
    units = {u.uid: u for u in snapshot.units()}
    by_key = {}
    for unit in units.values():
        for pc in unit.vaddrs:
            by_key.setdefault((pc, unit.key), []).append(unit)
    rows = []
    for probe in sorted(probes, key=lambda p: (p.variant, p.pc)):
        exact = by_key.get((probe.pc, probe.key), ())
        if len(exact) != 1:
            raise CoverageError("capture_mismatch", f"missing or ambiguous captured key at {probe.pc:#x}")
        unit = exact[0]
        if (unit.guest_size != probe.guest_size or unit.icount != probe.icount
                or unit.meta_status != "ok" or unit.origin != "elf_verified"
                or probe.pc + unit.guest_size > end):
            raise CoverageError("capture_mismatch", f"invalid captured body at {probe.pc:#x}")
        rows.append({
            "context": "normal" if probe.variant == 0 else "count-1",
            "vaddr": probe.pc,
            "key": {
                "page_addr0": probe.key.page_addr0, "page_addr1": probe.key.page_addr1,
                "flags": probe.key.flags, "cs_base": probe.key.cs_base, "cflags": probe.key.cflags,
            },
            "unit": unit.uid, "guest_size": unit.guest_size, "icount": unit.icount,
        })
    covered = {r["unit"] for r in rows}
    for unit in units.values():
        if unit.uid not in covered and any(start <= pc < end for pc in unit.vaddrs):
            raise CoverageError("unexpected_covered_key", f"unaccounted context in covered range: {unit.uid}")
    root_vaddrs = {trigger, *exception_roots}
    captured_roots = {pc for unit in units.values() for pc in unit.vaddrs if pc in root_vaddrs}
    if captured_roots != root_vaddrs:
        raise CoverageError("missing_external_root", "fixture reset or exception entry was not captured")
    roots = sorted(unit.uid for unit in units.values() if root_vaddrs.intersection(unit.vaddrs))
    return rows, roots


def produce_coverage(snapshot: Any, trace: str) -> dict[str, Any]:
    """Bind a real QEMU probe trace to the existing imported identities and keys."""
    identities, firmware = _checked_metadata(snapshot)
    start, end, trigger, probes = _trace_rows(trace)
    invalid, exception_roots = _check_fixture(firmware, start, end, trigger)
    rows, roots = _producer_inventory(snapshot, probes, trigger, start, end, exception_roots)
    context = probes[0].key
    doc = {
        "schema": _SCHEMA, "version": 1,
        "producer": {"kind": _PRODUCER, "trace": trace, "trace_sha256": sha256_id(trace.encode())},
        "identities": identities,
        "domain": {"start": start, "end": end, "entry_alignment": 2, "isa": "thumb",
                   "code_policy": "immutable-fixture", "code_coverage": "every-halfword",
                   "terminal": {"vaddr": invalid, "kind": "undefined-instruction"}},
        "context": {"flags": context.flags, "cs_base": context.cs_base,
                    "base_cflags": context.cflags & ~_COUNT_MASK,
                    "count_variants": [0, 1], "coverage": "complete-exact-contexts"},
        "entry": {"external_roots": roots, "policy": "all-entries-general",
                  "residual": ["out-of-domain-address", "other-cpu-context",
                               "indirect-transfer", "unmapped-fetch", "mutable-code"]},
        "inventory": rows,
    }
    if exception_roots:
        doc["entry"]["exception_vector_vaddrs"] = list(exception_roots)
    doc["coverage_id"] = content_id(doc, "coverage_id")
    return doc


def check_coverage(snapshot: Any, doc: Mapping[str, Any]) -> CoverageSelection:
    if not isinstance(doc, Mapping) or doc.get("schema") != _SCHEMA or doc.get("version") != 1:
        raise CoverageError("coverage_schema", "unknown coverage schema or version")
    try:
        trace = doc["producer"]["trace"]
        if doc["coverage_id"] != content_id(doc, "coverage_id"):
            raise CoverageError("coverage_digest", "producer record digest differs")
        check = produce_coverage(snapshot, trace)
        if check["identities"] != doc["identities"]:
            raise CoverageError("stale_coverage", "snapshot, image, modifier, map, capture, or runtime differs")
        if canonical_json(check) != canonical_json(doc):
            raise CoverageError("coverage_mismatch", "record differs from the checked producer inventory")
    except (KeyError, TypeError) as error:
        raise CoverageError("coverage_schema", str(error)) from error
    return CoverageSelection("complete", "checked_fixture_domain", check["coverage_id"],
                             tuple(sorted({row["unit"] for row in check["inventory"]})),
                             tuple(check["entry"]["external_roots"]))


def select_coverage(snapshot: Any, mode: CoverageMode, evidence: Mapping[str, Any] |
                    str | os.PathLike | None = None) -> CoverageSelection:
    """Resolve the requested analysis mode, failing closed for explicit complete."""
    if mode == "partial":
        return CoverageSelection("partial", "explicit_partial")
    if mode not in ("auto", "complete"):
        raise CoverageError("coverage_mode", f"unknown mode {mode!r}")
    snapshot = getattr(snapshot, "base", snapshot)
    try:
        if evidence is None and getattr(snapshot, "path", None):
            default = Path(snapshot.path) / _FILENAME
            if default.is_file():
                evidence = default
        if evidence is None:
            raise CoverageError("missing_coverage", "no producer record supplied")
        if not isinstance(evidence, Mapping):
            try:
                evidence = json.loads(Path(evidence).read_text())
            except (OSError, TypeError, ValueError) as error:
                raise CoverageError("coverage_read", str(error)) from error
        return check_coverage(snapshot, evidence)
    except CoverageError as error:
        if mode == "complete":
            raise
        return CoverageSelection("partial", error.code)

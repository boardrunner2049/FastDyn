"""Negative-control packager: mutants as loadable artifacts for O4 (B3/B4 design §9.3, §9.4; WP26).

**Test-only.** Every mutant of the §9.3 suite must be rejected statically
(V12-V17, WP18/WP19) *and*, outside the static-only set, be detected
dynamically (O4: it changes O1, O2a or O2b on its named fixture). The second
half needs the mutant as a deployable file. :func:`package_negative` builds
one:

1. link the (mutant) plan with :class:`scopt.link.NativeLinkerV2` (a plan the
   linker refuses cannot become an artifact: ``refused``, ``failed:<reason>``);
2. optionally apply ``mutate(link) -> link`` (byte-level mutants: drop a guard
   atom, flip a variant byte, change an icount immediate, ...) and
   :func:`reseal` the extension table (``code_fnv64``, ``guard_fnv64``,
   ``artifact_id8``), so that the v6 loader loads the mutant instead of dying
   on a hash (a loader-refusal test is WP22's, not an O4 control). A byte
   mutant drops the derived captures and skips the re-import (V8/V19 rows
   absent): it is never re-imported or re-optimized;
3. write the files and run :func:`scopt.validate.validate_b3` as for a real
   candidate, then **bypass** the failing rows of ``bypass`` (default
   V12-V17: hostsym equivalence, guarded-chain contract, fact certification,
   placement, variant relocation, link structure). Any other failing row
   refuses the mutant (``unbypassed:<code>``): it would not be the mutant the
   suite names, or it would not load;
4. write the directory **outside any store** with the sidecar tag
   ``x_negative_control`` and the marker file ``NEGATIVE_CONTROL.json``
   (:mod:`scopt.run.policy`), ``validation.json`` (the full rows, the bypassed
   failures included), ``ledger.jsonl`` (stage ``accepted``, verdict ``negative_control``) and a
   ``MANIFEST.sha256``.

Such a directory is never publishable (``scopt.formats.artifact.publish``
refuses ``negative_control``), deploy refuses it unless the caller passes
``allow_negative_control=True`` (``negative_control_artifact``), and campaign
preflight refuses it as an arm source. The packager never calls
``publish`` and refuses an output directory inside a store's ``artifacts/``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..canon import canonical_json, fnv1a64
from ..ids import sha256_id
from .policy import NEGATIVE_MARKER, NEGATIVE_TAG

__all__ = ["BYPASS_ROWS", "NegativeResult", "package_negative", "reseal", "NEGATIVE_SCHEMA"]

NEGATIVE_SCHEMA = "scopt.negative_control/1"
#: the static rows a negative control may fail (plan WP26: "bypasses V12-V17")
BYPASS_ROWS = ("V12", "V13", "V14", "V15", "V16", "V17")


@dataclass
class NegativeResult:
    status: str  # packaged | refused
    label: str
    reason: str | None = None
    path: str | None = None
    artifact_id: str | None = None
    bypassed: list[dict[str, Any]] = field(default_factory=list)  # failing rows bypassed
    blocking: list[dict[str, Any]] = field(default_factory=list)  # failing rows not bypassed
    validation: Any = None
    link: Any = None

    @property
    def packaged(self) -> bool:
        return self.status == "packaged"

    def to_json(self) -> dict[str, Any]:
        return {"schema": NEGATIVE_SCHEMA, "status": self.status, "label": self.label,
                "reason": self.reason, "path": self.path, "artifact_id": self.artifact_id,
                "bypassed": self.bypassed, "blocking": self.blocking}


def reseal(link: Any) -> Any:
    """Recompute the hashes of a (mutated) v6 link result: every variant's
    ``code_fnv64``, every slot's ``guard_fnv64`` (over the source unit's body)
    and ``artifact_id8``. A v5 result is returned unchanged."""
    from ..link import _artifact_id8

    ext = getattr(link, "ext", None)
    if ext is None:
        return link
    by_tb = {u.new_tb_offset: u for u in link.units}
    vs = []
    for e in ext.variants:
        body = next((v.body for v in link.variants if v.tb_offset == e.tb_offset), None)
        vs.append(e if body is None else replace(e, code_fnv64=fnv1a64(body)))
    ss = []
    for s in ext.slots:
        u = by_tb.get(s.src_tb_offset)
        ss.append(s if u is None else replace(
            s, guard_fnv64=fnv1a64(u.body[s.guard_off:s.guard_off + s.guard_len])))
    ext = replace(ext, variants=tuple(vs), slots=tuple(ss), checksum=None)
    ext = replace(ext, artifact_id8=_artifact_id8(ext, link.units, link.variants))
    return replace(link, ext=ext)


def _inside_store(p: Path) -> bool:
    return any(part == "artifacts" for part in p.resolve().parts)


def package_negative(snapshot: Any, plan: Any, out_dir: str | os.PathLike, *, label: str,
                     mutant: str | None = None, detector: str | None = None,
                     mutate: Callable[[Any], Any] | None = None,
                     bypass: Iterable[str] = BYPASS_ROWS, layout: Any = None,
                     runtime: Mapping[str, Any] | None = None, v6_exes: Iterable[str] = (),
                     scparse: Any = None, sc6parse: Any = None,
                     **vkw: Any) -> NegativeResult:
    """Package the mutant ``plan`` (+ ``mutate``) as ``out_dir/<label>/`` (see the
    module docstring). ``mutant`` names the §9.3 entry, ``detector`` the
    fixture/oracle that must detect it (both recorded in the tag)."""
    from ..formats import artifact as A
    from ..formats.envlayout_v1 import find_envlayout
    from ..formats.sc_records import ScFormatError
    from ..guard import GuardError, GuardOffsets
    from ..ledger import Ledger
    from ..link import NativeLinkerV2
    from ..validate import validate_b3, write_b3_files

    if not label or "/" in label or label.startswith("."):
        raise ValueError(f"bad negative-control label {label!r}")
    out = Path(out_dir) / label
    if _inside_store(out):
        raise ValueError(f"{out}: negative controls are never written into a store's "
                         "artifacts/ directory")
    if out.exists():
        raise FileExistsError(f"{out} exists; negative controls need a fresh directory")
    bypass = tuple(bypass)
    ri = dict(runtime) if runtime is not None else (
        dict(snapshot.runtime) if getattr(snapshot, "runtime", None) is not None else None)
    if layout is None:
        layout = find_envlayout((ri or {}).get("qemu_sha256"))
    try:
        off = GuardOffsets.from_layout(layout)
    except GuardError as e:
        if plan.grow:
            return NegativeResult("refused", label, f"{e.reason}")
        off = GuardOffsets()
    link = NativeLinkerV2(snapshot, plan, offsets=off).link()
    if not link.ok:
        f = link.failures[0]
        return NegativeResult("refused", label, f"failed:{f.reason}", link=link)
    if mutate is not None:
        # the derived captures describe the unmutated bytes: a byte mutant is never
        # re-imported (V8/V19 need the re-import and are skipped for it)
        link = replace(reseal(mutate(link)), derived_scms=(), capture_sources=())
        vkw.setdefault("skip_reimport", True)
    try:
        write_b3_files(out, snapshot, link)
    except ScFormatError as e:
        A.remove_tree(out)
        return NegativeResult("refused", label, f"write:{getattr(e, 'code', 'write_sc')}",
                              link=link)
    if ri is not None and runtime is not None:
        A.write_json_file(out / "runtime_identity.json", ri)
    from ..formats.supercache import canonical_snapshot_id

    out_sid = canonical_snapshot_id(out / "cache.sc")
    tag = {"schema": NEGATIVE_SCHEMA, "label": label, "mutant": mutant, "detector": detector,
           "bypass": list(bypass), "byte_mutation": mutate is not None,
           "reimport": "skipped" if vkw.get("skip_reimport") else "run",
           "note": "test-only negative control (design §9.3/§9.4 O4): V12-V17 bypassed; "
                   "never publishable, never a timing arm"}
    sidecar: dict[str, Any] = {
        "schema": "fastdyn.artifact_sidecar", "version": 1,
        "input_snapshot_id": snapshot.snapshot_id, "output_snapshot_id": out_sid,
        "bundle_id": None if snapshot.bundle is None else snapshot.bundle.get("bundle_id"),
        "runtime_identity_id": None if ri is None else ri.get("runtime_identity_id"),
        "profile_ids": [], "pipeline": [{
            "pass": "negative-control", "version": 1,
            "params_sha256": sha256_id(canonical_json({"label": label, "mutant": mutant})),
            "outcome": "candidate", "reason": mutant or "",
            "affected": sorted(set(plan.grow) | set(plan.rewrites)), "edit_digest": None,
            "duration_s": 0.0}],
        "move_map": link.move_map(), "pinned": [],
        "validation_sha256": "sha256:" + "0" * 64, "tool_versions": A.tool_versions(),
        "loader_requirement": A.LOADER_V7,
        NEGATIVE_TAG: tag,
    }
    if link.ext is not None:
        sidecar["x_ext_artifact_id8"] = bytes(link.ext.artifact_id8).hex()
    val, _res = validate_b3(snapshot, link, out, plan=plan, sidecar=sidecar, layout=layout,
                            v6_exes=v6_exes, runtime=ri, scparse=scparse, sc6parse=sc6parse,
                            **vkw)
    fails = val.fails()
    bypassed = [r for r in fails if r["code"] in bypass]
    blocking = [r for r in fails if r["code"] not in bypass]
    if blocking:
        A.remove_tree(out)
        return NegativeResult("refused", label, f"unbypassed:{blocking[0]['code']}",
                              bypassed=bypassed, blocking=blocking, validation=val, link=link)
    tag["bypassed_failures"] = sorted({r["code"] for r in bypassed})
    tag["statically_rejected"] = bool(bypassed)
    vb = A.write_json_file(out / A.VALIDATION, val.to_json(
        candidate_id=f"negative:{label}", input_snapshot_id=snapshot.snapshot_id,
        output_snapshot_id=out_sid))
    sidecar["validation_sha256"] = sha256_id(vb)
    A.write_json_file(out / A.SIDECAR, sidecar)
    A.write_json_file(out / NEGATIVE_MARKER, tag)
    led = Ledger()
    # stage "accepted" with verdict "negative_control": never "pass"
    led.emit("artifact", f"negative:{label}", "accepted", "negative_control",
             ("bypassed " + ",".join(tag["bypassed_failures"])) if bypassed
             else "no static row failed (mutant not rejected statically)",
             sidecar["validation_sha256"])
    led.write(out / A.LEDGER)
    mb = A.manifest_bytes(out)
    (out / A.MANIFEST).write_bytes(mb)
    from ..ids import artifact_id

    return NegativeResult("packaged", label, None, str(out), artifact_id(mb), bypassed, [],
                          val, link)

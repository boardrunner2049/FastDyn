"""Track ordinary captures separately from reimported optimizer generations."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile

from ..formats.artifact import ArtifactError, MANIFEST, SIDECAR, read_artifact, verify_manifest
from ..formats.sc_v5 import sha256_sparse
from ..formats.sc_v7 import canonical_snapshot_id


@dataclass(frozen=True)
class WarmBaseline:
    path: Path
    snapshot_id: str
    cache_sha256: str


def _read(path):
    return json.loads(path.read_text())


def _hex(sid):
    if not isinstance(sid, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", sid):
        raise ValueError(f"invalid snapshot ID: {sid}")
    return sid[7:]


def _prepared(prep, *, strict=False):
    if not prep or not (Path(prep) / "prep.json").is_file():
        return None
    doc = _read(Path(prep) / "prep.json")
    if (doc.get("schema") == "scopt.prepare/1" and doc.get("verdict") == "PASS"
            and doc.get("runs") and doc["runs"][0].get("kind") == "cold"):
        digest = doc.get("sc_sha256")
        if isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            return digest
    if strict:
        raise ValueError(f"invalid ordinary capture receipt: {Path(prep) / 'prep.json'}")
    return None


def _stage(path):
    artifact = read_artifact(path)
    if verify_manifest(path) or artifact.output_snapshot_id != canonical_snapshot_id(path / "cache.sc"):
        raise ValueError(f"optimizer stage failed manifest or output-snapshot verification: {path}")
    _hex(artifact.input_snapshot_id)
    return artifact.output_snapshot_id, artifact.input_snapshot_id


def _origin_dir(store, sid):
    return store / "warm-origins" / _hex(sid)


def record_import(snapshot, sc, prep):
    """Write local evidence outside the immutable snapshot and its file manifest."""
    snapshot, sc = Path(snapshot), Path(sc)
    sid = _read(snapshot / "snapshot.json")["snapshot_id"]
    root = _origin_dir(snapshot.parent.parent, sid)
    if (sc.parent / SIDECAR).is_file() and sc.name == "cache.sc":
        if not (sc.parent / MANIFEST).is_file():
            return
        try:
            child, parent = _stage(sc.parent)
        except (ArtifactError, KeyError, ValueError):
            # A raw/unpublished input can still be imported for experimentation.
            return
        if child != sid or parent == child:
            return
        name, fields = _hex(parent) + ".json", {"parent": parent}
    else:
        digest = _prepared(prep)
        if digest is None or digest != "sha256:" + sha256_sparse(snapshot / "cache.sc"):
            return
        name, fields = "capture.json", {"sc_sha256": digest}

    root.mkdir(parents=True, exist_ok=True)
    # Identical imports may finish concurrently. A reader sees one whole binding.
    with tempfile.NamedTemporaryFile("w", dir=root, delete=False) as f:
        tmp = Path(f.name)
        try:
            json.dump({"snapshot_id": sid, **fields}, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, root / name)
    finally:
        tmp.unlink(missing_ok=True)


def ordinary_warm(input_path, *, baseline=None, prep=None, stages=()):
    """Return a reachable, capture-bound store snapshot or None when unproved.

    Each traversed edge connects a packaged artifact's input and output
    SnapshotIds. A caller can supply missing cross-store or historical edges.
    """
    input_path = Path(input_path).resolve()
    if not (input_path / "snapshot.json").is_file():
        return None
    sid = _read(input_path / "snapshot.json")["snapshot_id"]
    baseline = Path(baseline).resolve() if baseline is not None else None
    roots = {input_path.parent.parent}
    if baseline is not None:
        roots.add(baseline.parent.parent)
    expected_capture = _prepared(prep, strict=bool(baseline or stages))
    supplied = {}
    for stage in stages:
        child, parent = _stage(Path(stage))
        supplied.setdefault(child, set()).add(parent)

    pending, seen, found = [sid], set(), {}
    while pending:
        child = pending.pop()
        if child in seen:
            continue
        seen.add(child)
        child_hex = _hex(child)
        candidates = [baseline] if baseline else [root / "snapshots" / child_hex for root in roots]
        for candidate in candidates:
            meta = candidate / "snapshot.json"
            if not meta.is_file() or _read(meta)["snapshot_id"] != child:
                continue
            origin = _origin_dir(candidate.parent.parent, child) / "capture.json"
            digests = {expected_capture}
            if expected_capture is None and origin.is_file():
                doc = _read(origin)
                if doc["snapshot_id"] != child:
                    raise ValueError(f"capture binding changed: {origin}")
                digests.add(doc["sc_sha256"])
            digests.discard(None)
            if digests:
                digest = "sha256:" + sha256_sparse(candidate / "cache.sc")
                if digest in digests:
                    if canonical_snapshot_id(candidate / "cache.sc") != child:
                        raise ValueError("ordinary warm cache snapshot ID disagrees with its recorded capture")
                    found[(child, digest)] = WarmBaseline(candidate, child, digest)
        parents = set(supplied.get(child, ()))
        for root in roots:
            for path in _origin_dir(root, child).glob("*.json"):
                if path.name != "capture.json":
                    record = _read(path)
                    if record["snapshot_id"] != child or path.stem != _hex(record["parent"]):
                        raise ValueError(f"stage binding changed: {path}")
                    parents.add(record["parent"])
        pending.extend(sorted(parents))
    # Independent original captures can converge on the same emitted snapshot.
    # Only the caller can distinguish the intended control in that situation.
    return next(iter(found.values())) if len(found) == 1 else None

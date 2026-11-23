"""Output artifact directory: sidecar, MANIFEST, atomic publish, rejections (design §4.6). WP10.

Layout of a published artifact (``store/artifacts/<hex of ArtifactId>/``)::

    cache.sc                 v5, written sparsely (holes stay holes)
    cache.sc.scopt.json      provenance sidecar (schemas/artifact_sidecar.v1.json);
                             not named *.fastdyn.json, so QEMU and FastDyn ignore it
    cache.scm                derived capture (cache.<k>.scm when the import used
                             several captures), re-import closure
    regions.jsonl            one CacheRegion v1 per unit, final offsets, slot order
    bundle.json              ExecutionBundle of the input snapshot
    runtime_identity.json    RuntimeIdentity of the input snapshot
    profiles.json            {"profiles": [{"profile_id", "verdict"}]}
    validation.json          one row per V1-V9 check
    ledger.jsonl             outcome ledger rows (§5.9)
    MANIFEST.sha256          "<sha256 hex>  <relative path>" per file above, sorted by path

* **ArtifactId** = ``sha256:`` + sha256 of the ``MANIFEST.sha256`` bytes (§4.1).
  The manifest lines are in ``sha256sum`` format, so ``sha256sum -c
  MANIFEST.sha256`` works in the directory. ``cache.sc`` is hashed over its
  full logical size (holes as zeros), like ``sha256sum``.
* **Publishing** (:func:`publish`): the directory is built in
  ``store/artifacts/.tmp-<pid>-<rand>/``; the manifest is written and
  verified, every file and the directory are fsynced, the directory is renamed
  to its final name and made read-only (files 0444, directory 0555). If the
  final directory already exists its manifest must be byte-identical
  (``artifact_conflict`` otherwise) and the staging copy is dropped.
* **Rejections** (:func:`write_rejection`): ``store/rejections/<hex of
  candidate_id>.json``, written atomically; nothing is published.
"""

from __future__ import annotations

import errno
import json
import os
import secrets
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..canon import canonical_json
from ..ids import artifact_id as _artifact_id
from . import schemas
from .sc_v5 import sha256_sparse

__all__ = [
    "SIDECAR", "MANIFEST", "VALIDATION", "LEDGER", "REGIONS", "PROFILES", "LOADER_V5",
    "LOADER_V7",
    "ArtifactError", "manifest_bytes", "parse_manifest", "verify_manifest", "staging_dir",
    "publish", "write_rejection", "read_artifact", "ArtifactInfo", "write_json_file",
    "tool_versions", "sidecar_errors", "id_hex", "artifact_dir", "rejection_path",
    "remove_tree", "capture_index", "derived_captures",
]

SIDECAR = "cache.sc.scopt.json"
MANIFEST = "MANIFEST.sha256"
VALIDATION = "validation.json"
LEDGER = "ledger.jsonl"
REGIONS = "regions.jsonl"
PROFILES = "profiles.json"
LOADER_V5 = "qemu-sc-v5-unmodified"
LOADER_V7 = "qemu-sc-v7"
REJECTION_SCHEMA = "scopt.rejection"


class ArtifactError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def id_hex(i: str) -> str:
    return i.split(":", 1)[1] if ":" in i else i


def artifact_dir(store: str | os.PathLike, artifact_id: str) -> Path:
    return Path(store) / "artifacts" / id_hex(artifact_id)


def rejection_path(store: str | os.PathLike, candidate_id: str) -> Path:
    return Path(store) / "rejections" / f"{id_hex(candidate_id)}.json"


def write_json_file(path: str | os.PathLike, obj: Any, *, mode: int | None = None) -> bytes:
    """Canonical JSON plus a newline, fsynced. Returns the bytes."""
    data = canonical_json(obj) + b"\n"
    _write_bytes(Path(path), data, mode=mode)
    return data


def _write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if mode is not None:
        os.chmod(path, mode)


def tool_versions() -> dict[str, str]:
    from .. import __version__

    try:
        import capstone
        cs = ".".join(str(x) for x in capstone.version_bind()[:2]) + f" ({capstone.__version__})"
    except Exception:  # pragma: no cover
        cs = "unavailable"
    try:
        import elftools
        pe = getattr(elftools, "__version__", "?")
    except Exception:  # pragma: no cover
        pe = "unavailable"
    return {"scopt": __version__, "capstone": cs, "pyelftools": pe,
            "python": ".".join(str(x) for x in sys.version_info[:3])}


def sidecar_errors(doc: Mapping[str, Any]) -> list[str]:
    return schemas.errors(dict(doc), "artifact_sidecar.v1")


# ---------------------------------------------------------------------------
# MANIFEST
# ---------------------------------------------------------------------------

def _files(root: Path) -> list[str]:
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() or p.is_symlink():
            rel = p.relative_to(root).as_posix()
            if rel != MANIFEST:
                out.append(rel)
    return sorted(out)


def manifest_bytes(root: str | os.PathLike, *, known: Mapping[str, str] | None = None) -> bytes:
    """``MANIFEST.sha256`` content for every file under ``root`` (except itself).

    ``known`` maps relative paths to an already computed hex sha256 (to avoid
    re-hashing a 2 GiB sparse ``cache.sc``).
    """
    root = Path(root)
    lines = []
    for rel in _files(root):
        p = root / rel
        if p.is_symlink():
            raise ArtifactError("artifact_symlink", rel)
        h = (known or {}).get(rel) or sha256_sparse(p)
        lines.append(f"{h}  {rel}\n")
    return "".join(lines).encode()


def parse_manifest(data: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for n, line in enumerate(data.decode("utf-8").splitlines(), 1):
        h, sep, rel = line.partition("  ")
        if not sep or len(h) != 64 or any(c not in "0123456789abcdef" for c in h) or not rel:
            raise ArtifactError("manifest_format", f"line {n}: {line!r}")
        if rel in out:
            raise ArtifactError("manifest_format", f"line {n}: duplicate {rel}")
        out[rel] = h
    if list(out) != sorted(out):
        raise ArtifactError("manifest_format", "lines are not sorted by path")
    return out


def verify_manifest(root: str | os.PathLike, *, known: Mapping[str, str] | None = None
                    ) -> list[str]:
    """Problems with ``root/MANIFEST.sha256`` (empty when every hash verifies and
    the manifest lists exactly the files present)."""
    root = Path(root)
    mp = root / MANIFEST
    if not mp.is_file():
        return ["manifest_missing"]
    try:
        want = parse_manifest(mp.read_bytes())
    except ArtifactError as e:
        return [f"{e.code}: {e.detail}"]
    have = _files(root)
    probs = [f"unlisted_file: {r}" for r in have if r not in want]
    probs += [f"missing_file: {r}" for r in want if r not in have]
    for rel, h in want.items():
        p = root / rel
        if not p.is_file():
            continue
        got = (known or {}).get(rel) or sha256_sparse(p)
        if got != h:
            probs.append(f"hash_mismatch: {rel}")
    return probs


# ---------------------------------------------------------------------------
# staging, publish, rejections
# ---------------------------------------------------------------------------

def staging_dir(store: str | os.PathLike) -> Path:
    d = Path(store) / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    t = d / f".tmp-{os.getpid()}-{secrets.token_hex(4)}"
    t.mkdir()
    return t


def _fsync_path(p: Path) -> None:
    fd = os.open(p, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def remove_tree(p: str | os.PathLike) -> None:
    def onerror(func, path, exc):  # read-only files / directories
        os.chmod(os.path.dirname(path), 0o755)
        if os.path.isdir(path):
            os.chmod(path, 0o755)
        else:
            os.chmod(path, 0o644)
        func(path)
    if os.path.exists(p):
        shutil.rmtree(p, onerror=onerror)


def _existing(final: Path, mb: bytes) -> str:
    """``"exists"`` when ``final`` holds the same manifest; else ``artifact_conflict``."""
    try:
        have = (final / MANIFEST).read_bytes()
    except OSError as e:
        raise ArtifactError("artifact_conflict",
                            f"{final} exists but its manifest is unreadable: {e}") from e
    if have != mb:
        raise ArtifactError("artifact_conflict", f"{final} exists with another manifest")
    return "exists"


def _validation_refusal(staging: Path) -> tuple[str, str] | None:
    """WP26 (review): a staging directory whose ``validation.json`` rejects the
    candidate (verdict not ``accepted`` or a failing row), or whose sidecar's
    ``validation_sha256`` does not name that ``validation.json``, is never
    published, whatever its tags say (B1-design §4.6). A directory without
    ``validation.json`` is left to the caller (the packagers always write it)."""
    from ..canon import sha256_id

    vp = staging / VALIDATION
    if not vp.is_file():
        return None
    vb = vp.read_bytes()
    try:
        val = json.loads(vb)
    except ValueError as e:
        return ("validation_failed", f"unreadable validation.json: {e}")
    if not isinstance(val, dict):
        return ("validation_failed", "validation.json is not an object")
    bad = [r for r in val.get("rows", []) if isinstance(r, dict) and r.get("result") == "fail"]
    if bad or val.get("verdict") != "accepted":
        return ("validation_failed", f"validation verdict {val.get('verdict')!r}, "
                f"{len(bad)} failing rows")
    sp = staging / SIDECAR
    if sp.is_file():
        try:
            side = json.loads(sp.read_text())
        except ValueError as e:
            return ("sidecar_schema", f"unreadable sidecar: {e}")
        want = side.get("validation_sha256") if isinstance(side, dict) else None
        if want != sha256_id(vb):
            return ("validation_mismatch", f"sidecar validation_sha256 {want} != sha256 of "
                    f"validation.json {sha256_id(vb)}")
    return None


def publish(staging: str | os.PathLike, store: str | os.PathLike, *,
            known: Mapping[str, str] | None = None) -> tuple[str, Path, str]:
    """Write MANIFEST, verify it, fsync and atomically rename ``staging``.

    Returns ``(artifact_id, final_dir, status)`` with status ``created`` or
    ``exists``. A concurrent publish of the same id that wins the race (the
    rename fails with ``ENOTEMPTY``/``EEXIST``) gives ``exists`` too. Raises
    :class:`ArtifactError` (``manifest_verify``, ``artifact_conflict``,
    ``publish_rename``, and WP26's ``negative_control`` / ``test_only_artifact``
    for directories tagged by :mod:`scopt.run.policy` and ``validation_failed`` /
    ``validation_mismatch`` for a ``validation.json`` that rejects the candidate
    or that the sidecar does not name) and never a bare
    ``OSError`` from the rename; the staging directory is removed in every case.
    """
    staging = Path(staging)
    try:
        # WP26: negative controls and test-policy artifacts are never publishable
        from ..run.policy import publish_refusal

        why = publish_refusal(staging)
        if why is not None:
            raise ArtifactError(*why)
        why = _validation_refusal(staging)
        if why is not None:
            raise ArtifactError(*why)
        mb = manifest_bytes(staging, known=known)
        _write_bytes(staging / MANIFEST, mb)
        probs = verify_manifest(staging, known=known)
        if probs:
            raise ArtifactError("manifest_verify", "; ".join(probs[:5]))
        aid = _artifact_id(mb)
        for rel in _files(staging) + [MANIFEST]:
            _fsync_path(staging / rel)
        _fsync_path(staging)
        final = artifact_dir(store, aid)
        if final.exists():
            return aid, final, _existing(final, mb)
        try:
            os.rename(staging, final)
        except OSError as e:
            # Another process published the same id between the check and the
            # rename (rename(2) of a directory onto a non-empty one: ENOTEMPTY or
            # EEXIST). Same manifest -> "exists"; anything else is an ArtifactError.
            if e.errno in (errno.ENOTEMPTY, errno.EEXIST) and final.exists():
                return aid, final, _existing(final, mb)
            raise ArtifactError("publish_rename", f"{staging} -> {final}: {e}") from e
        _fsync_path(final.parent)
        for rel in _files(final) + [MANIFEST]:
            os.chmod(final / rel, 0o444)
        for d in sorted({p.parent for p in final.rglob("*")}, key=lambda x: -len(x.parts)):
            os.chmod(d, 0o555)
        return aid, final, "created"
    finally:
        if staging.exists():
            remove_tree(staging)


def write_rejection(store: str | os.PathLike, candidate_id: str, doc: Mapping[str, Any]) -> Path:
    """Write ``store/rejections/<hex>.json`` atomically (replaces an older one)."""
    d = Path(store) / "rejections"
    d.mkdir(parents=True, exist_ok=True)
    final = rejection_path(store, candidate_id)
    tmp = d / f".tmp-{os.getpid()}-{secrets.token_hex(4)}.json"
    body = {"schema": REJECTION_SCHEMA, "version": 1, **doc}
    write_json_file(tmp, body)
    os.replace(tmp, final)
    _fsync_path(d)
    return final


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

@dataclass
class ArtifactInfo:
    path: Path
    artifact_id: str | None
    sidecar: dict[str, Any]
    bundle: dict[str, Any] | None
    runtime_identity: dict[str, Any] | None
    validation: dict[str, Any] | None
    captures: list[str]

    @property
    def input_snapshot_id(self) -> str:
        return self.sidecar["input_snapshot_id"]

    @property
    def output_snapshot_id(self) -> str:
        return self.sidecar["output_snapshot_id"]


def capture_index(name: str) -> int | None:
    """Derive order of a derived capture name: ``cache.scm`` is 0, ``cache.<k>.scm``
    is ``k``; None for any other name."""
    if name == "cache.scm":
        return 0
    if name.startswith("cache.") and name.endswith(".scm"):
        k = name[len("cache."):-len(".scm")]
        if k.isdigit() and not (len(k) > 1 and k.startswith("0")) and int(k) > 0:
            return int(k)
    return None


def derived_captures(root: str | os.PathLike) -> list[str]:
    """The ``*.scm`` file names of an artifact (or staging) directory in **derive
    order** (``cache.scm``, ``cache.1.scm``, ``cache.2.scm``, ...; the order
    :func:`scopt.link.derive_scm` wrote them, which is the order of the input's
    captures). A plain name sort would put ``cache.1.scm`` before ``cache.scm``
    and ``cache.10.scm`` before ``cache.2.scm``. Names outside the scheme sort
    last, by name."""
    names = [p.name for p in Path(root).glob("*.scm") if p.is_file()]
    return sorted(names, key=lambda n: (capture_index(n) is None, capture_index(n) or 0, n))


def _maybe_json(p: Path) -> dict[str, Any] | None:
    return json.loads(p.read_text()) if p.is_file() else None


def read_artifact(path: str | os.PathLike) -> ArtifactInfo:
    root = Path(path)
    side = _maybe_json(root / SIDECAR)
    if side is None:
        raise ArtifactError("not_an_artifact", f"{root}: no {SIDECAR}")
    mp = root / MANIFEST
    aid = _artifact_id(mp.read_bytes()) if mp.is_file() else None
    caps = derived_captures(root)
    return ArtifactInfo(root, aid, side, _maybe_json(root / "bundle.json"),
                        _maybe_json(root / "runtime_identity.json"),
                        _maybe_json(root / VALIDATION), caps)

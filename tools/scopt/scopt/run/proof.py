"""The post-run "artifact used" proof (§4.8).

At deploy time :func:`deployed_state` records what was put into the run
directory: the 80-byte header, the record count ``n``, a digest of the record
array ``[0, n)``, the file's inode, and the FastDyn manifest (bytes digest and
fingerprint). After the run :func:`prove` checks:

1. ``header``: the header equals the deployed one except ``record_count``
   (which must be ``>= n``) and ``checksum`` (which must be valid). In
   particular ``magic``, ``version``, ``code_address``, ``runtime_signature``
   and ``prologue_end`` are unchanged; otherwise QEMU truncated and rebuilt the
   file (supercache.c:586-610).
2. ``records``: record bytes ``[0, n)`` are byte-identical.
3. ``manifest``: the manifest fingerprint (and its bytes) are unchanged, i.e.
   the frontend did not decide the cache was stale.

and reports ``appended = record_count - n`` (fallback translations). The
count is reported only when the verdict is ``PASS``: after a failed check the
file may be a rebuilt cache whose ``record_count`` says nothing about the
deployed artifact, so ``appended`` is ``null`` (unknown is never zero) and the
raw post-run ``record_count`` is kept as ``record_count_after``. Two
supplementary checks make the verdict robust when a deleted cache is rebuilt
deterministically with identical bytes: the ``.sc`` must still exist and keep
its inode (FastDyn deletes a stale cache with ``unlink``; QEMU's own rebuild
keeps the inode but fails check 1).

Any failed check gives the verdict ``INVALID_artifact_not_used``; such a run
is never a timing sample. All checks pass: ``PASS``.

``.sc`` v6 (B3/B4 design §5.1, §9.4; WP26). For a deployed file whose header
version is 6 with the EXT_TABLE feature bit set the state also records the sha256 of the extension bytes
``[0x100, 0x10000)`` and the ext table's ``artifact_id8``, and :func:`prove`
adds two checks:

4. ``v6_header``: the header is still version 6 with the deployed feature
   flags. An old (v5-only) loader fails ``valid_header`` on a v6 file,
   truncates it and writes a fresh v5 header (F-B3-18); this check names
   that case (``header`` fails too).
5. ``ext``: the extension bytes are unchanged after the run (the v6 loader
   never writes them; an append keeps them, WP22 note 7).

A direct QEMU run without the FastDyn frontend (:func:`deployed_state` with
``frontend="none"``, the mps2 alias path of :mod:`scopt.run.deploy`) has no
manifest; its ``manifest`` check passes with a detail saying so, and the
other checks decide.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ..formats.layouts import SC_CODE_OFFSET
from ..formats.sc_v5 import HEADER_SIZE, RECORD_SIZE, ScHeader

#: v6 extension bytes covered by the post-run ``ext`` check (design §5.3, G2)
EXT_BEGIN = 0x100
EXT_END = SC_CODE_OFFSET
#: offset of ``artifact_id8`` in the ext header (file offset 0x130)
EXT_ID8_OFFSET = EXT_BEGIN + 48
SC6_VERSION = 6
SC7_VERSION = 7

__all__ = ["deployed_state", "prove", "prove_run", "deploy_refusal", "VERDICT_PASS",
           "VERDICT_INVALID", "ext_state", "EXT_BEGIN", "EXT_END"]

VERDICT_PASS = "PASS"
VERDICT_INVALID = "INVALID_artifact_not_used"

_HEADER_FIELDS = ("magic", "version", "header_size", "code_size", "code_address",
                  "prologue_end", "runtime_signature", "host_page_size", "target_page_bits",
                  "tb_size", "reserved")


def _read_exact(fd: int, n: int, off: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        b = os.pread(fd, n - len(out), off + len(out))
        if not b:
            break
        out += b
    return bytes(out)


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _manifest_state(manifest: str | os.PathLike | None) -> dict[str, Any] | None:
    if manifest is None or not os.path.isfile(manifest):
        return None
    raw = Path(manifest).read_bytes()
    try:
        fp = json.loads(raw).get("fingerprint")
    except (ValueError, AttributeError):
        fp = None
    return {"path": str(manifest), "sha256": _sha(raw), "fingerprint": fp}


def ext_state(fd: int) -> dict[str, Any]:
    """sha256 of the v6 extension bytes ``[0x100, 0x10000)`` and ``artifact_id8``."""
    ext = _read_exact(fd, EXT_END - EXT_BEGIN, EXT_BEGIN)
    ext = ext + bytes(EXT_END - EXT_BEGIN - len(ext))
    a = EXT_ID8_OFFSET - EXT_BEGIN
    return {"ext_sha256": _sha(ext), "ext_artifact_id8": ext[a:a + 8].hex()}


def deployed_state(sc_path: str | os.PathLike,
                   manifest: str | os.PathLike | None, *,
                   frontend: str = "fastdyn") -> dict[str, Any]:
    """What the proof compares against; call right after the ``.sc`` is in place.

    ``frontend="none"``: a direct QEMU run (no FastDyn, no manifest)."""
    if frontend not in ("fastdyn", "none"):
        raise ValueError(f"unknown frontend {frontend!r}")
    fd = os.open(os.fspath(sc_path), os.O_RDONLY | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        hb = _read_exact(fd, HEADER_SIZE, 0)
        if len(hb) < HEADER_SIZE:
            raise ValueError(f"{sc_path}: truncated header")
        h = ScHeader.from_bytes(hb)
        roff = SC_CODE_OFFSET + h.code_size
        avail = max(0, (st.st_size - roff) // RECORD_SIZE) if st.st_size > roff else 0
        n = min(h.record_count, avail)
        recs = _read_exact(fd, n * RECORD_SIZE, roff) if n else b""
        # a real v6 file: version 6 with the EXT_TABLE feature bit (a version-6 header
        # without it is a v5 file with its version field changed, B1-9 / WP18 rework 2)
        ext = ext_state(fd) if h.version == SC6_VERSION and h.reserved & 1 else None
    finally:
        os.close(fd)
    out = {
        "sc_path": str(Path(sc_path).resolve()),
        "header_hex": hb.hex(),
        "header": h.to_json(),
        "n": n,
        "records_sha256": _sha(recs),
        "file_size": st.st_size,
        "dev": st.st_dev,
        "inode": st.st_ino,
        "mtime_ns": st.st_mtime_ns,
        "manifest": _manifest_state(manifest),
        "frontend": frontend,
        "file_version": h.version,
    }
    if ext is not None:
        out.update(ext)
    if h.version == SC7_VERSION:
        from ..formats.sc_v7 import read_sc7, sc7_snapshot_id

        directory = read_sc7(sc_path)
        out.update(firmware_artifact_id8=directory.artifact_id8.hex(),
                   firmware_snapshot_id=sc7_snapshot_id(sc_path))
    return out


def prove(state: dict[str, Any]) -> dict[str, Any]:
    """Run the §4.8 checks against ``state`` (from :func:`deployed_state`)."""
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    sc = state["sc_path"]
    n = int(state["n"])
    dep = ScHeader.from_bytes(bytes.fromhex(state["header_hex"]))
    record_count_after = None
    if not os.path.isfile(sc):
        add("sc_exists", False, f"{sc} is missing after the run")
    else:
        add("sc_exists", True)
        fd = os.open(sc, os.O_RDONLY | os.O_CLOEXEC)
        try:
            st = os.fstat(fd)
            hb = _read_exact(fd, HEADER_SIZE, 0)
            if len(hb) < HEADER_SIZE:
                add("header", False, f"truncated header ({len(hb)} bytes)")
                post = None
            else:
                post = ScHeader.from_bytes(hb)
            recs = b""
            if post is not None:
                diffs = [f"{f}: {getattr(dep, f)!r} -> {getattr(post, f)!r}"
                         for f in _HEADER_FIELDS if getattr(dep, f) != getattr(post, f)]
                if post.record_count < n:
                    diffs.append(f"record_count {post.record_count} < deployed {n}")
                if not post.checksum_ok:
                    diffs.append("header checksum invalid after the run")
                add("header", not diffs, "; ".join(diffs))
                record_count_after = post.record_count
                recs = _read_exact(fd, n * RECORD_SIZE, SC_CODE_OFFSET + dep.code_size) if n else b""
            same = _sha(recs) == state["records_sha256"] and len(recs) == n * RECORD_SIZE
            add("records", same and post is not None,
                "" if same else f"record bytes [0,{n}) differ from the deployed ones")
            same_inode = (st.st_dev, st.st_ino) == (state["dev"], state["inode"])
            add("inode", same_inode, "" if same_inode else
                "the .sc was replaced (unlinked and recreated) during the run")
            if state.get("ext_sha256") is not None:
                if post is None:
                    add("v6_header", False, "no header after the run")
                else:
                    okv = post.version == SC6_VERSION and post.reserved == dep.reserved
                    add("v6_header", okv, "" if okv else
                        f"version {dep.version} -> {post.version}, feature_flags "
                        f"{dep.reserved:#x} -> {post.reserved:#x} (a v5-only loader "
                        "truncates a v6 file and writes a fresh v5 header)")
                es = ext_state(fd)
                oke = es["ext_sha256"] == state["ext_sha256"]
                add("ext", oke, "" if oke else
                    "extension bytes [0x100, 0x10000) changed during the run")
            if state.get("firmware_snapshot_id") is not None:
                if post is None:
                    add("v7_header", False, "no header after the run")
                    add("firmware_regions", False, "no v7 artifact after the run")
                else:
                    okv = post.version == SC7_VERSION and post.reserved == dep.reserved
                    add("v7_header", okv, "" if okv else
                        f"version {dep.version} -> {post.version}, feature_flags "
                        f"{dep.reserved:#x} -> {post.reserved:#x}")
                    try:
                        from ..formats.sc_v7 import read_sc7, sc7_snapshot_id

                        directory = read_sc7(sc)
                        sid = sc7_snapshot_id(sc)
                        okr = (directory.artifact_id8.hex() ==
                               state.get("firmware_artifact_id8") and
                               sid == state["firmware_snapshot_id"])
                        detail = "" if okr else (
                            f"artifact {state.get('firmware_artifact_id8')} -> "
                            f"{directory.artifact_id8.hex()}, snapshot "
                            f"{state['firmware_snapshot_id']} -> {sid}")
                    except (OSError, ValueError) as exc:
                        okr, detail = False, f"v7 directory/body validation failed: {exc}"
                    add("firmware_regions", okr, detail)
        finally:
            os.close(fd)
    m0 = state.get("manifest")
    if state.get("frontend") == "none":
        add("manifest", True, "direct QEMU run (no FastDyn frontend): no manifest to check")
    elif m0 is None:
        add("manifest", False, "no FastDyn manifest was recorded at deploy time")
    else:
        m1 = _manifest_state(m0["path"])
        if m1 is None:
            add("manifest", False, "manifest missing after the run")
        else:
            ok = m1["fingerprint"] == m0["fingerprint"] and m1["sha256"] == m0["sha256"]
            add("manifest", ok, "" if ok else
                f"fingerprint {m0['fingerprint']} -> {m1['fingerprint']}")
    verdict = VERDICT_PASS if all(c["ok"] for c in checks) else VERDICT_INVALID
    return {
        "verdict": verdict,
        "checks": checks,
        "failed": [c["check"] for c in checks if not c["ok"]],
        "n": n,
        # unknown (null) unless the artifact was provably used
        "appended": (record_count_after - n) if verdict == VERDICT_PASS else None,
        "record_count_after": record_count_after,
        "file_version": state.get("file_version", dep.version),
    }


def frozen_append_counts(run):
    """Verify that the sealed deployment survived and count late translations."""
    proof = run.get("proof") or {}
    if run.get("verdict") != "PASS" or proof.get("verdict") != "PASS" or proof.get("failed") != []:
        raise ValueError("frozen deployment failed the cache identity proof")
    checks = proof.get("checks") or []
    names = {c.get("check") for c in checks}
    required = {"sc_exists", "header", "records", "inode", "manifest"}
    if proof.get("file_version") == 6:
        required.update(("v6_header", "ext"))
    elif proof.get("file_version") == 7:
        required.update(("v7_header", "firmware_regions"))
    if not required <= names or any(c.get("ok") is not True for c in checks):
        raise ValueError("frozen deployment has incomplete cache identity checks")
    initial, after, appended = proof.get("n"), proof.get("record_count_after"), run.get("appended")
    if (type(initial) is not int or initial <= 0 or type(after) is not int or after < initial
            or type(appended) is not int or appended != after - initial
            or type(proof.get("appended")) is not int or proof["appended"] != appended):
        raise ValueError("frozen deployment has inconsistent late translation counts")
    return {"initial_records": initial, "record_count_after": after, "appended": appended}


def deploy_refusal(rd: Any) -> str | None:
    """Why ``rd`` must not be launched as a deployed run, or ``None``.

    A ``deploy.json`` that records a refusal (or lacks the deployed ``state``)
    means nothing was deployed; launching would only build a cold cache.
    """
    p = Path(rd.root) / "deploy.json"
    if not p.is_file():
        return None
    try:
        dep = json.loads(p.read_text())
    except ValueError as e:
        return f"deploy.json unreadable: {e}"
    if dep.get("refused"):
        r = dep["refused"]
        return "deploy was refused: " + (", ".join(r) if isinstance(r, list) else str(r))
    if not isinstance(dep.get("state"), dict):
        return "deploy.json has no deployed state"
    return None


def prove_run(rd: Any) -> dict[str, Any]:
    """:func:`prove` for a run directory deployed by :mod:`scopt.run.deploy`."""
    p = Path(rd.root) / "deploy.json"
    if not p.is_file():
        return {"verdict": VERDICT_INVALID, "checks": [
            {"check": "deployed", "ok": False, "detail": "deploy.json missing"}],
            "failed": ["deployed"], "n": None, "appended": None, "record_count_after": None}
    why = deploy_refusal(rd)
    if why is not None:
        return {"verdict": VERDICT_INVALID, "checks": [
            {"check": "deployed", "ok": False, "detail": why}],
            "failed": ["deployed"], "n": None, "appended": None, "record_count_after": None}
    return prove(json.loads(p.read_text())["state"])

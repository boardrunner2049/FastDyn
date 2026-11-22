"""``scopt profile run``: exact profile runs of a snapshot or artifact (§3.1-3.8, §5.11; WP14).

For each repetition (and input) the target is deployed into a fresh run
directory through the WP8 deploy oracle (a sparse warm copy, so the target is
never written), FastDyn runs once with ``QEMU_SC_PROFILE=<rd>/run.qprof`` and
``QEMU_SC_PROFILE_PHASES`` set (everything else ``QEMU_SC_*`` scrubbed), the
§4.8 proof must PASS, and the ``.qprof`` is ingested against the target with
the run's staged ``.sc`` (§3.6). The per-run profiles are merged into
``OUT/merged.profile.json`` (published to ``--store`` when given).

Isolation (T-B1-7): the target's files and every ``watch`` path (published
artifacts, the base manifest, ...) are hashed before the first run and after
the last; any difference fails the command. Rows the runs appended are
``PROFILE_ONLY`` in the profile (never merged into snapshot identities).

Writes ``OUT/profile_run.json``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..formats.sc_v5 import sha256_sparse
from .launcher import write_json

__all__ = ["ProfileRunError", "profile_run", "tree_hashes"]

SCHEMA = "scopt.profile_run/1"


class ProfileRunError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _file_hash(p: Path) -> str:
    if p.suffix == ".sc":
        return "sha256:" + sha256_sparse(p)
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return "sha256:" + h.hexdigest()


def tree_hashes(paths: Sequence[str | os.PathLike]) -> dict[str, Any]:
    """``{path: {"sha256", "size", "ino", "mtime_ns"}}`` of every regular file under
    ``paths`` (a ``.sc`` is hashed sparse-aware: data runs plus holes)."""
    out: dict[str, Any] = {}
    for root in paths:
        root = Path(root)
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
        for p in files:
            st = p.stat()
            out[str(p)] = {"sha256": _file_hash(p), "size": st.st_size, "ino": st.st_ino,
                           "mtime_ns": st.st_mtime_ns}
    return out


def _diff(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[str]:
    d = [f"removed {k}" for k in sorted(set(a) - set(b))]
    d += [f"added {k}" for k in sorted(set(b) - set(a))]
    d += [f"changed {k}" for k in sorted(set(a) & set(b)) if a[k] != b[k]]
    return d


def _load_json(p: str | os.PathLike) -> dict[str, Any]:
    return json.loads(Path(p).read_text())


def profile_run(base_toml: str | os.PathLike, target: str | os.PathLike,
                out: str | os.PathLike, *, phases: Sequence[str] = (), reps: int = 1,
                inputs: Sequence[str | os.PathLike] = (), input_bank: str = "input",
                input_split: str = "train", qemu: str | os.PathLike | None = None,
                bundle: str | os.PathLike | None = None,
                runtime_identity: str | os.PathLike | None = None,
                artifact_domain: bool = False, store: str | os.PathLike | None = None,
                watch: Sequence[str | os.PathLike] = (), quiet: bool = False,
                timeout_s: float = 300.0, peer=None) -> dict[str, Any]:
    """Profile ``target`` (store snapshot dir, artifact dir or ``.sc``) into ``out``."""
    from ..formats.profile_v1 import (ProfileError, check_flow, ingest, merge,
                                      publish_profile, write_profile)
    from ..formats.qprof_v1 import QprofFormatError
    from .deploy import DeployError, deploy_new
    from .launcher import launch

    if reps < 1:
        raise ProfileRunError("bad_reps", "reps must be >= 1")
    if input_split not in ("train", "heldout", "final"):
        raise ProfileRunError("bad_split", input_split)
    tgt = Path(target).resolve()
    tdir = tgt if tgt.is_dir() else None
    bpath = Path(bundle) if bundle else (tdir / "bundle.json" if tdir else None)
    rpath = Path(runtime_identity) if runtime_identity else (
        tdir / "runtime_identity.json" if tdir else None)
    if bpath is None or not bpath.is_file():
        raise ProfileRunError("no_bundle", "need the target's bundle.json (or --bundle)")
    if rpath is None or not rpath.is_file():
        raise ProfileRunError("no_runtime_identity",
                              "need the target's runtime_identity.json (or --runtime-identity)")
    bdoc, rdoc = _load_json(bpath), _load_json(rpath)
    if qemu is None:
        qemu = (rdoc.get("qemu_exe_stat") or {}).get("path")
    out = Path(out).resolve()
    if out.exists() and any(out.iterdir()):
        raise ProfileRunError("out_not_fresh", f"{out} exists and is not empty")
    out.mkdir(parents=True, exist_ok=True)
    phase_spec = ",".join(phases)
    watched = [tgt, *[Path(w).resolve() for w in watch]]
    before = tree_hashes(watched)

    plan: list[str | None] = [str(Path(i).resolve()) for i in inputs] or [None]
    runs: list[dict[str, Any]] = []
    profiles = []
    fails: list[str] = []
    for ii, inp in enumerate(plan):
        for rep in range(reps):
            name = f"rep-{rep}" if inp is None else f"in{ii}-rep-{rep}"
            rd_path = out / name
            rec: dict[str, Any] = {"name": name, "run_dir": str(rd_path), "input": inp, "rep": rep}
            runs.append(rec)
            try:
                rd, _dep = deploy_new(tgt, base_toml, rd_path, qemu=qemu, quiet=quiet,
                                      timeout_s=min(timeout_s, 300.0), label=f"profile:{name}")
            except DeployError as e:
                rec["deploy_refused"] = e.code
                fails.append(f"{name}: deploy refused {e.code}: {e.detail}")
                continue
            qprof = rd.root / "run.qprof"
            env = {"QEMU_SC_PROFILE": str(qprof)}
            if phase_spec:
                env["QEMU_SC_PROFILE_PHASES"] = phase_spec
            doc = launch(rd, timeout_s=timeout_s, extra_env=env, capture_files_ok=True,
                         inputs={input_bank: inp} if inp else None, **({"peer": peer} if peer is not None else {}))
            rec.update(verdict=doc["verdict"], exit_code=doc["exit_code"],
                       appended=doc["appended"], output_norm_sha256=doc["output_norm_sha256"],
                       guest_norm=doc["guest_norm"], qemu=doc["qemu"],
                       qemu_sha256=doc["qemu_sha256"], qprof=str(qprof))
            log = Path(doc["stdout_log"]).read_text(errors="replace")
            rec["profile_banner"] = "supercache-profile:" in log
            if doc["verdict"] != "PASS":
                fails.append(f"{name}: run verdict {doc['verdict']}")
                continue
            if not qprof.is_file():
                fails.append(f"{name}: {qprof} was not written")
                continue
            in_rows = []
            if inp:
                in_rows = [{"name": os.path.basename(inp), "split": input_split,
                            "sha256": "sha256:" + hashlib.sha256(Path(inp).read_bytes()).hexdigest()}]
            try:
                p = ingest(str(qprof), str(rd.cache), str(tgt if tdir is None else tdir),
                           bundle_id=bdoc["bundle_id"], contract_id=bdoc["contract_id"],
                           runtime_identity=rdoc, run_id=f"{out.name}/{name}", inputs=in_rows,
                           domain_kind="artifact_units" if artifact_domain else "snapshot_units")
            except (QprofFormatError, ProfileError) as e:
                rec["ingest_rejected"] = e.reason
                fails.append(f"{name}: ingest rejected {e.reason}: {getattr(e, 'detail', '')}")
                continue
            ppath = write_profile(rd.root / "profile.json", p)
            flow = check_flow(p)
            rec.update(profile=ppath, profile_id=p.profile_id, complete=p.complete,
                       flow_ok=flow.ok, units=len(p.units), profile_only=len(p.profile_only),
                       transient_entries=p.transient["entries"],
                       total_dispatches=sum(r["total_dispatches"] for r in p.collection["runs"]))
            if not flow.ok:
                fails.append(f"{name}: flow conservation fails")
            profiles.append(p)

    after = tree_hashes(watched)
    changed = _diff(before, after)
    if changed:
        fails.append("isolation: " + "; ".join(changed[:10]))
    res: dict[str, Any] = {
        "schema": SCHEMA, "base_toml": str(Path(base_toml).resolve()), "target": str(tgt),
        "phases": list(phases), "reps": reps, "inputs": plan if inputs else [],
        "qemu": None if qemu is None else str(qemu), "runs": runs,
        "isolation": {"watched": [str(w) for w in watched], "files": len(before),
                      "changed": changed, "ok": not changed, "before": before},
    }
    if profiles and len(profiles) == len(runs):
        try:
            m = merge(profiles) if len(profiles) > 1 else profiles[0]
            res["merged"] = {"path": write_profile(out / "merged.profile.json", m),
                             "profile_id": m.profile_id, "complete": m.complete,
                             "flow_ok": check_flow(m).ok, "runs": len(m.collection["runs"])}
            if store is not None:
                res["merged"]["published"] = publish_profile(store, m)
        except ProfileError as e:
            fails.append(f"merge: {e.reason}: {getattr(e, 'detail', '')}")
    res["verdict"] = "PASS" if not fails else "FAIL"
    res["failures"] = fails
    write_json(out / "profile_run.json", res)
    return res

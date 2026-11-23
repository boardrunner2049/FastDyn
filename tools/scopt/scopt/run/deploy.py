"""Deploy an artifact into a run directory (§4.8): manifest oracle, sparse copy,
``--check`` double run and the seed-run fallback.

The FastDyn frontend deletes a ``.sc`` whose ``<cache>.fastdyn.json``
fingerprint differs from what it computes for the current run (G13). The
fingerprint covers the whole TOML text, the generated work files, native-file
stats and the ``log_level`` that ``-q`` selects, so it cannot be predicted
offline. The oracle runs the real CLI through the shim (:mod:`.fdshim`), which
stops right before QEMU would start; ``_configure_supercache`` writes the
manifest exactly as the real run will compute it. Then the artifact's ``.sc``
is copied sparsely next to it.

``check``: run the shim twice more in two scratch work directories in
fingerprint-only mode (same ``run.toml``, nothing written to the cache
directory) and require that both fingerprints equal each other and the one
the oracle wrote. That catches nondeterministic generated files.

``method="seed"``: the fallback without the shim. One real cold run with the
same ``run.toml`` writes the manifest (and a cold ``.sc``); then the ``.sc`` is
replaced by the artifact.

Identity (§4.2, §4.8). An artifact directory must carry both ``bundle.json``
and ``runtime_identity.json`` (else ``artifact_identity_missing``). Deploy
recomputes the identity bundle from the run's TOML and the oracle manifest and
the pre-run RuntimeIdentity fields from the QEMU and plugin that FastDyn will
launch, and refuses on any difference: ``bundle_mismatch``,
``config_mismatch`` (``normalized_config_sha256``) or
``runtime_mismatch:<field>`` (``qemu_sha256``, ``qemu_exe_stat``,
``plugin_sha256``, ``plugin_stat``, ``build_options``, ``supercache_flat``).
``qemu_exe_stat`` matters because QEMU's ``runtime_signature`` hashes the
executable's dev/ino/size/mtime: a rebuilt or touched binary would make QEMU
truncate the cache. A bare ``.sc`` file has no identity documents; it is
deployed with ``identity.checked = false`` (the post-run proof still decides
whether it was used). A flat-mode base TOML never gets here: the TOML rewrite
refuses it as ``unsupported_flat``.

Refusals (``DeployError.code``): ``run_dir_not_fresh``, ``no_supercache``,
``unsupported_flat``, ``artifact_unreadable``, ``artifact_identity_missing``,
``oracle_failed``, ``oracle_no_manifest``, ``check_fingerprint_mismatch``,
``bundle_mismatch``, ``config_mismatch``, ``runtime_mismatch:<field>``,
``bundle_stale`` and the bundle reader's schema codes.

B3/B4 additions (WP26, design §5.1, §5.7, §9.4, §9.5):

* **Test-only artifacts** (:mod:`scopt.run.policy`): a negative control is
  refused ``negative_control_artifact`` unless ``allow_negative_control``; an
  artifact built under a test policy is refused ``test_only_artifact:<policy>``
  unless ``test_policies`` names that policy (``allow_ram_resident`` for
  F-B3-15/17); an unknown policy is ``unknown_test_policy:<policy>``.
* **v6 consistency** (artifact directories): the ``.sc`` header version and the
  sidecar's ``loader_requirement`` must agree (``loader_requirement_mismatch``),
  and a v6 file's ext ``artifact_id8`` must equal the sidecar's
  ``x_ext_artifact_id8`` (``ext_artifact_id_mismatch``; a v6 sidecar without
  it is ``ext_artifact_id_missing``). The loader cannot make this comparison
  (WP22 note 2).
* **Staleness (F-B3-16).** A changed patch plan (a) and a coverage-enabled
  FastDyn configuration (e) are refused by the identity checks with
  ``bundle_mismatch`` first (fields ``patch_plan_hash``,
  ``patch_bindings_hash``, ``instrumentation_hash`` for (a); ``contract_id``
  for (e)), followed by ``config_mismatch`` (the normalized TOML differs);
  :func:`identity_reasons` is the pure function, pinned by
  ``tests/unit/test_wp26_harness.py``. Without identity documents (a bare
  ``.sc``) nothing is checked here and the FastDyn fingerprint or the
  post-run proof decides.
* **Direct QEMU path** (:func:`deploy_direct`, :func:`launch_direct`): the
  mps2 alias run of F-B3-15 has no FastDyn frontend. A bare ``.sc`` is
  deployed with ``identity.checked = false`` into a fresh run directory
  together with the QEMU argv (``@SUPERCACHE@``/``@RUN@`` placeholders); the
  proof skips only the manifest check (``frontend = "none"``). An artifact
  directory with a RuntimeIdentity must name the QEMU executable's sha256
  (``runtime_mismatch:qemu_sha256``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ..formats.sc_v5 import HEADER_SIZE, ScHeader, sparse_copy
from .launcher import (FASTDYN_PY, FASTDYN_SRC, FDSHIM, RunDir, RunDirError, fastdyn_args, file_sha256,
                       launch, scrub_env, write_json)
from .policy import deploy_refusals
from .proof import EXT_BEGIN, deployed_state

__all__ = ["DeployError", "run_shim", "oracle", "check_fingerprints", "resolve_artifact",
           "deploy", "identity_reasons", "artifact_refusals", "deploy_direct", "launch_direct",
           "prepare_direct",
           "DIRECT_SCHEMA"]

DEPLOY_SCHEMA = "scopt.deploy/1"


class DeployError(RuntimeError):
    def __init__(self, code: str, detail: str = "", **info: Any):
        self.code = code
        self.detail = detail
        self.info = info
        super().__init__(f"{code}: {detail}" if detail else code)


# --------------------------------------------------------------------------
# shim
# --------------------------------------------------------------------------

def run_shim(rd: RunDir, *, out: str | os.PathLike, work: str | os.PathLike | None = None,
             fingerprint_only: bool = False, timeout_s: float = 120.0,
             extra_env: dict[str, str | None] | None = None) -> dict[str, Any]:
    """Run the FastDyn shim for ``rd`` (same flags and environment as the launch)."""
    args = fastdyn_args(rd)
    if work is not None:
        args[args.index("-o") + 1] = str(work)
    cmd = [str(FASTDYN_PY), str(FDSHIM), "--scopt-out", str(out)]
    if fingerprint_only:
        cmd.append("--scopt-fingerprint-only")
    cmd += args
    env, env_diff = scrub_env(qemu=rd.qemu, extra=extra_env)
    t0 = time.monotonic()
    try:
        p = subprocess.run(["setarch", "x86_64", "-R", *cmd], cwd=str(FASTDYN_SRC), env=env,
                           capture_output=True, text=True, timeout=timeout_s,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise DeployError("oracle_failed", f"shim timed out after {timeout_s}s") from e
    log = Path(str(out) + ".log")
    log.write_text(p.stdout + p.stderr)
    if not Path(out).is_file():
        raise DeployError("oracle_failed", f"shim exit {p.returncode}, no record; "
                          f"see {log}: {(p.stderr or p.stdout)[-400:]}")
    rec = json.loads(Path(out).read_text())
    rec["shim_cmd"] = cmd
    rec["shim_env_diff"] = env_diff
    rec["shim_exit"] = p.returncode
    rec["shim_seconds"] = time.monotonic() - t0
    write_json(out, rec)
    if p.returncode != 0 or not rec.get("reached_start_execution"):
        raise DeployError("oracle_failed", f"shim exit {p.returncode}; see {log}: "
                          f"{(p.stderr or p.stdout)[-400:]}")
    return rec


def oracle(rd: RunDir, **kw: Any) -> dict[str, Any]:
    """Write the FastDyn manifest for ``rd`` via the shim; returns the shim record."""
    if rd.cache is None:
        raise DeployError("no_supercache", "the run TOML has no [Machine].supercache")
    if rd.cache.exists():
        raise DeployError("run_dir_not_fresh", f"{rd.cache} already exists; the oracle would "
                          "let FastDyn delete or keep it")
    rec = run_shim(rd, out=rd.root / "oracle.json", **kw)
    sc = rec.get("supercache") or {}
    if Path(sc.get("cache_path", "")) != rd.cache:
        raise DeployError("oracle_failed", f"FastDyn used cache {sc.get('cache_path')}, "
                          f"expected {rd.cache}")
    if not rd.manifest.is_file():
        raise DeployError("oracle_no_manifest", f"{rd.manifest} was not written")
    man = json.loads(rd.manifest.read_text())
    if man.get("fingerprint") != sc.get("fingerprint"):
        raise DeployError("oracle_failed", "manifest fingerprint differs from the computed one")
    # The QEMU command line as FastDyn would launch it. Kept in the run root, not in
    # work/: the real launch recreates the work directory.
    write_json(rd.root / "qemu_argv.json", {
        "argv": rec["argv"], "cwd": rec["cwd"], "env_set": rec.get("env_set", {}),
        "env_unset": rec.get("env_unset", [])})
    return rec


def check_fingerprints(rd: RunDir, scratch: str | os.PathLike | None = None,
                       **kw: Any) -> dict[str, Any]:
    """``deploy --check``: two fingerprint-only shim runs in scratch work dirs."""
    scratch = Path(scratch) if scratch else rd.root / "check"
    if scratch.exists():
        shutil.rmtree(scratch)
    recs = []
    for name in ("a", "b"):
        d = scratch / name
        d.mkdir(parents=True)
        recs.append(run_shim(rd, out=d / "oracle.json", work=d / "work", fingerprint_only=True,
                             **kw))
    fps = [r["supercache"]["fingerprint"] for r in recs]
    manifest_fp = None
    if rd.manifest is not None and rd.manifest.is_file():
        manifest_fp = json.loads(rd.manifest.read_text()).get("fingerprint")
    diff = []
    a, b = (r["supercache"]["inputs"] for r in recs)
    for sect in sorted(set(a) | set(b)):
        if a.get(sect) != b.get(sect):
            diff.append(sect)
    ok = fps[0] == fps[1] and (manifest_fp is None or manifest_fp == fps[0])
    return {"ok": ok, "scratch": str(scratch), "fingerprints": fps,
            "manifest_fingerprint": manifest_fp, "differing_input_sections": diff}


# --------------------------------------------------------------------------
# deploy
# --------------------------------------------------------------------------

def resolve_artifact(artifact: str | os.PathLike) -> dict[str, Any]:
    """An artifact directory (``cache.sc`` + optional bundle/runtime identity) or a ``.sc``."""
    p = Path(artifact).resolve()
    if p.is_dir():
        sc = p / "cache.sc"
        bundle = p / "bundle.json"
        rid = p / "runtime_identity.json"
        hints = p / "cache.sc.restore-hints"
        return {"kind": "artifact_dir", "path": str(p), "sc": str(sc),
                "bundle": str(bundle) if bundle.is_file() else None,
                "runtime_identity": str(rid) if rid.is_file() else None,
                "restore_hints": str(hints) if hints.is_file() else None}
    return {"kind": "sc_file", "path": str(p), "sc": str(p), "bundle": None,
            "runtime_identity": None}


def identity_reasons(want_bundle: dict[str, Any] | None, have_bundle: dict[str, Any] | None,
                     want_ri: dict[str, Any] | None, have_ri: dict[str, Any] | None
                     ) -> tuple[list[str], dict[str, Any]]:
    """Deploy reason codes from the artifact's identity documents (``want_*``)
    and the ones recomputed for the run (``have_*``), in deploy order: the
    bundle first, then the RuntimeIdentity's pre-run fields. ``None`` on the
    artifact side skips that part. Returns ``(reasons, detail)``; ``detail``
    has ``bundle_fields`` (the differing bundle fields) when the bundles differ.
    """
    from ..formats.bundle_v1 import BundleError, check_bundle, check_runtime_identity

    reasons: list[str] = []
    detail: dict[str, Any] = {}
    if want_bundle is not None:
        try:
            check_bundle(want_bundle, have_bundle or {})
        except BundleError as e:
            reasons.append(e.code)
            detail["bundle_detail"] = e.detail
            if "fields" in e.info:
                detail["bundle_fields"] = e.info["fields"]
    if want_ri is not None:
        reasons += check_runtime_identity(want_ri, have_ri or {})
    return reasons, detail


def _ext_id8_of(sc: str | os.PathLike) -> str | None:
    with open(sc, "rb") as f:
        f.seek(EXT_BEGIN + 48)
        b = f.read(8)
    return b.hex() if len(b) == 8 else None


def artifact_refusals(art: dict[str, Any], header: ScHeader, *,
                      allow_negative_control: bool = False,
                      test_policies: Any = ()) -> tuple[list[str], dict[str, Any]]:
    """Reason codes from the artifact itself (test-only tags, v6 consistency)."""
    from .policy import artifact_tags

    info: dict[str, Any] = {"file_version": header.version}
    reasons = deploy_refusals(art["path"], allow_negative_control=allow_negative_control,
                              test_policies=test_policies)
    info["tags"] = artifact_tags(art["path"])  # a bare .sc too (its dir marker, sidecar)
    from ..formats.sc_v6 import SC7_SEMANTIC_MARKER

    if header.version != 7:
        reasons.append("legacy_cache_version")
    semantic = header.version == 7 and bool(header.reserved & SC7_SEMANTIC_MARKER)
    public = header.version == 7 and header.reserved == 0
    firmware = header.version == 7 and header.reserved == 1
    if header.version == 7 and not (public or semantic or firmware):
        reasons.append("unknown_v7_features")
    id8 = _ext_id8_of(art["sc"]) if semantic else None
    firmware_id8 = None
    firmware_directory = None
    if firmware:
        try:
            from ..formats.sc_v7 import read_sc7

            firmware_directory = read_sc7(art["sc"])
            firmware_id8 = firmware_directory.artifact_id8.hex()
        except (OSError, ValueError):
            reasons.append("firmware_region_directory_invalid")
    info["ext_artifact_id8"] = id8
    info["firmware_artifact_id8"] = firmware_id8
    side = None
    if art["kind"] == "artifact_dir":
        sp = Path(art["path"]) / "cache.sc.scopt.json"
        if sp.is_file():
            try:
                side = json.loads(sp.read_text())
            except (OSError, ValueError):
                side = None
    if isinstance(side, dict):
        from ..formats.artifact import LOADER_V7
        from ..formats.sc_v7 import loader_requirement

        lr = side.get("loader_requirement")
        info["loader_requirement"] = lr
        want_lr = (loader_requirement(firmware_directory) if firmware_directory is not None else
                   LOADER_V7 if public or semantic else None)
        if want_lr is not None and lr is not None and lr != want_lr:
            reasons.append("loader_requirement_mismatch")
        if semantic:
            sid8 = side.get("x_ext_artifact_id8")
            info["sidecar_ext_artifact_id8"] = sid8
            if sid8 is None:
                reasons.append("ext_artifact_id_missing")
            elif sid8 != id8:
                reasons.append("ext_artifact_id_mismatch")
        elif firmware:
            sid8 = side.get("x_firmware_artifact_id8")
            info["sidecar_firmware_artifact_id8"] = sid8
            if sid8 is None:
                reasons.append("firmware_artifact_id_missing")
            elif sid8 != firmware_id8:
                reasons.append("firmware_artifact_id_mismatch")
    return reasons, info


def _identity_checks(art: dict[str, Any], rd: RunDir, oracle_rec: dict[str, Any],
                     header: ScHeader) -> dict[str, Any]:
    from ..formats.bundle_v1 import (BundleError, identity_bundle, read_bundle,
                                     read_runtime_identity, runtime_identity)

    out: dict[str, Any] = {"reasons": [], "checked": art["kind"] == "artifact_dir"}
    if art["kind"] == "artifact_dir":
        missing = [n for n in ("bundle", "runtime_identity") if not art[n]]
        if missing:
            out["reasons"].append("artifact_identity_missing")
            out["missing"] = [f"{n}.json" for n in missing]
    if art["bundle"]:
        try:
            want = read_bundle(art["bundle"])
            have = identity_bundle(rd.toml, rd.manifest)
            out["bundle_id"] = have["bundle_id"]
            out["expected_bundle_id"] = want["bundle_id"]
            rs, det = identity_reasons(want, have, None, None)
            out["reasons"] += rs
            if "bundle_detail" in det:
                out["bundle_detail"] = det["bundle_detail"]
            if "bundle_fields" in det:
                out["bundle_fields"] = det["bundle_fields"]
        except BundleError as e:
            out["reasons"].append(e.code)
            out["bundle_detail"] = e.detail
    if art["runtime_identity"]:
        try:
            want_ri = read_runtime_identity(art["runtime_identity"])
            argv = oracle_rec.get("argv") or []
            plugin = None
            if "--plugin" in argv:
                plugin = argv[argv.index("--plugin") + 1].split(",", 1)[0]
                plugin = str((FASTDYN_SRC / plugin).resolve()) if not os.path.isabs(plugin) else plugin
            have_ri = runtime_identity(qemu=argv[0], plugin=plugin,
                                       config_text=rd.toml.read_text(), sc_header=header,
                                       fastdyn_src=FASTDYN_SRC)
            out["runtime_identity_id"] = have_ri["runtime_identity_id"]
            out["reasons"] += identity_reasons(None, None, want_ri, have_ri)[0]
        except BundleError as e:
            out["reasons"].append(e.code)
            out["runtime_identity_detail"] = e.detail
    return out


def deploy(artifact: str | os.PathLike, rd: RunDir, *, check: bool = False,
           method: str = "oracle", timeout_s: float = 120.0,
           manifest_override: str | os.PathLike | None = None,
           enforce_identity: bool = True, allow_negative_control: bool = False,
           test_policies: Any = (), expected_sc_sha256: str | None = None,
           measured_runtime: dict[str, str] | None = None,
           expected_hint_sha256: str | None = None) -> dict[str, Any]:
    """Deploy ``artifact`` into the fresh run directory ``rd``; writes ``deploy.json``.

    ``manifest_override`` replaces the oracle's manifest with the given file
    after the copy (only for negative tests of the proof: a wrong manifest must
    make the frontend delete the ``.sc``). ``allow_negative_control`` and
    ``test_policies`` admit test-only artifacts (:mod:`scopt.run.policy`); the
    artifact checks run before the oracle (no FastDyn run for a refusal).
    """
    art = resolve_artifact(artifact)
    if (rd.root / "deploy.json").exists() or (rd.root / "run.json").exists():
        raise DeployError("run_dir_not_fresh", f"{rd.root} was already deployed or run")
    if rd.cache is None:
        raise DeployError("no_supercache", "the run TOML has no [Machine].supercache")
    try:
        with open(art["sc"], "rb") as f:
            hb = f.read(HEADER_SIZE)
        header = ScHeader.from_bytes(hb)
    except (OSError, ValueError) as e:
        raise DeployError("artifact_unreadable", f"{art['sc']}: {e}") from e

    t0 = time.monotonic()
    doc: dict[str, Any] = {"schema": DEPLOY_SCHEMA, "artifact": art, "method": method,
                           "run_dir": str(rd.root),
                           # informational: QEMU will rebuild such a file; the proof catches it
                           "artifact_header_problems": header.problems(),
                           "artifact_header_problems_v6": header.problems(versions=(5, 6))}
    try:
        reasons, ainfo = artifact_refusals(art, header,
                                           allow_negative_control=allow_negative_control,
                                           test_policies=test_policies)
    except OSError as e:
        raise DeployError("artifact_unreadable", f"{art['sc']}: {e}") from e
    doc["artifact_checks"] = {**ainfo, "allow_negative_control": allow_negative_control,
                              "test_policies": sorted(test_policies), "reasons": reasons}
    if reasons:
        write_json(rd.root / "deploy.json", {**doc, "refused": reasons})
        raise DeployError(reasons[0], "; ".join(reasons), artifact_checks=ainfo)
    if method == "oracle":
        rec = oracle(rd, timeout_s=timeout_s)
    elif method == "seed":
        if rd.cache.exists():
            raise DeployError("run_dir_not_fresh", f"{rd.cache} exists")
        seed = launch(rd, timeout_s=timeout_s, prove=False, run_json="seed-run.json")
        if seed["exit_code"] != 0 or not rd.manifest.is_file():
            raise DeployError("oracle_failed", f"seed run exit {seed['exit_code']}")
        man = json.loads(rd.manifest.read_text())
        rec = {"seed_run": str(rd.root / "seed-run.json"), "argv": [seed.get("qemu")],
               "supercache": {"cache_path": str(rd.cache), "fingerprint": man.get("fingerprint"),
                              "inputs": man.get("inputs")}}
        write_json(rd.root / "oracle.json", rec)
        if rd.cache.exists():
            rd.cache.unlink()
    else:
        raise ValueError(f"unknown deploy method {method!r}")
    doc["oracle"] = {"fingerprint": rec["supercache"]["fingerprint"],
                     "qemu": (rec.get("argv") or [None])[0],
                     "record": str(rd.root / "oracle.json")}

    if check:
        doc["check"] = check_fingerprints(rd, timeout_s=timeout_s)
        if not doc["check"]["ok"]:
            write_json(rd.root / "deploy.json", {**doc, "refused": "check_fingerprint_mismatch"})
            raise DeployError("check_fingerprint_mismatch",
                              f"fingerprints {doc['check']['fingerprints']} vs manifest "
                              f"{doc['check']['manifest_fingerprint']}", check=doc["check"])

    ids = _identity_checks(art, rd, rec, header)
    doc["identity"] = ids
    if ids["reasons"] and enforce_identity:
        write_json(rd.root / "deploy.json", {**doc, "refused": ids["reasons"]})
        raise DeployError(ids["reasons"][0], "; ".join(ids["reasons"]), identity=ids)

    if measured_runtime is not None:
        actual_runtime = {"qemu_sha256": file_sha256(doc["oracle"]["qemu"]),
                          "ram_storage": rd.meta.get("ram_storage", "as-configured")}
        if actual_runtime != measured_runtime:
            write_json(rd.root / "deploy.json", {**doc, "refused": ["selected_runtime_changed"]})
            raise DeployError("selected_runtime_changed", str(actual_runtime))
    hint = art.get("restore_hints")
    hint_meta = Path(art["sc"] + ".scopt.json")
    declared_hint = None
    if art["kind"] == "artifact_dir" and hint_meta.is_file():
        try:
            declared_hint = json.loads(hint_meta.read_text()).get("x_restore_hints")
        except (ValueError, OSError) as exc:
            raise DeployError("artifact_unreadable", str(exc)) from exc
    if bool(hint) != (declared_hint is not None):
        raise DeployError("restore_hints_unsealed", "restore hint and artifact declaration disagree")
    dest_hint = Path(str(rd.cache) + ".restore-hints")
    if dest_hint.exists():
        raise DeployError("run_dir_not_fresh", f"unexpected restore hint at {dest_hint}")
    if hint:
        from ..formats.artifact import verify_manifest
        if (not isinstance(declared_hint, dict) or verify_manifest(art["path"])
                or file_sha256(hint) != declared_hint.get("sha256")):
            raise DeployError("restore_hints_unsealed", "restore hint failed manifest or digest verification")
    sparse_copy(art["sc"], rd.cache)
    if hint:
        shutil.copyfile(hint, dest_hint)
        doc["restore_hint_sha256"] = file_sha256(dest_hint)
        if doc["restore_hint_sha256"] != declared_hint["sha256"]:
            raise DeployError("restore_hint_changed", str(dest_hint))
    if expected_sc_sha256 is not None:
        if doc.get("restore_hint_sha256") != expected_hint_sha256:
            write_json(rd.root / "deploy.json", {**doc, "refused": ["selected_restore_hint_changed"]})
            raise DeployError("selected_restore_hint_changed", str(dest_hint))
        from ..formats.sc_v5 import sha256_sparse
        actual = "sha256:" + sha256_sparse(rd.cache)
        if actual != expected_sc_sha256:
            write_json(rd.root / "deploy.json", {**doc, "refused": ["selected_cache_changed"]})
            raise DeployError("selected_cache_changed", f"{actual} differs from {expected_sc_sha256}")
        doc["selected_cache_sha256"] = actual
    if manifest_override is not None:
        shutil.copyfile(manifest_override, rd.manifest)
        doc["manifest_override"] = str(manifest_override)
    doc["state"] = deployed_state(rd.cache, rd.manifest)
    doc["deploy_seconds"] = time.monotonic() - t0
    write_json(rd.root / "deploy.json", doc)
    return doc


def deploy_new(artifact: str | os.PathLike, base_toml: str | os.PathLike,
               run_dir: str | os.PathLike, **kw: Any) -> tuple[RunDir, dict[str, Any]]:
    """Create ``run_dir`` from ``base_toml`` and deploy ``artifact`` into it."""
    from .launcher import create_run_dir
    from .toml_rewrite import TomlRewriteError

    rd_kw = {k: kw.pop(k) for k in ("quiet", "qemu", "label", "cache_name") if k in kw}
    try:
        rd = create_run_dir(base_toml, run_dir, **rd_kw)
    except (RunDirError, TomlRewriteError) as e:
        raise DeployError(e.code, e.detail) from e
    return rd, deploy(artifact, rd, **kw)


# --------------------------------------------------------------------------
# direct QEMU path (no FastDyn frontend): the mps2 alias run of F-B3-15
# --------------------------------------------------------------------------

DIRECT_SCHEMA = "scopt.direct_run/1"
#: placeholders of a direct argv template
PH_SC = "@SUPERCACHE@"
PH_RUN = "@RUN@"
PH_QEMU = "@QEMU@"


def deploy_direct(artifact: str | os.PathLike, run_dir: str | os.PathLike, *,
                  argv: list[str], qemu: str | os.PathLike | None = None,
                  cache_name: str = "cache.sc", allow_negative_control: bool = False,
                  test_policies: Any = ()) -> dict[str, Any]:
    """Deploy ``artifact`` for a direct QEMU run (no FastDyn) into the fresh ``run_dir``.

    ``argv`` is the QEMU command template: its first element is the QEMU binary
    or ``@QEMU@`` (replaced by ``qemu``); ``@SUPERCACHE@`` becomes the deployed
    cache path and ``@RUN@`` the run directory. The run directory gets
    ``cache/<cache_name>`` (a sparse copy), ``direct.json`` (the expanded argv)
    and ``deploy.json`` (``method = "direct"``, ``identity.checked = false``,
    state with ``frontend = "none"``). Refusals as :func:`deploy` (test-only
    tags, v6 consistency); an artifact directory whose RuntimeIdentity names
    another QEMU sha256 is ``runtime_mismatch:qemu_sha256``."""
    from ..formats.bundle_v1 import BundleError, read_runtime_identity
    from .launcher import file_sha256

    root = Path(run_dir).resolve()
    if root.exists() and any(root.iterdir()):
        raise DeployError("run_dir_not_fresh", f"{root} exists and is not empty")
    art = resolve_artifact(artifact)
    try:
        with open(art["sc"], "rb") as f:
            header = ScHeader.from_bytes(f.read(HEADER_SIZE))
    except (OSError, ValueError) as e:
        raise DeployError("artifact_unreadable", f"{art['sc']}: {e}") from e
    if not argv:
        raise DeployError("direct_argv_missing", "empty QEMU argv template")
    exe = str(qemu) if argv[0] == PH_QEMU else argv[0]
    if exe == PH_QEMU or not os.path.isfile(exe):
        raise DeployError("direct_qemu_missing", f"QEMU binary {exe!r} not found")
    if not any(PH_SC in a for a in argv):
        raise DeployError("direct_argv_missing", f"no {PH_SC} placeholder in the argv template")
    root.mkdir(parents=True, exist_ok=True)
    cache = root / "cache" / cache_name
    full = [exe] + [a.replace(PH_SC, str(cache)).replace(PH_RUN, str(root)) for a in argv[1:]]
    doc: dict[str, Any] = {"schema": DEPLOY_SCHEMA, "artifact": art, "method": "direct",
                           "run_dir": str(root),
                           "artifact_header_problems": header.problems(),
                           "artifact_header_problems_v6": header.problems(versions=(5, 6))}
    reasons, ainfo = artifact_refusals(art, header, allow_negative_control=allow_negative_control,
                                       test_policies=test_policies)
    ids: dict[str, Any] = {"checked": False, "reasons": [], "frontend": "none",
                           "why_unchecked": "direct QEMU run: no FastDyn manifest, no bundle"}
    qsha = file_sha256(exe)
    if art["runtime_identity"]:
        try:
            want = read_runtime_identity(art["runtime_identity"])
            if want.get("qemu_sha256") is not None and want["qemu_sha256"] != qsha:
                ids["reasons"].append("runtime_mismatch:qemu_sha256")
            ids["runtime_identity_qemu_checked"] = True
        except BundleError as e:
            ids["reasons"].append(e.code)
    doc["artifact_checks"] = {**ainfo, "allow_negative_control": allow_negative_control,
                              "test_policies": sorted(test_policies), "reasons": reasons}
    doc["identity"] = ids
    reasons = reasons + ids["reasons"]
    if reasons:
        write_json(root / "deploy.json", {**doc, "refused": reasons})
        raise DeployError(reasons[0], "; ".join(reasons))
    cache.parent.mkdir(parents=True, exist_ok=True)
    sparse_copy(art["sc"], cache)
    os.chmod(cache, 0o600)
    write_json(root / "direct.json", {"schema": DIRECT_SCHEMA, "argv": full, "cwd": str(root),
                                      "qemu": exe, "qemu_sha256": qsha, "cache": str(cache)})
    doc["state"] = deployed_state(cache, None, frontend="none")
    write_json(root / "deploy.json", doc)
    return doc


def launch_direct(run_dir: str | os.PathLike, *, timeout_s: float = 60.0,
                  extra_env: dict[str, str | None] | None = None) -> dict[str, Any]:
    """Run the QEMU command of ``run_dir/direct.json`` once (``setarch x86_64 -R``,
    ``timeout``, cwd = the run directory, environment without ``QEMU_SC_*`` /
    ``FASTDYN_*`` except ``extra_env``) and prove it; writes ``run.json``.
    ``guest_norm`` is every stdout/stderr line (the alias image prints only
    semihosting lines) plus ``exit_code=<rc>``."""
    import hashlib

    from types import SimpleNamespace

    from .launcher import file_sha256
    from .proof import deploy_refusal, prove_run

    root = Path(run_dir).resolve()
    rd = SimpleNamespace(root=root)
    why = deploy_refusal(rd)
    if why is not None:
        raise RunDirError("deploy_refused", f"{root}: {why}; nothing to launch")
    if (root / "run.json").exists():
        raise RunDirError("run_dir_used", f"{root / 'run.json'} exists")
    d = json.loads((root / "direct.json").read_text())
    qsha = file_sha256(d["qemu"])
    if d.get("qemu_sha256") is not None and qsha != d["qemu_sha256"]:
        # the binary changed between deploy and launch (WP26 review): the deploy's
        # identity checks were made against another QEMU
        raise RunDirError("runtime_mismatch",
                          f"{d['qemu']}: sha256 {qsha} != {d['qemu_sha256']} at deploy")
    env, env_diff = scrub_env(qemu=None, extra=extra_env)
    env.pop("FASTDYN_QEMU", None)
    cmd = ["setarch", "x86_64", "-R", "timeout", "--signal=TERM", "--kill-after=5",
           f"{timeout_s:g}", *d["argv"]]
    t0 = time.monotonic()
    with open(root / "stdout.log", "wb") as f:
        p = subprocess.run(cmd, cwd=str(root), env=env, stdout=f, stderr=subprocess.STDOUT,
                           stdin=subprocess.DEVNULL, timeout=timeout_s + 30)
    wall = time.monotonic() - t0
    text = (root / "stdout.log").read_text(errors="replace")
    norm = [x for x in text.splitlines()] + [f"exit_code={p.returncode}"]
    (root / "guest.norm").write_text("\n".join(norm) + "\n")
    pr = prove_run(rd)
    verdict = pr["verdict"] if p.returncode == 0 else "INVALID_exit_code"
    doc = {"schema": DIRECT_SCHEMA, "run_dir": str(root), "argv": cmd, "cwd": str(root),
           "env_diff": env_diff, "qemu": d["qemu"], "qemu_sha256": qsha,
           "exit_code": p.returncode, "timed_out": p.returncode == 124, "wall_s": wall,
           "guest_norm": norm,
           "guest_norm_sha256": "sha256:" + hashlib.sha256(
               ("\n".join(norm) + "\n").encode()).hexdigest(),
           "proof": pr, "appended": pr.get("appended"), "verdict": verdict}
    write_json(root / "run.json", doc)
    return doc


def prepare_direct(out: str | os.PathLike, *, argv: list[str],
                   qemu: str | os.PathLike | None = None, cache_name: str = "cache.sc",
                   timeout_s: float = 60.0) -> dict[str, Any]:
    """The non-FastDyn import path: one cold, capture-on direct QEMU run (the mps2
    alias image of F-B3-15) in the fresh directory ``out``.

    Writes ``out/cache/<cache_name>`` (the cold cache QEMU builds),
    ``out/capture.scm`` (``QEMU_SC_CAPTURE``, the only ``QEMU_SC_*`` variable
    set), ``stdout.log`` and ``prep.json``. Import it without a manifest or
    bundle (``scopt import --sc out/cache/<name> --scm out/capture.scm
    --firmware <elf>``); the snapshot then has no ExecutionBundle, and its
    artifacts deploy only through :func:`deploy_direct` (``identity.checked =
    false``)."""
    from .launcher import file_sha256

    root = Path(out).resolve()
    if root.exists() and any(root.iterdir()):
        raise DeployError("run_dir_not_fresh", f"{root} exists and is not empty")
    if not argv or not any(PH_SC in a for a in argv):
        raise DeployError("direct_argv_missing", f"no {PH_SC} placeholder in the argv template")
    exe = str(qemu) if argv[0] == PH_QEMU else argv[0]
    if exe == PH_QEMU or not os.path.isfile(exe):
        raise DeployError("direct_qemu_missing", f"QEMU binary {exe!r} not found")
    cache = root / "cache" / cache_name
    cache.parent.mkdir(parents=True, exist_ok=True)
    scm = root / "capture.scm"
    full = [exe] + [a.replace(PH_SC, str(cache)).replace(PH_RUN, str(root)) for a in argv[1:]]
    env, env_diff = scrub_env(qemu=None, extra={"QEMU_SC_CAPTURE": str(scm)})
    env.pop("FASTDYN_QEMU", None)
    cmd = ["setarch", "x86_64", "-R", "timeout", "--signal=TERM", "--kill-after=5",
           f"{timeout_s:g}", *full]
    t0 = time.monotonic()
    with open(root / "stdout.log", "wb") as f:
        p = subprocess.run(cmd, cwd=str(root), env=env, stdout=f, stderr=subprocess.STDOUT,
                           stdin=subprocess.DEVNULL, timeout=timeout_s + 30)
    doc: dict[str, Any] = {"schema": "scopt.prepare_direct/1", "out": str(root), "argv": cmd,
                           "env_diff": env_diff, "qemu": exe, "qemu_sha256": file_sha256(exe),
                           "exit_code": p.returncode, "wall_s": time.monotonic() - t0,
                           "cache": str(cache) if cache.is_file() else None,
                           "capture": str(scm) if scm.is_file() else None,
                           "capture_size": scm.stat().st_size if scm.is_file() else None}
    doc["verdict"] = "PASS" if p.returncode == 0 and doc["cache"] and doc["capture"] \
        else "FAIL"
    write_json(root / "prep.json", doc)
    return doc

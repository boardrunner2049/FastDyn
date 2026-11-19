"""``scopt prepare``: capture-on cold run(s) in a fresh run directory (§4.3, §5.11; WP14).

Layout of ``OUT`` (a WP8 run directory, so ``scopt import OUT`` finds exactly one
``.sc`` and every capture)::

    OUT/run.toml, rundir.json        the base TOML with only the path keys rewritten
    OUT/cache/<name>.sc (+manifest)  the cache QEMU wrote on the cold run
    OUT/capture.scm                  QEMU_SC_CAPTURE of the cold run
    OUT/capture-<k>.scm              captures of further inputs (k >= 1), see below
    OUT/run.json, run-<k>.json       launcher records (exit, BENCH, outputs)
    OUT/oracle/                      an oracle-only run directory of the same TOML:
                                     oracle.json holds the QEMU argv for
                                     ``scopt bundle runtime-identity --run-dir OUT/oracle``
    OUT/prep.json                    summary (verdict PASS/FAIL, records, banners)

The first input (or none) is a cold run: empty cache, capture on. Each further
input runs warm on that same cache with capture on and its own capture file;
it appends the TBs the earlier runs did not translate (their captures carry
those slots; §2.5.8 unions captures by slot).

Rules (§4.8): ``setarch x86_64 -R``, timeout, scrubbed environment with only
``QEMU_SC_CAPTURE`` set, ``supercache_flat=false`` required in the base TOML.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..formats.sc_v5 import read_sc, sha256_sparse
from .launcher import RunDir, RunDirError, _cleanup_tmpfs_ram, create_run_dir, file_sha256, launch, write_json

__all__ = ["PrepareError", "prepare", "CAPTURE_BANNER"]

PREP_SCHEMA = "scopt.prepare/1"
CAPTURE_BANNER = "supercache-capture:"


class PrepareError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _check_base(base_toml: Path) -> None:
    cfg = tomllib.loads(base_toml.read_text(encoding="utf-8"))
    m = cfg.get("Machine", {})
    if not m.get("supercache"):
        raise PrepareError("no_supercache", f"{base_toml} has no [Machine].supercache")
    if m.get("supercache_flat", None) is not False:
        raise PrepareError("unsupported_flat",
                           f"{base_toml} must set supercache_flat = false explicitly")


def prepare(base_toml: str | os.PathLike, out: str | os.PathLike, *,
            inputs: Sequence[str | os.PathLike] = (), input_bank: str = "input",
            qemu: str | os.PathLike | None = None, quiet: bool = False,
            timeout_s: float = 300.0, oracle: bool = True) -> dict[str, Any]:
    """Run the prep capture(s) into the fresh directory ``out``; returns prep.json."""
    from .deploy import DeployError, oracle as run_oracle

    base = Path(base_toml).resolve()
    _check_base(base)
    out = Path(out).resolve()
    rd = create_run_dir(base, out, quiet=quiet, qemu=qemu, label="prepare")
    runs: list[dict[str, Any]] = []
    fails: list[str] = []
    plan: list[str | None] = [str(Path(i).resolve()) for i in inputs] or [None]
    for k, inp in enumerate(plan):
        scm = out / ("capture.scm" if k == 0 else f"capture-{k}.scm")
        if scm.exists():
            raise PrepareError("run_dir_not_fresh", f"{scm} exists")
        before = None
        if k > 0 and rd.cache is not None and rd.cache.is_file():
            before = read_sc(rd.cache).header.record_count
        doc = launch(rd, timeout_s=timeout_s, extra_env={"QEMU_SC_CAPTURE": str(scm)},
                     prove=False, capture_files_ok=True, allow_rerun=k > 0,
                     run_json="run.json" if k == 0 else f"run-{k}.json",
                     inputs={input_bank: inp} if inp else None)
        log = Path(doc["stdout_log"]).read_text(errors="replace")
        rec: dict[str, Any] = {
            "k": k, "kind": "cold" if k == 0 else "warm_append", "input": inp,
            "run_json": str(out / ("run.json" if k == 0 else f"run-{k}.json")),
            "exit_code": doc["exit_code"], "timed_out": doc["timed_out"],
            "qemu": doc["qemu"], "qemu_sha256": doc["qemu_sha256"],
            "capture": str(scm), "capture_exists": scm.is_file(),
            "capture_size": scm.stat().st_size if scm.is_file() else None,
            "capture_banner": CAPTURE_BANNER in log,
            "records_before": before,
        }
        if rd.cache is not None and rd.cache.is_file():
            sc = read_sc(rd.cache)
            rec["records_after"] = sc.header.record_count
            rec["valid_records"] = len(sc.valid_slots)
        if doc["exit_code"] != 0:
            fails.append(f"run {k}: exit {doc['exit_code']}")
        if not rec["capture_banner"]:
            fails.append(f"run {k}: no '{CAPTURE_BANNER}' banner (QEMU without capture?)")
        if not rec["capture_exists"]:
            fails.append(f"run {k}: {scm} was not written")
        if not rec.get("records_after"):
            fails.append(f"run {k}: no cache records")
        runs.append(rec)
        if fails:
            break
    prep: dict[str, Any] = {
        "schema": PREP_SCHEMA, "base_toml": str(base), "base_toml_sha256": file_sha256(base),
        "out": str(out), "input_bank": input_bank, "runs": runs,
        "sc": None if rd.cache is None else str(rd.cache),
        "manifest": None if rd.manifest is None else str(rd.manifest),
        "captures": [r["capture"] for r in runs if r["capture_exists"]],
    }
    if rd.cache is not None and rd.cache.is_file():
        prep["sc_sha256"] = "sha256:" + sha256_sparse(rd.cache)
    if oracle and not fails:
        ord_ = create_run_dir(base, out / "oracle", quiet=quiet, qemu=qemu, label="prepare-oracle")
        try:
            orec = run_oracle(ord_, timeout_s=min(timeout_s, 300.0))
            # The fingerprint hashes the whole TOML, run-dir paths included, so it
            # differs from the prep run's; only the QEMU argv is used from here.
            prep["oracle"] = {"run_dir": str(ord_.root), "qemu": (orec.get("argv") or [None])[0],
                              "fingerprint": (orec.get("supercache") or {}).get("fingerprint")}
            if prep["oracle"]["qemu"] and runs and runs[0].get("qemu") and \
                    os.path.realpath(prep["oracle"]["qemu"]) != os.path.realpath(runs[0]["qemu"]):
                fails.append(f"oracle QEMU {prep['oracle']['qemu']} differs from the prep run's "
                             f"{runs[0]['qemu']}")
        except (DeployError, RunDirError) as e:
            fails.append(f"oracle: {e}")
        finally:
            # The standalone oracle never calls launch, which normally owns this cleanup.
            _cleanup_tmpfs_ram(ord_)
    prep["verdict"] = "PASS" if not fails else "FAIL"
    prep["failures"] = fails
    write_json(out / "prep.json", prep)
    return prep


def load(path: str | os.PathLike) -> Mapping[str, Any]:
    return json.loads(Path(path).read_text())


def run_dir(out: str | os.PathLike) -> RunDir:
    return RunDir.open(out)

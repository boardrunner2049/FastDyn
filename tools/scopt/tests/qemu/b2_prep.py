#!/usr/bin/env python3
"""F-B2-7 preparation (WP15C-PREP): fresh snapshot, profile and frozen candidates.

    b2_prep.py --workload compute|blinky --out OUT [--qemu QEMU] [--profile-reps 3]
               [--timeout 300]

Builds everything the F-B2-7 timing campaign deploys, from **train inputs
only** (compute) or the fixed input (blinky), with the integrated runtime.
Every step is a ``python -m scopt ...`` subprocess run from this worktree
(``OUT/cmds.jsonl``, output in ``OUT/logs/``), as in ``b1_gate.py``::

    prepare (train inputs) -> bundle identity / runtime-identity -> import
    -> profile run (phase steady at the fixture's window marker, --reps on every
       train input; exact counts) -> profile check
    -> optimize --pipeline identity
    -> optimize --pipeline b2-layout  (+ --disable P for every B2 pass)
    -> validate A (V1-V9, MANIFEST)  -> validate A --dynamic (V10) on the
       held-out inputs (compute) / the fixed input (blinky)
    -> report

The held-out inputs are used only by V10 (correctness), never by prepare or
the profile. Final inputs are never touched. ``OUT/prep_summary.json`` holds
every identity the campaign file freezes (snapshot, profile, artifacts and
their ``cache.sc`` sha256). No timing is measured here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

WT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WT))
from scopt.formats.sc_v5 import read_sc, sha256_sparse  # noqa: E402
from scopt.model.cache_snapshot import snapshot_dir_name  # noqa: E402

ROOT = Path("/data/extra_copy/fastdyn")
PY = sys.executable
INT_QEMU = ROOT / "wt/qemu-int/build/qemu-system-arm"
CP = ROOT / "fixtures/inputs"
PASSES = ("hotcold-partition", "cluster-hot", "order-cold")
WORKLOADS: dict[str, dict[str, Any]] = {
    "compute": {
        "config": ROOT / "runs/wp13-r2/compute/configs/compute-sc.toml",
        "train": [CP / f"train/freertos-mps2-compute/train-{i}.bin" for i in (1, 2, 3)],
        "heldout": [CP / f"heldout/freertos-mps2-compute/heldout-{i}.bin" for i in (1, 2, 3)],
        # vComputeWindowStart (benchmark_start), QUALIFICATION.md §2
        "phase": "steady@0x2990",
    },
    "blinky": {
        "config": ROOT / "runs/qint/configs/freertos-sc-int.toml",
        "train": [], "heldout": [],
        # prvQueueReceiveTask entry (benchmark_start, executed once)
        "phase": "steady@0x3018",
    },
}


def sha256_file(p: Path | str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return "sha256:" + h.hexdigest()


class Prep:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.cfg = WORKLOADS[a.workload]
        self.base = Path(self.cfg["config"])
        self.out = Path(a.out).resolve()
        if not a.retry_v10 and self.out.exists() and any(self.out.iterdir()):
            raise SystemExit(f"{self.out} exists and is not empty (fresh OUT required)")
        self.logs = self.out / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.n = len((self.out / "cmds.jsonl").read_text().splitlines()) \
            if (self.out / "cmds.jsonl").is_file() else 0
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = str(WT)
        self.store = self.out / "store"
        self.failures: list[str] = []

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} [{self.a.workload}] {msg}"
        print(line, flush=True)
        with open(self.out / "prep.log", "a") as f:
            f.write(line + "\n")

    def scopt(self, name: str, *args: Any) -> tuple[int, str, Path]:
        self.n += 1
        argv = [PY, "-m", "scopt", *[str(x) for x in args]]
        t0 = time.monotonic()
        p = subprocess.run(argv, cwd=str(WT), env=self.env, capture_output=True, text=True,
                           timeout=self.a.timeout * 60)
        dt = time.monotonic() - t0
        logp = self.logs / f"{self.n:02d}-{name}.out"
        logp.write_text(p.stdout + ("\n--- stderr ---\n" + p.stderr if p.stderr else ""))
        with open(self.out / "cmds.jsonl", "a") as f:
            f.write(json.dumps({"n": self.n, "name": name, "argv": argv[2:],
                                "rc": p.returncode, "seconds": round(dt, 3),
                                "log": str(logp)}) + "\n")
        self.log(f"scopt {' '.join(str(x) for x in args[:2])} ... rc={p.returncode} "
                 f"({dt:.1f}s)")
        return p.returncode, p.stdout, logp

    def need(self, ok: bool, what: str) -> None:
        if not ok:
            self.failures.append(what)
            self.log(f"FAIL: {what}")
            raise RuntimeError(what)

    # -- chain ------------------------------------------------------------------
    def prepare(self) -> dict[str, Any]:
        d = self.out / "prep"
        args = ["prepare", "-c", self.base, "--out", d, "--qemu", self.a.qemu,
                "--timeout", self.a.timeout]
        for i in self.cfg["train"]:
            args += ["--inputs", i]
        rc, _, _ = self.scopt("prepare", *args)
        prep = json.loads((d / "prep.json").read_text()) if (d / "prep.json").is_file() else {}
        self.need(rc == 0 and prep.get("verdict") == "PASS", f"prepare: {prep.get('failures')}")
        rc1, _, _ = self.scopt("bundle-identity", "bundle", "identity", "-c", d / "run.toml",
                               "--manifest", prep["manifest"], "--out", self.out / "bundle.json")
        rc2, _, _ = self.scopt("runtime-identity", "bundle", "runtime-identity", "--run-dir",
                               d / "oracle", "--sc", prep["sc"], "--scm", d / "capture.scm",
                               "--out", self.out / "runtime_identity.json")
        self.need(rc1 == 0 and rc2 == 0, "bundle identity / runtime-identity")
        return prep

    def import_(self, prep: dict[str, Any]) -> dict[str, Any]:
        rc, out, _ = self.scopt("import", "import", self.out / "prep", "--bundle",
                                self.out / "bundle.json", "--runtime-identity",
                                self.out / "runtime_identity.json", "--store", self.store,
                                "--json")
        self.need(rc == 0, "import")
        return json.loads(out)

    def profile(self, snap: Path, prep: dict[str, Any]) -> dict[str, Any]:
        d = self.out / "prof"
        args = ["profile", "run", "-c", self.base, "--snapshot", snap, "--reps",
                self.a.profile_reps, "--phase", self.cfg["phase"], "--out", d, "--store",
                self.store, "--qemu", self.a.qemu, "--timeout", self.a.timeout,
                "--watch", prep["sc"], "--watch", prep["manifest"], "--input-split", "train"]
        for i in self.cfg["train"]:
            args += ["--inputs", i]
        rc, _, _ = self.scopt("profile-run", *args)
        pr = json.loads((d / "profile_run.json").read_text()) \
            if (d / "profile_run.json").is_file() else {}
        self.need(rc == 0 and pr.get("verdict") == "PASS", f"profile run: {pr.get('failures')}")
        merged = Path(pr["merged"]["published"] or pr["merged"]["path"])
        rc, out, _ = self.scopt("profile-check", "profile", "check", merged)
        self.need(rc == 0, "profile check (flow conservation)")
        p = json.loads(merged.read_text())
        st: dict[str, int] = {}
        entries: dict[str, int] = {}
        for u in p["units"]:
            st[str(u.get("status"))] = st.get(str(u.get("status")), 0) + 1
            for ph, c in (u.get("phase") or {}).items():
                if isinstance(c, dict) and isinstance(c.get("entries"), int):
                    entries[ph] = entries.get(ph, 0) + c["entries"]
        return {"path": str(merged), "profile_id": pr["merged"]["profile_id"],
                "file_sha256": sha256_file(merged), "runs": pr["merged"]["runs"],
                "complete": pr["merged"]["complete"], "flow_ok": pr["merged"]["flow_ok"],
                "phase": self.cfg["phase"], "inputs": [str(i) for i in self.cfg["train"]],
                "reps_per_input": self.a.profile_reps,
                "per_run": [{k: r.get(k) for k in ("name", "verdict", "appended",
                                                    "total_dispatches", "profile_only",
                                                    "flow_ok")} for r in pr["runs"]],
                "unit_status_counts": st, "entries_per_phase": entries,
                "phases_doc": p.get("phases"),
                "isolation_ok": (pr.get("isolation") or {}).get("ok"),
                "profile_run_json": str(d / "profile_run.json")}

    def candidate(self, snap: Path, profile: str, label: str, pipeline: str,
                  disable: str | None) -> dict[str, Any]:
        args = ["optimize", snap, "--pipeline", pipeline, "--store", self.store,
                "--profile", profile, "--json"]
        if disable:
            args += ["--disable", disable]
        rc, out, logp = self.scopt(f"optimize-{label}", *args)
        row: dict[str, Any] = {"label": label, "pipeline": pipeline, "disable": disable,
                               "optimize_rc": rc, "optimize_log": str(logp)}
        try:
            doc = json.loads(out)
        except ValueError:
            row["error"] = "optimize output is not JSON"
            return row
        row.update(status=doc["status"], reason=doc["reason"], artifact_id=doc["artifact_id"],
                   artifact_dir=doc["artifact_dir"], validation=doc["validation"])
        if doc["status"] != "published":
            return row
        art = Path(doc["artifact_dir"])
        side = json.loads((art / "cache.sc.scopt.json").read_text())
        row["x_candidate_id"] = side.get("x_candidate_id")
        row["cache_sc_sha256"] = "sha256:" + sha256_sparse(art / "cache.sc")
        row["manifest_sha256_file"] = sha256_file(art / "MANIFEST.sha256")
        row["moved"] = len(side["move_map"])
        pinned = {p["unit"] for p in side["pinned"]}
        row["pinned"] = len(pinned)
        row["pinned_moved"] = sorted(set(side["move_map"]) & pinned)
        row["profile_ids"] = side.get("profile_ids")
        row["pipeline_outcomes"] = [{"pass": e.get("pass"), "outcome": e.get("outcome"),
                                     "reason": e.get("reason")}
                                    for e in side.get("pipeline", [])]
        loc = side.get("x_locality") or {}
        row["locality_status"] = loc.get("status")
        if loc.get("status") == "ok":
            row["locality"] = {s: {
                "coverage": [{k: c.get(k) for k in ("coverage", "units", "hot_bytes",
                                                     "hot_code_bytes", "lines64",
                                                     "lines64_code", "pages4k",
                                                     "pages4k_code", "span_bytes")}
                             for c in (loc.get(s) or {}).get("coverage", [])],
                "edges": (loc.get(s) or {}).get("edges")} for s in ("input", "candidate")}
        sc = read_sc(art / "cache.sc")
        st = os.stat(art / "cache.sc")
        row["sizes"] = {"code_size": sc.header.code_size, "logical_size": st.st_size,
                        "allocated_bytes": st.st_blocks * 512, "records": len(sc.valid_slots),
                        "populated_native_bytes": sum(r.end_offset - r.tc_offset
                                                      for r in sc.records if r)}
        rc, out, logp = self.scopt(f"validate-{label}", "validate", art, "--json")
        try:
            v = json.loads(out)
            row["revalidate"] = {"rc": rc, "verdict": v["verdict"], "codes": v["codes"],
                                 "stored_codes_agree": v.get("stored_codes_agree")}
        except ValueError:
            row["revalidate"] = {"rc": rc, "error": "not JSON", "log": str(logp)}
        row["v10"] = self.v10(art, label)
        self.judge(row)
        return row

    def v10(self, art: Path, label: str) -> dict[str, Any]:
        """``scopt validate A --dynamic`` (V10) into the fresh ``OUT/v10/<label>``."""
        dargs = ["validate", art, "--dynamic", "-c", self.base, "--out",
                 self.out / "v10" / label, "--qemu", self.a.qemu, "--timeout", self.a.timeout,
                 "--json"]
        for i in self.cfg["heldout"]:
            dargs += ["--inputs", i]
        rc, out, logp = self.scopt(f"v10-{label}", *dargs)
        try:
            d = json.loads(out)
            return {"rc": rc, "verdict": d["verdict"], "pairs": len(d["runs"]),
                    "appended": d["appended"], "failures": d["failures"],
                    "inputs": [str(i) for i in self.cfg["heldout"]] or ["fixed"],
                    "dynamic_json": str(self.out / "v10" / label / "dynamic.json")}
        except ValueError:
            return {"rc": rc, "error": "not JSON", "log": str(logp)}

    @staticmethod
    def judge(row: dict[str, Any]) -> None:
        v = row["validation"] or {}
        row["ok"] = (row.get("status") == "published"
                     and all(v.get(f"V{i}") == "pass" for i in range(1, 9))
                     and v.get("V9") in ("pass", "skip")
                     and row["revalidate"].get("verdict") == "accepted"
                     and row["v10"].get("verdict") == "pass"
                     and not row["pinned_moved"])

    def retry_v10(self, label: str) -> int:
        """Re-run V10 of candidate ``label`` after a failed attempt whose cause was
        outside the artifact (e.g. the *baseline* run could not map its arena).
        The failed attempt stays in the summary (``v10_attempts``) and in the
        artifact's ledger; only a passing retry makes the candidate ok."""
        sp = self.out / "prep_summary.json"
        summary = json.loads(sp.read_text())
        row = next(c for c in summary["candidates"] if c["label"] == label)
        attempts = row.setdefault("v10_attempts", [row["v10"]])
        k = len(attempts)
        new = self.v10(Path(row["artifact_dir"]), f"{label}-retry{k}")
        new["retry_reason"] = self.a.retry_reason
        attempts.append(new)
        row["v10"] = new
        self.judge(row)
        self.failures = [f"candidate {c['label']} not ok" for c in summary["candidates"]
                         if not c.get("ok")]
        rc, _, _ = self.scopt("report-after-retry", "report", self.out, "--out",
                              self.out / "report" / "report.md", "--profile",
                              summary["profile"]["path"])
        summary["report_rc"] = rc
        summary["failures"] = self.failures
        summary["verdict"] = "pass" if not self.failures else "fail"
        summary.setdefault("retries", []).append(
            {"label": label, "attempt": k, "verdict": new.get("verdict"),
             "reason": self.a.retry_reason, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        sp.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        self.log(f"V10 retry {label} #{k}: {new.get('verdict')}; verdict {summary['verdict']}")
        return 0 if summary["verdict"] == "pass" else 1

    def run(self) -> int:
        t0 = time.monotonic()
        summary: dict[str, Any] = {"schema": "scopt.b2_prep/1", "workload": self.a.workload,
                                   "out": str(self.out), "base_config": str(self.base),
                                   "base_config_sha256": sha256_file(self.base),
                                   "qemu": self.a.qemu, "qemu_sha256": sha256_file(self.a.qemu),
                                   "train_inputs": {str(i): sha256_file(i)
                                                    for i in self.cfg["train"]},
                                   "heldout_inputs_v10": {str(i): sha256_file(i)
                                                          for i in self.cfg["heldout"]},
                                   "final_inputs_used": [], "started": time.time()}
        try:
            prep = self.prepare()
            summary["prep"] = {"sc": prep["sc"], "sc_sha256": prep["sc_sha256"],
                               "manifest": prep["manifest"],
                               "runs": [{k: r.get(k) for k in ("k", "kind", "input",
                                                                "records_after", "exit_code",
                                                                "qemu_sha256")}
                                        for r in prep["runs"]]}
            imp = self.import_(prep)
            snap = self.store / "snapshots" / snapshot_dir_name(imp["snapshot_id"])
            summary["snapshot"] = {
                "snapshot_id": imp["snapshot_id"], "dir": str(snap),
                "cache_sc_sha256": "sha256:" + sha256_sparse(snap / "cache.sc"),
                "records": imp["records"]["valid"], "binding": imp["capture"]["binding"],
                "status": imp["status"], "pin_reasons": imp["pin_reasons"],
                "bundle_id": imp.get("bundle_id"),
                "runtime_identity_id": imp.get("runtime_identity_id")}
            sc = read_sc(snap / "cache.sc")
            st = os.stat(snap / "cache.sc")
            summary["snapshot"]["sizes"] = {
                "code_size": sc.header.code_size, "logical_size": st.st_size,
                "allocated_bytes": st.st_blocks * 512,
                "populated_native_bytes": sum(r.end_offset - r.tc_offset
                                              for r in sc.records if r)}
            summary["snapshot"]["icount_units"] = sum(
                1 for r in sc.records if r and (r.cflags & 0x2000))  # CF_USE_ICOUNT
            prof = self.profile(snap, prep)
            summary["profile"] = prof
            cands = [self.candidate(snap, prof["path"], "identity", "identity", None),
                     self.candidate(snap, prof["path"], "b2-layout", "b2-layout", None)]
            for p in PASSES:
                cands.append(self.candidate(snap, prof["path"], f"no-{p}", "b2-layout", p))
            summary["candidates"] = cands
            ident = cands[0]
            summary["identity_equals_warm"] = (ident.get("cache_sc_sha256")
                                               == summary["snapshot"]["cache_sc_sha256"])
            for c in cands:
                c["cache_sc_equals_warm"] = (c.get("cache_sc_sha256")
                                             == summary["snapshot"]["cache_sc_sha256"])
            for c in cands:
                if not c.get("ok"):
                    self.failures.append(f"candidate {c['label']} not ok")
        except RuntimeError:
            pass
        rc, _, _ = self.scopt("report", "report", self.out, "--out",
                              self.out / "report" / "report.md",
                              *(["--profile", summary["profile"]["path"]]
                                if summary.get("profile") else []))
        summary["report"] = str(self.out / "report" / "report.md")
        summary["report_rc"] = rc
        summary["failures"] = self.failures
        summary["verdict"] = "pass" if not self.failures else "fail"
        summary["duration_s"] = round(time.monotonic() - t0, 1)
        (self.out / "prep_summary.json").write_text(json.dumps(summary, indent=2,
                                                               sort_keys=True) + "\n")
        self.log(f"verdict {summary['verdict']} ({summary['duration_s']} s)")
        return 0 if summary["verdict"] == "pass" else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workload", choices=sorted(WORKLOADS), required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--qemu", default=str(INT_QEMU))
    ap.add_argument("--profile-reps", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--retry-v10", default=None, metavar="LABEL",
                    help="re-run V10 of one candidate of an existing OUT (keeps the failed "
                         "attempt on record); needs --retry-reason")
    ap.add_argument("--retry-reason", default=None)
    a = ap.parse_args(argv)
    if a.retry_v10:
        if not a.retry_reason:
            ap.error("--retry-v10 needs --retry-reason")
        return Prep(a).retry_v10(a.retry_v10)
    return Prep(a).run()


if __name__ == "__main__":
    sys.exit(main())

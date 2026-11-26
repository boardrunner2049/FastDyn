#!/usr/bin/env python3
"""WP11 real-data acceptance: ``b2-layout`` and its ablations on a real store.

    wp11_b2_real.py --workload minimal|compute --store STORE --snapshot SID \\
                    --profile PROFILE.json --out OUT [--qemu QEMU] [--timeout 300]

``STORE`` must be a *copy* of a B1 gate store (the gate's own stores stay
read-only); the driver checks that the source gate store, if given with
``--gate-store``, is byte-identical before and after.

Every step is a ``python -m scopt ...`` subprocess run from this worktree
(``OUT/cmds.jsonl``, output in ``OUT/logs/``), as in ``b1_gate.py``. For each
candidate (``b2-layout`` and ``--disable`` of each of its passes):

1. ``scopt optimize S --pipeline b2-layout [--disable P] --profile PROFILE``;
2. ``scopt validate A`` (static re-validation, V1-V9 and MANIFEST);
3. ``scopt validate A --dynamic`` (V10) on the held-out inputs (compute) or the
   fixed input (minimal);

then ``scopt report OUT`` (ledger, locality model). The report model
(``report.json``) is checked against each artifact's own ``ledger.jsonl``: every
pass row is scoped to its artifact and shows that artifact's verdicts (no row
mixes candidates). With ``--boot-profile`` (a profile without the layout phase,
e.g. the B1 gate's boot-only profile), ``b2-layout`` is optimized again with
both profiles in both orders: the move map must equal the single-profile one
and ``x_locality`` must use the phase profile. Results: ``OUT/wp11.json``.
No timing is measured here.
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
from scopt.formats.sc_records import sha256_sparse  # noqa: E402

ROOT = Path(os.environ.get("FASTDYN_ROOT") or Path(__file__).resolve().parents[4])
PY = sys.executable
INT_QEMU = ROOT / "wt/qemu-int/build/qemu-system-arm"
CP = ROOT / "fixtures/inputs"
WORKLOADS: dict[str, dict[str, Any]] = {
    "minimal": {"config": ROOT / "src/fastdyn/configs/b0-sc.toml", "heldout": []},
    "compute": {"config": ROOT / "runs/wp13-r2/compute/configs/compute-sc.toml",
                "heldout": [CP / f"heldout/freertos-mps2-compute/heldout-{i}.bin"
                            for i in (1, 2, 3)]},
}
PASSES = ("hotcold-partition", "cluster-hot", "order-cold")


def tree_digest(d: Path) -> dict[str, str]:
    """Path -> sha256 (sparse-aware for .sc) of every file under ``d``."""
    out = {}
    for p in sorted(d.rglob("*")):
        if p.is_file():
            if p.suffix == ".sc":
                out[str(p.relative_to(d))] = sha256_sparse(p)
            else:
                out[str(p.relative_to(d))] = "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class Driver:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.out = Path(a.out)
        self.logs = self.out / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.n = len((self.out / "cmds.jsonl").read_text().splitlines()) \
            if (self.out / "cmds.jsonl").is_file() else 0
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = str(WT)
        self.cfg = WORKLOADS[a.workload]

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} [{self.a.workload}] {msg}"
        print(line, flush=True)
        with open(self.out / "wp11.log", "a") as f:
            f.write(line + "\n")

    def scopt(self, name: str, *args: Any) -> tuple[int, str, Path]:
        self.n += 1
        argv = [PY, "-m", "scopt", *[str(x) for x in args]]
        t0 = time.monotonic()
        p = subprocess.run(argv, cwd=str(WT), env=self.env, capture_output=True, text=True,
                           timeout=self.a.timeout * 40)
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

    def candidate(self, label: str, disable: str | None) -> dict[str, Any]:
        a = self.a
        args = ["optimize", Path(a.store) / "snapshots" / a.snapshot, "--pipeline", "b2-layout",
                "--store", a.store, "--profile", a.profile, "--json"]
        if disable:
            args += ["--disable", disable]
        rc, out, logp = self.scopt(f"optimize-{label}", *args)
        row: dict[str, Any] = {"label": label, "disable": disable, "optimize_rc": rc,
                               "optimize_log": str(logp)}
        try:
            doc = json.loads(out)
        except ValueError:
            row["error"] = "optimize output is not JSON"
            return row
        row.update(status=doc["status"], reason=doc["reason"], artifact_id=doc["artifact_id"],
                   artifact_dir=doc["artifact_dir"], validation=doc["validation"],
                   link=doc["link"], notes=doc.get("notes"))
        if doc["status"] != "published":
            return row
        art = Path(doc["artifact_dir"])
        side = json.loads((art / "cache.sc.scopt.json").read_text())
        row["pipeline"] = side["pipeline"]
        row["moved"] = len(side["move_map"])
        pinned = {p["unit"] for p in side["pinned"]}
        row["pinned"] = len(pinned)
        row["pinned_moved"] = sorted(set(side["move_map"]) & pinned)
        row["profile_ids"] = side["profile_ids"]
        loc = side.get("x_locality") or {}
        row["locality_status"] = loc.get("status")
        row["locality"] = {side_: {
            "coverage": [{k: c[k] for k in ("coverage", "units", "entries", "hot_bytes",
                                             "hot_code_bytes", "lines64", "lines64_code",
                                             "pages4k", "pages4k_code", "span_bytes")}
                         for c in (loc.get(side_) or {}).get("coverage", [])],
            "executed": (loc.get(side_) or {}).get("executed"),
            "edges": (loc.get(side_) or {}).get("edges"),
            "non_exact": (loc.get(side_) or {}).get("non_exact")}
            for side_ in ("input", "candidate")} if loc.get("status") == "ok" else None
        # static re-validation
        rc, out, logp = self.scopt(f"validate-{label}", "validate", art, "--json")
        try:
            v = json.loads(out)
            row["revalidate"] = {"rc": rc, "verdict": v["verdict"], "codes": v["codes"],
                                 "stored_codes_agree": v.get("stored_codes_agree")}
        except ValueError:
            row["revalidate"] = {"rc": rc, "error": "not JSON", "log": str(logp)}
        # V10
        dargs = ["validate", art, "--dynamic", "-c", self.cfg["config"], "--out",
                 self.out / "v10" / label, "--qemu", a.qemu, "--timeout", a.timeout, "--json"]
        for i in self.cfg["heldout"]:
            dargs += ["--inputs", i]
        rc, out, logp = self.scopt(f"v10-{label}", *dargs)
        try:
            d = json.loads(out)
            row["v10"] = {"rc": rc, "verdict": d["verdict"], "pairs": len(d["runs"]),
                          "appended": d["appended"], "failures": d["failures"],
                          "inputs": [str(i) for i in self.cfg["heldout"]] or ["fixed"]}
        except ValueError:
            row["v10"] = {"rc": rc, "error": "not JSON", "log": str(logp)}
        return row

    def check_report(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """The report's pass rows vs each artifact's own ledger.jsonl."""
        model = json.loads((self.out / "report" / "report.json").read_text())
        led = model["ledger"]
        pbs = led.get("passes_by_scope") or {}
        problems = []
        per = {}
        for r in rows:
            aid = r.get("artifact_id")
            if not aid:
                continue
            own = [json.loads(x) for x in (Path(r["artifact_dir"]) / "ledger.jsonl")
                   .read_text().splitlines() if x.strip()]
            want = [(x["id"], x["verdict"], x["reason"]) for x in own if x["subject"] == "pass"]
            got = [(pid, c["verdict"], c["reason"])
                   for pid, c in (pbs.get(aid) or {}).get("passes", [])]
            per[r["label"]] = got
            if got != want:
                problems.append({"label": r["label"], "want": want, "got": got})
        multi = [(t["id"], t.get("scope")) for t in led["table"] if t["subject"] == "pass"
                 and any(c.get("rows", 0) > 1 for c in t["stages"].values())]
        unscoped = [t["id"] for t in led["table"] if t["subject"] == "pass" and not t.get("scope")]
        if multi:
            problems.append({"pass_cells_with_several_rows": multi})
        if unscoped:
            problems.append({"unscoped_pass_rows": unscoped})
        return {"ok": not problems, "problems": problems, "pass_outcomes": per}

    def two_profiles(self, full: dict[str, Any]) -> dict[str, Any]:
        """b2-layout with (boot-only, steady) and (steady, boot-only) profiles."""
        a = self.a
        out: dict[str, Any] = {"boot_profile": a.boot_profile, "runs": {}}
        ok = True
        want = json.loads((Path(full["artifact_dir"]) / "cache.sc.scopt.json").read_text())
        for name, profs in (("boot-first", (a.boot_profile, a.profile)),
                            ("steady-first", (a.profile, a.boot_profile))):
            args = ["optimize", Path(a.store) / "snapshots" / a.snapshot, "--pipeline",
                    "b2-layout", "--store", a.store, "--json"]
            for p in profs:
                args += ["--profile", p]
            rc, txt, logp = self.scopt(f"optimize-2prof-{name}", *args)
            row: dict[str, Any] = {"rc": rc, "log": str(logp)}
            try:
                doc = json.loads(txt)
                side = json.loads((Path(doc["artifact_dir"]) / "cache.sc.scopt.json")
                                  .read_text())
                pp = {p["pass"]: p for p in side["pipeline"]}
                hc = pp.get("hotcold-partition") or {}
                row.update(status=doc["status"], artifact_id=doc["artifact_id"],
                           validation=doc["validation"],
                           hotcold=(hc.get("outcome"), hc.get("reason")),
                           sidecar_profile_ids=side.get("profile_ids"),
                           x_locality_profile=(side.get("x_locality") or {}).get("profile_id"),
                           same_move_map=side["move_map"] == want["move_map"])
                row["ok"] = (doc["status"] == "published" and hc.get("outcome") == "candidate"
                             and row["same_move_map"]
                             and row["x_locality_profile"] == (want.get("x_locality") or {})
                             .get("profile_id"))
            except (ValueError, KeyError, OSError) as e:
                row.update(ok=False, error=f"{type(e).__name__}: {e}")
            ok = ok and row["ok"]
            out["runs"][name] = row
        out["ok"] = ok
        return out

    def run(self) -> int:
        a = self.a
        gate_before = tree_digest(Path(a.gate_store)) if a.gate_store else None
        rows = [self.candidate("b2-layout", None)]
        for p in PASSES:
            rows.append(self.candidate(f"no-{p}", p))
        rc, _, logp = self.scopt("report", "report", self.out, "--out",
                                 self.out / "report" / "report.md", "--profile", a.profile)
        report_check = self.check_report(rows)
        two_prof = self.two_profiles(rows[0]) if a.boot_profile else None
        gate_after = tree_digest(Path(a.gate_store)) if a.gate_store else None
        ok_rows = []
        for r in rows:
            v = r.get("validation") or {}
            ok = (r.get("status") == "published"
                  and all(v.get(f"V{i}") == "pass" for i in range(1, 9))
                  and v.get("V9") in ("pass", "skip")
                  and (r.get("revalidate") or {}).get("verdict") == "accepted"
                  and (r.get("v10") or {}).get("verdict") == "pass"
                  and not r.get("pinned_moved"))
            r["ok"] = ok
            ok_rows.append(ok)
        doc = {"schema": "scopt.wp11_real/1", "workload": a.workload, "store": a.store,
               "snapshot": a.snapshot, "profile": a.profile, "qemu": a.qemu,
               "gate_store": a.gate_store,
               "gate_store_unchanged": None if gate_before is None else gate_before == gate_after,
               "gate_store_files": None if gate_before is None else len(gate_before),
               "report_rc": rc, "report_ledger_check": report_check,
               "two_profiles": two_prof, "candidates": rows,
               "verdict": "pass" if all(ok_rows) and gate_before == gate_after
               and report_check["ok"] and (two_prof is None or two_prof["ok"]) else "fail"}
        (self.out / "wp11.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        self.log(f"verdict {doc['verdict']}")
        return 0 if doc["verdict"] == "pass" else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workload", choices=sorted(WORKLOADS), required=True)
    ap.add_argument("--store", required=True)
    ap.add_argument("--snapshot", required=True, help="SnapshotId hex (directory name)")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--gate-store", default=None,
                    help="the read-only source store; checked unchanged")
    ap.add_argument("--boot-profile", default=None,
                    help="a profile without the layout phase, for the two-profile check")
    ap.add_argument("--qemu", default=str(INT_QEMU))
    ap.add_argument("--timeout", type=float, default=300.0)
    return Driver(ap.parse_args(argv)).run()


if __name__ == "__main__":
    sys.exit(main())

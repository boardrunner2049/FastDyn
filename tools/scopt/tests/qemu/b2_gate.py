#!/usr/bin/env python3
"""B2 fixtures driver (design §7, WP15A): F-B2-1, F-B2-2, F-B2-4, F-B2-5, F-B2-6 end to end.

    b2_gate.py --workload minimal|fault|compute --out OUT [--qemu QEMU] [--reps 2]
               [--profile-reps 1] [--timeout 300] [--fixtures F-B2-1,F-B2-2,...]

Modelled on ``b1_gate.py``: every step is a ``python -m scopt ...`` subprocess
run from this worktree (``OUT/cmds.jsonl``, output in ``OUT/logs/``), and the
checks read the files those commands wrote. All QEMU runs use the integrated
runtime (``wt/qemu-int/build/qemu-system-arm``; the harness launches through
``setarch x86_64 -R`` with a timeout, in fresh run directories with their own
cache, work and RAM, ``supercache_flat=false``). No timing is measured.

Workloads:

* ``minimal``  ``src/fastdyn/configs/b0-sc.toml``, phase ``steady@0x08003972``;
* ``fault``    ``fixtures/fault/fault_fixture.elf`` (variants a-d in one image),
               rendered from ``fault-fixture.toml.in`` like ``fdrun.py --arm
               sc-noicount`` (no icount: design §9 keeps CF_USE_ICOUNT units out of
               the F-B2 gates), phase ``steady@0x58`` (``fault_bench_start``). Guest
               output is also compared with ``fixtures/fault/expected/fixture.norm``
               (stacked PCs equal to the ELF addresses);
* ``compute``  F-B2-6 only, on a sparse copy of the B1 gate compute snapshot
               (train-1) with the held-out input ``heldout-1`` (budget-cut TBs are
               appended naturally) and the WP11 steady profile.

Chain for minimal and fault: prepare (capture on) -> bundle identity /
runtime-identity -> import -> profile run (phases) -> fixtures -> report.

Fixtures (design §7; each one's checks and evidence paths go to ``OUT/b2-gate.json``):

* F-B2-1  ``move-units@1``: the hottest unit (minimal: hottest relocatable unit by
          all-phase entries; fault: hottest steady-phase unit) to
          ``ROUND_UP(max_end, 64)`` and to that + 4 MiB. V1-V9 rows, an independent
          capstone diff (changed bytes and instructions only at the declared
          prologue/ext fixup fields; external targets unchanged, intra targets
          shifted by d), V10 (equal outputs, appended 0, proof PASS), a profile of
          the artifact (``profile run --artifact``; the raw ``.qprof`` row at the new
          tb_offset carries the unit's executions, nothing at the old one), and
          ``diag exec`` (Trace lines at the new ``tc.ptr``, none at the old one).
* F-B2-2  ``reverse-layout@1`` (+ ``move-units@1`` for the mirror's fixed points):
          every relocatable unit moves; every fixup class is applied (counts, each
          field value checked as ``v - d`` / ``v + d``); slot order and the LIFO
          winner of every duplicate key are unchanged; V10 equal outputs; on the
          F-B2-4 tampered snapshot the pinned unit is untouched. The synthetic
          duplicate-key test is ``tests/acceptance/test_b2_synthetic.py``.
* F-B2-4  one HELPER_CALL site of the hottest unit that has one is removed from the
          capture; the unit is pinned (``unexplained_reference``); ``b2-layout``
          (and ``--disable cluster-hot``) reports
          ``skipped_unsupported:unexplained_reference:<uid>`` in ``affected``; the
          artifact is valid (V1-V8, re-validation) and runs (V10).
* F-B2-5  a unit with a plugin call forced to ``tb_offset 0x60000000``: the linker
          rejects the placement with ``rel32_range`` and rolls it back (artifact ==
          baseline, V9; sidecar pinned ``failed:rel32_range``; V10); with its
          original slot taken by another explicit placement the candidate is
          rejected with provenance ``failed:rel32_range`` and nothing is published
          (store and snapshot unchanged: the baseline is retained).
* F-B2-6  a record appended by a capture-off warm run is a ``no_meta`` unit; it
          stays fixed under ``reverse-layout`` and ``b2-layout`` (skipped as
          ``skipped_unsupported:no_meta``), a PLACE onto its interval is rolled
          back (``place_overlap``), a PLACE of the unit itself is refused; V10
          equal outputs. Minimal and fault have no input bank, so the appended
          record comes from a snapshot without its last K records (as in T-B1-4/7);
          compute uses the held-out input.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

WT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WT))
from scopt.formats.sc_v5 import read_sc, sha256_sparse, sparse_copy, write_sc  # noqa: E402
from scopt.model.cache_snapshot import snapshot_dir_name  # noqa: E402

ROOT = Path("/data/extra_copy/fastdyn")
PY = sys.executable
INT_QEMU = ROOT / "wt/qemu-int/build/qemu-system-arm"
INT_QEMU_SHA = "sha256:8864c72671971cf4488b5afa76eeee02a3e2a472c45af81ec1314464e6035f41"
FAULT = ROOT / "fixtures/fault"
CP = ROOT / "fixtures/inputs"
PIPES = WT / "tests/data/pipelines"
FIXTURES = ["F-B2-1", "F-B2-2", "F-B2-4", "F-B2-5", "F-B2-6"]
F5_TARGET = 0x60000000

WORKLOADS: dict[str, dict[str, Any]] = {
    "minimal": {"config": ROOT / "src/fastdyn/configs/b0-sc.toml",
                "phases": ["steady@0x08003972"], "hot_rule": "relocatable_total",
                "expected_norm": None, "fixtures": FIXTURES, "trunc_k": 2, "inputs": []},
    "fault": {"template": FAULT / "fault-fixture.toml.in", "cache_name": "fault-fixture.sc",
              "phases": ["steady@0x58"], "hot_rule": "steady",
              "expected_norm": FAULT / "expected/fixture.norm", "fixtures": FIXTURES,
              "trunc_k": 2, "inputs": []},
    "compute": {"config": ROOT / "runs/wp13-r2/compute/configs/compute-sc.toml",
                "gate": ROOT / "runs/wp14/gate/compute",
                "profile": ROOT / "runs/wp11/r2/compute/store/profiles/"
                "3a6a37c7f2acb6ca476a5b2b14e5d0dd4f090b65588bf1e9ff8d9606f9722a57/profile.json",
                "phases": [], "hot_rule": "steady", "expected_norm": None,
                "fixtures": ["F-B2-6"], "trunc_k": 0,
                "inputs": [CP / "heldout/freertos-mps2-compute/heldout-1.bin"]},
}
_GUEST_LINE = re.compile(r"^(Current CPU type|Reg [0-9]+:|\[BENCH\]|qemu-system-arm:)")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return "sha256:" + h.hexdigest()


def tree_digest(d: Path) -> dict[str, str]:
    out = {}
    for p in sorted(Path(d).rglob("*")):
        if p.is_file():
            out[str(p.relative_to(d))] = (sha256_sparse(p) if p.suffix == ".sc"
                                          else sha256_file(p))
    return out


def _reloc(snap, uid: str) -> bool:
    """``ok_relocatable`` (CacheSnapshot.status): relocatable, metadata ok, not pinned."""
    return snap.status(uid) == "ok_relocatable"


def round_up(v: int, a: int) -> int:
    return (v + a - 1) // a * a


def _signed(b: bytes) -> int:
    return int.from_bytes(b, "little", signed=True)


def _unsigned(b: bytes) -> int:
    return int.from_bytes(b, "little", signed=False)


def lifo_winners(sc) -> dict[Any, str]:
    """Key -> UnitId of the record QEMU keeps for it: records are loaded in slot
    order and a later duplicate replaces an earlier one (design §4.7, LIFO by slot)."""
    win: dict[Any, str] = {}
    for uid, u in zip(sc.unit_ids(), sc.units()):
        win[u.record.key] = uid
    return win


def duplicate_keys(sc) -> dict[Any, list[str]]:
    by: dict[Any, list[str]] = collections.defaultdict(list)
    for uid, u in zip(sc.unit_ids(), sc.units()):
        by[u.record.key].append(uid)
    return {k: v for k, v in by.items() if len(v) > 1}


# ---------------------------------------------------------------------------
# independent checks on artifact bytes
# ---------------------------------------------------------------------------

def capstone_diff(u, new_tb: int, new_body: bytes, code_address: int) -> dict[str, Any]:
    """Compare a unit's input bytes with its relocated bytes, independently of V2/V3.

    Both host code ranges are swept with capstone (meta-less sweep up to the
    constant pool) at their own absolute addresses. Requirements: equal length,
    the same instruction tiling and mnemonics, every changed byte inside a
    declared fixup field, every changed instruction overlapping a fixup field,
    every direct branch / RIP-relative target outside the unit unchanged and every
    target inside the unit (own struct, pool, intra labels) shifted by d.
    """
    from scopt.decode.x86 import bad_insns, sweep_to_pool

    d = new_tb - u.tb_offset
    old_body = u.code + u.search
    fields: dict[int, Any] = {}
    for f in u.reloc.fixups:
        for i in range(f.field_off, f.field_off + f.width):
            fields[i] = f
    diff = [i for i, (a, b) in enumerate(zip(old_body, new_body)) if a != b]
    outside = [i for i in diff if i not in fields]
    old_tc = code_address + u.tc_offset
    new_tc = old_tc + d
    oi, opool = sweep_to_pool(u.code, old_tc)
    ni, npool = sweep_to_pool(bytes(new_body[:len(u.code)]), new_tc)
    tiling = (len(oi) == len(ni) and opool - old_tc == npool - new_tc
              and all(a.size == b.size and a.mnemonic == b.mnemonic for a, b in zip(oi, ni)))
    lo, hi = code_address + u.tb_offset, code_address + u.end_offset
    changed_insns, changed_no_field, ext_same, ext_bad, intra_shift, intra_bad = 0, [], 0, [], 0, []
    classes: collections.Counter = collections.Counter()
    for a, b in zip(oi, ni):
        span = range(a.address - old_tc, a.end - old_tc)
        fx = {fields[i].target_class for i in span if i in fields}
        if a.raw != b.raw:
            changed_insns += 1
            if not fx:
                changed_no_field.append(f"{a.address - old_tc:#x} {a.text()} -> {b.text()}")
            for c in fx:
                classes[c] += 1
        for ta, tb_ in ((a.branch_target, b.branch_target), (a.rip_target, b.rip_target)):
            if ta is None:
                continue
            if lo <= ta < hi:
                if tb_ == ta + d:
                    intra_shift += 1
                else:
                    intra_bad.append(f"{a.address - old_tc:#x} {a.text()} | {b.text()}")
            elif tb_ == ta:
                ext_same += 1
            else:
                ext_bad.append(f"{a.address - old_tc:#x} {a.text()} | {b.text()}")
    ok = (len(old_body) == len(new_body) and not outside and tiling and not changed_no_field
          and not ext_bad and not intra_bad and len(bad_insns(oi)) == len(bad_insns(ni))
          and set(c.split(".")[0] for c in classes) <= {"prologue", "ext"})
    return {"ok": ok, "delta": d, "insns": len(oi), "bad_insns": len(bad_insns(oi)),
            "tiling_equal": tiling, "changed_bytes": len(diff),
            "changed_bytes_outside_fixups": outside[:10], "changed_insns": changed_insns,
            "changed_insns_by_fixup_class": dict(classes),
            "changed_insns_without_fixup": changed_no_field[:5],
            "external_targets_unchanged": ext_same, "external_targets_changed": ext_bad[:5],
            "intra_targets_shifted_by_d": intra_shift, "intra_targets_wrong": intra_bad[:5],
            "fixup_fields": len(u.reloc.fixups),
            "fixup_classes": dict(collections.Counter(f.target_class for f in u.reloc.fixups))}


def fixup_values(u, new_body: bytes, d: int) -> tuple[collections.Counter, list[str]]:
    """Per target class, the fixups whose new field value is exactly ``v - d``
    (rel_minus_delta) or ``v + d`` (abs_plus_delta); mismatches listed."""
    old = u.code + u.search
    ok: collections.Counter = collections.Counter()
    bad = []
    for f in u.reloc.fixups:
        ob = old[f.field_off:f.field_off + f.width]
        nb = new_body[f.field_off:f.field_off + f.width]
        if f.rule == "rel_minus_delta":
            good = _signed(nb) == _signed(ob) - d
        elif f.rule == "abs_plus_delta":
            good = _unsigned(nb) == (_unsigned(ob) + d) % (1 << (8 * f.width))
        else:
            good = nb == ob
        if good:
            ok[f.target_class] += 1
        else:
            bad.append(f"{u.uid} {f.target_class}@{f.field_off:#x}")
    return ok, bad


# ---------------------------------------------------------------------------
# result plumbing
# ---------------------------------------------------------------------------

class Checks:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.evidence: list[str] = []
        self.notes: list[str] = []

    def add(self, name: str, ok: bool, detail: Any = "") -> bool:
        self.rows.append({"check": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    def ev(self, *paths: Any) -> None:
        self.evidence += [str(p) for p in paths if p]

    def result(self, *, not_run: str | None = None) -> dict[str, Any]:
        if not_run is not None:
            verdict = "not_run"
        else:
            verdict = "pass" if self.rows and all(r["ok"] for r in self.rows) else "fail"
        return {"verdict": verdict, "not_run_reason": not_run, "checks": self.rows,
                "failed": [r["check"] for r in self.rows if not r["ok"]],
                "evidence": list(dict.fromkeys(self.evidence)), "notes": self.notes}


class B2Gate:
    def __init__(self, workload: str, out: Path, qemu: Path, reps: int, profile_reps: int,
                 timeout: float):
        self.wl = workload
        self.cfg = WORKLOADS[workload]
        self.out = out
        self.qemu = qemu
        self.reps = reps
        self.profile_reps = profile_reps
        self.timeout = timeout
        self.logs = out / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.n = 0
        self.results: dict[str, dict[str, Any]] = {}
        self.state: dict[str, Any] = {}
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = str(WT)
        self.base: Path = Path(self.cfg.get("config") or out / "configs" / "unset.toml")

    # -- plumbing ---------------------------------------------------------
    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} [{self.wl}] {msg}"
        print(line, flush=True)
        with open(self.out / "b2-gate.log", "a") as f:
            f.write(line + "\n")

    def scopt(self, name: str, *args: Any) -> tuple[int, str, Path]:
        self.n += 1
        argv = [PY, "-m", "scopt", *[str(a) for a in args]]
        t0 = time.monotonic()
        p = subprocess.run(argv, cwd=str(WT), env=self.env, capture_output=True, text=True,
                           timeout=self.timeout * 40)
        dt = time.monotonic() - t0
        logp = self.logs / f"{self.n:03d}-{name}.out"
        logp.write_text(p.stdout + ("\n--- stderr ---\n" + p.stderr if p.stderr else ""))
        with open(self.out / "cmds.jsonl", "a") as f:
            f.write(json.dumps({"n": self.n, "name": name, "argv": argv[2:],
                                "rc": p.returncode, "seconds": round(dt, 3),
                                "log": str(logp)}) + "\n")
        self.log(f"scopt {' '.join(str(a) for a in args[:2])} ... rc={p.returncode} "
                 f"({dt:.1f}s) [{name}]")
        return p.returncode, p.stdout, logp

    @staticmethod
    def _json(text: str) -> dict[str, Any] | None:
        try:
            return json.loads(text)
        except ValueError:
            return None

    def phase_args(self) -> list[str]:
        out: list[str] = []
        for ph in self.cfg["phases"]:
            out += ["--phase", ph]
        return out

    def input_args(self, opt: str = "--inputs") -> list[str]:
        out: list[str] = []
        for i in self.cfg["inputs"]:
            out += [opt, str(i)]
        return out

    # -- setup ------------------------------------------------------------
    def render_fault_config(self) -> Path:
        """``fdrun.render(template, 'sc-noicount', ...)`` with the integrated QEMU."""
        d = self.out / "configs"
        d.mkdir(exist_ok=True)
        s = Path(self.cfg["template"]).read_text()
        sc = f'supercache = "cache/{self.cfg["cache_name"]}"\nsupercache_flat = false'
        s = s.replace("@SUPERCACHE@", sc).replace("@ICOUNT@", "").replace("@QEMU@",
                                                                          str(self.qemu))
        s = s.replace("@LOG_OPTIONS@", "none")
        p = d / "fault-fixture-sc.toml"
        p.write_text(s)
        return p

    def import_cmd(self, name: str, store: Path, *args: Any) -> tuple[int, dict | None, Path]:
        rc, out, logp = self.scopt(name, "import", *args, "--bundle", self.out / "bundle.json",
                                   "--runtime-identity", self.out / "runtime_identity.json",
                                   "--store", store, "--json")
        return rc, self._json(out) if rc == 0 else None, logp

    def setup(self) -> bool:
        c = Checks()
        self.state["setup"] = c
        if self.wl == "compute":
            return self.setup_from_gate(c)
        if self.wl == "fault":
            self.base = self.render_fault_config()
        c.ev(self.base)
        d = self.out / "prep"
        rc, _, logp = self.scopt("prepare", "prepare", "-c", self.base, "--out", d, "--qemu",
                                 self.qemu, "--timeout", self.timeout, *self.input_args())
        prep = json.loads((d / "prep.json").read_text()) if (d / "prep.json").is_file() else {}
        c.ev(d / "prep.json", logp)
        if not c.add("prepare PASS (capture-on cold run)", rc == 0 and prep.get("verdict") == "PASS",
                     prep.get("failures")):
            return False
        c.add("prepare used the integrated QEMU",
              all(r.get("qemu_sha256") == INT_QEMU_SHA for r in prep["runs"]),
              [r.get("qemu_sha256") for r in prep["runs"]])
        self.state["prep"] = prep
        self.state["prep_dir"] = d
        self.state["prep_sc"] = Path(prep["sc"])
        self.state["prep_manifest"] = Path(prep["manifest"])
        self.state["prep_scm"] = Path(prep["captures"][0])
        rc1, _, l1 = self.scopt("bundle-identity", "bundle", "identity", "-c", d / "run.toml",
                                "--manifest", prep["manifest"], "--out", self.out / "bundle.json")
        rc2, _, l2 = self.scopt("runtime-identity", "bundle", "runtime-identity", "--run-dir",
                                d / "oracle", "--sc", prep["sc"], "--scm", d / "capture.scm",
                                "--out", self.out / "runtime_identity.json")
        c.ev(l1, l2)
        if not c.add("bundle identity and runtime-identity", rc1 == 0 and rc2 == 0, (rc1, rc2)):
            return False
        rc, s, logp = self.import_cmd("import", self.out / "store", d)
        c.ev(logp)
        if not c.add("import accepted", rc == 0 and s is not None, rc):
            return False
        self.state["summary"] = s
        self.state["snapshot_id"] = s["snapshot_id"]
        self.state["snap"] = self.out / "store/snapshots" / snapshot_dir_name(s["snapshot_id"])
        c.add("every slot binds", s["capture"]["binding"] == {"bound": s["records"]["valid"]},
              s["capture"]["binding"])
        c.notes.append(f"snapshot {s['snapshot_id']}: status {s['status']}, classes {s['classes']}")
        return self.profile_snapshot(c, self.state["snap"], self.out / "prof", "profile")

    def setup_from_gate(self, c: Checks) -> bool:
        """compute: sparse copy of the B1 gate snapshot (read-only source)."""
        gate = Path(self.cfg["gate"])
        g = json.loads((gate / "gate.json").read_text())
        sid = g["snapshot_id"]
        src = gate / "store/snapshots" / snapshot_dir_name(sid)
        before = tree_digest(src)
        dst = self.out / "store/snapshots" / snapshot_dir_name(sid)
        dst.mkdir(parents=True)
        for p in sorted(src.rglob("*")):
            q = dst / p.relative_to(src)
            if p.is_dir():
                q.mkdir(parents=True, exist_ok=True)
            elif p.suffix == ".sc":
                sparse_copy(p, q, fsync=False)
            else:
                q.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, q)
        for n in ("bundle.json", "runtime_identity.json"):
            shutil.copyfile(gate / n, self.out / n)
        pd = self.out / "profiles"
        pd.mkdir(exist_ok=True)
        shutil.copyfile(self.cfg["profile"], pd / "compute-steady.profile.json")
        self.state["profile"] = pd / "compute-steady.profile.json"
        self.state["snapshot_id"] = sid
        self.state["snap"] = dst
        self.state["gate_src"] = (src, before)
        c.ev(gate / "gate.json", dst, self.state["profile"])
        c.add("B1 gate compute snapshot copied (sparse) and identical",
              tree_digest(dst) == before, len(before))
        c.add("profile is for this snapshot", json.loads(
            self.state["profile"].read_text())["snapshot_id"] == sid)
        return True

    def profile_snapshot(self, c: Checks, target: Path, out: Path, name: str,
                         artifact: bool = False) -> bool:
        args = ["profile", "run", "-c", self.base, "--snapshot", target, "--reps",
                self.profile_reps, "--out", out, "--qemu", self.qemu, "--timeout", self.timeout,
                *self.phase_args(), *self.input_args()]
        if artifact:
            args.append("--artifact")
        rc, _, logp = self.scopt(name, *args)
        pr = json.loads((out / "profile_run.json").read_text()) if \
            (out / "profile_run.json").is_file() else {}
        c.ev(out / "profile_run.json", logp)
        ok = c.add(f"{name}: profile run PASS (proof, ingest, flow, isolation)",
                   rc == 0 and pr.get("verdict") == "PASS", pr.get("failures"))
        if ok and name == "profile":  # the snapshot profile every fixture uses
            self.state["profile"] = Path(pr["merged"]["path"])
        self.state[f"{name}_run"] = pr
        return ok

    def open_snap(self, path: Path):
        from scopt.importer.import_cache import open_snapshot

        return open_snapshot(path)

    def snap_obj(self):
        if "snap_obj" not in self.state:
            self.state["snap_obj"] = self.open_snap(self.state["snap"])
        return self.state["snap_obj"]

    def entries(self, profile: Path, phase: str | None) -> dict[str, int]:
        p = json.loads(Path(profile).read_text())
        pid = None if phase is None else next(
            (str(x["id"]) for x in p["phases"] if x["name"] == phase), None)
        out = {}
        for u in p["units"]:
            if u.get("status") != "exact":
                continue
            ph = u.get("phase", {})
            out[u["unit"]] = (sum(v.get("entries", 0) for v in ph.values()) if phase is None
                              else int((ph.get(pid) or {}).get("entries", 0)))
        return out

    # -- shared steps ------------------------------------------------------
    def optimize(self, name: str, snapdir: Path, pipeline: Any, store: Path, *,
                 params: dict | None = None, profiles: tuple = (),
                 disable: tuple = ()) -> dict[str, Any]:
        args = ["optimize", snapdir, "--pipeline", pipeline, "--store", store, "--json"]
        if params is not None:
            pd = self.out / "params"
            pd.mkdir(exist_ok=True)
            pf = pd / f"{name}.json"
            pf.write_text(json.dumps(params, indent=1, sort_keys=True) + "\n")
            args += ["--params", pf]
        for p in profiles:
            args += ["--profile", p]
        for x in disable:
            args += ["--disable", x]
        rc, out, logp = self.scopt(name, *args)
        doc = self._json(out) or {}
        res: dict[str, Any] = {"rc": rc, "log": str(logp), "doc": doc,
                               "published": doc.get("status") == "published"}
        if res["published"]:
            art = Path(doc["artifact_dir"])
            res["art"] = art
            res["side"] = json.loads((art / "cache.sc.scopt.json").read_text())
            res["validation"] = json.loads((art / "validation.json").read_text())
            res["ledger"] = [json.loads(x) for x in (art / "ledger.jsonl").read_text()
                             .splitlines() if x.strip()]
        return res

    def v_rows(self, c: Checks, label: str, res: dict[str, Any], *, v9: tuple = ("pass", "skip"),
               upto: int = 8) -> bool:
        """V1..V8 pass (and V9 in ``v9``) from the stored validation.json rows."""
        if not res["published"]:
            c.add(f"{label}: published", False,
                  {"rc": res["rc"], "status": res["doc"].get("status"),
                   "reason": res["doc"].get("reason")})
            return False
        val = res["validation"]
        codes = val["codes"]
        per: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        for r in val["rows"]:
            per[r["code"]][r["result"]] += 1
        c.ev(res["art"] / "validation.json", res["art"] / "cache.sc.scopt.json",
             res["art"] / "ledger.jsonl", res["log"])
        ok = all(codes.get(f"V{i}") == "pass" for i in range(1, upto + 1)) and \
            codes.get("V9") in v9 and val["verdict"] == "accepted"
        return c.add(f"{label}: V1-V{upto} pass, V9 {'/'.join(v9)} (validation.json rows)", ok,
                     {"codes": codes, "rows": {k: dict(v) for k, v in sorted(per.items())}})

    def revalidate(self, c: Checks, label: str, art: Path) -> bool:
        rc, out, logp = self.scopt(f"revalidate-{label}", "validate", art, "--json")
        doc = self._json(out) or {}
        c.ev(logp)
        return c.add(f"{label}: scopt validate (static re-validation, MANIFEST) accepted",
                     rc == 0 and doc.get("verdict") == "accepted" and doc.get("stored_codes_agree"),
                     {"rc": rc, "codes": doc.get("codes")})

    def v10(self, c: Checks, label: str, art: Path, *, baseline: Path | None = None,
            expect_appended: int | None = 0, reps: int | None = None) -> dict[str, Any] | None:
        out = self.out / "v10" / label
        args = ["validate", art, "--dynamic", "-c", self.base, "--out", out, "--qemu", self.qemu,
                "--timeout", self.timeout, "--reps", reps or self.reps, "--json",
                *self.input_args()]
        if baseline is not None:
            args += ["--baseline", baseline]
        rc, txt, logp = self.scopt(f"v10-{label}", *args)
        dj = out / "dynamic.json"
        c.ev(dj, out / "ledger.jsonl", logp)
        doc = json.loads(dj.read_text()) if dj.is_file() else None
        if doc is None:
            c.add(f"{label}: V10 ran", False, f"rc={rc}")
            return None
        runs = doc["runs"]
        c.add(f"{label}: V10 pass: equal exit code, normalized and guest output vs the baseline "
              f"({len(runs)} pair(s))", rc == 0 and doc["verdict"] == "pass", doc["failures"][:5])
        c.add(f"{label}: post-run proof PASS in every run (artifact used)",
              all(r.get(a, {}).get("verdict") == "PASS" for r in runs
                  for a in ("artifact", "baseline")),
              [(r.get("artifact", {}).get("verdict"), r.get("baseline", {}).get("verdict"))
               for r in runs])
        if expect_appended is not None:
            c.add(f"{label}: appended = {expect_appended} in both arms",
                  all(x == expect_appended for x in doc["appended"]["artifact"]
                      + doc["appended"]["baseline"]), doc["appended"])
        if self.cfg.get("expected_norm"):
            want = [ln for ln in Path(self.cfg["expected_norm"]).read_text().splitlines()
                    if _GUEST_LINE.match(ln)]
            got = [[ln for ln in r.get(a, {}).get("guest_norm") or [] if _GUEST_LINE.match(ln)]
                   for r in runs for a in ("artifact", "baseline")]
            c.add(f"{label}: guest output (stacked PCs, xPSR, CFSR, BFAR, IPSR, variant d) equals "
                  "fixtures/fault/expected/fixture.norm in both arms",
                  bool(got) and all(g == want for g in got),
                  {"expected_lines": len(want), "mismatch": next(
                      ([ln for ln in g if ln not in want][:3] for g in got if g != want), [])})
        return doc

    def diag(self, c: Checks, label: str, run_dir: Path) -> dict[str, Any] | None:
        out = self.out / "diag" / label
        rc, _, logp = self.scopt(f"diag-{label}", "diag", "exec", "--run-dir", run_dir, "--out",
                                 out, "--json", "--timeout", self.timeout)
        dj = out / "diag.json"
        c.ev(dj, logp)
        if not dj.is_file():
            c.add(f"{label}: diag exec ran", False, f"rc={rc}")
            return None
        d = json.loads(dj.read_text())
        log = out / "qemu.log"
        if log.is_file() and log.stat().st_size > 512 << 20:
            (out / "qemu.log.sha256").write_text(f"{sha256_file(log)}  {log.stat().st_size}\n")
            log.unlink()
            c.notes.append(f"{label}: diag qemu.log deleted after parsing (sha256 kept)")
        c.add(f"{label}: diag exec: every Trace tc.ptr is code_address + tc_offset of an "
              "artifact record", d["exit_code"] == 0 and d["all_traces_in_snapshot"]
              and not d["tc_not_in_snapshot"],
              {"trace_lines": d["trace_lines"], "unique_tc": d["unique_tc"],
               "outside": d["tc_not_in_snapshot"][:5]})
        return d

    def report_ledger_stage(self, res: dict[str, Any], subject: str, uid: str, stage: str):
        return [r for r in res.get("ledger", []) if r["subject"] == subject and r["id"] == uid
                and r["stage"] == stage]

    def check_art_record(self, c: Checks, label: str, art: Path, uids: list[str],
                         snap) -> bool:
        """The given units' records and bytes in the artifact equal the input's."""
        sc = read_sc(art / "cache.sc")
        by = dict(zip(sc.unit_ids(), sc.units()))
        bad = []
        for uid in uids:
            u = snap.unit(uid)
            a = by.get(uid)
            if a is None or (a.record.tb_offset, a.record.tc_offset, a.record.end_offset) != \
                    (u.tb_offset, u.tc_offset, u.end_offset) or a.body != u.code + u.search \
                    or a.record_bytes != u.record:
                bad.append(uid)
        return c.add(f"{label}: unit(s) untouched in the artifact (offsets, record and bytes "
                     f"identical): {len(uids)}", not bad and bool(uids), bad[:5])

    def all_moved_bytes(self, snap, art: Path) -> dict[str, Any]:
        """Fixup values and capstone diffs of every moved unit of an artifact."""
        sc = read_sc(art / "cache.sc")
        by = dict(zip(sc.unit_ids(), sc.units()))
        ca = snap.header.code_address
        applied: collections.Counter = collections.Counter()
        bad_fix: list[str] = []
        cs_bad: list[Any] = []
        moved = 0
        cs_tot: collections.Counter = collections.Counter()
        for u in snap.units():
            a = by[u.uid]
            d = a.record.tb_offset - u.tb_offset
            if d == 0:
                continue
            moved += 1
            ok, bad = fixup_values(u, a.body, d)
            applied.update(ok)
            bad_fix += bad
            cd = capstone_diff(u, a.record.tb_offset, a.body, ca)
            cs_tot.update({"insns": cd["insns"], "changed_insns": cd["changed_insns"],
                           "external_targets_unchanged": cd["external_targets_unchanged"],
                           "intra_targets_shifted_by_d": cd["intra_targets_shifted_by_d"]})
            if not cd["ok"]:
                cs_bad.append({"unit": u.uid, **{k: cd[k] for k in (
                    "changed_bytes_outside_fixups", "changed_insns_without_fixup",
                    "external_targets_changed", "intra_targets_wrong", "tiling_equal")}})
        return {"moved": moved, "applied_by_class": dict(applied), "bad_fixups": bad_fix[:10],
                "capstone": dict(cs_tot), "capstone_bad": cs_bad[:5]}

    # -- F-B2-1 ------------------------------------------------------------
    def pick_hot(self) -> tuple[str, int, str]:
        snap = self.snap_obj()
        rel = [u for u in snap.units() if snap.status(u.uid) == "ok_relocatable"]
        if self.cfg["hot_rule"] == "relocatable_total":
            ent = self.entries(self.state["profile"], None)
            rule = "hottest relocatable unit by all-phase entries (design: on minimal)"
        else:
            ent = self.entries(self.state["profile"], "steady")
            rule = "hottest steady-phase unit (relocatable)"
        u = max(rel, key=lambda u: (ent.get(u.uid, 0), u.uid))
        return u.uid, ent.get(u.uid, 0), rule

    def f1(self) -> None:
        c = Checks()
        snap = self.snap_obj()
        uid, n, rule = self.pick_hot()
        u = snap.unit(uid)
        ca = snap.header.code_address
        c.notes.append(f"unit {uid} slot {u.slot} vaddrs {[hex(v) for v in u.vaddrs]} "
                       f"tb_offset {u.tb_offset:#x}: {rule}, {n} entries")
        c.add("chosen unit has executions and is relocatable",
              n > 0 and snap.status(uid) == "ok_relocatable", (n, snap.status(uid)))
        prof_all = self.entries(self.state["profile"], None)
        out: dict[str, Any] = {"unit": uid, "slot": u.slot, "old_tb_offset": hex(u.tb_offset)}
        for label, extra in (("max_end", 0), ("plus4MiB", 4 << 20)):
            lab = f"f1-{label}"
            target = round_up(snap.max_end, 64) + extra
            res = self.optimize(lab, self.state["snap"], PIPES / "test-move-units.toml",
                                self.out / "store",
                                params={"move-units": {"moves": {uid: hex(target)}}})
            if not self.v_rows(c, lab, res, v9=("skip",)):
                continue
            side, art = res["side"], res["art"]
            mm = side["move_map"]
            c.add(f"{lab}: exactly the unit moved, to {target:#x} (= ROUND_UP(max_end,64)"
                  f"{' + 4 MiB' if extra else ''})",
                  list(mm) == [uid] and int(mm[uid]["new_tb_offset"], 16) == target,
                  mm)
            self.revalidate(c, lab, art)
            sc = read_sc(art / "cache.sc")
            a = dict(zip(sc.unit_ids(), sc.units()))[uid]
            cd = capstone_diff(u, a.record.tb_offset, a.body, ca)
            (self.out / f"{lab}-capstone.json").write_text(json.dumps(cd, indent=1) + "\n")
            c.ev(self.out / f"{lab}-capstone.json")
            c.add(f"{lab}: capstone diff only at the declared prologue/ext fixup fields "
                  "(external targets unchanged, intra targets shifted by d)", cd["ok"],
                  {k: cd[k] for k in ("delta", "insns", "changed_bytes", "changed_insns",
                                      "changed_insns_by_fixup_class", "fixup_classes",
                                      "external_targets_unchanged", "intra_targets_shifted_by_d",
                                      "changed_bytes_outside_fixups", "external_targets_changed",
                                      "intra_targets_wrong")})
            c.add(f"{lab}: other units byte-identical (offsets and bytes)",
                  self.check_art_record(Checks(), lab, art,
                                        [x.uid for x in snap.units() if x.uid != uid], snap),
                  "")
            dyn = self.v10(c, lab, art)
            # profile of the artifact
            pd = self.out / "prof-art" / label
            if self.profile_snapshot(c, art, pd, f"profile-{lab}", artifact=True):
                self.f1_profile(c, lab, pd, uid, u.tb_offset, target, prof_all.get(uid, 0))
            if dyn and dyn["runs"]:
                rd = Path(dyn["runs"][0]["artifact"]["run_dir"])
                dg = self.diag(c, lab, rd)
                if dg is not None:
                    self.f1_diag(c, lab, dg, u, target, ca)
            out[label] = {"artifact": str(art), "artifact_id": res["doc"]["artifact_id"],
                          "new_tb_offset": hex(target)}
        self.state["f1"] = out
        self.results["F-B2-1"] = c.result()

    def f1_profile(self, c: Checks, lab: str, pd: Path, uid: str, old_tb: int, new_tb: int,
                   want: int) -> None:
        from scopt.formats.qprof_v1 import read_qprof

        pr = json.loads((pd / "profile_run.json").read_text())
        r0 = pr["runs"][0]
        q = read_qprof(r0["qprof"])
        rows_new = [t for t in q.tbs if t.tb_offset == new_tb]
        rows_old = [t for t in q.tbs if t.tb_offset == old_tb]
        n_new = sum(p.entries for t in rows_new for p in t.phases)
        prof = json.loads(Path(r0["profile"]).read_text())
        pu = next((x for x in prof["units"] if x["unit"] == uid), None)
        n_prof = sum(v["entries"] for v in pu["phase"].values()) if pu else None
        c.ev(r0["qprof"], r0["profile"])
        c.add(f"{lab}: profile on the artifact attributes the unit's executions to the new "
              f"tb_offset {new_tb:#x} (raw .qprof row), none to the old {old_tb:#x}",
              len(rows_new) == 1 and n_new > 0 and not rows_old,
              {"qprof_rows_at_new": len(rows_new), "entries_at_new": n_new,
               "qprof_rows_at_old": len(rows_old), "domain": prof["domain"]})
        c.add(f"{lab}: the artifact profile joins that row to the same UnitId with the snapshot "
              f"profile's count ({want} entries per run)",
              pu is not None and pu["status"] == "exact" and n_prof == n_new == want
              * len(prof["collection"]["runs"]),
              {"artifact_profile_entries": n_prof, "snapshot_profile_entries": want})

    def f1_diag(self, c: Checks, lab: str, dg: dict[str, Any], u, new_tb: int, ca: int) -> None:
        new_tc = ca + new_tb + (u.tc_offset - u.tb_offset)
        old_tc = ca + u.tc_offset
        log = Path(dg["diag_dir"]) / "qemu.log"
        n_new = n_old = None
        if log.is_file():
            pat_new = re.compile(rf"^Trace \d+: 0x0*{new_tc:x} ")
            pat_old = re.compile(rf"^Trace \d+: 0x0*{old_tc:x} ")
            n_new = n_old = 0
            with open(log, errors="replace") as f:
                for ln in f:
                    if ln.startswith("Trace "):
                        n_new += bool(pat_new.match(ln))
                        n_old += bool(pat_old.match(ln))
        slot_n = (dg.get("per_slot_entries") or {}).get(str(u.slot), 0)
        c.add(f"{lab}: diag exec shows the new tc.ptr {new_tc:#x} (slot {u.slot}), never the "
              f"old {old_tc:#x}", slot_n > 0 and (n_new is None or (n_new > 0 and n_old == 0)),
              {"per_slot_entries": slot_n, "trace_lines_at_new_tc": n_new,
               "trace_lines_at_old_tc": n_old})

    # -- F-B2-2 ------------------------------------------------------------
    def reverse_all(self, name: str, snapdir: Path, store: Path, snap) -> dict[str, Any]:
        """reverse-layout over every relocatable unit, plus explicit moves for the mirror's
        fixed points (units whose reversed position equals the original one)."""
        from scopt.link import NativeLinker
        from scopt.manager import PassManager

        cand = PassManager(snap, PIPES / "test-reverse-layout.toml").execute()
        fixed = [x.uid for x in NativeLinker(cand, derive_captures=False).link().units
                 if not x.moved and _reloc(snap, x.uid)]
        off = round_up(snap.max_end, 64) + (8 << 20)
        moves = {}
        for uid in fixed:
            moves[uid] = hex(off)
            x = snap.unit(uid)
            off = round_up(off + x.end_offset - x.tb_offset, 64)
        res = self.optimize(name, snapdir, PIPES / "test-reverse-move.toml", store,
                            params={"reverse-layout": {"exclude": fixed},
                                    "move-units": {"moves": moves}})
        res["fixed_points"] = fixed
        return res

    def f2_checks(self, c: Checks, lab: str, res: dict[str, Any], snap, snapdir: Path) -> None:
        if not self.v_rows(c, lab, res, v9=("skip",)):
            return
        art = res["art"]
        side = res["side"]
        reloc = [u.uid for u in snap.units() if _reloc(snap, u.uid)]
        pinned = [u.uid for u in snap.units() if u.uid not in set(reloc)]
        c.notes.append(f"{lab}: {len(reloc)} relocatable, {len(pinned)} pinned/not relocatable; "
                       f"mirror fixed points moved explicitly: {len(res['fixed_points'])}")
        c.add(f"{lab}: every relocatable unit moved ({len(reloc)})",
              set(side["move_map"]) == set(reloc), {"moved": len(side["move_map"]),
                                                     "relocatable": len(reloc)})
        mb = self.all_moved_bytes(snap, art)
        want = collections.Counter(f.target_class for u in snap.units() if u.uid in set(reloc)
                                   for f in u.reloc.fixups)
        (self.out / f"{lab}-fixups.json").write_text(json.dumps(
            {"snapshot_fixup_classes": dict(want), **mb}, indent=1) + "\n")
        c.ev(self.out / f"{lab}-fixups.json")
        need = {"prologue.tb_ret", "prologue.epilogue", "ext.plugin", "ext.ptr_riprel"}
        c.add(f"{lab}: every fixup class applied, each field == v - d / v + d (counts)",
              mb["applied_by_class"] == dict(want) and not mb["bad_fixups"]
              and need <= set(mb["applied_by_class"]),
              {"applied": mb["applied_by_class"], "snapshot": dict(want),
               "bad": mb["bad_fixups"]})
        if self.wl == "minimal" and not pinned:
            c.add(f"{lab}: G17 counts (316 tb_ret, 3 epilogue, 3 plugin calls, 3 plugin LEAs)",
                  mb["applied_by_class"] == {"prologue.tb_ret": 316, "prologue.epilogue": 3,
                                             "ext.plugin": 3, "ext.ptr_riprel": 3},
                  mb["applied_by_class"])
        c.add(f"{lab}: capstone diff of every moved unit only at fixup fields",
              not mb["capstone_bad"], {"totals": mb["capstone"], "bad": mb["capstone_bad"]})
        insc = read_sc(Path(snapdir) / "cache.sc")
        outsc = read_sc(art / "cache.sc")
        c.add(f"{lab}: records keep the original slot order",
              outsc.unit_ids() == insc.unit_ids(), len(outsc.unit_ids()))
        dups = duplicate_keys(insc)
        c.add(f"{lab}: LIFO winner of every key unchanged (duplicate keys: {len(dups)})",
              lifo_winners(insc) == lifo_winners(outsc),
              {"duplicate_keys": {f"{k.page_addr0:#x}/{k.flags:#x}/{k.cflags:#x}": v
                                  for k, v in list(dups.items())[:5]}})
        if pinned:
            self.check_art_record(c, f"{lab}: pinned", art, pinned, snap)
            c.add(f"{lab}: pinned units listed in the sidecar, not in move_map",
                  set(pinned) <= {p["unit"] for p in side["pinned"]}
                  and not set(pinned) & set(side["move_map"]), pinned[:5])
        self.revalidate(c, lab, art)

    def f2(self) -> None:
        c = Checks()
        snap = self.snap_obj()
        res = self.reverse_all("f2-reverse", self.state["snap"], self.out / "store", snap)
        self.f2_checks(c, "f2-reverse", res, snap, self.state["snap"])
        if res["published"]:
            dyn = self.v10(c, "f2-reverse", res["art"])
            if dyn and dyn["runs"]:
                self.diag(c, "f2-reverse", Path(dyn["runs"][0]["artifact"]["run_dir"]))
        self.f2_lifo(c)
        c.notes.append("synthetic duplicate-key LIFO test: "
                       "tests/acceptance/test_b2_synthetic.py::test_f_b2_2_duplicate_key_lifo")
        c.notes.append("pinned units untouched on real data: see the F-B2-4 tampered snapshot "
                       "(f4-reverse) below")
        self.state["f2_checks"] = c
        self.results["F-B2-2"] = c.result()

    def f2_lifo(self, c: Checks) -> None:
        """Duplicate key on a real file: the real snapshots have none, so a relocated copy
        of the hottest unit is appended as the last slot (same key and guest bytes, so
        UnitId ``#1``; no capture chunk, so ``no_meta``). QEMU indexes records in slot
        order and its bucket lists are newest-first, so the later slot must be the one
        executed, before and after a layout that moves ``#0``."""
        from scopt.link import relocate_unit

        snap = self.snap_obj()
        uid, n, _ = self.pick_hot()
        x = snap.unit(uid)
        d = self.out / "f2-lifo"
        prep = d / "prep"
        prep.mkdir(parents=True)
        sc = read_sc(self.state["snap"] / "cache.sc")
        new_tb = round_up(snap.max_end, 64)
        lk = relocate_unit(x, new_tb)
        name = self.state["prep_sc"].name
        write_sc(prep / name, sc.header, list(sc.units()) + [(lk.record, lk.body)])
        shutil.copyfile(self.state["prep_manifest"], prep / (name + ".fastdyn.json"))
        shutil.copyfile(self.state["prep_scm"], prep / "capture.scm")
        store = d / "store"
        rc, s, logp = self.import_cmd("import-f2-lifo", store, prep)
        c.ev(logp)
        if not c.add("f2-lifo: snapshot with a duplicate-key copy of the hottest unit imports",
                     rc == 0 and s is not None, rc):
            return
        sdir = store / "snapshots" / snapshot_dir_name(s["snapshot_id"])
        dsnap = self.open_snap(sdir)
        uids = [u.uid for u in dsnap.units()]
        dup = uids[-1]
        k0, k1 = x.slot, len(uids) - 1
        c.add("f2-lifo: the copy is the same TB under the same key (UnitId #1, last slot, "
              "no_meta)", dup == uid.split("#")[0] + "#1" and uids[k0] == uid
              and dsnap.status(dup) == "no_meta", {"copy": dup, "status": dsnap.status(dup)})
        res = self.reverse_all("f2-lifo-reverse", sdir, store, dsnap)
        if not self.v_rows(c, "f2-lifo-reverse", res, v9=("skip",)):
            return
        insc, outsc = read_sc(sdir / "cache.sc"), read_sc(res["art"] / "cache.sc")
        c.add("f2-lifo: records keep the slot order; LIFO winner of the duplicate key is still "
              "the later slot (#1)", outsc.unit_ids() == insc.unit_ids()
              and lifo_winners(insc) == lifo_winners(outsc)
              and lifo_winners(outsc)[x.key] == dup, len(duplicate_keys(outsc)))
        c.add("f2-lifo: #0 moved by the layout, #1 (no_meta) fixed",
              uid in res["side"]["move_map"] and dup not in res["side"]["move_map"],
              res["side"]["move_map"].get(uid))
        dyn = self.v10(c, "f2-lifo", res["art"])
        if not dyn or not dyn["runs"]:
            return
        for arm in ("baseline", "artifact"):
            dg = self.diag(c, f"f2-lifo-{arm}", Path(dyn["runs"][0][arm]["run_dir"]))
            if dg is None:
                continue
            pe = dg.get("per_slot_entries") or {}
            c.add(f"f2-lifo-{arm}: QEMU executes the later duplicate (slot {k1}, Trace lines > 0) "
                  f"and never the earlier one (slot {k0})",
                  pe.get(str(k1), 0) > 0 and pe.get(str(k0), 0) == 0,
                  {"slot_earlier": pe.get(str(k0), 0), "slot_later": pe.get(str(k1), 0)})

    # -- F-B2-4 ------------------------------------------------------------
    def f4(self) -> None:
        from scopt.formats import scm_v1 as S
        from scopt.formats.enums import SiteKind
        from tests.synth import builders as B

        c = Checks()
        snap = self.snap_obj()
        ent = self.entries(self.state["profile"], "steady")
        data = self.state["prep_scm"].read_bytes()
        uid_of = {u.slot: u.uid for u in snap.units()}
        best = None
        for ch in S.parse_scm(data).chunks:
            if not isinstance(ch.body, S.TbChunk):
                continue
            tb = ch.body
            for i, site in enumerate(tb.sites):
                if site.kind == SiteKind.HELPER_CALL:
                    uid = uid_of.get(tb.slot)
                    n = ent.get(uid, 0)
                    if uid and (best is None or (n, uid) > (best[0], best[2])):
                        best = (n, tb.slot, uid, i)
                    break
        if not c.add("a unit with a HELPER_CALL site exists", best is not None):
            self.results["F-B2-4"] = c.result()
            return
        n, slot, uid, site_i = best
        c.notes.append(f"tampered: slot {slot} {uid} (steady entries {n}), HELPER_CALL site "
                       f"#{site_i} removed from the capture")
        prep = self.out / "f4/prep"
        prep.mkdir(parents=True)
        sc_src = self.state["prep_sc"]
        sparse_copy(sc_src, prep / sc_src.name, fsync=False)
        shutil.copyfile(self.state["prep_manifest"], prep / self.state["prep_manifest"].name)
        (prep / "capture.scm").write_bytes(B.drop_site(data, slot, site_i))
        store = self.out / "f4/store"
        rc, s, logp = self.import_cmd("import-f4", store, prep)
        c.ev(logp)
        if not c.add("tampered capture imports", rc == 0 and s is not None, rc):
            self.results["F-B2-4"] = c.result()
            return
        reasons = s.get("pin_reasons") or {}
        c.add("the unit is pinned with unexplained_reference (exactly one pinned unit)",
              (s["status"] or {}).get("pinned") == 1
              and any(k.startswith("unexplained_reference") for k in reasons),
              {"status": s["status"], "pin_reasons": reasons})
        c.add("same SnapshotId as the clean import (the .sc is unchanged; the profile applies)",
              s["snapshot_id"] == self.state["snapshot_id"], s["snapshot_id"])
        sdir = store / "snapshots" / snapshot_dir_name(s["snapshot_id"])
        tsnap = self.open_snap(sdir)
        c.add("the tampered unit is pinned in the reopened snapshot",
              tsnap.status(uid) == "pinned", (tsnap.status(uid),
                                              list(tsnap.unit(uid).reloc.reasons)))
        want = f"skipped_unsupported:unexplained_reference:{uid}"
        for lab, dis in (("f4-b2-layout", ()), ("f4-no-cluster-hot", ("cluster-hot",))):
            res = self.optimize(lab, sdir, "b2-layout", store, profiles=(self.state["profile"],),
                                disable=dis)
            if not self.v_rows(c, lab, res):
                continue
            side, art = res["side"], res["art"]
            pp = {p["pass"]: p for p in side["pipeline"]}
            where = [p for p in ("cluster-hot", "order-cold")
                     if want in (pp.get(p, {}).get("affected") or [])]
            c.add(f"{lab}: layout reports {want} in affected",
                  bool(where) if not dis else "order-cold" in where,
                  {"passes": where, "outcomes": {k: (v.get("outcome"), v.get("reason"))
                                                 for k, v in pp.items()}})
            c.add(f"{lab}: unit in sidecar pinned, not moved; {len(side['move_map'])} units moved",
                  uid in {p["unit"] for p in side["pinned"]} and uid not in side["move_map"]
                  and len(side["move_map"]) > 0,
                  next((p for p in side["pinned"] if p["unit"] == uid), None))
            self.check_art_record(c, lab, art, [uid], tsnap)
            urow = self.report_ledger_stage(res, "unit", uid, "inspected")
            arow = [r for r in res["ledger"] if r["subject"] == "artifact"
                    and r["stage"] == "inspected"]
            c.add(f"{lab}: ledger: unit inspected = pinned (unexplained_reference), artifact "
                  "inspected = partial",
                  len(urow) == 1 and urow[0]["verdict"] == "pinned"
                  and "unexplained_reference" in urow[0]["reason"]
                  and len(arow) == 1 and arow[0]["verdict"] == "partial",
                  {"unit": urow, "artifact": [(r["verdict"], r.get("counts")) for r in arow]})
            self.revalidate(c, lab, art)
            self.v10(c, lab, art)
        # F-B2-2 on the tampered snapshot: the pinned unit is untouched by reverse-layout
        c2 = self.state.get("f2_checks")
        res = self.reverse_all("f4-reverse", sdir, store, tsnap)
        if c2 is not None:
            self.f2_checks(c2, "f4-reverse", res, tsnap, sdir)
            if res["published"]:
                self.v10(c2, "f4-reverse", res["art"])
            self.results["F-B2-2"] = c2.result()
        self.state["f4"] = {"unit": uid, "slot": slot, "site": site_i, "snapshot_dir": str(sdir)}
        self.results["F-B2-4"] = c.result()

    # -- F-B2-5 ------------------------------------------------------------
    def f5(self) -> None:
        c = Checks()
        snap = self.snap_obj()
        ent = self.entries(self.state["profile"], None)
        cands = [u for u in snap.units() if _reloc(snap, u.uid)
                 and any(f.target_class == "ext.plugin" for f in u.reloc.fixups)]
        if not c.add("a relocatable unit with a plugin call exists", bool(cands), len(cands)):
            self.results["F-B2-5"] = c.result()
            return
        a = max(cands, key=lambda u: (ent.get(u.uid, 0), u.uid))
        span_a = a.end_offset - a.tb_offset
        c.notes.append(f"unit {a.uid} slot {a.slot} vaddrs {[hex(v) for v in a.vaddrs]} "
                       f"tb_offset {a.tb_offset:#x}, {ent.get(a.uid, 0)} entries, plugin calls "
                       f"{sum(f.target_class == 'ext.plugin' for f in a.reloc.fixups)}")
        c.add(f"{F5_TARGET:#x} is inside the arena bound (so only rel32 can fail)",
              F5_TARGET + span_a < snap.header.code_size - snap.header.host_page_size - 1024,
              hex(snap.header.code_size))
        store = self.out / "store"
        snap_before = tree_digest(self.state["snap"])
        # (a) rollback
        res = self.optimize("f5-rollback", self.state["snap"], PIPES / "test-move-units.toml",
                            store, params={"move-units": {"moves": {a.uid: hex(F5_TARGET)}}})
        if self.v_rows(c, "f5-rollback", res, v9=("pass",)):
            side, art = res["side"], res["art"]
            xl = side.get("x_link") or {}
            c.add("f5-rollback: the linker rejects the placement with rel32_range and rolls the "
                  "unit back (2 link attempts)",
                  [(r["unit"], r["reason"]) for r in xl.get("rollbacks", [])]
                  == [(a.uid, "rel32_range")] and xl.get("attempts") == 2, xl)
            c.add("f5-rollback: provenance: sidecar pinned failed:rel32_range, move_map empty",
                  {"unit": a.uid, "reasons": ["failed:rel32_range"]} in side["pinned"]
                  and side["move_map"] == {}, side["pinned"])
            urow = self.report_ledger_stage(res, "unit", a.uid, "compiled")
            c.add("f5-rollback: ledger unit row compiled fail rel32_range",
                  any(r["verdict"] == "fail" and "rel32_range" in r["reason"] for r in urow), urow)
            c.add("f5-rollback: baseline retained: artifact cache.sc == input cache.sc (V9 sha256)",
                  sha256_sparse(art / "cache.sc") == sha256_sparse(self.state["snap"] / "cache.sc"))
            self.revalidate(c, "f5-rollback", art)
            self.v10(c, "f5-rollback", art)
        # (b) the rolled-back unit's original slot is taken -> the candidate is rejected
        others = [u for u in snap.units() if u.uid != a.uid and _reloc(snap, u.uid)
                  and u.end_offset - u.tb_offset <= span_a]
        if not c.add("f5-reject: a second unit fits into the first one's interval", bool(others)):
            self.results["F-B2-5"] = c.result()
            return
        b = max(others, key=lambda u: (u.end_offset - u.tb_offset, u.uid))
        c.notes.append(f"second unit {b.uid} slot {b.slot} placed at {a.tb_offset:#x}")
        before_art = sorted(p.name for p in (store / "artifacts").iterdir())
        rej_before = sorted(p.name for p in (store / "rejections").iterdir()) \
            if (store / "rejections").is_dir() else []
        res = self.optimize("f5-reject", self.state["snap"], PIPES / "test-move-units.toml", store,
                            params={"move-units": {"moves": {a.uid: hex(F5_TARGET),
                                                             b.uid: hex(a.tb_offset)}}})
        doc = res["doc"]
        c.ev(res["log"], doc.get("rejection_path"))
        c.add("f5-reject: optimize exits 2, candidate rejected with provenance failed:rel32_range",
              res["rc"] == 2 and doc.get("status") == "rejected"
              and doc.get("reason") == "failed:rel32_range", {"rc": res["rc"],
                                                              "reason": doc.get("reason")})
        rej = json.loads(Path(doc["rejection_path"]).read_text()) \
            if doc.get("rejection_path") and Path(doc["rejection_path"]).is_file() else {}
        reasons = [(f["unit"], f["reason"]) for f in (rej.get("link") or {}).get("failures", [])]
        c.add("f5-reject: rejection document: rel32_range for the unit, rollback_slot_taken",
              (a.uid, "rel32_range") in reasons and any(r == "rollback_slot_taken"
                                                       for _, r in reasons), reasons)
        led = [(r["subject"], r["stage"], r["verdict"], r["reason"]) for r in rej.get("ledger", [])]
        c.add("f5-reject: ledger rows compiled fail rel32_range and accepted fail "
              "failed:rel32_range", any(x[1:] == ("compiled", "fail", "rel32_range") for x in led)
              and any(x[1:] == ("accepted", "fail", "failed:rel32_range") for x in led),
              [x for x in led if x[0] == "artifact"])
        after_art = sorted(p.name for p in (store / "artifacts").iterdir())
        rej_after = sorted(p.name for p in (store / "rejections").iterdir())
        c.add("f5-reject: nothing published (store artifacts unchanged); one rejection document "
              "added", after_art == before_art and len(set(rej_after) - set(rej_before)) == 1,
              {"artifacts": len(after_art), "new_rejections": sorted(set(rej_after)
                                                                     - set(rej_before))})
        c.add("f5: baseline retained: the input snapshot is unchanged",
              tree_digest(self.state["snap"]) == snap_before, len(snap_before))
        self.results["F-B2-5"] = c.result()

    # -- F-B2-6 ------------------------------------------------------------
    def f6_seed(self, c: Checks) -> tuple[Path, Path, Path, int] | None:
        """Return (post-run .sc, its manifest, derived capture, appended) of a capture-off
        warm run whose cache lacks some TBs the run executes."""
        store = self.out / "f6/store"
        if self.wl == "compute":
            src = self.state["snap"]
            ostore = self.out / "store"
        else:
            k = self.cfg["trunc_k"]
            d = self.out / "f6/trunc"
            d.mkdir(parents=True)
            sc = read_sc(self.state["prep_sc"])
            n = len(sc.units())
            write_sc(d / "trunc.sc", sc.header, sc.units()[:n - k])
            rc, s, logp = self.import_cmd("import-f6-trunc", store, "--sc", d / "trunc.sc",
                                          "--scm", self.state["prep_scm"], "--manifest",
                                          self.state["prep_manifest"])
            c.ev(logp)
            if not c.add(f"snapshot without the last {k} records imported", rc == 0 and s
                         and s["status"].get("ok_relocatable") == n - k,
                         s and s["status"]):
                return None
            src = store / "snapshots" / snapshot_dir_name(s["snapshot_id"])
            ostore = store
            c.notes.append(f"training snapshot = the first {n - k} of {n} records (no input bank "
                           "on this workload; the dropped TBs are executed again by the warm run)")
        res = self.optimize("f6-identity", src, "identity", ostore)
        if not self.v_rows(c, "f6-identity (derived capture source)", res, v9=("pass",),
                           upto=8):
            return None
        art = res["art"]
        dyn = self.v10(c, "f6-seed", art, expect_appended=None, reps=1)
        if dyn is None or not dyn["runs"]:
            return None
        appended = dyn["runs"][0]["artifact"]["appended"]
        c.add("f6-seed: the capture-off warm run appended records",
              appended is not None and appended > 0
              and (self.wl == "compute" or appended == self.cfg["trunc_k"]),
              {"appended": dyn["appended"], "inputs": dyn["inputs"]})
        rd = Path(dyn["runs"][0]["artifact"]["run_dir"])
        scs = sorted((rd / "cache").glob("*.sc"))
        if not scs:
            c.add("f6-seed: post-run cache present", False, str(rd))
            return None
        post = scs[0]
        return post, Path(str(post) + ".fastdyn.json"), art / "cache.scm", appended

    def f6(self) -> None:
        c = Checks()
        seed = self.f6_seed(c)
        if seed is None:
            self.results["F-B2-6"] = c.result()
            return
        post, man, scm, appended = seed
        store = self.out / "f6/store"
        d = self.out / "f6/post"
        d.mkdir(parents=True, exist_ok=True)
        sparse_copy(post, d / post.name, fsync=False)
        shutil.copyfile(man, d / man.name)
        shutil.copyfile(scm, d / "derived.scm")
        rc, s, logp = self.import_cmd("import-f6-post", store, "--sc", d / post.name, "--scm",
                                      d / "derived.scm", "--manifest", d / man.name)
        c.ev(logp, d)
        if not c.add("post-run .sc imported with the derived capture", rc == 0 and s is not None,
                     rc):
            self.results["F-B2-6"] = c.result()
            return
        c.add(f"the {appended} appended record(s) are no_meta units (no capture: capture off)",
              s["status"].get("no_meta") == appended, {"status": s["status"],
                                                       "meta_status": s.get("meta_status")})
        sdir = store / "snapshots" / snapshot_dir_name(s["snapshot_id"])
        snap6 = self.open_snap(sdir)
        nm = [u.uid for u in snap6.units() if u.meta_status != "ok"]
        c.notes.append("no_meta units: " + ", ".join(
            f"{x} slot {snap6.unit(x).slot} tb {snap6.unit(x).tb_offset:#x}" for x in nm))
        c.add("no_meta units have status no_meta (never relocatable)",
              bool(nm) and all(snap6.status(x) == "no_meta" for x in nm),
              {x: snap6.status(x) for x in nm})
        self.state["f6"] = {"snapshot_dir": str(sdir), "no_meta": nm}
        # (i) reverse-layout: every other relocatable unit moves around the fixed unit(s)
        res = self.optimize("f6-reverse", sdir, PIPES / "test-reverse-layout.toml", store)
        if self.v_rows(c, "f6-reverse", res, v9=("skip",)):
            side = res["side"]
            c.add("f6-reverse: no_meta units stay fixed (not in move_map, in sidecar pinned)",
                  not set(nm) & set(side["move_map"]) and set(nm) <= {p["unit"] for p in
                                                                    side["pinned"]},
                  [p for p in side["pinned"] if p["unit"] in nm])
            c.add("f6-reverse: layout planned around them (other units moved)",
                  len(side["move_map"]) > 0, len(side["move_map"]))
            self.check_art_record(c, "f6-reverse", res["art"], nm, snap6)
            self.revalidate(c, "f6-reverse", res["art"])
            self.v10(c, "f6-reverse", res["art"])
        # (ii) b2-layout with a profile of this snapshot
        if self.wl == "compute":
            prof = self.state["profile"]
        else:
            ok = self.profile_snapshot(c, sdir, self.out / "f6/prof", "profile-f6")
            prof = Path(self.state["profile-f6_run"]["merged"]["path"]) if ok else None
        if prof is not None:
            res = self.optimize("f6-b2-layout", sdir, "b2-layout", store, profiles=(prof,))
            if self.v_rows(c, "f6-b2-layout", res):
                side = res["side"]
                pp = {p["pass"]: p for p in side["pipeline"]}
                aff = [x for p in ("cluster-hot", "order-cold")
                       for x in (pp.get(p, {}).get("affected") or [])]
                c.add("f6-b2-layout: every no_meta unit reported as skipped_unsupported:no_meta",
                      all(f"skipped_unsupported:no_meta:{x}" in aff for x in nm),
                      [x for x in aff if x.startswith("skipped_")][:10])
                c.add("f6-b2-layout: no_meta units stay fixed; others moved",
                      not set(nm) & set(side["move_map"]) and len(side["move_map"]) > 0,
                      len(side["move_map"]))
                self.check_art_record(c, "f6-b2-layout", res["art"], nm, snap6)
                self.revalidate(c, "f6-b2-layout", res["art"])
                self.v10(c, "f6-b2-layout", res["art"])
        # (iii) PLACE onto the no_meta unit's interval -> place_overlap rollback
        x = snap6.unit(nm[0])
        rel = [u for u in snap6.units() if _reloc(snap6, u.uid)]
        r_ = max(rel, key=lambda u: (u.slot, u.uid))
        res = self.optimize("f6-overlap", sdir, PIPES / "test-move-units.toml", store,
                            params={"move-units": {"moves": {r_.uid: hex(x.tb_offset)}}})
        if self.v_rows(c, "f6-overlap", res, v9=("pass",)):
            rb = (res["side"].get("x_link") or {}).get("rollbacks", [])
            c.add("f6-overlap: a PLACE onto the no_meta unit's interval is rolled back "
                  "(place_overlap); the unit stays", [(r["unit"], r["reason"]) for r in rb]
                  == [(r_.uid, "place_overlap")] and res["side"]["move_map"] == {}, rb)
            self.check_art_record(c, "f6-overlap", res["art"], nm, snap6)
        # (iv) PLACE of the no_meta unit itself -> refused by the pass manager
        res = self.optimize("f6-place-nometa", sdir, PIPES / "test-move-units.toml", store,
                            params={"move-units": {"moves": {x.uid: hex(round_up(
                                snap6.max_end, 64))}}})
        pp = {p["pass"]: p for p in (res.get("side") or {}).get("pipeline", [])}
        mu = pp.get("move-units", {})
        refused = "not_relocatable" in json.dumps(mu) or \
            "not_relocatable" in Path(res["log"]).read_text()
        c.add("f6-place-nometa: a PLACE of the no_meta unit is refused (not_relocatable); the "
              "unit is not moved", refused and (not res["published"]
                                                or x.uid not in res["side"]["move_map"]),
              {"published": res["published"], "move-units": {k: mu.get(k) for k in (
                  "outcome", "reason", "detail")}})
        self.results["F-B2-6"] = c.result()

    # -- report ------------------------------------------------------------
    def report(self) -> dict[str, Any]:
        rc, _, logp = self.scopt("report", "report", self.out, "--out",
                                 self.out / "report/report.md")
        return {"rc": rc, "report": str(self.out / "report/report.md"),
                "report_json": str(self.out / "report/report.json"), "log": str(logp)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workload", choices=sorted(WORKLOADS), required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--qemu", type=Path, default=INT_QEMU)
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--profile-reps", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--fixtures", default=None, help="comma-separated subset")
    ap.add_argument("--config", type=Path, help="override the workload's base TOML (e.g. a "
                    "copy whose [Machine].qemu_path names --qemu; WP22 T-B3-27)")
    ap.add_argument("--gate-dir", type=Path, help="compute: B1 gate output to copy the "
                    "snapshot from (default runs/wp14/gate/compute)")
    ap.add_argument("--profile", type=Path, help="compute: profile.json of that snapshot")
    a = ap.parse_args(argv)
    out = a.out.resolve()
    if out.exists() and any(out.iterdir()):
        ap.error(f"{out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    qemu = a.qemu.resolve()
    g = B2Gate(a.workload, out, qemu, a.reps, a.profile_reps, a.timeout)
    if a.config:
        g.base = a.config.resolve()
    if a.gate_dir or a.profile:
        g.cfg = dict(g.cfg)
        if a.gate_dir:
            g.cfg["gate"] = a.gate_dir.resolve()
        if a.profile:
            g.cfg["profile"] = a.profile.resolve()
    want = [f for f in (a.fixtures.split(",") if a.fixtures else g.cfg["fixtures"])]
    head = subprocess.run(["git", "-C", str(WT), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(WT), "status", "--porcelain"], capture_output=True,
                           text=True).stdout.strip()
    doc: dict[str, Any] = {
        "schema": "scopt.b2_gate/1", "workload": a.workload, "out": str(out),
        "qemu": str(qemu), "qemu_sha256": sha256_file(qemu), "scopt_worktree": str(WT),
        "scopt_head": head, "scopt_dirty": bool(dirty), "fixtures_requested": want,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "loadavg_start": os.getloadavg()}
    t0 = time.monotonic()
    g.log(f"start: workload={a.workload} qemu={qemu} fixtures={want}")
    ok = False
    try:
        ok = g.setup()
    except Exception:  # noqa: BLE001 - recorded as a failed setup
        g.state["setup"].add("setup raised", False, traceback.format_exc()[-2000:])
    doc["config"] = str(g.base)
    doc["config_sha256"] = sha256_file(g.base) if g.base.is_file() else None
    doc["setup"] = g.state["setup"].result()
    steps = {"F-B2-1": g.f1, "F-B2-2": g.f2, "F-B2-4": g.f4, "F-B2-5": g.f5, "F-B2-6": g.f6}
    for f in FIXTURES:
        if f not in want:
            g.results[f] = Checks().result(not_run=f"not requested for workload {a.workload}")
            continue
        if not ok:
            g.results[f] = Checks().result(not_run="setup failed")
            continue
        g.log(f"--- {f}")
        try:
            steps[f]()
        except Exception:  # noqa: BLE001 - a crash is a failed fixture, never hidden
            c = Checks()
            c.add(f"{f} driver raised", False, traceback.format_exc()[-3000:])
            g.results[f] = c.result()
    if "gate_src" in g.state:
        src, before = g.state["gate_src"]
        doc["gate_source_unchanged"] = tree_digest(src) == before
    doc["report"] = g.report() if ok else {"rc": None}
    doc["fixtures"] = {f: g.results[f] for f in FIXTURES}
    doc["snapshot_id"] = g.state.get("snapshot_id")
    doc["profile"] = str(g.state["profile"]) if g.state.get("profile") else None
    for k in ("f1", "f4", "f6"):
        if k in g.state:
            doc[k] = g.state[k]
    ran = [f for f in want if f in FIXTURES]
    doc["verdict"] = "PASS" if ok and ran and all(
        doc["fixtures"][f]["verdict"] == "pass" for f in ran) and \
        doc.get("gate_source_unchanged", True) else "FAIL"
    doc["seconds"] = round(time.monotonic() - t0, 1)
    doc["loadavg_end"] = os.getloadavg()
    doc["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    (out / "b2-gate.json").write_text(json.dumps(doc, indent=1, sort_keys=True, default=str)
                                      + "\n")
    for f in FIXTURES:
        r = doc["fixtures"][f]
        g.log(f"{f}: {r['verdict']}" + (f" failed={r['failed']}" if r["failed"] else "")
              + (f" ({r['not_run_reason']})" if r["not_run_reason"] else ""))
    g.log(f"verdict={doc['verdict']} ({doc['seconds']}s)")
    return 0 if doc["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""B1 gate driver (design §6, WP14): the CLI end to end on one workload.

    b1_gate.py --workload minimal|freertos|compute --out OUT [--qemu QEMU]
               [--reps 5] [--profile-reps 3] [--t-b1-0 T-B1-0.json] [--timeout 300]

Every step is a ``python -m scopt ...`` subprocess run from this worktree
(recorded in ``OUT/cmds.jsonl``, output in ``OUT/logs/``); the checks then read
the files those commands wrote. Chain (§8)::

    qualify-capture -> prepare -> bundle identity / runtime-identity -> import
    -> graph export/import -> profile run -> optimize --pipeline identity
    -> validate -> profile run (artifact) -> validate --dynamic -> run (arms)
    -> diag exec -> report

Results, one entry per test id, in ``OUT/gate.json``:

* T-B1-0  read from the given T-B1-0 driver result (``tests/qemu/t_b1_0.py``);
* T-B1-1  ``scopt qualify-capture`` (qualification.json);
* T-B1-2  the fresh prep ``.sc``: v5 invariants, ``scparse.py`` agreement on every
          field, read->write round trip byte-identical (``scopt sc identity-copy``);
* T-B1-3  the import summary: every slot binds, no unexplained/missing/recovery
          pins, G17 tallies (minimal), CF_NOIRQ units relocatable, helper symbols;
* T-B1-4  graph round trip and checks, profile edges, the two-TB pair from a
          ``Linking TBs`` line of ``scopt diag exec``, and ``not_in_snapshot`` on a
          real truncated snapshot (first 100 records);
* T-B1-5  identity artifact: V1-V9 incl. V9 sha256, derived ``.scm`` re-imports to
          the same SnapshotId, ``scopt validate`` re-validation, V10, ``scopt run``
          warm vs identity ``--reps`` interleaved (equal outputs, proof PASS,
          appended 0), ``scopt diag exec`` tc.ptr all in the artifact;
* T-B1-6  profile exactness: flow conservation, per-key entries == ``-d
          exec,nochain`` Trace counts, the 0x08018766 TB, phase steady reached;
* T-B1-7  isolation hashes around both profile runs; PROFILE_ONLY rows on the
          truncated snapshot equal its appended records;
* T-B1-8  recovery evidence of every unit; the synthetic shifted-host_end case
          runs in the T-B1-9 pytest file;
* T-B1-9  ``pytest tests/acceptance/test_b1_9_rejections.py`` with
          ``B1_GATE_DIR=OUT`` (real files from this run plus synthetic builders);
* ledger  ``scopt report`` table: the identity artifact's stages.

T-B1-2/4/6/7/8/9 are gated on minimal only (design §6); on the FreeRTOS
workloads they are run where cheap and reported as informational.
"""

from __future__ import annotations

import argparse
import collections
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
from scopt.formats.sc_v5 import read_sc, sha256_sparse, write_sc  # noqa: E402
from scopt.model.cache_snapshot import snapshot_dir_name  # noqa: E402

ROOT = Path(os.environ.get("FASTDYN_ROOT") or Path(__file__).resolve().parents[4])
PY = sys.executable
INT_QEMU = ROOT / "wt/qemu-int/build/qemu-system-arm"
B0_GOLDEN = ROOT / "runs/b0/cache/minimal.sc"
CP_INPUTS = ROOT / "fixtures/inputs"

WORKLOADS: dict[str, dict[str, Any]] = {
    "minimal": {"config": ROOT / "src/fastdyn/configs/b0-sc.toml", "inputs": [],
                "heldout": [], "phases": ["steady@0x08003972"], "gated": "all",
                "truncate": 100},
    "freertos": {"config": ROOT / "runs/qint/configs/freertos-sc-int.toml", "inputs": [],
                 "heldout": [], "phases": [], "gated": ("T-B1-0", "T-B1-1", "T-B1-3", "T-B1-5"),
                 "truncate": None},
    "compute": {"config": ROOT / "runs/wp13-r2/compute/configs/compute-sc.toml",
                "inputs": [CP_INPUTS / "train/freertos-mps2-compute/train-1.bin"],
                "heldout": [CP_INPUTS / "heldout/freertos-mps2-compute/heldout-1.bin"],
                "phases": [], "gated": ("T-B1-0", "T-B1-1", "T-B1-3", "T-B1-5"),
                "truncate": None},
}
TESTS = [f"T-B1-{i}" for i in range(10)]
CF_NOIRQ = 0x10000  # cflags bit of the io-recompile TBs (QEMU CF_NOIRQ)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return "sha256:" + h.hexdigest()


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


class Gate:
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
        self.base = Path(self.cfg["config"])

    # -- plumbing ---------------------------------------------------------
    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} [{self.wl}] {msg}"
        print(line, flush=True)
        with open(self.out / "gate.log", "a") as f:
            f.write(line + "\n")

    def scopt(self, name: str, *args: Any) -> tuple[int, str, Path]:
        self.n += 1
        argv = [PY, "-m", "scopt", *[str(a) for a in args]]
        t0 = time.monotonic()
        p = subprocess.run(argv, cwd=str(WT), env=self.env, capture_output=True, text=True,
                           timeout=self.timeout * 40)
        dt = time.monotonic() - t0
        logp = self.logs / f"{self.n:02d}-{name}.out"
        logp.write_text(p.stdout + ("\n--- stderr ---\n" + p.stderr if p.stderr else ""))
        rec = {"n": self.n, "name": name, "argv": argv[2:], "rc": p.returncode,
               "seconds": round(dt, 3), "log": str(logp)}
        with open(self.out / "cmds.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
        self.log(f"scopt {' '.join(str(a) for a in args[:2])} ... rc={p.returncode} "
                 f"({dt:.1f}s)")
        return p.returncode, p.stdout, logp

    def gated(self, tid: str) -> bool:
        g = self.cfg["gated"]
        return g == "all" or tid in g

    def inputs_args(self, opt: str = "--inputs") -> list[str]:
        out: list[str] = []
        for i in self.cfg["inputs"]:
            out += [opt, str(i)]
        return out

    # -- steps ------------------------------------------------------------
    def t1(self) -> None:
        c = Checks()
        d = self.out / "t-b1-1"
        args = ["qualify-capture", "-c", self.base, "--qemu", self.qemu, "--out", d,
                "--timeout", self.timeout]
        if self.cfg["inputs"]:
            args += ["--input", self.cfg["inputs"][0]]
        rc, _, logp = self.scopt("qualify-capture", *args)
        q = d / "qualification.json"
        c.ev(q, logp)
        if not q.is_file():
            c.add("qualification.json written", False, f"rc={rc}")
        else:
            doc = json.loads(q.read_text())
            c.add("verdict PASS", doc["verdict"] == "PASS", doc["failures"][:5])
            c.add("mode", doc["mode"] in ("whole_file", "per_key"),
                  f"{doc['mode']} (weaker_mode={doc['weaker_mode']})")
            c.add("runs: 3 off + 2 on", len(doc["runs"]) == 5)
            shas = {r.get("sc_sha256") for r in doc["runs"]}
            c.notes.append(f"mode={doc['mode']}; .sc sha256 {sorted(shas)}; records "
                           f"{sorted({r.get('record_count') for r in doc['runs']})}")
            c.add("qemu is the integrated binary",
                  doc["qemu_sha256"] == [sha256_file(self.qemu)], doc["qemu_sha256"])
            self.state["t1_mode"] = doc["mode"]
        self.results["T-B1-1"] = c.result()

    def prepare(self) -> bool:
        d = self.out / "prep"
        rc, _, logp = self.scopt("prepare", "prepare", "-c", self.base, "--out", d,
                                 "--qemu", self.qemu,
                                 "--timeout", self.timeout, *self.inputs_args())
        prep = json.loads((d / "prep.json").read_text()) if (d / "prep.json").is_file() else {}
        self.state["prep"] = prep
        self.state["prep_dir"] = d
        if rc != 0 or prep.get("verdict") != "PASS":
            self.log(f"prepare failed: {prep.get('failures')}")
            return False
        self.state["prep_sc"] = Path(prep["sc"])
        self.state["prep_manifest"] = Path(prep["manifest"])
        rc1, _, _ = self.scopt("bundle-identity", "bundle", "identity", "-c", d / "run.toml",
                               "--manifest", prep["manifest"], "--out", self.out / "bundle.json")
        rc2, _, _ = self.scopt("runtime-identity", "bundle", "runtime-identity", "--run-dir",
                               d / "oracle", "--sc", prep["sc"], "--scm", d / "capture.scm",
                               "--out", self.out / "runtime_identity.json")
        return rc1 == 0 and rc2 == 0

    def t2(self) -> None:
        from scopt.validate import scparse_compare

        c = Checks()
        sc_path = self.state["prep_sc"]
        sc = read_sc(sc_path)
        h = sc.header
        c.ev(sc_path)
        c.add("header and records valid", sc.loadable and not sc.header_problems
              and not sc.problems and not sc.skipped,
              {"header_problems": sc.header_problems, "skipped": len(sc.skipped)})
        c.add("magic/v5/header_size", (h.magic, h.version, h.header_size) == (b"QSCACHE1", 5, 80))
        c.add("tb_size 152", h.tb_size == 152, h.tb_size)
        c.add("target_page_bits 10", h.target_page_bits == 10, h.target_page_bits)
        c.add("tb % 64 == 0", all(r.tb_offset % 64 == 0 for r in sc.records if r))
        c.add("tc - tb == 192", all(r.tc_offset - r.tb_offset == 192 for r in sc.records if r))
        c.add("file size = 0x10000 + code_size + n*104",
              sc.file_size == sc.expected_file_size, (sc.file_size, sc.expected_file_size))
        ok, det = scparse_compare(sc_path, sc)
        c.add("scparse.py agrees on every field", ok, det)
        d = self.out / "t-b1-2"
        d.mkdir(exist_ok=True)
        rc, out, logp = self.scopt("identity-copy", "sc", "identity-copy", "--require-identity",
                                   sc_path, d / "roundtrip.sc")
        c.ev(logp, d / "roundtrip.sc")
        same = (d / "roundtrip.sc").is_file() and \
            sha256_sparse(d / "roundtrip.sc") == sha256_sparse(sc_path)
        c.add("read->write round trip byte-identical", rc == 0 and same,
              f"rc={rc} status={json.loads(out).get('status') if out.strip() else None}")
        self.results["T-B1-2"] = c.result()

    def import_(self) -> bool:
        rc, out, logp = self.scopt("import", "import", self.state["prep_dir"], "--bundle",
                                   self.out / "bundle.json", "--runtime-identity",
                                   self.out / "runtime_identity.json", "--store",
                                   self.out / "store", "--json")
        if rc != 0:
            self.state["import_log"] = logp
            return False
        s = json.loads(out)
        self.state["summary"] = s
        self.state["snap"] = self.out / "store/snapshots" / snapshot_dir_name(s["snapshot_id"])
        self.state["snapshot_id"] = s["snapshot_id"]
        return True

    def t3(self) -> None:
        from scopt.importer.import_cache import G17, open_snapshot

        c = Checks()
        s = self.state["summary"]
        snapd = self.state["snap"]
        c.ev(snapd / "snapshot.json", snapd / "evidence.jsonl")
        valid = s["records"]["valid"]
        c.add("100% of slots bind", s["capture"]["binding"] == {"bound": valid},
              f"{s['capture']['binding']} of {valid}")
        reasons = s["pin_reasons"]
        for code in ("unexplained_reference", "missing_reference", "recovery_mismatch"):
            n = sum(v for k, v in reasons.items() if k == code or k.startswith(code))
            c.add(f"0 {code}", n == 0, n)
        c.notes.append(f"status {s['status']}; pin reasons {reasons}")
        if self.wl == "minimal":
            c.add("class totals equal G17", s["tallies"] == G17, s["tallies"])
        else:
            c.notes.append(f"tallies (G17 is minimal-only): {s['tallies']}")
        snap = open_snapshot(snapd)
        noirq = [u for u in snap.units() if u.key.cflags & CF_NOIRQ]
        bad = [u.uid for u in noirq if snap.status(u.uid) != "ok_relocatable"]
        c.add("CF_NOIRQ io-recompile TBs are ok_relocatable", not bad,
              f"{len(noirq)} CF_NOIRQ units, not relocatable: {bad[:5]}")
        if self.wl == "minimal":
            c.add("CF_NOIRQ units exist (minimal)", bool(noirq), len(noirq))
        hs = s["helper_symbols"]
        c.add("helper symbols equal HELPER names", hs["ok"] == hs["checked"] and not hs["mismatch"],
              hs)
        c.add("snapshot id stable on re-open", snap.snapshot_id == s["snapshot_id"])
        self.state["snap_obj"] = snap
        self.results["T-B1-3"] = c.result()

    def t8(self) -> None:
        c = Checks()
        ev = self.state["snap"] / "evidence.jsonl"
        c.ev(ev)
        rows = [json.loads(x) for x in ev.read_text().splitlines()]
        rec = [r for r in rows if r.get("check") == "recovery"]
        units = len(self.state["snap_obj"].units())
        c.add("a recovery row per unit", len(rec) == units, f"{len(rec)} rows, {units} units")
        c.add("every recovery row passes", all(r["result"] == "pass" for r in rec),
              [r for r in rec if r["result"] != "pass"][:3])
        n = 0
        for r in rec:
            try:
                n += int(str(r.get("detail", "")).split()[0])
            except (ValueError, IndexError):
                pass
        if self.wl == "minimal":
            c.add("raddr + helper/plugin returns checked == 336 + 58 + 3", n == 397, n)
        c.notes.append(f"{n} return offsets unwound to their OpMark insn")
        c.notes.append("synthetic shifted host_end -> recovery_mismatch: "
                       "tests/acceptance/test_b1_9_rejections.py::test_recovery_mismatch_synthetic")
        self.results["T-B1-8"] = c.result()

    def graph(self) -> None:
        c = Checks()
        d = self.out / "graph"
        d.mkdir(exist_ok=True)
        S = self.state["snap"]
        rc, _, l1 = self.scopt("graph-export", "graph", "export", S, "--out", d / "graph.json")
        rc2, _, l2 = self.scopt("graph-import", "graph", "import", d / "graph.json", "--check", S)
        c.ev(d / "graph.json", l1, l2)
        c.add("export -> import -> export byte-identical, check passes", rc == 0 and rc2 == 0,
              f"export rc={rc} import rc={rc2}")
        doc = json.loads((d / "graph.json").read_text())
        edges = doc["edges"]
        c.add("every edge has a captured exit site", all(e["exit"].get("id") for e in edges),
              len(edges))
        c.add("static edges carry candidates, never dst", all("dst" not in e for e in edges))
        dyn = [e for e in edges if e["exit"]["kind"] in ("goto_ptr", "exit_null")]
        c.add("goto_ptr/exit_null are dynamic", all(e["resolution"] == "dynamic" for e in dyn),
              len(dyn))
        c.notes.append("resolutions: " + json.dumps(collections.Counter(
            e["resolution"] for e in edges)))
        self.state["graph_checks"] = c
        self.state["graph_doc"] = doc

    def graph_profile(self, profile: Path) -> None:
        c = self.state["graph_checks"]
        d = self.out / "graph"
        S = self.state["snap"]
        rc, _, l1 = self.scopt("graph-export-profile", "graph", "export", S, "--profile", profile,
                               "--out", d / "graph-profile.json")
        rc2, _, l2 = self.scopt("graph-import-profile", "graph", "import",
                                d / "graph-profile.json", "--check", S)
        c.ev(d / "graph-profile.json", l1, l2)
        c.add("profile graph round trip and check", rc == 0 and rc2 == 0, f"{rc} {rc2}")
        doc = json.loads((d / "graph-profile.json").read_text())
        no_ev = [e for e in doc["edges"] if not e["exit"].get("id") and not e.get("observed")]
        c.add("every edge has a captured exit site or profile evidence", not no_ev,
              f"{len(doc['edges'])} edges, {len(no_ev)} without evidence")

    def graph_pair(self, diag: dict[str, Any]) -> None:
        c = self.state["graph_checks"]
        snap = self.state["snap_obj"]
        by_slot = {u.slot: u for u in snap.units()}
        static = {(e["src"], e["exit"].get("slot")): e for e in self.state["graph_doc"]["edges"]
                  if e["exit"]["kind"] == "goto_tb"}
        links = diag.get("linking") or []
        c.add("diag exec printed Linking TBs lines", bool(links), len(links))
        if not links:
            return
        bad = []
        for ln in links:
            src, dst = by_slot.get(ln["src_slot"]), by_slot.get(ln["dst_slot"])
            e = static.get((src.uid, ln["index"])) if src else None
            if not (src and dst and e and dst.uid in e["candidates"]):
                bad.append(ln)
        c.add("every runtime link is a static goto_tb edge with dst among candidates", not bad,
              f"{len(links)} links, {len(bad)} unexplained: {bad[:3]}")
        ln = links[0]
        a, b = by_slot[ln["src_slot"]], by_slot[ln["dst_slot"]]
        d = self.out / "graph"
        S = self.state["snap"]
        rc, _, l1 = self.scopt("graph-export-pair", "graph", "export", S, "--unit", a.uid,
                               "--unit", b.uid, "--out", d / "pair.json")
        rc2, _, l2 = self.scopt("graph-import-pair", "graph", "import", d / "pair.json",
                                "--check", S)
        c.ev(d / "pair.json", l1, l2)
        c.add("two-TB goto_tb pair: round trip and check", rc == 0 and rc2 == 0, f"{rc} {rc2}")
        pair = json.loads((d / "pair.json").read_text())
        e = next((x for x in pair["edges"] if x["src"] == a.uid and
                  x["exit"]["kind"] == "goto_tb" and x["exit"].get("slot") == ln["index"]), None)
        c.add("pair edge: candidates == [dst], candidates_unique",
              e is not None and e["candidates"] == [b.uid]
              and e["resolution"] == "candidates_unique",
              {"link": ln, "edge": e and {k: e[k] for k in ("candidates", "resolution")}})

    def truncated(self) -> Path | None:
        """A real snapshot without the records >= N, imported with the full capture."""
        n = self.cfg["truncate"]
        if not n:
            return None
        d = self.out / "trunc"
        d.mkdir(exist_ok=True)
        sc = read_sc(self.state["prep_sc"])
        tpath = d / "trunc.sc"
        write_sc(tpath, sc.header, sc.units()[:n])
        rc, out, logp = self.scopt("import-trunc", "import", "--sc", tpath, "--scm",
                                   self.state["prep_dir"] / "capture.scm", "--manifest",
                                   self.state["prep_manifest"], "--bundle",
                                   self.out / "bundle.json", "--runtime-identity",
                                   self.out / "runtime_identity.json", "--store",
                                   self.out / "store-trunc", "--json")
        if rc != 0:
            return None
        s = json.loads(out)
        tdir = self.out / "store-trunc/snapshots" / snapshot_dir_name(s["snapshot_id"])
        self.state["trunc_dir"] = tdir
        self.state["trunc_n"] = n
        self.state["trunc_appended_expected"] = len(sc.units()) - n
        # T-B1-4: a seeded destination absent from the snapshot -> not_in_snapshot
        c = self.state["graph_checks"]
        rc, _, l1 = self.scopt("graph-export-trunc", "graph", "export", tdir, "--out",
                               d / "graph.json")
        c.ev(d / "graph.json", logp, l1)
        tdoc = json.loads((d / "graph.json").read_text())
        full = {(e["src"], e["exit"].get("id")): e for e in self.state["graph_doc"]["edges"]}
        kept = set(read_sc(tpath).unit_ids())
        nis = [e for e in tdoc["edges"] if e["resolution"] == "not_in_snapshot"]
        # an edge of the truncated graph is not_in_snapshot either because the cold
        # run never translated its destination (also not_in_snapshot in the full
        # graph), or because truncation dropped every candidate (the seeded case)
        seeded, already, other = [], [], []
        for e in nis:
            fe = full.get((e["src"], e["exit"].get("id")))
            if fe is None or e["candidates"]:
                other.append(e)
            elif fe["resolution"] == "not_in_snapshot":
                already.append(e)
            elif fe["candidates"] and not set(fe["candidates"]) & kept:
                seeded.append(e)
            else:
                other.append(e)
        n_full = sum(1 for e in self.state["graph_doc"]["edges"]
                     if e["resolution"] == "not_in_snapshot")
        c.add("seeded destination absent from the snapshot -> not_in_snapshot "
              "(real truncated snapshot)", rc == 0 and bool(seeded) and not other,
              f"{len(nis)} not_in_snapshot edges in the truncated graph: {len(seeded)} whose "
              f"full-snapshot candidates were all dropped, {len(already)} already "
              f"not_in_snapshot in the full graph ({n_full} there: destinations never "
              f"translated), {len(other)} unexplained")
        return tdir

    def profile(self) -> None:
        c6, c7 = Checks(), Checks()
        S = self.state["snap"]
        d = self.out / "prof"
        args = ["profile", "run", "-c", self.base, "--snapshot", S, "--reps", self.profile_reps,
                "--out", d, "--store", self.out / "store", "--qemu", self.qemu,
                "--watch", self.state["prep_sc"], "--watch", self.state["prep_manifest"],
                *self.inputs_args()]
        for ph in self.cfg["phases"]:
            args += ["--phase", ph]
        rc, _, logp = self.scopt("profile-run", *args)
        pr = json.loads((d / "profile_run.json").read_text()) if \
            (d / "profile_run.json").is_file() else {}
        c6.ev(d / "profile_run.json", logp)
        c7.ev(d / "profile_run.json")
        c6.add("profile run PASS (proof, ingest)", rc == 0 and pr.get("verdict") == "PASS",
               pr.get("failures"))
        if not pr.get("merged"):
            self.results["T-B1-6"] = c6.result()
            self.results["T-B1-7"] = c7.result()
            return
        merged = Path(pr["merged"]["path"])
        self.state["profile"] = merged
        c6.ev(merged)
        c6.add("flow conservation holds in every run",
               all(r.get("flow_ok") for r in pr["runs"]), [r.get("flow_ok") for r in pr["runs"]])
        rc, out, l2 = self.scopt("profile-check", "profile", "check", merged)
        c6.add("flow conservation holds on the merged profile (every unit and phase)", rc == 0,
               out.strip()[:300])
        p = json.loads(merged.read_text())
        c6.add("complete, method exact_dispatch_unchained_v1",
               p["collection"]["complete"] and
               p["collection"]["method"] == "exact_dispatch_unchained_v1")
        statuses = collections.Counter(u["status"] for u in p["units"])
        c6.add("every unit exact", set(statuses) == {"exact"}, dict(statuses))
        disp = {r["total_dispatches"] for r in p["collection"]["runs"]}
        c6.add("total dispatches equal across runs (deterministic)", len(disp) == 1, sorted(disp))
        c7.add("isolation: target, prep .sc and manifest unchanged after profiling",
               pr["isolation"]["ok"], pr["isolation"]["changed"][:5])
        c7.notes.append(f"{pr['isolation']['files']} files hashed before/after")
        self.state["c6"], self.state["c7"] = c6, c7
        self.state["profile_run"] = pr

    def profile_oracle(self) -> None:
        """T-B1-6 Trace oracle: a direct QEMU run with -d exec,nochain of the deployed snapshot."""
        c6 = self.state.get("c6")
        if c6 is None:
            return
        S = self.state["snap"]
        d = self.out / "t-b1-6"
        rc, _, l1 = self.scopt("deploy-nochain", "deploy", S, "-c", self.base, "--run-dir",
                               d / "rd", "--qemu", self.qemu)
        rc2, out, l2 = self.scopt("diag-nochain", "diag", "exec", "--run-dir", d / "rd", "--out",
                                  d / "nochain", "--nochain", "--no-int", "--json")
        c6.ev(d / "nochain/diag.json", l1, l2)
        if rc != 0 or rc2 != 0 or not (d / "nochain/diag.json").is_file():
            c6.add("nochain oracle ran", False, f"deploy rc={rc} diag rc={rc2}")
            return
        diag = json.loads((d / "nochain/diag.json").read_text())
        c6.add("nochain run exit 0", diag["exit_code"] == 0, diag["exit_code"])
        trace = {(int(k["pc"], 16), int(k["flags"], 16), int(k["cs_base"], 16)): k["count"]
                 for k in diag["entries_by_key"]}
        # one run's profile (rep-0), keyed like the Trace lines: (first vaddr, flags, cs_base)
        p0 = json.loads(Path(self.state["profile_run"]["runs"][0]["profile"]).read_text())
        sc = read_sc(Path(S) / "cache.sc")
        rec = {uid: u.record for uid, u in zip(sc.unit_ids(), sc.units())}
        prof: collections.Counter = collections.Counter()
        for u in p0["units"]:
            r = rec[u["unit"]]
            key = (int(u["vaddrs"][0], 16), r.flags, r.cs_base)
            prof[key] += sum(ph["entries"] for ph in u["phase"].values())
        extra = {"profile_only": len(p0["profile_only"]),
                 "transient_entries": p0["transient"]["entries"]}
        diffs = {f"0x{k[0]:x}/0x{k[1]:x}/0x{k[2]:x}": (prof.get(k, 0), trace.get(k, 0))
                 for k in set(prof) | set(trace) if prof.get(k, 0) != trace.get(k, 0)}
        exact = self.state.get("t1_mode") == "whole_file"
        tot_p, tot_t = sum(prof.values()), sum(trace.values())
        c6.add("sum entries per (vaddr, flags, cs_base) == -d exec,nochain Trace count"
               + (" (exact: T-B1-1a deterministic)" if exact else " (tolerance)"),
               (not diffs and not extra["profile_only"] and not extra["transient_entries"])
               if exact else abs(tot_p - tot_t) <= 0.01 * max(tot_t, 1),
               {"keys": len(trace), "profile_total": tot_p, "trace_total": tot_t,
                "differing_keys": dict(list(diffs.items())[:5]), **extra})
        merged = json.loads(Path(self.state["profile"]).read_text())
        if self.wl == "minimal":
            hot = [u for u in merged["units"] if "0x8018766" in u["vaddrs"]]
            runs = len(merged["collection"]["runs"])
            boot = sum(u["phase"]["0"]["entries"] for u in hot) / runs
            steady = sum(u["phase"].get("1", {}).get("entries", 0) for u in hot) / runs
            c6.add("the 0x08018766 TB: ~19.4k entries, all in boot",
                   bool(hot) and 19000 <= boot <= 19800 and steady == 0,
                   f"{len(hot)} unit(s); per run boot={boot} steady={steady}")
            ph = merged["phases"]
            st = next((x for x in ph if x["name"] == "steady"), None)
            c6.add("phase steady reached at the 0x08003972 marker",
                   st is not None and st["reached"] and st["marker"] == "0x8003972",
                   st)
        self.results["T-B1-6"] = c6.result()

    def profile_trunc(self, tdir: Path | None) -> None:
        c7 = self.state.get("c7")
        if c7 is None:
            return
        if tdir is None:
            c7.notes.append("PROFILE_ONLY on a truncated snapshot: not run for this workload")
            return
        d = self.out / "prof-trunc"
        args = ["profile", "run", "-c", self.base, "--snapshot", tdir, "--reps", 1, "--out", d,
                "--qemu", self.qemu]
        for ph in self.cfg["phases"]:
            args += ["--phase", ph]
        rc, _, logp = self.scopt("profile-run-trunc", *args)
        c7.ev(d / "profile_run.json", logp)
        pr = json.loads((d / "profile_run.json").read_text()) if \
            (d / "profile_run.json").is_file() else {}
        c7.add("truncated-snapshot profile run PASS", rc == 0 and pr.get("verdict") == "PASS",
               pr.get("failures"))
        if not pr.get("runs") or not pr["runs"][0].get("profile"):
            return
        r0 = pr["runs"][0]
        p = json.loads(Path(r0["profile"]).read_text())
        want = self.state["trunc_appended_expected"]
        c7.add("appended slots are reported as PROFILE_ONLY",
               r0["appended"] == want and len(p["profile_only"]) == want
               and len(p["units"]) == self.state["trunc_n"],
               f"appended={r0['appended']} profile_only={len(p['profile_only'])} "
               f"snapshot units={len(p['units'])} (expected {want} appended)")
        tuids = set(read_sc(Path(tdir) / "cache.sc").unit_ids())
        c7.add("profile units are exactly the truncated snapshot's (no PROFILE_ONLY merged in)",
               {u["unit"] for u in p["units"]} == tuids)

    def optimize(self) -> bool:
        c = Checks()
        S = self.state["snap"]
        args = ["optimize", S, "--pipeline", "identity", "--store", self.out / "store", "--json"]
        if self.state.get("profile"):
            args += ["--profile", self.state["profile"]]
        rc, out, logp = self.scopt("optimize-identity", *args)
        c.ev(logp)
        self.state["c5"] = c
        if rc != 0:
            c.add("optimize --pipeline identity publishes", False, f"rc={rc}")
            return False
        res = json.loads(out)
        A = Path(res["artifact_dir"])
        self.state["artifact"] = A
        self.state["artifact_id"] = res["artifact_id"]
        c.ev(A, A / "validation.json", A / "cache.sc.scopt.json", A / "ledger.jsonl")
        codes = res["validation"]
        c.add("V1-V9 pass", all(codes.get(f"V{i}") == "pass" for i in range(1, 10)), codes)
        c.add("V9: sha256(out cache.sc) == sha256(in cache.sc)",
              sha256_sparse(A / "cache.sc") == sha256_sparse(S / "cache.sc"))
        c.add("output snapshot id == input", res["output_snapshot_id"] == self.state["snapshot_id"])
        scms = sorted(A.glob("cache*.scm"))
        sargs = ["import", "--sc", A / "cache.sc"]
        for s in scms:
            sargs += ["--scm", s]
        rc2, out2, l2 = self.scopt("reimport-artifact", *sargs, "--json")
        c.ev(l2)
        ok = False
        if rc2 == 0:
            s2 = json.loads(out2)
            ok = s2["snapshot_id"] == self.state["snapshot_id"] and \
                s2["capture"]["binding"] == {"bound": s2["records"]["valid"]}
        c.add("derived .scm re-imports to the same SnapshotId, every slot bound", ok,
              f"rc={rc2} captures={[s.name for s in scms]}")
        rc3, out3, l3 = self.scopt("validate-static", "validate", A)
        c.ev(l3)
        c.add("scopt validate A (V1-V9 re-validation, MANIFEST)", rc3 == 0, out3.strip()[-200:])
        return True

    def profile_artifact(self) -> None:
        c7 = self.state.get("c7")
        if c7 is None or not self.cfg["phases"]:
            return
        A = self.state["artifact"]
        d = self.out / "prof-artifact"
        args = ["profile", "run", "-c", self.base, "--snapshot", A, "--artifact", "--reps", 1,
                "--out", d, "--qemu", self.qemu, "--watch", self.state["snap"], "--watch",
                self.out / "store/artifacts", "--watch", self.state["prep_manifest"]]
        for ph in self.cfg["phases"]:
            args += ["--phase", ph]
        rc, _, logp = self.scopt("profile-run-artifact", *args)
        pr = json.loads((d / "profile_run.json").read_text()) if \
            (d / "profile_run.json").is_file() else {}
        c7.ev(d / "profile_run.json", logp)
        c7.add("profiling the published artifact leaves it, the snapshot and the base manifest "
               "unchanged", rc == 0 and pr.get("isolation", {}).get("ok") is True,
               (pr.get("failures"), pr.get("isolation", {}).get("files")))

    def v10(self) -> None:
        c = self.state["c5"]
        A = self.state["artifact"]
        args = ["validate", A, "--dynamic", "-c", self.base, "--out", self.out / "v10",
                "--qemu", self.qemu, "--timeout", self.timeout]
        for i in self.cfg["heldout"] or self.cfg["inputs"]:
            args += ["--inputs", i]
        rc, out, logp = self.scopt("validate-dynamic", *args)
        dj = self.out / "v10/dynamic.json"
        c.ev(dj, self.out / "v10/ledger.jsonl", logp)
        doc = json.loads(dj.read_text()) if dj.is_file() else {}
        c.add("V10 (validate --dynamic) pass", rc == 0 and doc.get("verdict") == "pass",
              doc.get("failures"))

    def runs(self) -> dict[str, Any] | None:
        c = self.state["c5"]
        A = self.state["artifact"]
        d = self.out / "timing"
        rc, out, logp = self.scopt("run-warm-identity", "run", "-c", self.base, "--arms",
                                   "warm,identity", "--warm", self.state["snap"], "--artifact",
                                   f"identity={A}", "--reps", self.reps, "--qemu", self.qemu,
                                   "--timeout", self.timeout, "--out", d, *self.inputs_args())
        c.ev(d / "campaign.json", d / "runs.jsonl", d / "report.md", logp)
        rows = [json.loads(x) for x in (d / "runs.jsonl").read_text().splitlines()] \
            if (d / "runs.jsonl").is_file() else []
        arms = collections.Counter(r["arm"] for r in rows)
        c.add(f"warm x{self.reps} and identity x{self.reps}, interleaved, every run a valid "
              "sample", rc == 0 and arms == {"warm": self.reps, "identity": self.reps}
              and all(r["valid"] for r in rows), dict(arms))
        c.add("post-run proof PASS in every run", all(r["verdict"] == "PASS" for r in rows))
        c.add("appended = 0 in both arms", all(r["appended"] == 0 for r in rows),
              sorted({(r["arm"], r["appended"]) for r in rows}))
        norms = {r["output_norm_sha256"] for r in rows}
        gnorms = {r["guest_norm_sha256"] for r in rows}
        c.add("equal normalized outputs (warm vs identity)", len(norms) == 1 and len(gnorms) == 1,
              {"output_norm": sorted(norms), "guest_norm": sorted(gnorms)})
        order = [r["arm"] for r in sorted(rows, key=lambda r: r["started"])]
        c.notes.append(f"run order: {','.join(order)}")
        ident = sorted((r for r in rows if r["arm"] == "identity"), key=lambda r: r["rep"])
        return ident[0] if ident else None

    def diag(self, ident_row: dict[str, Any] | None) -> dict[str, Any] | None:
        c = self.state["c5"]
        if ident_row is None:
            c.add("diag exec on an identity run", False, "no identity run")
            return None
        rc, out, logp = self.scopt("diag-identity", "diag", "exec", "--run-dir",
                                   ident_row["run_dir"], "--out", self.out / "diag-identity",
                                   "--json", "--timeout", self.timeout)
        dj = self.out / "diag-identity/diag.json"
        c.ev(dj, logp)
        if not dj.is_file():
            c.add("diag exec ran", False, f"rc={rc}")
            return None
        d = json.loads(dj.read_text())
        big = self.out / "diag-identity/qemu.log"
        if big.is_file() and big.stat().st_size > 512 << 20:
            c.notes.append(f"diag-identity/qemu.log ({big.stat().st_size} bytes) deleted after "
                           "parsing; diag.json holds the per-tc and per-key counts")
            big.unlink()
        sc = read_sc(self.state["artifact"] / "cache.sc")
        c.add("diag exec: every Trace tc.ptr == code_address + tc_offset of an artifact record",
              d["exit_code"] == 0 and d["all_traces_in_snapshot"] and not d["tc_not_in_snapshot"]
              and d["sc"] == str(self.state["artifact"] / "cache.sc"),
              {"trace_lines": d["trace_lines"], "unique_tc": d["unique_tc"],
               "tc_in_snapshot": d["tc_in_snapshot"], "records": len(sc.valid_slots),
               "outside": d["tc_not_in_snapshot"][:5], "inputs": d.get("inputs")})
        return d

    def report(self) -> dict[str, Any]:
        rc, _, logp = self.scopt("report", "report", self.out, "--out",
                                 self.out / "report/report.md")
        rj = self.out / "report/report.json"
        res: dict[str, Any] = {"report": str(self.out / "report/report.md"),
                               "report_json": str(rj), "rc": rc}
        if not rj.is_file():
            res["verdict"] = "fail"
            return res
        model = json.loads(rj.read_text())
        aid = self.state.get("artifact_id")
        row = next((t for t in model["ledger"]["table"] if t["subject"] == "artifact"
                    and t["id"] == aid), None)
        res["artifact_id"] = aid
        res["row"] = row
        need = ("inspected", "compiled", "accepted", "validated_dynamic")
        if row is None:
            res["verdict"] = "fail"
            res["stages"] = {}
        else:
            res["stages"] = {s: row["stages"][s]["verdict"] for s in row["stages"]}
            res["verdict"] = "pass" if all(res["stages"].get(s) == "pass" for s in need) and \
                res["stages"].get("measured") == "measured" else "fail"
        units = model["ledger"].get("units", {})
        res["unit_inspected"] = units.get("inspected")
        return res

    def t9(self) -> None:
        c = Checks()
        d = self.out / "t-b1-9"
        d.mkdir(exist_ok=True)
        env = dict(self.env)
        env["B1_GATE_DIR"] = str(self.out)
        argv = [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                "tests/acceptance/test_b1_9_rejections.py", "-rA",
                f"--junitxml={d / 'junit.xml'}"]
        p = subprocess.run(argv, cwd=str(WT), env=env, capture_output=True, text=True,
                           timeout=3600)
        (d / "pytest.out").write_text(p.stdout + p.stderr)
        c.ev(d / "junit.xml", d / "pytest.out")
        tail = [ln for ln in p.stdout.splitlines() if ln.strip()][-1:] or [""]
        skipped = "skipped" in tail[0]
        c.add("rejection suite passes (each case asserts its reason code)",
              p.returncode == 0 and not skipped, tail[0])
        self.results["T-B1-9"] = c.result()

    def t0(self, path: Path | None) -> None:
        c = Checks()
        if path is None or not path.is_file():
            self.results["T-B1-0"] = c.result(not_run="no T-B1-0 driver result given")
            return
        doc = json.loads(path.read_text())
        w = doc.get("workloads", {}).get(self.wl)
        c.ev(path)
        if w is None:
            self.results["T-B1-0"] = c.result(not_run=f"{path} has no {self.wl} workload")
            return
        rq = w["requalification"]
        c.add("re-qualification (nocache, cold, warm x2): outputs, appended 0, proof PASS",
              rq["verdict"] == "PASS", rq["failures"][:5])
        c.add("new binary is the integrated one",
              doc["new_qemu_sha256"] == sha256_file(self.qemu), doc["new_qemu_sha256"])
        tm = w.get("timing")
        if tm:
            s = tm["stats"]
            c.add(f"timing: {tm['pairs_requested']} interleaved pairs collected",
                  tm["pairs_valid"] >= tm["pairs_requested"],
                  f"{tm['pairs_valid']}/{tm['pairs_requested']}")
            # the flag rule of t_b1_0.flag_status, recomputed for older t-b1-0.json files
            # that only recorded "median > 1%"
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "t_b1_0", Path(__file__).resolve().parent / "t_b1_0.py")
            t0 = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(t0)
            flag = s.get("flag") or t0.flag_status(s["overhead_median"], *s["overhead_ci95"])
            c.notes.append(
                "disabled-hook cost (not gated): median old={:.1f}us new={:.1f}us overhead={:+.2f}% "
                "CI95=[{:+.2f}%, {:+.2f}%] paired={:+.2f}% CI95=[{:+.2f}%, {:+.2f}%] flag={}"
                .format(s["median_old"] / 1e3, s["median_new"] / 1e3, s["overhead_median"] * 100,
                        s["overhead_ci95"][0] * 100, s["overhead_ci95"][1] * 100,
                        s["paired"]["median_ratio_minus_1"] * 100, s["paired"]["ci95"][0] * 100,
                        s["paired"]["ci95"][1] * 100, flag))
            self.state["t0_flagged"] = flag == "flagged"
            self.state["t0_flag"] = flag
        else:
            c.notes.append("timing part not run")
        self.results["T-B1-0"] = c.result()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workload", choices=sorted(WORKLOADS), required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--qemu", type=Path, default=INT_QEMU)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--profile-reps", type=int, default=3)
    ap.add_argument("--t-b1-0", dest="t_b1_0", type=Path)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--skip-t9", action="store_true")
    ap.add_argument("--config", type=Path, help="override the workload's base TOML (e.g. a "
                    "copy whose [Machine].qemu_path names --qemu; WP22 T-B3-27)")
    a = ap.parse_args(argv)
    out = a.out.resolve()
    if out.exists() and any(out.iterdir()):
        ap.error(f"{out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    qemu = a.qemu.resolve()
    g = Gate(a.workload, out, qemu, a.reps, a.profile_reps, a.timeout)
    if a.config:
        g.base = a.config.resolve()
    head = subprocess.run(["git", "-C", str(WT), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(WT), "status", "--porcelain"], capture_output=True,
                           text=True).stdout.strip()
    doc: dict[str, Any] = {
        "schema": "scopt.b1_gate/1", "workload": a.workload, "out": str(out),
        "config": str(g.base), "config_sha256": sha256_file(g.base),
        "inputs": [str(i) for i in g.cfg["inputs"]],
        "qemu": str(qemu), "qemu_sha256": sha256_file(qemu),
        "scopt_worktree": str(WT), "scopt_head": head, "scopt_dirty": bool(dirty),
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "loadavg_start": os.getloadavg(),
        "b0_golden_sha256_before": sha256_sparse(B0_GOLDEN) if B0_GOLDEN.is_file() else None,
    }
    t_start = time.monotonic()
    g.log(f"start: config={g.base} qemu={qemu}")
    g.t1()
    prep_ok = g.prepare()
    if not prep_ok:
        for t in TESTS[2:]:
            g.results.setdefault(t, Checks().result(not_run="prepare/bundle failed"))
    else:
        g.t2()
        if not g.import_():
            for t in ("T-B1-3", "T-B1-4", "T-B1-5", "T-B1-6", "T-B1-7", "T-B1-8"):
                g.results[t] = Checks().result(not_run="import rejected")
        else:
            g.t3()
            g.t8()
            g.graph()
            tdir = g.truncated()
            g.profile()
            if g.gated("T-B1-6"):
                g.profile_oracle()
            elif "c6" in g.state:
                # -d exec,nochain over the ~1 s FreeRTOS window wrote > 12 GB of log
                # before it was stopped (runs/wp14/dev/gate-frt1); minimal only.
                g.state["c6"].notes.append(
                    "nochain Trace oracle not run on this workload (T-B1-6 is gated on "
                    "minimal; a -d exec,nochain log of the FreeRTOS window exceeds 12 GB)")
            g.profile_trunc(tdir)
            if g.state.get("profile"):
                g.graph_profile(g.state["profile"])
            if g.optimize():
                g.profile_artifact()
                g.v10()
                ident = g.runs()
                diag = g.diag(ident)
                if diag:
                    g.graph_pair(diag)
            g.results["T-B1-5"] = g.state["c5"].result()
            g.results["T-B1-4"] = g.state["graph_checks"].result()
            if "c7" in g.state:
                g.results["T-B1-7"] = g.state["c7"].result()
            g.results.setdefault("T-B1-6", (g.state.get("c6") or Checks()).result())
    if a.workload == "minimal" and not a.skip_t9:
        g.t9()
    else:
        g.results.setdefault("T-B1-9", Checks().result(
            not_run="T-B1-9 is gated on minimal only" if a.workload != "minimal"
            else "--skip-t9"))
    g.t0(a.t_b1_0)
    doc["ledger"] = g.report() if g.state.get("artifact_id") else {"verdict": "not_run"}
    for t in TESTS:
        r = g.results.get(t) or Checks().result(not_run="not reached")
        r["gated"] = g.gated(t)
        g.results[t] = r
    doc["tests"] = {t: g.results[t] for t in TESTS}
    doc["snapshot_id"] = g.state.get("snapshot_id")
    doc["artifact_id"] = g.state.get("artifact_id")
    doc["artifact_dir"] = str(g.state["artifact"]) if g.state.get("artifact") else None
    doc["profile"] = str(g.state["profile"]) if g.state.get("profile") else None
    doc["b0_golden_sha256_after"] = sha256_sparse(B0_GOLDEN) if B0_GOLDEN.is_file() else None
    doc["b0_golden_unchanged"] = doc["b0_golden_sha256_after"] == doc["b0_golden_sha256_before"]
    gated = [t for t in TESTS if doc["tests"][t]["gated"]]
    doc["gated_tests"] = gated
    doc["verdict"] = "PASS" if all(doc["tests"][t]["verdict"] == "pass" for t in gated) and \
        doc["ledger"].get("verdict") == "pass" and doc["b0_golden_unchanged"] else "FAIL"
    doc["loadavg_end"] = os.getloadavg()
    doc["seconds"] = round(time.monotonic() - t_start, 1)
    doc["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    (out / "gate.json").write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")
    for t in TESTS:
        r = doc["tests"][t]
        g.log(f"{t}: {r['verdict']}{'' if r['gated'] else ' (informational)'}"
              + (f" failed={r['failed']}" if r["failed"] else "")
              + (f" ({r['not_run_reason']})" if r["not_run_reason"] else ""))
    g.log(f"ledger: {doc['ledger'].get('verdict')} {doc['ledger'].get('stages')}")
    g.log(f"verdict={doc['verdict']} ({doc['seconds']}s)")
    return 0 if doc["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

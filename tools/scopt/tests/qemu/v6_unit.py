#!/usr/bin/env python3
"""QEMU acceptance of the semantic extension inside a v7 SuperCache.

    v6_unit.py --qemu QINT_V6 --out OUT [--only CASE ...] [--keep]

Chain (every QEMU run through the scopt harness: fresh run dir, oracle
deploy, ``setarch x86_64 -R``, timeout, ``supercache_flat=false``):

1. ``scopt prepare`` + bundle/runtime identity + ``scopt import`` of the
   minimal fixture (``src/fastdyn/configs/b0-sc.toml``) **on the given
   binary** (``runtime_signature`` binds the file to the exe);
2. Public control: the base snapshot warm (proof PASS, appended 0, B0 output);
3. ``identity-semantic``: the same v7 file with an empty extension table
   (EXT_TABLE only) -> exit 0, proof PASS, appended 0, guest output equal to
   the public control, ``QEMU_SC_V6_STATS`` reports the table loaded;
4. ``guarded``: grown R units with SPEC slots + variants (relocated clones of
   C) and GENERAL slots (``v6_synth.py``) -> exit 0, output equal, proof PASS,
   appended 0, links armed, no invariant violation; a second warm run with
   ``QEMU_SC_V6_STATS`` unset gives the same output;
5. ``stale-signature``: identity-semantic with another ``runtime_signature`` ->
   ``supercache: discarding v7 file: runtime signature`` and the stock
   truncate + cold run (exit 0, proof INVALID by design);
6. one malformed file per mutation of ``v6_synth.MUTATIONS`` -> QEMU exits
   non-zero with ``supercache: invalid v6 extension: <reason>``, no guest line
   was printed (the check runs in the code-buffer setup, before the machine
   starts), and the deployed ``.sc`` is unchanged (sparse sha256, size and
   inode before == after: no truncation, R2).

Writes ``OUT/v6-unit.json`` (verdict per case, evidence paths). Large QEMU
logs are not produced (no ``-d``).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

WT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scopt.formats.sc_records import ScHeader, sha256_sparse  # noqa: E402
from scopt.formats.layouts import SC_VERSION
from scopt.formats.sc_semantic import SC7_SEMANTIC_MARKER
from scopt.run.deploy import deploy_new  # noqa: E402
from scopt.run.launcher import file_sha256, launch  # noqa: E402

import v6_synth as V  # noqa: E402

ROOT = Path(os.environ.get("FASTDYN_ROOT") or Path(__file__).resolve().parents[4])
PY = sys.executable
BASE_TOML = ROOT / "src/fastdyn/configs/b0-sc.toml"
B0_NORM = ROOT / "runs/b0/warm1.log"

# Default link set on the minimal fixture (R vaddr): SPEC links into
# vStartLEDFlashTasks (printreg virtual), the 0x80039d4 helper (4 callers),
# 0x8003a02 and 0x8006674; GENERAL slots on two more callers.
SPEC = (0x8003972, 0x800054C, 0x800056C, 0x800057A, 0x8000588, 0x80006A6, 0x8000D56)
GENERAL = (0x80006BA, 0x8000594)


def sh(cmd: list[Any], log: Path, **kw: Any) -> int:
    with open(log, "w") as f:
        return subprocess.run([str(c) for c in cmd], stdout=f, stderr=subprocess.STDOUT,
                              cwd=str(WT), **kw).returncode


class Unit:
    def __init__(self, qemu: Path, out: Path) -> None:
        self.qemu = qemu
        self.out = out
        self.cases: dict[str, Any] = {}

    # -- base snapshot ----------------------------------------------------
    def prepare(self) -> None:
        o = self.out / "base"
        o.mkdir(parents=True, exist_ok=True)
        prep = o / "prep"
        rc = sh([PY, "-m", "scopt", "prepare", "-c", BASE_TOML, "--out", prep, "--qemu",
                 self.qemu, "--timeout", 120], o / "prepare.log")
        pj = json.loads((prep / "prep.json").read_text())
        if rc or pj.get("verdict") != "PASS":
            raise SystemExit(f"prepare failed: rc={rc} {pj.get('failures')}")
        rc = sh([PY, "-m", "scopt", "bundle", "identity", "-c", prep / "run.toml", "--manifest",
                 pj["manifest"], "--out", o / "bundle.json"], o / "bundle.log")
        rc |= sh([PY, "-m", "scopt", "bundle", "runtime-identity", "--run-dir", prep / "oracle",
                  "--sc", pj["sc"], "--scm", prep / "capture.scm", "--out",
                  o / "runtime_identity.json"], o / "ri.log")
        with open(o / "import.json", "w") as f:
            rc |= subprocess.run([PY, "-m", "scopt", "import", str(prep), "--bundle",
                                  str(o / "bundle.json"), "--runtime-identity",
                                  str(o / "runtime_identity.json"), "--store", str(o / "store"),
                                  "--json"], stdout=f, stderr=subprocess.DEVNULL,
                                 cwd=str(WT)).returncode
        if rc:
            raise SystemExit("bundle/import failed")
        s = json.loads((o / "import.json").read_text())
        from scopt.importer.import_cache import open_snapshot
        from scopt.model.cache_snapshot import snapshot_dir_name
        self.base_sc = Path(pj["sc"])
        self.snap = open_snapshot(o / "store/snapshots" / snapshot_dir_name(s["snapshot_id"]))
        self.base_info = {"sc": str(self.base_sc), "sc_sha256": sha256_sparse(self.base_sc),
                          "snapshot_id": s["snapshot_id"], "status": s["status"]}

    # -- one run -------------------------------------------------------------
    def run(self, name: str, sc: Path, *, stats: bool = True, timeout: float = 120,
            prove: bool | None = None) -> dict[str, Any]:
        rd_path = self.out / "runs" / name
        rd, dep = deploy_new(sc, BASE_TOML, rd_path, qemu=self.qemu)
        before = {"sha256": sha256_sparse(rd.cache), "size": rd.cache.stat().st_size,
                  "inode": rd.cache.stat().st_ino}
        env = {"QEMU_SC_V6_STATS": str(rd.root / "v6-stats.json")} if stats else None
        r = launch(rd, timeout_s=timeout, extra_env=env, prove=prove)
        after = {"sha256": sha256_sparse(rd.cache) if rd.cache.exists() else None,
                 "size": rd.cache.stat().st_size if rd.cache.exists() else None,
                 "inode": rd.cache.stat().st_ino if rd.cache.exists() else None}
        text = Path(r["stdout_log"]).read_text(errors="replace")
        st = rd.root / "v6-stats.json"
        return {"run_dir": str(rd.root), "exit_code": r["exit_code"], "timed_out": r["timed_out"],
                "qemu": r.get("qemu"), "qemu_sha256": r.get("qemu_sha256"),
                "proof": (r.get("proof") or {}).get("verdict"),
                "appended": (r.get("proof") or {}).get("appended"),
                "guest_norm": r["guest_norm"], "output_norm_sha256": r["output_norm_sha256"],
                "before": before, "after": after,
                "stats": json.loads(st.read_text()) if st.is_file() else None,
                "log_lines": [l for l in text.splitlines()
                              if "supercache" in l and ("discarding" in l or "invalid" in l)][:8]}

    def header(self, run):
        with Path(run["run_dir"], "cache/minimal.sc").open("rb") as stream:
            return ScHeader.from_bytes(stream.read(80))

    def check(self, name: str, ok_items: list[tuple[str, bool, Any]], run: dict[str, Any],
              extra: dict[str, Any] | None = None) -> None:
        verdict = "PASS" if all(ok for _, ok, _ in ok_items) else "FAIL"
        self.cases[name] = {"verdict": verdict,
                            "checks": [{"check": c, "ok": bool(ok), "detail": d}
                                       for c, ok, d in ok_items],
                            "run": run, **(extra or {})}
        print(f"[{verdict}] {name}" + "".join(f"\n    FAIL {c}: {d}" for c, ok, d in ok_items
                                              if not ok), flush=True)

    # -- cases ----------------------------------------------------------------
    def case_public(self) -> None:
        r = self.run("public-control", self.base_sc)
        self.public_guest = r["guest_norm"]
        st = r["stats"] or {}
        self.check("public-control", [
            ("exit 0", r["exit_code"] == 0, r["exit_code"]),
            ("proof PASS, appended 0", r["proof"] == "PASS" and r["appended"] == 0,
             (r["proof"], r["appended"])),
            ("guest output has Reg 0:0x00000003", any("Reg 0:0x00000003" in l
                                                      for l in r["guest_norm"]), r["guest_norm"]),
            ("v6 stats: not loaded", st.get("loaded") is False, st.get("loaded")),
        ], r)

    def case_identity(self) -> None:
        p = self.out / "files/identity-semantic.sc"
        meta = V.identity_model(str(self.base_sc)).write(p)
        r = self.run("identity-semantic", p)
        st = r["stats"] or {}
        header = self.header(r)
        self.check("identity-semantic", [
            ("exit 0", r["exit_code"] == 0, r["exit_code"]),
            ("proof PASS, appended 0", r["proof"] == "PASS" and r["appended"] == 0,
             (r["proof"], r["appended"])),
            ("guest output equals the public control", r["guest_norm"] == self.public_guest,
             r["guest_norm"]),
            ("stats: loaded, canonical version, 0 entries, nothing armed",
             st.get("loaded") is True and st.get("file_version") == SC_VERSION
             and (st.get("n_variants"), st.get("n_slots"), st.get("n_links")) == (0, 0, 0)
             and st.get("links_armed") == 0, {k: st.get(k) for k in (
                 "loaded", "file_version", "feature_flags", "n_variants", "n_slots", "n_links")}),
            ("semantic table retained after replay", (header.version, header.reserved) ==
             (SC_VERSION, SC7_SEMANTIC_MARKER | V.FEAT_EXT_TABLE), None),
        ], r, {"file": meta})

    def case_guarded(self) -> None:
        p = self.out / "files/guarded.sc"
        meta = V.guarded_model(str(self.base_sc), self.snap, SPEC, GENERAL).write(p)
        self.guarded_meta = meta
        r = self.run("guarded", p)
        r2 = self.run("guarded-nostats", p, stats=False)
        st = r["stats"] or {}
        refused = {k: v for k, v in (st.get("refused") or {}).items() if v}
        self.check("guarded", [
            ("exit 0", r["exit_code"] == 0 and r2["exit_code"] == 0,
             (r["exit_code"], r2["exit_code"])),
            ("proof PASS, appended 0", r["proof"] == "PASS" and r["appended"] == 0,
             (r["proof"], r["appended"])),
            ("guest output equals the public control (stats on and off)",
             r["guest_norm"] == self.public_guest and r2["guest_norm"] == self.public_guest,
             (r["guest_norm"], r2["guest_norm"])),
            ("table loaded with all entries",
             (st.get("n_variants"), st.get("n_slots"), st.get("n_links"))
             == (len(SPEC), len(SPEC) + len(GENERAL), len(SPEC)), st),
            ("at least one SPEC link armed", (st.get("links_armed") or 0) >= 1,
             (st.get("links_armed"), refused)),
            ("no invariant violation", st.get("invariant_violations") == 0,
             st.get("invariant_violations")),
        ], r, {"file": meta, "nostats_run": r2, "refused": refused})

    def case_stale(self) -> None:
        p = self.out / "files/stale-signature.sc"
        m = V.identity_model(str(self.base_sc))
        m.header = m.header.replace(runtime_signature=m.header.runtime_signature ^ 1)
        m.write(p)
        r = self.run("stale-signature", p)
        header = self.header(r)
        self.check("stale-signature", [
            ("warning 'discarding v7 file: runtime signature'",
             any("discarding v7 file: runtime signature" in l for l in r["log_lines"]),
             r["log_lines"]),
            ("exit 0 (stock truncate + cold run)", r["exit_code"] == 0, r["exit_code"]),
            ("file rewritten as a fresh public cache", r["after"]["sha256"] != r["before"]["sha256"]
             and (header.version, header.reserved) == (SC_VERSION, 0), None),
            ("guest output equals the public control (apart from the warning line)",
             [l for l in r["guest_norm"] if "discarding v7 file" not in l] == self.public_guest,
             r["guest_norm"]),
        ], r)

    def case_mutants(self, only: set[str] | None) -> None:
        for name, (needs, fn, reason) in V.MUTATIONS.items():
            if only and f"mut:{name}" not in only and "mutants" not in only:
                continue
            if needs == "i":
                m = V.identity_model(str(self.base_sc))
            elif needs == "s":
                m = V.guarded_model(str(self.base_sc), self.snap, SPEC[1:2])
            else:
                m = V.guarded_model(str(self.base_sc), self.snap, SPEC[1:2], GENERAL[:1])
            fn(m)
            p = self.out / f"files/mut-{name}.sc"
            m.write(p)
            r = self.run(f"mut-{name}", p, prove=False)
            want = f"supercache: invalid v6 extension: "
            hit = [l for l in r["log_lines"] if want in l]
            self.check(f"mut:{name}", [
                ("QEMU exits non-zero", r["exit_code"] != 0 and not r["timed_out"],
                 (r["exit_code"], r["timed_out"])),
                (f"reason contains {reason!r}", any(reason in l for l in hit), hit),
                ("no guest line printed (before guest execution)",
                 not any(l.startswith(("Reg ", "[BENCH]")) for l in r["guest_norm"]),
                 r["guest_norm"]),
                ("file not truncated or rewritten (sha256/size/inode unchanged)",
                 r["before"] == r["after"], (r["before"], r["after"])),
            ], r)
            # malformed files are 2 GiB sparse; keep only the evidence
            os.unlink(p)
            if Path(r["run_dir"], "cache/minimal.sc").exists():
                os.unlink(Path(r["run_dir"], "cache/minimal.sc"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qemu", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--only", nargs="*", help="public identity guarded stale mutants mut:<name>")
    a = ap.parse_args()
    out = a.out.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} exists and is not empty")
    (out / "files").mkdir(parents=True)
    u = Unit(a.qemu.resolve(), out)
    t0 = time.time()
    u.prepare()
    only = set(a.only) if a.only else None
    u.case_public()
    if not only or "identity" in only:
        u.case_identity()
    if not only or "guarded" in only:
        u.case_guarded()
    if not only or "stale" in only:
        u.case_stale()
    u.case_mutants(only)
    verdict = "PASS" if all(c["verdict"] == "PASS" for c in u.cases.values()) else "FAIL"
    doc = {"schema": "wp22.v6-unit/1", "verdict": verdict, "qemu": str(u.qemu),
           "qemu_sha256": file_sha256(u.qemu), "base": u.base_info,
           "seconds": round(time.time() - t0, 1),
           "summary": {k: v["verdict"] for k, v in u.cases.items()}, "cases": u.cases}
    (out / "v6-unit.json").write_text(json.dumps(doc, indent=1, default=str) + "\n")
    print(f"verdict {verdict}: {sum(v['verdict'] == 'PASS' for v in u.cases.values())}/"
          f"{len(u.cases)} cases pass -> {out / 'v6-unit.json'}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

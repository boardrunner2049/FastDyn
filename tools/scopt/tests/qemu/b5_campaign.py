#!/usr/bin/env python3
"""B5 campaign (WP30): prepare, freeze, check, run, diagnose and report the B3/B4 timing
campaign on the frozen gate binary. WP30 defines the campaign and runs ``prep``,
``freeze`` and ``check``; it never runs ``run`` (nothing is timed in WP30).

    b5_campaign.py prep   [--def DEF] [--out runs/b5/prep] [--only a,b]
    b5_campaign.py freeze [--def DEF] --prep runs/b5/prep/prep_summary.json --out CAMPAIGN.toml
    b5_campaign.py check  --campaign CAMPAIGN.toml [--expect-sha256 HEX] [--no-git]
    b5_campaign.py run    --campaign CAMPAIGN.toml --expect-sha256 HEX --out-root DIR
                          [--sets a,b] [--cpus 8-11] [--smoke --reps N]
    b5_campaign.py diag   --campaign CAMPAIGN.toml --expect-sha256 HEX --out-root DIR
                          [--splits heldout,final] [--smoke]
    b5_campaign.py plan   --campaign CAMPAIGN.toml [--expect-sha256 HEX] --out DIR
    b5_campaign.py report --campaign CAMPAIGN.toml --out-root DIR

``prep`` (untimed) resolves every arm of the definition (``campaigns/b5-*.def.toml``):
``warm`` is the gate-binary compute base; ``reuse:<json>#<path>`` takes a published
artifact recorded by WP28/WP29 together with its held-out V10 verdict; ``build:<recipe>``
builds the arm on the same base (``identity``, ``identity-v6``, ``b2-layout [--disable P]``,
``b3 [--disable P]``), re-validates it, publishes a verified copy (``scopt publish``) and
runs V10 on the held-out inputs (correctness only). Train inputs are the only profile
input; final inputs are never touched.

``freeze`` writes the frozen campaign: every identity the timing deploys or depends on
(gate exe sha256 + tag commit, plugin, firmware sha256/size/mtime, base TOML, snapshot,
training profile, RuntimeIdentity, calibration, every arm's ``cache.sc`` sparse sha256,
``MANIFEST.sha256`` and artifact id, every input and split manifest) and the git ids of
the scopt package, ``pipelines/``, the definition and this driver. Arms whose ``cache.sc``
equals an earlier arm's are excluded as duplicates. It refuses when any timing run on a
final input exists on the gate binary, or when the code to be frozen is not committed.

``check`` is the freeze checker: it recomputes every frozen value (and that the campaign
file is committed and clean, and the code at HEAD equals the frozen ids) and lists each
mismatch; exit 1 on any.

``run`` is the only timing command. It re-executes itself under ``flock`` on the
host-wide timing lock, pins ``scopt run`` to at most 4 CPUs (``taskset``), samples the load
average, runs ``check`` before the first set, before the first final set and after the
last, writes one derived ``scopt run`` campaign per set (every deploy arm with its frozen
sha256), reports each set with the B5 section, prunes the per-run cache copies and
``io.log`` files (sha256 and size kept) and evaluates the rules R1-R6 into
``OUT_ROOT/b5_campaign.json``. ``--smoke --reps N`` is the only way to change the
repetitions and refuses the final split.

``diag`` (untimed, never under the lock): base-snapshot profiles on the held-out and final
inputs (evaluation only) and one ``QEMU_SC_V6_STATS`` run per (v6 arm, input) with C'
gdb hit counts. The final split is refused until its timing sets are done.

``plan`` (untimed, no QEMU) runs ``check`` and ``scopt run --dry-run`` on every derived set
campaign: the full scopt preflight and the interleaved schedule, with run counts and a
duration estimate. WP30 runs it as the last step.

``report`` is one command: ``scopt report OUT_ROOT --b5-campaign CAMPAIGN``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
WT = HERE.parents[1]
for _p in (WT, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from b2_campaign import _hex, sha256_file, sha256_sc, toml_dumps  # noqa: E402

ROOT = Path("/data/extra_copy/fastdyn")
PY = sys.executable
SCHEMA = "scopt.b5_campaign/1"
DEF_SCHEMA = "scopt.b5_definition/1"
DRIVER_REL = "tests/qemu/b5_campaign.py"
DEF_DEFAULT = WT / "campaigns/b5-freertos.def.toml"
ENVLAYOUT_DIR = ROOT / "runs/wp18/envlayout"
SELFTEST = ROOT / "runs/wp28/hostsym_selftest.json"
LOCK_ENV = "B5_TIMING_LOCK_HELD"
MAX_CPUS = 4


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def rd(p: str | os.PathLike) -> Any:
    return json.loads(Path(p).read_text())


def dump(p: str | os.PathLike, doc: Any) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")
    tmp.replace(p)


def git(*args: str, cwd: Path = WT) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def load_def(path: str | os.PathLike) -> dict[str, Any]:
    d = tomllib.loads(Path(path).read_text())
    if d.get("schema") != DEF_SCHEMA:
        raise SystemExit(f"{path}: schema {d.get('schema')!r} != {DEF_SCHEMA}")
    return d


_SEL = re.compile(r"^([^\[]+)\[(\w+)=([^\]]+)\]$")


def resolve_ref(spec: str) -> Any:
    """``<json file>#<path>``: dotted keys, ``list[key=value]`` selects one list item."""
    f, _, path = spec.partition("#")
    cur: Any = rd(f)
    for tok in path.split("."):
        m = _SEL.match(tok)
        if m:
            lst = cur[m.group(1)]
            hits = [x for x in lst if str(x.get(m.group(2))) == m.group(3)]
            if len(hits) != 1:
                raise KeyError(f"{spec}: {tok} selects {len(hits)} items")
            cur = hits[0]
        else:
            cur = cur[tok]
    return cur


def parse_cpus(s: str) -> list[int]:
    out: list[int] = []
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a), int(b) + 1))
        elif part.strip():
            out.append(int(part))
    return sorted(set(out))


def gate_record(defn: dict[str, Any]) -> dict[str, Any]:
    return rd(defn["runtime"]["gate_binary_record"])


def cli(args: list[Any], logf: Path, *, timeout: float = 7200) -> tuple[int, str, float]:
    """``python -m scopt ARGS`` from this worktree; stdout returned, both logged."""
    logf.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    p = subprocess.run([PY, "-m", "scopt", *[str(a) for a in args]], cwd=str(WT),
                       capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "SCOPT_ENVLAYOUT_PATH": str(ENVLAYOUT_DIR),
                            "PYTHONPATH": str(WT)})
    dt = time.time() - t0
    logf.write_text("$ python -m scopt " + " ".join(str(a) for a in args) + "\n--- stdout\n" +
                    p.stdout[-200000:] + "\n--- stderr\n" + p.stderr[-20000:] +
                    f"\nexit={p.returncode} seconds={dt:.1f}\n")
    return p.returncode, p.stdout, dt


def jload(s: str) -> dict[str, Any]:
    try:
        return json.loads(s)
    except ValueError:
        i = s.find("{")
        try:
            return json.loads(s[i:]) if i >= 0 else {}
        except ValueError:
            return {}


def code_identity(wt: Path = WT, def_rel: str = "campaigns/b5-freertos.def.toml"
                  ) -> dict[str, Any]:
    dirty = git("status", "--porcelain", "--", "scopt", "pipelines", DRIVER_REL, def_rel,
                "tests/qemu/b2_campaign.py", cwd=wt)
    return {"scopt_tree": git("rev-parse", "HEAD:./scopt", cwd=wt),
            "pipelines_tree": git("rev-parse", "HEAD:./pipelines", cwd=wt),
            "driver_blob": git("rev-parse", f"HEAD:./{DRIVER_REL}", cwd=wt),
            "helper_blob": git("rev-parse", "HEAD:./tests/qemu/b2_campaign.py", cwd=wt),
            "definition_blob": git("rev-parse", f"HEAD:./{def_rel}", cwd=wt),
            "dirty": [x for x in dirty.splitlines() if x.strip()]}


def artifact_identity(adir: Path) -> dict[str, Any]:
    from scopt.ids import artifact_id
    from scopt.run.arms import sc_file_version

    man = adir / "MANIFEST.sha256"
    out = {"source": str(adir), "sha256": sha256_sc(adir / "cache.sc"),
           "loader": f"v{sc_file_version(adir)}"}
    if man.is_file():
        out.update(manifest_sha256=sha256_file(man), artifact_id=artifact_id(man.read_bytes()))
    v = adir / "validation.json"
    if v.is_file():
        vd = rd(v)
        out["validation_verdict"] = vd.get("verdict")
        out["validation"] = ",".join(f"{k}={vd['codes'][k]}" for k in
                                     sorted(vd.get("codes", {}), key=lambda c: int(c[1:])))
    side = adir / "cache.sc.scopt.json"
    if side.is_file():
        sd = rd(side)
        out["input_snapshot_id"] = sd.get("input_snapshot_id")
        out["output_snapshot_id"] = (rd(v).get("output_snapshot_id") if v.is_file() else None)
        out["pipeline"] = [f"{p.get('pass')}:{p.get('outcome')}" for p in sd.get("pipeline", [])]
        out["profile_ids"] = [p.get("profile_id") for p in sd.get("profile_ids", [])]
    return out


# --------------------------------------------------------------------------
# prep (untimed)
# --------------------------------------------------------------------------

def _heldout(defn: dict[str, Any]) -> list[Path]:
    s = next(x for x in defn["set"] if x["split"] == "heldout")
    return sorted(Path(s["input_set"]).glob("*.bin"))


def _base(defn: dict[str, Any]) -> dict[str, Any]:
    b = rd(defn["workload"]["base_from"])["base"]
    for k in ("snapshot", "ri", "prep", "profile", "config"):
        if not Path(b[k]).exists():
            raise SystemExit(f"base {k} missing: {b[k]}")
    return b


def build_arm(arm: dict[str, Any], base: dict[str, Any], out: Path, gate: dict[str, Any]
              ) -> dict[str, Any]:
    """Build one ``build:<recipe>`` arm into ``out/store/<arm>``, publish a verified copy
    into ``out/pubstore/<arm>``. Returns the row (artifact dir, timings, problems)."""
    recipe = arm["source"].removeprefix("build:").split()
    name = arm["name"]
    store = out / "store" / name
    if store.exists():
        subprocess.run(["chmod", "-R", "u+w", str(store)], check=False)
        shutil.rmtree(store)
    row: dict[str, Any] = {"recipe": " ".join(recipe)}
    disable = [recipe[i + 1] for i, x in enumerate(recipe) if x == "--disable"]
    t0 = time.time()
    if recipe[0] == "identity-v6":
        code = ("import json,sys\n"
                "from scopt.importer.import_cache import open_snapshot\n"
                "from scopt.link import B3Plan\nfrom scopt.validate import package_b3\n"
                "from scopt.formats.envlayout_v1 import find_envlayout\n"
                "snap,ri,store,sha,cid=sys.argv[1:6]\n"
                "r=package_b3(open_snapshot(snap),B3Plan(force_v6=True),store,candidate_id=cid,"
                "runtime=json.load(open(ri)),v6_exes=[sha],layout=find_envlayout('sha256:'+sha))\n"
                "print(json.dumps({'status':r.status,'reason':r.reason,"
                "'artifact_dir':r.artifact_dir}))\n")
        cid = "sha256:" + hashlib.sha256(f"b5:identity-v6:{base['snapshot']}".encode()
                                         ).hexdigest()
        p = subprocess.run([PY, "-c", code, base["snapshot"], base["ri"], str(store),
                            gate["exe_sha256"], cid], cwd=str(WT), capture_output=True,
                           text=True, env={**os.environ, "PYTHONPATH": str(WT),
                                           "SCOPT_ENVLAYOUT_PATH": str(ENVLAYOUT_DIR)})
        (out / "logs").mkdir(parents=True, exist_ok=True)
        (out / "logs" / f"build-{name}.log").write_text(p.stdout + "\n--- stderr\n" +
                                                        p.stderr[-20000:])
        doc = jload(p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "")
        rc = p.returncode
    else:
        if recipe[0] == "identity":
            args: list[Any] = ["optimize", base["snapshot"], "--pipeline", "identity",
                               "--store", store, "--profile", base["profile"], "--json"]
        elif recipe[0] == "b2-layout":
            args = ["optimize", base["snapshot"], "--pipeline", "b2-layout", "--store", store,
                    "--profile", base["profile"], "--json"]
        elif recipe[0] == "b3":
            args = ["optimize", base["snapshot"], "--pipeline", "b3", "--b3", "--store", store,
                    "--runtime-identity", base["ri"], "--prep", base["prep"], "--profile",
                    base["profile"], "--selftest", SELFTEST, "--json"]
        else:
            return {**row, "ok": False, "problem": f"unknown recipe {recipe[0]}"}
        for d in disable:
            args += ["--disable", d]
        rc, so, _ = cli(args, out / "logs" / f"build-{name}.log")
        doc = jload(so)
    row["build_s"] = round(time.time() - t0, 1)
    row.update(build_rc=rc, status=doc.get("status"), reason=doc.get("reason"),
               built_dir=doc.get("artifact_dir"))
    if doc.get("status") != "published" or not doc.get("artifact_dir"):
        return {**row, "ok": False, "problem": f"build not published: {doc.get('reason')}"}
    pub = out / "pubstore" / name
    if pub.exists():
        subprocess.run(["chmod", "-R", "u+w", str(pub)], check=False)
        shutil.rmtree(pub)
    rc, so, dt = cli(["publish", doc["artifact_dir"], "--store", pub, "--json"],
                     out / "logs" / f"publish-{name}.log")
    pj = jload(so)
    row["publish"] = {"rc": rc, "status": pj.get("status"), "reason": pj.get("reason") or
                      pj.get("error"), "artifact_dir": pj.get("artifact_dir"),
                      "seconds": round(dt, 1)}
    if not pj.get("artifact_dir"):
        return {**row, "ok": False, "problem": f"publish failed: {row['publish']['reason']}"}
    row["artifact_dir"] = pj["artifact_dir"]
    row["same_cache_as_built"] = sha256_sc(Path(pj["artifact_dir"]) / "cache.sc") == \
        sha256_sc(Path(doc["artifact_dir"]) / "cache.sc")
    return row


def revalidate_and_v10(adir: Path, name: str, base: dict[str, Any], out: Path,
                       gate: dict[str, Any], heldout: list[Path]) -> dict[str, Any]:
    from scopt.run.arms import sc_file_version

    row: dict[str, Any] = {}
    if sc_file_version(adir) == 6:
        # scopt validate cannot read v6 artifacts (WP21 note 7); the verified re-check of
        # a v6 artifact is scopt publish (manifest, accepted validation.json, output
        # SnapshotId recomputed from the bytes, staged copy = source manifest), as in WP28
        row["revalidate"] = {"verdict": "accepted", "by": "scopt publish (v6; WP21 note 7)",
                             "seconds": 0.0}
    else:
        rc, so, dt = cli(["validate", adir, "--snapshot", base["snapshot"], "--json"],
                         out / "logs" / f"validate-{name}.log")
        v = jload(so)
        row["revalidate"] = {"rc": rc, "verdict": v.get("verdict"), "seconds": round(dt, 1),
                             "by": "scopt validate --snapshot BASE",
                             "stored_codes_agree": v.get("stored_codes_agree")}
    vd = out / "v10" / name
    if vd.exists():
        shutil.rmtree(vd)
    args: list[Any] = ["validate", adir, "--dynamic", "-c", base["config"], "--out", vd,
                       "--qemu", gate["exe_path"], "--baseline", base["snapshot"],
                       "--timeout", "300", "--json"]
    for i in heldout:
        args += ["--inputs", i]
    rc, so, dt = cli(args, out / "logs" / f"v10-{name}.log")
    d = jload(so)
    row["v10"] = {"rc": rc, "verdict": d.get("verdict"), "failures": d.get("failures"),
                  "appended": d.get("appended"), "inputs": [str(i) for i in heldout],
                  "dynamic_json": str(vd / "dynamic.json"), "seconds": round(dt, 1)}
    return row


def prep(a: argparse.Namespace) -> int:
    defn = load_def(a.defn)
    gate = gate_record(defn)
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    base = _base(defn)
    heldout = _heldout(defn)
    sp = out / "prep_summary.json"
    summary: dict[str, Any] = rd(sp) if (sp.is_file() and a.only) else {}
    summary.update({"schema": "scopt.b5_prep/1", "definition": str(Path(a.defn).resolve()),
                    "definition_sha256": sha256_file(a.defn), "out": str(out),
                    "gate": {"exe_path": gate["exe_path"], "exe_sha256": gate["exe_sha256"],
                             "tag": gate["tag"], "commit": gate["commit"]},
                    "base": base, "heldout_inputs": [str(i) for i in heldout],
                    "final_inputs_used": False})
    exe_now = sha256_file(gate["exe_path"])
    summary["gate"]["exe_sha256_now"] = exe_now
    if _hex(exe_now) != gate["exe_sha256"]:
        print(f"refused: gate exe sha256 {exe_now} != record {gate['exe_sha256']}",
              file=sys.stderr)
        return 2
    # base consistency: WP29 artifacts were built on a separately prepared base with the
    # same snapshot id; their config and cache must be byte-identical to WP28's
    cons = {}
    w29 = ROOT / "runs/wp29/compute"
    sid = Path(base["snapshot"]).name
    cons["config_equal_wp29"] = sha256_file(base["config"]) == sha256_file(
        w29 / "configs/compute-sc-gate.toml")
    cons["cache_equal_wp29"] = sha256_sc(Path(base["snapshot"]) / "cache.sc") == sha256_sc(
        w29 / "store/snapshots" / sid / "cache.sc")
    summary["base_consistency"] = cons
    arms = summary.setdefault("arms", {})
    only = set(a.only.split(",")) if a.only else None
    for arm in defn["arm"]:
        name = arm["name"]
        if only and name not in only:
            continue
        row: dict[str, Any] = {k: arm.get(k) for k in ("name", "kind", "role", "category",
                                                         "parent", "ablated_pass", "priority",
                                                         "note", "source")}
        t0 = time.time()
        try:
            if arm["kind"] != "deploy":
                row["ok"] = True
            elif arm["source"] == "warm":
                d = Path(base["snapshot"])
                row.update(origin="base", source_dir=str(d), sha256=sha256_sc(d / "cache.sc"),
                           snapshot_id="sha256:" + d.name, ok=True)
            elif arm["source"].startswith("reuse:"):
                d = Path(resolve_ref(arm["source"].removeprefix("reuse:")))
                v10 = resolve_ref(arm["v10"]) if arm.get("v10") else None
                row.update(origin="reuse", source_dir=str(d), **artifact_identity(d),
                           v10={"verdict": v10, "evidence": arm.get("v10")})
                row["ok"] = (row.get("validation_verdict") == "accepted" and v10 == "pass")
                if not row["ok"]:
                    row["problem"] = f"validation {row.get('validation_verdict')} v10 {v10}"
            else:
                b = build_arm(arm, base, out, gate)
                row.update(origin="build", build=b)
                if b.get("artifact_dir"):
                    d = Path(b["artifact_dir"])
                    row.update(source_dir=str(d), **artifact_identity(d))
                    row.update(revalidate_and_v10(d, name, base, out, gate, heldout))
                    pub = b.get("publish") or {}
                    row["ok"] = (b.get("ok", True) and row.get("validation_verdict") ==
                                 "accepted" and row["revalidate"]["verdict"] == "accepted"
                                 and row["v10"]["verdict"] == "pass"
                                 and pub.get("status") in ("created", "exists")
                                 and b.get("same_cache_as_built") is True)
                    if not row["ok"]:
                        row["problem"] = (f"validation {row.get('validation_verdict')} "
                                          f"revalidate {row['revalidate']['verdict']} "
                                          f"v10 {row['v10']['verdict']}")
                else:
                    row["ok"] = False
                    row["problem"] = b.get("problem")
        except Exception as e:  # noqa: BLE001 - recorded, never hidden
            row["ok"] = False
            row["problem"] = f"{type(e).__name__}: {e}"
        row["prep_s"] = round(time.time() - t0, 1)
        arms[name] = row
        print(f"{time.strftime('%H:%M:%S')} prep {name}: ok={row.get('ok')} "
              f"{row.get('problem') or ''} ({row['prep_s']} s)", flush=True)
        dump(sp, summary)
    bad = [n for n, r in arms.items() if not r.get("ok")]
    summary["failures"] = bad
    summary["verdict"] = "pass" if not bad and all(cons.values()) else "fail"
    dump(sp, summary)
    print(f"prep verdict {summary['verdict']} failures {bad} consistency {cons} -> {sp}")
    return 0 if summary["verdict"] == "pass" else 1


# --------------------------------------------------------------------------
# freeze
# --------------------------------------------------------------------------

_SKIP_DIRS = {"runs", "work", "ram", "cache", "store", "pubstore", "artifacts", "snapshots",
              "dyn", "fx", "logs", "twins", "copies", "hits", "v10", "negative", "mutants"}


def scan_timing_campaigns(root: Path = ROOT / "runs") -> list[dict[str, Any]]:
    """Every ``scopt run`` campaign.json under ``root`` (timing campaigns): its split, its
    QEMU sha256 and whether it names a final input."""
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in _SKIP_DIRS]
        if "campaign.json" in fns:
            p = Path(dp) / "campaign.json"
            try:
                d = rd(p)
            except (OSError, ValueError):
                continue
            if d.get("schema") != "scopt.campaign/1":
                continue
            c = d.get("campaign") or {}
            txt = json.dumps(c.get("inputs") or []) + str(c.get("input_set") or "")
            out.append({"path": str(p), "split": c.get("split"),
                        "qemu_seen": d.get("qemu_seen"), "status": d.get("status"),
                        "final": "/inputs/final/" in txt, "heldout": "/inputs/heldout/" in txt})
    return out


def freeze(a: argparse.Namespace) -> int:
    defn = load_def(a.defn)
    ps = rd(a.prep)
    if ps.get("verdict") != "pass":
        print(f"refused: prep verdict {ps.get('verdict')} failures {ps.get('failures')}",
              file=sys.stderr)
        return 2
    gate = gate_record(defn)
    gsha = "sha256:" + gate["exe_sha256"]
    camps = scan_timing_campaigns()
    final_gate = [c for c in camps if c["final"] and gsha in (c.get("qemu_seen") or [])]
    if final_gate:
        print(f"refused: timing runs on final inputs exist on the gate binary: {final_gate}",
              file=sys.stderr)
        return 2
    def_rel = str(Path(a.defn).resolve().relative_to(WT))
    code = code_identity(WT, def_rel)
    if code["dirty"] and not a.allow_dirty_for_test:
        print(f"refused: uncommitted changes in the code to be frozen: {code['dirty']}",
              file=sys.stderr)
        return 2
    base = ps["base"]
    wl = defn["workload"]
    cfg = tomllib.loads(Path(base["config"]).read_text())
    fw = Path(cfg["CPU"]["cpu0"][0]["binary"])
    prof = rd(base["profile"])
    arms_out: list[dict[str, Any]] = []
    excluded = [dict(e) for e in defn.get("excluded", [])]
    seen: dict[str, str] = {}
    for arm in defn["arm"]:
        r = ps["arms"][arm["name"]]
        x: dict[str, Any] = {k: arm[k] for k in ("name", "kind", "role", "category")}
        for k in ("parent", "ablated_pass", "priority", "note"):
            if arm.get(k) is not None:
                x[k] = arm[k]
        if arm["kind"] == "deploy":
            sha = r["sha256"]
            if arm.get("aa_control") and seen.get(sha) == "warm":
                x["aa_control"] = True
                x["sha256_equals_warm"] = True
            elif sha in seen:
                excluded.append({"name": arm["name"], "reason": f"duplicate: cache.sc sparse "
                                 f"sha256 equals arm {seen[sha]} (would time the same bytes "
                                 "twice)", "sha256": sha, "duplicate_of": seen[sha]})
                continue
            seen.setdefault(sha, arm["name"])
            x.update(source=r["source_dir"], sha256=sha, origin=r["origin"],
                     recipe=arm["source"])
            for k in ("artifact_id", "manifest_sha256", "loader", "validation",
                      "validation_verdict", "input_snapshot_id", "output_snapshot_id",
                      "snapshot_id"):
                if r.get(k) is not None:
                    x[k] = r[k]
            if r.get("pipeline"):
                x["pipeline"] = r["pipeline"]
            if r["origin"] == "reuse":
                x["v10_verdict"] = r["v10"]["verdict"]
                x["v10_evidence"] = r["v10"]["evidence"]
            elif r["origin"] == "build":
                x["v10_verdict"] = r["v10"]["verdict"]
                x["v10_evidence"] = r["v10"]["dynamic_json"]
                x["prep"] = {"optimize_s": r["build"].get("build_s"),
                             "validate_s": r["revalidate"].get("seconds"),
                             "v10_s": r["v10"].get("seconds"),
                             "publish_s": (r["build"].get("publish") or {}).get("seconds")}
        arms_out.append(x)
    names = {x["name"] for x in arms_out}
    sets = []
    for s in defn["set"]:
        d = Path(s["input_set"])
        an = [n for n in defn["schedule"][s["arms"]] if n in names]
        dropped = [n for n in defn["schedule"][s["arms"]] if n not in names]
        sets.append({"name": s["name"], "split": s["split"], "input_set": str(d),
                     "input_manifest_sha256": sha256_file(d / "MANIFEST.json"),
                     "inputs": [{"path": str(p), "sha256": sha256_file(p)}
                                for p in sorted(d.glob("*.bin"))],
                     "arms": an, "arms_dropped_as_duplicates": dropped,
                     "reps": int(s["reps"]), "seed": int(s["seed"]),
                     "timeout_s": float(defn["schedule"]["timeout_s"]), "out": s["name"],
                     **({"purpose": s["purpose"]} if s.get("purpose") else {})})
    heldout_before = [c["path"] for c in camps if c["heldout"]
                      and gsha in (c.get("qemu_seen") or [])]
    train_before = [c["path"] for c in camps if c["split"] == "train"
                    and gsha in (c.get("qemu_seen") or [])]
    cal = Path(wl["calibration"])
    doc: dict[str, Any] = {
        "schema": SCHEMA, "name": defn["name"],
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "frozen_note": (
            "Frozen by WP30 before any B5 timing run and before any timing run on a final "
            "input on the gate binary (final_timing_campaigns_on_gate_at_freeze = 0). The "
            "snapshot and the training profile come from train inputs only. Held-out inputs "
            "were used for V10 correctness and, BEFORE this freeze, by WP28's reported B3 "
            "performance rows (heldout_timing_on_gate_before_freeze); no candidate was "
            "re-tuned after them, the frozen candidates are the WP28/WP29 gate artifacts "
            "plus arms built here from the same base and profile. Nothing in this file may "
            "change after the commit that adds it; b5_campaign.py check refuses any "
            "mismatch. No retuning after looking at held-out or final results."),
        "design": defn["design"],
        "definition": {"path": def_rel, "sha256": sha256_file(a.defn),
                       "blob": code["definition_blob"]},
        "freeze": {"branch": git("rev-parse", "--abbrev-ref", "HEAD"),
                   "code_commit_at_freeze": git("rev-parse", "HEAD"),
                   "scopt_tree": code["scopt_tree"], "pipelines_tree": code["pipelines_tree"],
                   "driver_blob": code["driver_blob"], "helper_blob": code["helper_blob"],
                   "final_timing_campaigns_on_gate_at_freeze": len(final_gate),
                   "heldout_timing_on_gate_before_freeze": heldout_before,
                   "train_timing_on_gate_before_freeze": train_before,
                   "prep_summary": str(Path(a.prep).resolve()),
                   "prep_summary_sha256": sha256_file(a.prep),
                   "checker": f"{DRIVER_REL} check --campaign <this file> --expect-sha256 "
                              "<sha256 of this file>"},
        "runtime": {"qemu": gate["exe_path"], "qemu_sha256": gsha,
                    "gate_tag": gate["tag"], "gate_commit": gate["commit"],
                    "gate_worktree": gate["worktree"],
                    "gate_binary_record": defn["runtime"]["gate_binary_record"],
                    "plugin": defn["runtime"]["plugin"],
                    "plugin_sha256": sha256_file(defn["runtime"]["plugin"]),
                    "launch": defn["runtime"]["launch"], "rule": defn["runtime"]["rule"]},
        "schedule": {k: v for k, v in defn["schedule"].items()
                     if k not in ("core_arms", "ablation_arms", "calibration_arms")},
        "timing": defn["timing"], "rules": defn["rules"], "diag": defn["diag"],
        "workload": {
            "name": wl["name"], "note": wl["note"], "phase": wl["phase"],
            "base_config": base["config"], "base_config_sha256": sha256_file(base["config"]),
            "nocache_config": "derived",
            "firmware": {"path": str(fw), "sha256": sha256_file(fw),
                         "size": fw.stat().st_size, "mtime_ns": fw.stat().st_mtime_ns},
            "snapshot": {"dir": base["snapshot"],
                         "snapshot_id": "sha256:" + Path(base["snapshot"]).name,
                         "cache_sc_sha256": sha256_sc(Path(base["snapshot"]) / "cache.sc"),
                         "snapshot_json_sha256":
                             sha256_file(Path(base["snapshot"]) / "snapshot.json")},
            "profile": {"path": base["profile"], "sha256": sha256_file(base["profile"]),
                        "profile_id": prof.get("profile_id"),
                        "inputs": [{"name": i.get("name"), "sha256": i.get("sha256"),
                                    "split": i.get("split")}
                                   for i in (prof.get("collection") or {}).get("inputs", [])],
                        "runs": len((prof.get("collection") or {}).get("runs") or [])},
            "runtime_identity": {"path": base["ri"], "sha256": sha256_file(base["ri"])},
            "calibration": {"path": str(cal), "sha256": sha256_file(cal),
                            "note": wl["calibration_note"]},
            "base_consistency": ps.get("base_consistency")},
        "arm": arms_out,
        "skipped_unsupported": defn.get("skipped_unsupported", []),
        "excluded": excluded,
        "set": sets,
    }
    text = ("# B5 frozen timing campaign (WP30). GENERATED by tests/qemu/b5_campaign.py "
            "freeze from\n# campaigns/b5-freertos.def.toml; DO NOT EDIT: any change is a "
            "freeze violation (b5_campaign.py check).\n" + toml_dumps(doc))
    tomllib.loads(text)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(text)
    print(f"wrote {a.out} sha256 {sha256_file(a.out)}")
    return 0


# --------------------------------------------------------------------------
# check (the freeze checker)
# --------------------------------------------------------------------------

def load(path: str | os.PathLike) -> dict[str, Any]:
    return tomllib.loads(Path(path).read_text())


def check(campaign: str | os.PathLike, expect_sha256: str | None = None, *,
          check_git: bool = True, wt: Path = WT) -> dict[str, Any]:
    """Every frozen identity against the files on disk: ``{ok, problems, checked}``."""
    from scopt.ids import artifact_id

    probs: list[str] = []
    checked = 0
    cpath = Path(campaign).resolve()

    def eq(what: str, want: Any, got: Any) -> None:
        nonlocal checked
        checked += 1
        if want != got:
            probs.append(f"{what}: frozen {want} != now {got}")

    def fsha(what: str, p: str | os.PathLike, want: str, sc: bool = False) -> None:
        try:
            got = sha256_sc(p) if sc else sha256_file(p)
        except OSError as e:
            got = f"missing ({e.strerror})"
        eq(what, _hex(want), _hex(got))

    if expect_sha256:
        fsha("campaign file", cpath, expect_sha256)
    doc = load(cpath)
    if doc.get("schema") != SCHEMA:
        return {"ok": False, "problems": [f"campaign schema {doc.get('schema')} != {SCHEMA}"],
                "checked": checked}
    if check_git:
        try:
            repo = Path(git("rev-parse", "--show-toplevel", cwd=cpath.parent))
            st = git("status", "--porcelain", "--", str(cpath.relative_to(repo)), cwd=repo)
            eq("campaign file committed and clean", "", st)
            code = code_identity(wt, doc["definition"]["path"])
            fr = doc["freeze"]
            eq("scopt package (git tree at HEAD)", fr["scopt_tree"], code["scopt_tree"])
            eq("pipelines (git tree at HEAD)", fr["pipelines_tree"], code["pipelines_tree"])
            eq("driver b5_campaign.py (git blob at HEAD)", fr["driver_blob"],
               code["driver_blob"])
            eq("helper b2_campaign.py (git blob at HEAD)", fr["helper_blob"],
               code["helper_blob"])
            eq("definition (git blob at HEAD)", doc["definition"]["blob"],
               code["definition_blob"])
            eq("uncommitted code changes", [], code["dirty"])
            rt = doc["runtime"]
            eq("gate tag commit", rt["gate_commit"],
               git("rev-parse", f"{rt['gate_tag']}^{{commit}}", cwd=Path(rt["gate_worktree"])))
        except (subprocess.CalledProcessError, ValueError, KeyError) as e:
            probs.append(f"git check failed: {e}")
    rt = doc["runtime"]
    fsha("qemu (gate binary)", rt["qemu"], rt["qemu_sha256"])
    try:
        eq("gate-binary record exe sha256", _hex(rt["qemu_sha256"]),
           rd(rt["gate_binary_record"])["exe_sha256"])
    except (OSError, ValueError, KeyError) as e:
        probs.append(f"gate-binary record unreadable: {e}")
    fsha("plugin", rt["plugin"], rt["plugin_sha256"])
    w = doc["workload"]
    fsha("base config", w["base_config"], w["base_config_sha256"])
    fw = w["firmware"]
    fsha("firmware", fw["path"], fw["sha256"])
    try:
        st = os.stat(fw["path"])
        eq("firmware size", fw["size"], st.st_size)
        eq("firmware mtime_ns (fingerprinted)", fw["mtime_ns"], st.st_mtime_ns)
    except OSError as e:
        probs.append(f"firmware {e}")
    sn = w["snapshot"]
    fsha("snapshot cache.sc", Path(sn["dir"]) / "cache.sc", sn["cache_sc_sha256"], sc=True)
    fsha("snapshot.json", Path(sn["dir"]) / "snapshot.json", sn["snapshot_json_sha256"])
    pr = w["profile"]
    fsha("training profile", pr["path"], pr["sha256"])
    try:
        pd = rd(pr["path"])
        eq("profile_id", pr["profile_id"], pd.get("profile_id"))
        eq("profile inputs are train only", True, all(
            i.get("split") == "train" for i in (pd.get("collection") or {}).get("inputs", [])))
    except (OSError, ValueError) as e:
        probs.append(f"profile unreadable: {e}")
    fsha("runtime identity", w["runtime_identity"]["path"], w["runtime_identity"]["sha256"])
    fsha("calibration", w["calibration"]["path"], w["calibration"]["sha256"])
    names = set()
    for arm in doc["arm"]:
        names.add(arm["name"])
        if arm["kind"] != "deploy":
            continue
        src = Path(arm["source"])
        n = arm["name"]
        fsha(f"{n}: cache.sc", src / "cache.sc", arm["sha256"], sc=True)
        if arm.get("artifact_id"):
            man = src / "MANIFEST.sha256"
            fsha(f"{n}: MANIFEST.sha256", man, arm["manifest_sha256"])
            try:
                eq(f"{n}: artifact id", arm["artifact_id"], artifact_id(man.read_bytes()))
            except OSError as e:
                probs.append(f"{n}: {e}")
            eq(f"{n}: artifact dir name", _hex(arm["artifact_id"]), src.name)
            eq(f"{n}: not a negative control / test-only artifact", False,
               (src / "NEGATIVE_CONTROL.json").exists() or (src / "TEST_ONLY.json").exists())
    seeds = [s["seed"] for s in doc["set"]]
    eq("distinct set seeds", len(seeds), len(set(seeds)))
    for s in doc["set"]:
        fsha(f"{s['name']}: input MANIFEST.json", Path(s["input_set"]) / "MANIFEST.json",
             s["input_manifest_sha256"])
        eq(f"{s['name']}: input files", sorted(i["path"] for i in s["inputs"]),
           sorted(str(p) for p in Path(s["input_set"]).glob("*.bin")))
        for i in s["inputs"]:
            fsha(f"{s['name']}: input {Path(i['path']).name}", i["path"], i["sha256"])
        eq(f"{s['name']}: reps >= {doc['schedule']['min_reps']}", True,
           int(s["reps"]) >= int(doc["schedule"]["min_reps"]))
        eq(f"{s['name']}: arms known", [], [x for x in s["arms"] if x not in names])
        eq(f"{s['name']}: warm (reference) in set", True, "warm" in s["arms"])
        par = {a["name"]: a.get("parent") for a in doc["arm"]}
        eq(f"{s['name']}: every ablation's parent in the same set", [],
           [x for x in s["arms"] if par.get(x) and par[x] not in s["arms"]])
    return {"ok": not probs, "problems": probs, "checked": checked}


# --------------------------------------------------------------------------
# run (timing; the only command that times)
# --------------------------------------------------------------------------

def set_campaign_toml(doc: dict[str, Any], s: dict[str, Any], *, reps: int, name: str) -> str:
    """The ``scopt run --campaign`` TOML of one set (scopt.run.arms.load_campaign)."""
    w = doc["workload"]
    camp = {"name": name, "base_config": w["base_config"], "qemu": doc["runtime"]["qemu"],
            "reps": reps, "seed": int(s["seed"]), "reference": "warm",
            "timeout_s": float(s["timeout_s"]), "input_bank": "input",
            "input_set": s["input_set"], "split": s["split"]}
    by = {a["name"]: a for a in doc["arm"]}
    arms = []
    for n in s["arms"]:
        a = by[n]
        x = {"name": n, "kind": a["kind"], "role": a["role"]}
        if a["kind"] == "deploy":
            x["source"] = a["source"]
            x["sha256"] = a["sha256"]
        if a.get("parent"):
            x["parent"] = a["parent"]
            if a.get("ablated_pass"):
                x["ablated_pass"] = a["ablated_pass"]
        arms.append(x)
    unsupported = {u["name"]: f"{u['reason']}: {u.get('detail', '')}"[:300]
                   for u in doc.get("skipped_unsupported", []) if "<" not in u["name"]}
    not_run = {e["name"]: e["reason"][:300] for e in doc.get("excluded", [])
               if e.get("duplicate_of")}
    out: dict[str, Any] = {"campaign": camp, "arm": arms}
    if unsupported:
        out["unsupported"] = unsupported
    if not_run:
        out["not_run"] = not_run
    return ("# derived from the frozen B5 campaign by b5_campaign.py run; do not edit\n" +
            toml_dumps(out))


def _scopt_log(args: list[Any], log: Path, *, prefix: list[str] = ()) -> int:
    env = {**os.environ, "PYTHONPATH": str(WT), "SCOPT_ENVLAYOUT_PATH": str(ENVLAYOUT_DIR)}
    argv = [*prefix, PY, "-m", "scopt", *[str(x) for x in args]]
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w") as f:
        f.write(json.dumps(argv) + "\n")
        f.flush()
        p = subprocess.run(argv, cwd=str(WT), env=env, stdout=f, stderr=subprocess.STDOUT)
    return p.returncode


def prune_set(out: Path) -> dict[str, Any]:
    """Disk rule: record sha256 + size of every per-run cache copy and io.log of a set,
    then delete them (run.json/runs.jsonl keep the proofs)."""
    rows = []
    pats = ["runs/*/*/cache/*.sc", "runs/*/*/work/io.log"]
    for pat in pats:
        for p in sorted(out.glob(pat)):
            st = p.stat()
            h = sha256_sc(p) if p.suffix == ".sc" else sha256_file(p)
            rows.append({"path": str(p.relative_to(out)), "sha256": h, "size": st.st_size,
                         "allocated_bytes": st.st_blocks * 512})
            p.unlink()
    with open(out / "deleted-files.jsonl", "a") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    return {"deleted": len(rows), "bytes": sum(r["size"] for r in rows)}


def evaluate_set(out: Path, min_pairs: int, arms: list[str]) -> dict[str, Any]:
    """Rules R1-R5 per set from campaign.json and the report model."""
    res: dict[str, Any] = {"rules": {}, "failed": []}
    try:
        cj = rd(out / "campaign.json")
        model = rd(out / "report" / "report.json")
    except (OSError, ValueError) as e:
        res["failed"].append(f"unreadable: {e}")
        return res
    res["rules"]["R1_freeze"] = {"ok": cj.get("status") == "done" and cj.get("freeze_ok") is True,
                                 "status": cj.get("status"), "freeze_ok": cj.get("freeze_ok")}
    g = next((x for x in model.get("groups", []) if x["name"].startswith("campaign:")), None)
    if g is None:
        res["failed"].append("no campaign group in report.json")
        return res
    pairs = {c["arm"]: c.get("n_pairs") for c in g["comparisons"]
             if c["why"] == "vs_reference" and c["metric"] == "bench" and c["input"] == "*"}
    short = {a: pairs.get(a) for a in arms if a != "warm" and (pairs.get(a) or 0) < min_pairs}
    res["rules"]["R2_samples"] = {"ok": not short, "pairs_vs_warm": pairs, "short": short}
    res["rules"]["R3_correctness"] = {"ok": not g["correctness"],
                                      "mismatches": len(g["correctness"])}
    qs = g.get("qemu_sha256_valid") or []
    res["rules"]["R4_qemu"] = {"ok": len(qs) == 1, "qemu_sha256_valid": qs}
    res["rules"]["R5_reported"] = {"ok": (out / "report" / "report.md").is_file()
                                   and bool(model.get("b5"))}
    res["failed"] = [k for k, v in res["rules"].items() if not v["ok"]]
    return res


class LoadSampler:
    def __init__(self, path: Path, every: float):
        self.path, self.every = path, every
        self._stop = threading.Event()
        self.samples: list[list[float]] = []
        self.t = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        with open(self.path, "a") as f:
            while not self._stop.is_set():
                la = list(os.getloadavg())
                self.samples.append(la)
                f.write(f"{time.time():.0f} {la[0]:.2f} {la[1]:.2f} {la[2]:.2f}\n")
                f.flush()
                self._stop.wait(self.every)

    def __enter__(self) -> "LoadSampler":
        self.t.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self.t.join(5)

    def summary(self) -> dict[str, Any]:
        one = [s[0] for s in self.samples]
        return {"n": len(one), "mean_1min": (sum(one) / len(one)) if one else None,
                "min_1min": min(one) if one else None, "max_1min": max(one) if one else None}


def run(a: argparse.Namespace) -> int:
    doc = load(a.campaign)
    tm = doc["timing"]
    cpus = parse_cpus(a.cpus or tm["cpu_list_default"])
    if len(cpus) > MAX_CPUS:
        print(f"refused: {len(cpus)} CPUs > {MAX_CPUS} (timing rule)", file=sys.stderr)
        return 2
    if os.environ.get(LOCK_ENV) != "1":
        # re-exec under the host-wide timing lock (blocking: one timing job at a time)
        argv = ["flock", tm["lock"], "env", f"{LOCK_ENV}=1", PY, str(Path(__file__).resolve()),
                *sys.argv[1:]]
        print(f"waiting for {tm['lock']} ...", flush=True)
        return subprocess.run(argv).returncode
    t0 = time.time()
    if a.reps is not None and not a.smoke:
        print("refused: --reps changes the frozen repetitions; only --smoke may", file=sys.stderr)
        return 2
    if a.smoke and a.reps is None:
        print("refused: --smoke needs --reps", file=sys.stderr)
        return 2
    chk = check(a.campaign, a.expect_sha256)
    if not chk["ok"]:
        print("refused: frozen identities do not match:\n  " + "\n  ".join(chk["problems"]),
              file=sys.stderr)
        return 1
    sets = doc["set"]
    if a.sets:
        want = [x.strip() for x in a.sets.split(",") if x.strip()]
        unknown = set(want) - {s["name"] for s in sets}
        if unknown:
            print(f"refused: unknown sets {sorted(unknown)}", file=sys.stderr)
            return 2
        sets = [s for s in sets if s["name"] in want]
    if a.smoke and any(s["split"] == "final" for s in sets):
        print("refused: a smoke run never touches the final split", file=sys.stderr)
        return 2
    root = Path(a.out_root).resolve()
    for s in sets:
        d = root / s["out"]
        if d.exists() and any(d.iterdir()):
            print(f"refused: {d} exists and is not empty (never re-run into old results)",
                  file=sys.stderr)
            return 2
    root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(a.campaign, root / "b5-campaign.toml")
    summary: dict[str, Any] = {
        "schema": "scopt.b5_campaign_run/1", "campaign": str(Path(a.campaign).resolve()),
        "campaign_sha256": sha256_file(a.campaign), "smoke": bool(a.smoke),
        "reps_override": a.reps, "check_before": chk, "sets": {}, "started": t0,
        "cpu_budget": {"cpus": cpus, "n": len(cpus), "max": MAX_CPUS, "pinning": "taskset -c",
                       "lock": tm["lock"], "lock_held": True},
        "host_start": {"loadavg": os.getloadavg(), "nproc": os.cpu_count()}}
    sumpath = root / ("smoke.json" if a.smoke else "b5_campaign.json")
    prof = doc["workload"]["profile"]["path"]
    cpu_arg = ",".join(str(c) for c in cpus)
    with LoadSampler(root / "timing-load.log", float(tm.get("load_sample_s", 30))) as ls:
        final_checked = False
        for s in sets:
            if s["split"] == "final" and not final_checked:
                c2 = check(a.campaign, a.expect_sha256)
                summary["check_before_final"] = c2
                final_checked = True
                if not c2["ok"]:
                    summary["verdict"] = "incomplete"
                    summary["failed"] = {"check_before_final": c2["problems"]}
                    dump(sumpath, summary)
                    return 1
            reps = a.reps if a.smoke else int(s["reps"])
            name = s["name"] + ("-smoke" if a.smoke else "")
            ctoml = root / "campaigns" / f"{s['name']}.toml"
            ctoml.parent.mkdir(parents=True, exist_ok=True)
            ctoml.write_text(set_campaign_toml(doc, s, reps=reps, name=name))
            out = root / s["out"]
            ts = time.time()
            la0 = os.getloadavg()
            print(f"{time.strftime('%H:%M:%S')} set {s['name']}: {len(s['arms'])} arms x "
                  f"{reps} reps x {len(s['inputs'])} inputs -> {out}", flush=True)
            rc = _scopt_log(["run", "--campaign", ctoml, "--out", out, "--no-report"],
                            root / "logs" / f"{s['name']}-run.log",
                            prefix=["taskset", "-c", cpu_arg])
            rrc = _scopt_log(["report", out, "--out", out / "report" / "report.md",
                              "--profile", prof, "--b5-campaign", a.campaign],
                             root / "logs" / f"{s['name']}-report.log")
            pr = prune_set(out)
            ev = evaluate_set(out, int(doc["rules"]["min_valid_pairs"]) if not a.smoke else 1,
                              s["arms"])
            summary["sets"][s["name"]] = {"out": str(out), "campaign_toml": str(ctoml),
                                          "reps": reps, "run_rc": rc, "report_rc": rrc,
                                          "pruned": pr, "loadavg_start": la0,
                                          "loadavg_end": os.getloadavg(),
                                          "seconds": round(time.time() - ts, 1), **ev}
            dump(sumpath, summary)
        summary["load"] = ls.summary()
    chk2 = check(a.campaign, a.expect_sha256)
    summary["check_after"] = chk2
    rrc = _scopt_log(["report", root, "--out", root / "report" / "report.md", "--profile", prof,
                      "--b5-campaign", a.campaign], root / "logs" / "combined-report.log")
    summary["combined_report"] = {"rc": rrc, "path": str(root / "report" / "report.md")}
    failed = {n: v["failed"] for n, v in summary["sets"].items() if v["failed"]}
    if not chk2["ok"]:
        failed["check_after"] = chk2["problems"]
    r6 = len(cpus) <= MAX_CPUS and summary["load"]["n"] > 0
    summary["R6_lock"] = {"ok": r6, "cpus": cpus, "load": summary["load"]}
    if not r6:
        failed["R6_lock"] = "cpu budget or load samples missing"
    summary["verdict"] = ("smoke_ok" if a.smoke and not failed else "smoke_failed" if a.smoke
                          else "measured_and_reported" if not failed else "incomplete")
    summary["failed"] = failed
    summary["duration_s"] = round(time.time() - t0, 1)
    summary["host_end"] = {"loadavg": os.getloadavg()}
    dump(sumpath, summary)
    print(f"verdict {summary['verdict']} ({summary['duration_s']} s) -> {sumpath}")
    return 0 if not failed else 1


# --------------------------------------------------------------------------
# diag (untimed)
# --------------------------------------------------------------------------

def diag(a: argparse.Namespace) -> int:
    if os.environ.get(LOCK_ENV) == "1":
        print("refused: diag is untimed work and never runs under the timing lock",
              file=sys.stderr)
        return 2
    chk = check(a.campaign, a.expect_sha256, check_git=not a.no_git)
    if not chk["ok"]:
        print("refused: frozen identities do not match:\n  " + "\n  ".join(chk["problems"]),
              file=sys.stderr)
        return 1
    doc = load(a.campaign)
    root = Path(a.out_root).resolve()
    w = doc["workload"]
    rt = doc["runtime"]
    splits = [x for x in (a.splits or ",".join(doc["diag"]["v6_stats_splits"])).split(",") if x]
    for sp in splits:
        if sp == "final" and not a.smoke:
            fin = [s for s in doc["set"] if s["split"] == "final"]
            done = all((rdp := root / s["out"] / "campaign.json").is_file() and
                       rd(rdp).get("status") == "done" for s in fin)
            if not done:
                print("refused: diag on final inputs before the final timing sets are done",
                      file=sys.stderr)
                return 2
        if a.smoke and sp == "final":
            print("refused: a smoke diag never touches the final split", file=sys.stderr)
            return 2
    by = {x["name"]: x for x in doc["arm"]}
    idx_path = root / "b5-diag.json"
    idx: dict[str, Any] = rd(idx_path) if idx_path.is_file() else {"oracles": {},
                                                                    "profiles": {}}
    idx["campaign_sha256"] = sha256_file(a.campaign)
    idx["smoke"] = bool(a.smoke) or bool(idx.get("smoke"))
    for sp in splits:
        s = next(x for x in doc["set"] if x["split"] == sp)
        inputs = [i["path"] for i in s["inputs"]][: (1 if a.smoke else None)]
        if sp in doc["diag"]["base_profiles"] and not a.no_profiles:
            pd = root / "diag" / "profiles" / sp
            if pd.exists():
                shutil.rmtree(pd)
            rc, _, dt = cli(["profile", "run", "-c", w["base_config"], "--snapshot",
                             w["snapshot"]["dir"], "--reps", "1", "--out", pd, "--qemu",
                             rt["qemu"], "--timeout", "600", "--phase", w["phase"],
                             *sum((["--inputs", i] for i in inputs), [])],
                            root / "logs" / f"diag-profile-{sp}.log")
            mp = pd / "merged.profile.json"
            if mp.is_file():
                idx["profiles"][sp] = str(mp)
            print(f"diag profile {sp}: rc={rc} ({dt:.0f} s)", flush=True)
        for arm in doc["diag"]["v6_stats_arms"]:
            x = by.get(arm)
            if x is None:
                continue
            from scopt.run.diag import artifact_probe_addrs

            labels = [p["what"] for p in artifact_probe_addrs(Path(x["source"]) / "cache.sc")
                      if p.get("kind") == "variant"]
            o3: dict[str, Any] = {"stats": {"links_armed": ">=0"}}
            if labels:
                o3["hits"] = {lab: ">=0" for lab in labels}
            dd = root / "diag" / sp / arm
            if dd.exists():
                shutil.rmtree(dd)
            args: list[Any] = ["validate", x["source"], "--dynamic", "--oracles", "O1,O3,O5",
                               "-c", w["base_config"], "--baseline", w["snapshot"]["dir"],
                               "--out", dd, "--qemu", rt["qemu"], "--o3", json.dumps(o3),
                               "--json"]
            # gdb hit counters only on the SPEC variants' C' (hardware breakpoints, <= 4
            # per gdb run); a GENERAL-only artifact gets a label that matches no probe, so
            # no gdb run happens (otherwise every guard stub S/F would be probed)
            for lab in labels or ["__no_variant__"]:
                args += ["--hit-target", lab]
            for i in inputs:
                args += ["--inputs", i]
            rc, _, dt = cli(args, root / "logs" / f"diag-{sp}-{arm}.log", timeout=14400)
            if (dd / "dynamic_oracles.json").is_file():
                idx["oracles"].setdefault(sp, {})[arm] = str(dd / "dynamic_oracles.json")
            print(f"diag {sp} {arm}: rc={rc} ({dt:.0f} s)", flush=True)
            dump(idx_path, idx)
    dump(idx_path, idx)
    print(f"diag index -> {idx_path}")
    return 0


SECONDS_PER_RUN = 1.77  # WP28 held-out timing: 798 s for 450 runs (deploy + launch + proof)
MB_PER_RUN_KEPT = 1.0     # run dir after pruning the cache copy and io.log (estimate)


def plan(a: argparse.Namespace) -> int:
    """Untimed dry run (no QEMU, no lock): ``check``, then per set the derived ``scopt run``
    campaign through ``scopt run --dry-run`` (the full preflight: frozen sha256 of every
    arm, v6-capable QEMU for v6 arms, ablation parents, input manifests; and the
    interleaved schedule). Writes ``OUT/b5-plan.json`` with run counts and estimates."""
    chk = check(a.campaign, a.expect_sha256, check_git=not a.no_git)
    doc = load(a.campaign)
    root = Path(a.out).resolve()
    if root.exists() and any(root.iterdir()):
        print(f"refused: {root} exists and is not empty", file=sys.stderr)
        return 2
    root.mkdir(parents=True)
    res: dict[str, Any] = {"schema": "scopt.b5_plan/1", "campaign": str(Path(a.campaign).resolve()),
                           "campaign_sha256": sha256_file(a.campaign), "check": chk,
                           "timed": False, "sets": {}}
    tot = 0
    for s in doc["set"]:
        ctoml = root / "campaigns" / f"{s['name']}.toml"
        ctoml.parent.mkdir(parents=True, exist_ok=True)
        ctoml.write_text(set_campaign_toml(doc, s, reps=int(s["reps"]), name=s["name"]))
        out = root / s["name"]
        rc = _scopt_log(["run", "--campaign", ctoml, "--out", out, "--dry-run", "--no-report"],
                        root / "logs" / f"{s['name']}-dry-run.log")
        cj = rd(out / "campaign.json") if (out / "campaign.json").is_file() else {}
        sched = cj.get("schedule") or []
        per_arm: dict[str, int] = {}
        for st in sched:
            per_arm[st["arm"]] = per_arm.get(st["arm"], 0) + 1
        n = len(sched)
        tot += n
        res["sets"][s["name"]] = {
            "split": s["split"], "rc": rc, "status": cj.get("status"),
            "error": cj.get("error"), "arms": s["arms"], "n_arms": len(s["arms"]),
            "reps": s["reps"], "inputs": len(s["inputs"]), "scheduled_runs": n,
            "runs_per_arm": per_arm,
            "interleaved": len({(x["rep"], x.get("input")) for x in sched[: len(s["arms"])]}) == 1
            if sched else None,
            "frozen_sha_checked": sorted((cj.get("identities") or {}).get("arms", {})),
            "qemu": (cj.get("identities") or {}).get("qemu"),
            "estimated_hours": round(n * SECONDS_PER_RUN / 3600, 2)}
        print(f"{s['name']}: rc={rc} status={cj.get('status')} runs={n}", flush=True)
    res["scheduled_runs_total"] = tot
    res["estimated_hours_total"] = round(tot * SECONDS_PER_RUN / 3600, 2)
    res["estimated_disk_gb_after_pruning"] = round(tot * MB_PER_RUN_KEPT / 1024, 1)
    res["ok"] = chk["ok"] and all(v["status"] == "dry_run" for v in res["sets"].values())
    dump(root / "b5-plan.json", res)
    print(f"plan ok={res['ok']} runs={tot} ~{res['estimated_hours_total']} h -> "
          f"{root / 'b5-plan.json'}")
    return 0 if res["ok"] else 1


def report(a: argparse.Namespace) -> int:
    doc = load(a.campaign)
    root = Path(a.out_root).resolve()
    return subprocess.run([PY, "-m", "scopt", "report", str(root), "--out",
                           str(root / "report" / "report.md"), "--profile",
                           doc["workload"]["profile"]["path"], "--b5-campaign",
                           str(Path(a.campaign).resolve())], cwd=str(WT),
                          env={**os.environ, "PYTHONPATH": str(WT)}).returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep")
    p.add_argument("--def", dest="defn", default=str(DEF_DEFAULT))
    p.add_argument("--out", default=str(ROOT / "runs/b5/prep"))
    p.add_argument("--only", default=None)
    f = sub.add_parser("freeze")
    f.add_argument("--def", dest="defn", default=str(DEF_DEFAULT))
    f.add_argument("--prep", required=True)
    f.add_argument("--out", required=True)
    f.add_argument("--allow-dirty-for-test", action="store_true",
                   help=argparse.SUPPRESS)
    c = sub.add_parser("check")
    c.add_argument("--campaign", required=True)
    c.add_argument("--expect-sha256", default=None)
    c.add_argument("--no-git", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--campaign", required=True)
    r.add_argument("--expect-sha256", required=True)
    r.add_argument("--out-root", required=True)
    r.add_argument("--sets", default=None)
    r.add_argument("--cpus", default=None, help="CPU list for taskset (<= 4 CPUs)")
    r.add_argument("--smoke", action="store_true")
    r.add_argument("--reps", type=int, default=None)
    d = sub.add_parser("diag")
    d.add_argument("--campaign", required=True)
    d.add_argument("--expect-sha256", default=None)
    d.add_argument("--out-root", required=True)
    d.add_argument("--splits", default=None)
    d.add_argument("--smoke", action="store_true")
    d.add_argument("--no-profiles", action="store_true")
    d.add_argument("--no-git", action="store_true")
    pl = sub.add_parser("plan")
    pl.add_argument("--campaign", required=True)
    pl.add_argument("--expect-sha256", default=None)
    pl.add_argument("--out", required=True)
    pl.add_argument("--no-git", action="store_true")
    rp = sub.add_parser("report")
    rp.add_argument("--campaign", required=True)
    rp.add_argument("--out-root", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "prep":
        return prep(a)
    if a.cmd == "freeze":
        return freeze(a)
    if a.cmd == "check":
        res = check(a.campaign, a.expect_sha256, check_git=not a.no_git)
        print(json.dumps(res, indent=2))
        return 0 if res["ok"] else 1
    if a.cmd == "run":
        return run(a)
    if a.cmd == "diag":
        return diag(a)
    if a.cmd == "plan":
        return plan(a)
    return report(a)


if __name__ == "__main__":
    sys.exit(main())

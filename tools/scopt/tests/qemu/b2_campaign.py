#!/usr/bin/env python3
"""F-B2-7 campaign: freeze, check and run the frozen B2 FreeRTOS timing campaign.

    b2_campaign.py freeze --compute PREP_SUMMARY --blinky PREP_SUMMARY --out CAMPAIGN.toml
    b2_campaign.py check  --campaign CAMPAIGN.toml [--expect-sha256 HEX]
    b2_campaign.py run    --campaign CAMPAIGN.toml --expect-sha256 HEX --out-root DIR
                          [--sets a,b] [--smoke --reps N]

``freeze`` writes the campaign file from the ``b2_prep.py`` summaries (design §7
F-B2-7, plan WP15: the candidate and profile sha256 values are frozen before
timing). The file is then committed; nothing in it may change afterwards.

``check`` recomputes every frozen identity from the files on disk and refuses
(exit 1, one line per mismatch) when anything differs:

* the campaign file itself (``--expect-sha256``) and that it is committed and
  clean in its git repository;
* the scopt package and this driver (git tree/blob ids at HEAD, no uncommitted
  changes), so the code that times is the code that was frozen;
* the QEMU binary, the FastDyn plugin, the firmware ELF (sha256, size and
  mtime: the supercache fingerprint hashes the ELF's mtime), the base TOMLs;
* the warm snapshot (``cache.sc`` sparse sha256, ``snapshot.json``), the
  profile (file sha256 and ``profile_id``), every artifact (``cache.sc``
  sparse sha256, ``MANIFEST.sha256`` file sha256 and the artifact id derived
  from it), every input file and split ``MANIFEST.json``.

``run`` runs ``check``, then for every timing set (in file order) writes the
derived ``scopt run`` campaign TOML (``OUT_ROOT/campaigns/<set>.toml``, every
deploy arm with its frozen sha256) and runs::

    python -m scopt run --campaign OUT_ROOT/campaigns/<set>.toml --out OUT_ROOT/<set>
    python -m scopt report OUT_ROOT/<set> --out OUT_ROOT/<set>/report/report.md --profile P

then ``check`` again, one combined ``scopt report`` over every set
(``OUT_ROOT/report/``), and ``OUT_ROOT/b2_campaign.json``: the per-set rule
evaluation (``[rules]`` of the campaign file). A set whose output directory
exists is refused (``scopt run`` needs a fresh directory): a campaign is never
silently re-run or resumed into old results.

``--smoke`` (with ``--reps``) is the only way to change the repetitions; it is
for proving the command end to end and is labelled ``smoke`` everywhere. A
smoke run refuses the ``final`` split.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

WT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WT))

ROOT = Path("/data/extra_copy/fastdyn")
PY = sys.executable
SCHEMA = "scopt.b2_campaign/1"
DRIVER_REL = "tests/qemu/b2_campaign.py"


# --------------------------------------------------------------------------
# hashing
# --------------------------------------------------------------------------

def sha256_file(p: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return "sha256:" + h.hexdigest()


def sha256_sc(p: str | os.PathLike) -> str:
    from scopt.formats.sc_v5 import sha256_sparse

    return "sha256:" + sha256_sparse(p)


def _hex(s: str) -> str:
    return s.split(":", 1)[1] if s.startswith("sha256:") else s


def git(*args: str, cwd: Path = WT) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def code_identity(wt: Path = WT) -> dict[str, Any]:
    """Git ids of the scopt package and of this driver at HEAD, plus uncommitted changes."""
    dirty = git("status", "--porcelain", "--", "scopt", "pipelines", DRIVER_REL, cwd=wt)
    return {"scopt_tree": git("rev-parse", "HEAD:./scopt", cwd=wt),
            "pipelines_tree": git("rev-parse", "HEAD:./pipelines", cwd=wt),
            "driver_blob": git("rev-parse", f"HEAD:./{DRIVER_REL}", cwd=wt),
            "dirty": [x for x in dirty.splitlines() if x.strip()]}


# --------------------------------------------------------------------------
# a small TOML writer (str/int/float/bool, lists of scalars, tables, arrays of tables)
# --------------------------------------------------------------------------

def _tv(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_tv(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_tv(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"cannot write {type(v)} to TOML")


def _key(k: str) -> str:
    return k if k.replace("_", "").replace("-", "").isalnum() else json.dumps(k)


def _is_table(v: Any) -> bool:
    return isinstance(v, dict) and not v.get("__inline__")


def _is_aot(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def toml_dumps(doc: dict[str, Any], prefix: str = "") -> str:
    lines: list[str] = []
    for k, v in doc.items():
        if not _is_table(v) and not _is_aot(v):
            if isinstance(v, dict):
                v = {a: b for a, b in v.items() if a != "__inline__"}
            lines.append(f"{_key(k)} = {_tv(v)}")
    for k, v in doc.items():
        name = f"{prefix}{_key(k)}"
        if _is_table(v):
            lines.append(f"\n[{name}]")
            lines.append(toml_dumps(v, name + ".").rstrip("\n"))
        elif _is_aot(v):
            for x in v:
                lines.append(f"\n[[{name}]]")
                lines.append(toml_dumps(x, name + ".").rstrip("\n"))
    return "\n".join(x for x in lines if x != "") + "\n"


# --------------------------------------------------------------------------
# freeze
# --------------------------------------------------------------------------

QEMU = ROOT / "wt/qemu-int/build/qemu-system-arm"
PLUGIN = ROOT / "src/fastdyn/build/libfastdyn.so"
FIRMWARE = {
    "compute": ROOT / "fixtures/freertos-mps2-compute/FreeRTOS/FreeRTOS/Demo/"
                      "CORTEX_MPS2_QEMU_IAR_GCC/build/gcc/output/RTOSDemo.out",
}
INPUT_SETS = {
    "heldout": ROOT / "fixtures/inputs/heldout/freertos-mps2-compute",
    "final": ROOT / "fixtures/inputs/final/freertos-mps2-compute",
}
REPS = 30
TIMEOUT_S = 300.0
SEEDS = {"compute-heldout": 15701, "compute-final": 15702, "blinky-fixed": 15703}
PASS_ABLATABLE = {
    "cluster-hot": "meaningful: hot units in slot order (the partition without clustering)",
    "order-cold": "meaningful: clustered hot units placed first-fit around every other unit, "
                  "which stays at its original offset (clustering without segregation)",
}
PASS_DEGENERATE = {
    "hotcold-partition": "degenerate (WP11, docs/b2-layout.md): without the classification "
                         "both layout passes return SKIPPED_UNKNOWN missing_hotness and the "
                         "candidate is the input layout (0 moved); its cache.sc is "
                         "byte-identical to the warm snapshot and to the identity artifact, "
                         "so it would time the identity arm a second time",
}


def _firmware_of(base_config: str) -> str:
    cfg = tomllib.loads(Path(base_config).read_text())
    return cfg["CPU"]["cpu0"][0]["binary"]


def _file_id(p: str | os.PathLike) -> dict[str, Any]:
    st = os.stat(p)
    return {"path": str(p), "sha256": sha256_file(p), "size": st.st_size,
            "mtime_ns": st.st_mtime_ns}


def _workload_doc(name: str, s: dict[str, Any], role: str, note: str) -> dict[str, Any]:
    cand = {c["label"]: c for c in s["candidates"]}
    snap = s["snapshot"]
    prof = s["profile"]
    fw = _firmware_of(s["base_config"])
    arms: list[dict[str, Any]] = [
        {"name": "nocache", "kind": "nocache", "role": "baseline",
         "note": "base TOML with [Machine].supercache commented out (derived by scopt run)"},
        {"name": "empty", "kind": "empty", "role": "baseline",
         "note": "cold run in a fresh run dir, nothing deployed"},
        {"name": "warm", "kind": "deploy", "role": "reference",
         "source": snap["dir"], "sha256": snap["cache_sc_sha256"],
         "snapshot_id": snap["snapshot_id"],
         "note": "warm baseline: the unoptimized cache of the prepare runs (the snapshot the "
                 "candidates were optimized from)"},
    ]
    def vinfo(c: dict[str, Any]) -> dict[str, Any]:
        att = c.get("v10_attempts") or [c["v10"]]
        return {"validation": ",".join(f"{k}={v}" for k, v in sorted(c["validation"].items())),
                "v10_verdict": c["v10"]["verdict"], "v10_attempts": len(att),
                "v10_failed_attempts": [a.get("dynamic_json", "") for a in att
                                        if a.get("verdict") != "pass"]}

    for label, arm in (("identity", "identity"), ("b2-layout", "layout")):
        c = cand[label]
        arms.append({"name": arm, "kind": "deploy", "role": "candidate",
                     "source": c["artifact_dir"], "sha256": c["cache_sc_sha256"],
                     "artifact_id": c["artifact_id"],
                     "manifest_sha256": c["manifest_sha256_file"],
                     "pipeline": c["pipeline"], "moved_units": c["moved"], **vinfo(c)})
    for pss in PASS_ABLATABLE:
        c = cand[f"no-{pss}"]
        arms.append({"name": f"layout-no-{pss}", "kind": "deploy", "role": "ablation",
                     "parent": "layout", "ablated_pass": pss,
                     "source": c["artifact_dir"], "sha256": c["cache_sc_sha256"],
                     "artifact_id": c["artifact_id"],
                     "manifest_sha256": c["manifest_sha256_file"],
                     "pipeline": f"b2-layout --disable {pss}", "moved_units": c["moved"],
                     "note": PASS_ABLATABLE[pss], **vinfo(c)})
    excluded = []
    for pss, why in PASS_DEGENERATE.items():
        c = cand[f"no-{pss}"]
        excluded.append({"name": f"layout-no-{pss}", "reason": why,
                         "artifact_id": c["artifact_id"], "sha256": c["cache_sc_sha256"],
                         "moved_units": c["moved"],
                         "sha256_equals_warm": c["cache_sc_sha256"] == snap["cache_sc_sha256"],
                         "sha256_equals_identity":
                             c["cache_sc_sha256"] == cand["identity"]["cache_sc_sha256"]})
    return {
        "name": name, "role": role, "note": note,
        "prep_summary": str(Path(s["out"]) / "prep_summary.json"),
        "base_config": s["base_config"], "base_config_sha256": s["base_config_sha256"],
        "nocache_config": "derived",
        "firmware": _file_id(fw),
        "snapshot": {"snapshot_id": snap["snapshot_id"], "dir": snap["dir"],
                     "cache_sc_sha256": snap["cache_sc_sha256"],
                     "snapshot_json_sha256": sha256_file(Path(snap["dir"]) / "snapshot.json"),
                     "records": snap["records"], "bundle_id": snap.get("bundle_id"),
                     "runtime_identity_id": snap.get("runtime_identity_id"),
                     "status": snap["status"], "icount_units": snap.get("icount_units"),
                     "built_from": "scopt prepare on " + (
                         ", ".join(Path(i).name for i in s["train_inputs"]) or "the fixed input")},
        "profile": {"profile_id": prof["profile_id"], "path": prof["path"],
                    "sha256": prof["file_sha256"], "phase": prof["phase"],
                    "runs": prof["runs"], "reps_per_input": prof["reps_per_input"],
                    "inputs": [{"path": p, "sha256": h} for p, h in s["train_inputs"].items()]
                    or [{"path": "fixed", "sha256": ""}],
                    "complete": prof["complete"], "flow_ok": prof["flow_ok"],
                    "unit_status_counts": prof["unit_status_counts"]},
        "arm": arms,
        "excluded_arm": excluded,
    }


def freeze(a: argparse.Namespace) -> int:
    comp = json.loads(Path(a.compute).read_text())
    blink = json.loads(Path(a.blinky).read_text())
    for s in (comp, blink):
        if s.get("verdict") != "pass":
            print(f"refused: {s['out']} prep verdict {s.get('verdict')}", file=sys.stderr)
            return 2
        if s.get("final_inputs_used"):
            print("refused: a prep used final inputs", file=sys.stderr)
            return 2
    finals = scan_final_runs()
    if finals:
        print(f"refused: runs on final inputs already exist: {finals[:5]}", file=sys.stderr)
        return 2
    code = code_identity()
    if code["dirty"]:
        print(f"refused: uncommitted changes in the code to be frozen: {code['dirty']}",
              file=sys.stderr)
        return 2
    sets = []
    for split in ("heldout", "final"):
        d = INPUT_SETS[split]
        sets.append({"name": f"compute-{split}", "workload": "compute", "split": split,
                     "input_set": str(d),
                     "input_manifest_sha256": sha256_file(d / "MANIFEST.json"),
                     "inputs": [{"path": str(p), "sha256": sha256_file(p)}
                                for p in sorted(d.glob("*.bin"))],
                     "reps": REPS, "seed": SEEDS[f"compute-{split}"], "timeout_s": TIMEOUT_S,
                     "out": f"compute-{split}"})
    sets.append({"name": "blinky-fixed", "workload": "blinky", "split": "fixed",
                 "inputs": [], "reps": REPS, "seed": SEEDS["blinky-fixed"],
                 "timeout_s": TIMEOUT_S, "out": "blinky-fixed",
                 "note": "informational (secondary workload, no input splits): the window is "
                         "~99.96% idle loop (fixtures/freertos-mps2-compute/QUALIFICATION.md)"})
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "name": "b2-freertos",
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "frozen_note": (
            "Frozen by WP15C-PREP before any run on a final input existed (final_runs_at_freeze "
            "= 0). Snapshot and profile were built from train inputs only; held-out inputs "
            "were used only for V10 correctness. Nothing in this file may change after the "
            "commit that adds it; b2_campaign.py check refuses any mismatch. No retuning "
            "after looking at final inputs (plan WP15): if a final-set result is unwelcome it "
            "is reported as it is."),
        "design": "design/B1-design.md section 7 F-B2-7; IMPLEMENTATION-PLAN.md WP15",
        "freeze": {"branch": git("rev-parse", "--abbrev-ref", "HEAD"),
                   "code_commit_at_freeze": git("rev-parse", "HEAD"),
                   "scopt_tree": code["scopt_tree"], "pipelines_tree": code["pipelines_tree"],
                   "driver_blob": code["driver_blob"], "final_runs_at_freeze": len(finals),
                   "checker": f"{DRIVER_REL} check --campaign <this file> --expect-sha256 "
                              "<sha256 of this file>"},
        "runtime": {"qemu": str(QEMU), "qemu_sha256": sha256_file(QEMU),
                    "qemu_note": "integrated WP-QINT binary (RUNTIME.md); every arm, the "
                                 "snapshot and the profile use it. The disabled-hook cost "
                                 "(RUNTIME.md decision 2026-11-16) is identical in every arm "
                                 "and cancels out of the paired comparisons; nothing is "
                                 "reported as a speedup over the B0 binary.",
                    "plugin": str(PLUGIN), "plugin_sha256": sha256_file(PLUGIN),
                    "launch": "setarch x86_64 -R, timeout, fresh run dir per run (own cache, "
                              "work, RAM), supercache_flat = false, RAM reset per run",
                    "fastdyn_src": str(ROOT / "src/fastdyn")},
        "schedule": {
            "interleave": True,
            "order": "per set: per repetition the input order is shuffled and per (rep, input) "
                     "the arm order is shuffled with random.Random(seed) (scopt.run.arms."
                     "make_schedule); the schedule is written to campaign.json before the "
                     "first run. Sets run one after another in the order listed.",
            "deploy_check": "scopt deploy --check on each deploy arm's first run",
            "min_reps_per_arm_and_set": 30,
        },
        "metrics": {
            "required": [
                "median paired speedup t_ref/t_arm per (arm vs warm) and (ablation vs layout), "
                "with 95% percentile bootstrap CI over pairs (10000 resamples, seeded), "
                "geometric mean, share of faster pairs; per input and over all inputs",
                "raw times of every run (timing.csv, report.json raw)",
                "BENCH guest-window host time (vCPU throughput on fixed work) = primary "
                "metric; wall and children CPU time secondary",
                "appended (fallback translation) counts per run and arm",
                "configured arena capacity (code_size), logical file size, allocated blocks "
                "(st_blocks*512), populated native bytes, per deployed .sc",
                "static locality MODEL: hot bytes, 64 B lines and 4 KiB pages covering 50/90/99% "
                "of exact steady entries, span, hot-edge distance (sidecar x_locality; "
                "labelled model, not a measurement)",
                "every regression: significant slowdowns, ablations faster than layout, "
                "output mismatches, invalid runs, fallback translations, mixed QEMU, freeze "
                "violations, locality model worse than warm",
            ],
            "primary_metric": "bench",
            "reference_arm": "warm",
            "bootstrap": 10000,
            "report_seed": 0,
        },
        "rules": {
            "R1_freeze": "b2_campaign.py check passes before the first and after the last set; "
                         "every scopt run campaign.json has status done and freeze_ok true",
            "R2_samples": "every arm of every set has >= 30 valid paired samples with the "
                          "reference arm (valid = exit 0, no timeout, BENCH printed, no stray "
                          "capture files, post-run proof PASS for deploy arms, NOT_PROVED and "
                          "no cache for nocache)",
            "R3_correctness": "no output_mismatch: every run's normalized guest output equals "
                              "the warm run of the same (rep, input) pair",
            "R4_qemu": "every valid sample ran the frozen QEMU sha256 (no mixed_qemu)",
            "R5_reported": "scopt report rendered per set and combined, with every required "
                           "metric and the regressions section",
            "verdict": "F-B2-7 = measured_and_reported when R1-R5 hold, else incomplete "
                       "(listing the failed rules). The speedup itself is NOT a pass criterion: "
                       "per comparison the verdict is faster / slower / "
                       "no_significant_difference (95% CI excludes 1 or not), and a null or "
                       "negative result is reported plainly, never tuned away.",
            "min_valid_pairs": 30,
        },
        "known_issues": [
            "Rare arena mmap collision: a warm/deploy run can exit 1 with QEMU 'allocate "
            "supercache code buffer: File exists' (MAP_FIXED_NOREPLACE at the cache's "
            "code_address 0x7fff6c000000 found an existing mapping, apparently "
            "timing-dependent host address-space layout). Seen once in this preparation (the "
            "warm BASELINE run of the first b2-layout V10 attempt, heldout-3; the V10 retry "
            "passed). Such a run is an invalid sample (exit_code:1), listed under invalid "
            "runs; it is never dropped silently and never retried by the campaign.",
            "Held-out and final inputs append fallback translations (about 200 records per "
            "run on held-out inputs: icount-budget-limited TBs and new entry points, "
            "fixtures/freertos-mps2-compute/QUALIFICATION.md section 6), identically in warm, "
            "identity and layout arms; appended counts are reported per run.",
            "identity and warm deploy byte-identical cache.sc files (V9): the identity arm "
            "is an A/A control of the deploy path, not a different layout.",
        ],
        "workload": [
            _workload_doc("compute", comp, "primary",
                          "FreeRTOS MPS2 CM3 compute fixture, icount (shift=0). Every unit is "
                          "CF_USE_ICOUNT; such units are qualified for B2 on this workload by "
                          "the B1 gate T-B1-3 (import cross-check, all units ok_relocatable) "
                          "and T-B1-5 (identity artifact static and dynamic) in "
                          "runs/wp14/gate/compute/gate.json (design section 9 row "
                          "'CF_USE_ICOUNT units')."),
            _workload_doc("blinky", blink, "secondary_informational",
                          "FreeRTOS MPS2 CM3 blinky fixture, icount. Fixed input (no splits). "
                          "Its BENCH window is ~99.96% idle loop, so layout can barely matter; "
                          "reported for information only."),
        ],
        "set": sets,
    }
    text = ("# F-B2-7 frozen timing campaign (WP15C-PREP). GENERATED by tests/qemu/"
            "b2_campaign.py freeze;\n# DO NOT EDIT: any change is a freeze violation "
            "(b2_campaign.py check).\n" + toml_dumps(doc))
    tomllib.loads(text)  # must parse
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(text)
    print(f"wrote {a.out} sha256 {sha256_file(a.out)}")
    return 0


def scan_final_runs(root: Path = ROOT / "runs") -> list[str]:
    """run.json files under runs/b2 that name a final input (none may exist at freeze)."""
    out = []
    d = root / "b2"
    if not d.is_dir():
        return out
    for p in d.rglob("run*.json"):
        try:
            if "/inputs/final/" in p.read_text(errors="replace"):
                out.append(str(p))
        except OSError:
            pass
    return out


# --------------------------------------------------------------------------
# check
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
        probs.append(f"campaign schema {doc.get('schema')} != {SCHEMA}")
        return {"ok": False, "problems": probs, "checked": checked}
    if check_git:
        try:
            repo = Path(git("rev-parse", "--show-toplevel", cwd=cpath.parent))
            rel = str(cpath.relative_to(repo))
            st = git("status", "--porcelain", "--", rel, cwd=repo)
            eq("campaign file committed and clean", "", st)
            code = code_identity(wt)
            fr = doc["freeze"]
            eq("scopt package (git tree at HEAD)", fr["scopt_tree"], code["scopt_tree"])
            eq("pipelines (git tree at HEAD)", fr["pipelines_tree"], code["pipelines_tree"])
            eq("driver b2_campaign.py (git blob at HEAD)", fr["driver_blob"],
               code["driver_blob"])
            eq("uncommitted code changes", [], code["dirty"])
        except (subprocess.CalledProcessError, ValueError) as e:
            probs.append(f"git check failed: {e}")
    rt = doc["runtime"]
    fsha("qemu", rt["qemu"], rt["qemu_sha256"])
    fsha("plugin", rt["plugin"], rt["plugin_sha256"])
    for w in doc["workload"]:
        n = w["name"]
        fsha(f"{n}: base config", w["base_config"], w["base_config_sha256"])
        fw = w["firmware"]
        fsha(f"{n}: firmware", fw["path"], fw["sha256"])
        try:
            st = os.stat(fw["path"])
            eq(f"{n}: firmware size", fw["size"], st.st_size)
            eq(f"{n}: firmware mtime_ns (fingerprinted)", fw["mtime_ns"], st.st_mtime_ns)
        except OSError as e:
            probs.append(f"{n}: firmware {e}")
        sn = w["snapshot"]
        fsha(f"{n}: snapshot cache.sc", Path(sn["dir"]) / "cache.sc", sn["cache_sc_sha256"],
             sc=True)
        fsha(f"{n}: snapshot.json", Path(sn["dir"]) / "snapshot.json",
             sn["snapshot_json_sha256"])
        pr = w["profile"]
        fsha(f"{n}: profile file", pr["path"], pr["sha256"])
        try:
            eq(f"{n}: profile_id", pr["profile_id"],
               json.loads(Path(pr["path"]).read_text()).get("profile_id"))
        except (OSError, ValueError) as e:
            probs.append(f"{n}: profile unreadable: {e}")
        for i in pr["inputs"]:
            if i["path"] != "fixed":
                fsha(f"{n}: profile (train) input {Path(i['path']).name}", i["path"],
                     i["sha256"])
        for arm in w["arm"]:
            if arm["kind"] != "deploy":
                continue
            src = Path(arm["source"])
            fsha(f"{n}/{arm['name']}: cache.sc", src / "cache.sc", arm["sha256"], sc=True)
            if arm.get("artifact_id"):
                man = src / "MANIFEST.sha256"
                fsha(f"{n}/{arm['name']}: MANIFEST.sha256", man, arm["manifest_sha256"])
                try:
                    eq(f"{n}/{arm['name']}: artifact id", arm["artifact_id"],
                       artifact_id(man.read_bytes()))
                except OSError as e:
                    probs.append(f"{n}/{arm['name']}: {e}")
                eq(f"{n}/{arm['name']}: artifact dir name", _hex(arm["artifact_id"]), src.name)
    for s in doc["set"]:
        if s.get("input_set"):
            fsha(f"{s['name']}: input MANIFEST.json", Path(s["input_set"]) / "MANIFEST.json",
                 s["input_manifest_sha256"])
            listed = sorted(str(p) for p in Path(s["input_set"]).glob("*.bin"))
            eq(f"{s['name']}: input files", sorted(i["path"] for i in s["inputs"]), listed)
        for i in s["inputs"]:
            fsha(f"{s['name']}: input {Path(i['path']).name}", i["path"], i["sha256"])
        eq(f"{s['name']}: reps >= {doc['schedule']['min_reps_per_arm_and_set']}", True,
           int(s["reps"]) >= int(doc["schedule"]["min_reps_per_arm_and_set"]))
    return {"ok": not probs, "problems": probs, "checked": checked}


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def set_campaign_toml(doc: dict[str, Any], s: dict[str, Any], *, reps: int,
                      name: str) -> str:
    """The ``scopt run --campaign`` TOML of one set (scopt.run.arms.load_campaign)."""
    w = next(x for x in doc["workload"] if x["name"] == s["workload"])
    camp: dict[str, Any] = {
        "name": name, "base_config": w["base_config"], "qemu": doc["runtime"]["qemu"],
        "reps": reps, "seed": int(s["seed"]), "reference": "warm",
        "timeout_s": float(s["timeout_s"]), "input_bank": "input",
    }
    if s.get("input_set"):
        camp["input_set"] = s["input_set"]
        camp["split"] = s["split"]
    else:
        camp["split"] = s["split"]
    arms = []
    for a in w["arm"]:
        x = {"name": a["name"], "kind": a["kind"], "role": a["role"]}
        if a["kind"] == "deploy":
            x["source"] = a["source"]
            x["sha256"] = a["sha256"]
        if a.get("parent"):
            x["parent"] = a["parent"]
            x["ablated_pass"] = a["ablated_pass"]
        arms.append(x)
    not_run = {e["name"]: e["reason"] for e in w.get("excluded_arm", [])}
    out = {"campaign": camp, "arm": arms}
    if not_run:
        out["not_run"] = not_run
    return ("# derived from the frozen b2-freertos campaign by b2_campaign.py run; "
            "do not edit\n" + toml_dumps(out))


def _scopt(args: list[Any], log: Path) -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(WT)
    argv = [PY, "-m", "scopt", *[str(x) for x in args]]
    with open(log, "w") as f:
        f.write(json.dumps(argv[2:]) + "\n")
        f.flush()
        p = subprocess.run(argv, cwd=str(WT), env=env, stdout=f, stderr=subprocess.STDOUT)
    return p.returncode


def evaluate_set(out: Path, min_pairs: int) -> dict[str, Any]:
    """The rules R1-R5 (per set) from campaign.json and the report model."""
    res: dict[str, Any] = {"rules": {}, "failed": []}
    try:
        cj = json.loads((out / "campaign.json").read_text())
    except (OSError, ValueError) as e:
        res["failed"].append(f"campaign.json unreadable: {e}")
        return res
    r1 = cj.get("status") == "done" and cj.get("freeze_ok") is True
    res["rules"]["R1_freeze"] = {"ok": r1, "status": cj.get("status"),
                                 "freeze_ok": cj.get("freeze_ok")}
    try:
        model = json.loads((out / "report" / "report.json").read_text())
    except (OSError, ValueError) as e:
        res["failed"].append(f"report.json unreadable: {e}")
        return res
    g = next((x for x in model.get("groups", []) if x["name"].startswith("campaign:")), None)
    if g is None:
        res["failed"].append("no campaign group in report.json")
        return res
    ref = g.get("reference")
    pairs = {}
    for c in g["comparisons"]:
        if c["why"] == "vs_reference" and c["metric"] == "bench" and c["input"] == "*":
            pairs[c["arm"]] = c.get("n_pairs")
    short = {a: n for a, n in pairs.items() if (n or 0) < min_pairs}
    ref_valid = (g["per_arm"].get(ref) or {}).get("valid", 0)
    res["rules"]["R2_samples"] = {"ok": not short and ref_valid >= min_pairs,
                                  "pairs_vs_reference": pairs, "reference_valid": ref_valid,
                                  "short": short}
    res["rules"]["R3_correctness"] = {"ok": not g["correctness"],
                                      "mismatches": len(g["correctness"])}
    qs = g.get("qemu_sha256_valid") or []
    res["rules"]["R4_qemu"] = {"ok": len(qs) == 1, "qemu_sha256_valid": qs}
    res["rules"]["R5_reported"] = {"ok": (out / "report" / "report.md").is_file()
                                   and "regressions" in model}
    res["failed"] = [k for k, v in res["rules"].items() if not v["ok"]]
    res["speedups_bench_vs_reference"] = {
        c["arm"]: {k: c.get(k) for k in ("median", "ci", "verdict", "n_pairs")}
        for c in g["comparisons"] if c["metric"] == "bench" and c["input"] == "*"
        and c["why"] == "vs_reference"}
    res["ablations_bench_vs_parent"] = {
        c["arm"]: {k: c.get(k) for k in ("reference", "median", "ci", "verdict", "n_pairs")}
        for c in g["comparisons"] if c["metric"] == "bench" and c["input"] == "*"
        and c["why"] == "ablation_vs_parent"}
    res["regressions"] = [r for r in model.get("regressions", [])
                          if r.get("group") == g["name"]]
    return res


def run(a: argparse.Namespace) -> int:
    t0 = time.time()
    if a.reps is not None and not a.smoke:
        print("refused: --reps changes the frozen repetitions; only --smoke may", file=sys.stderr)
        return 2
    if a.smoke and a.reps is None:
        print("refused: --smoke needs --reps", file=sys.stderr)
        return 2
    chk = check(a.campaign, a.expect_sha256)
    if not chk["ok"]:
        print("refused: frozen identities do not match:", file=sys.stderr)
        for p in chk["problems"]:
            print(f"  {p}", file=sys.stderr)
        return 1
    doc = load(a.campaign)
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
    (root / "campaigns").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "schema": "scopt.b2_campaign_run/1", "campaign": str(Path(a.campaign).resolve()),
        "campaign_sha256": sha256_file(a.campaign), "smoke": bool(a.smoke),
        "reps_override": a.reps, "check_before": chk, "sets": {}, "started": t0,
        "host_start": os.getloadavg()}
    sumpath = root / ("smoke.json" if a.smoke else "b2_campaign.json")
    profiles = {w["name"]: w["profile"]["path"] for w in doc["workload"]}
    for s in sets:
        reps = a.reps if a.smoke else int(s["reps"])
        name = s["name"] + ("-smoke" if a.smoke else "")
        ctoml = root / "campaigns" / f"{s['name']}.toml"
        ctoml.write_text(set_campaign_toml(doc, s, reps=reps, name=name))
        out = root / s["out"]
        ts = time.time()
        print(f"{time.strftime('%H:%M:%S')} set {s['name']}: scopt run reps={reps} -> {out}",
              flush=True)
        rc = _scopt(["run", "--campaign", ctoml, "--out", out, "--no-report"],
                    root / "logs" / f"{s['name']}-run.log")
        rrc = _scopt(["report", out, "--out", out / "report" / "report.md", "--profile",
                      profiles[s["workload"]]], root / "logs" / f"{s['name']}-report.log")
        ev = evaluate_set(out, int(doc["rules"]["min_valid_pairs"]) if not a.smoke else 1)
        summary["sets"][s["name"]] = {"out": str(out), "campaign_toml": str(ctoml),
                                      "reps": reps, "run_rc": rc, "report_rc": rrc,
                                      "seconds": round(time.time() - ts, 1), **ev}
        print(f"{time.strftime('%H:%M:%S')} set {s['name']}: run rc={rc} report rc={rrc} "
              f"failed rules={ev['failed']} ({time.time() - ts:.0f} s)", flush=True)
        sumpath.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    chk2 = check(a.campaign, a.expect_sha256)
    summary["check_after"] = chk2
    rrc = _scopt(["report", *[root / s["out"] for s in sets], "--out",
                  root / "report" / "report.md",
                  *[x for w in doc["workload"] for x in ("--profile", w["profile"]["path"])]],
                 root / "logs" / "combined-report.log")
    summary["combined_report"] = {"rc": rrc, "path": str(root / "report" / "report.md")}
    failed = {n: v["failed"] for n, v in summary["sets"].items() if v["failed"]}
    if not chk2["ok"]:
        failed["check_after"] = chk2["problems"]
    summary["verdict"] = ("smoke_ok" if a.smoke and not failed else
                          "smoke_failed" if a.smoke else
                          "measured_and_reported" if not failed else "incomplete")
    summary["failed"] = failed
    summary["duration_s"] = round(time.time() - t0, 1)
    summary["host_end"] = os.getloadavg()
    sumpath.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(f"verdict {summary['verdict']} ({summary['duration_s']} s) -> {sumpath}")
    return 0 if not failed else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--compute", required=True)
    f.add_argument("--blinky", required=True)
    f.add_argument("--out", required=True)
    c = sub.add_parser("check")
    c.add_argument("--campaign", required=True)
    c.add_argument("--expect-sha256", default=None)
    c.add_argument("--no-git", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--campaign", required=True)
    r.add_argument("--expect-sha256", required=True)
    r.add_argument("--out-root", required=True)
    r.add_argument("--sets", default=None, help="comma-separated set names (default: all)")
    r.add_argument("--smoke", action="store_true")
    r.add_argument("--reps", type=int, default=None)
    a = ap.parse_args(argv)
    if a.cmd == "freeze":
        return freeze(a)
    if a.cmd == "check":
        res = check(a.campaign, a.expect_sha256, check_git=not a.no_git)
        print(json.dumps(res, indent=2))
        return 0 if res["ok"] else 1
    return run(a)


if __name__ == "__main__":
    sys.exit(main())

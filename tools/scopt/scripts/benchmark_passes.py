"""Benchmark pipeline prefixes and leave-one-pass-out arms on a qualified firmware."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import statistics
import subprocess
import sys

from scopt.formats.sc_v5 import sha256_sparse
from scopt.formats.sc_v6 import read_sc6
from scopt.manager import HOST_INLINE_ALIASES, load_pipeline
from scopt.run.benchmark_memory import benchmark_ram_override
from scopt.run.pass_policy import PolicyMismatch, check_requested, policy_arguments


HERE = Path(__file__).resolve().parent


def load(path):
    return json.loads(path.read_text())


def save(path, doc):
    path.write_text(json.dumps(doc, indent=2) + "\n")


def pair_stats(samples, before, after):
    ratios = [r["ns"][before] / r["ns"][after] for r in samples]
    # Resample pairs within input, retaining the mix of hot and general-entry paths.
    bank = {}
    for r, ratio in zip(samples, ratios):
        bank.setdefault(r["input"], []).append(ratio)
    rng = random.Random(20261118)
    draws = sorted(statistics.median([x for group in bank.values()
                                      for x in rng.choices(group, k=len(group))])
                   for _ in range(4000))
    return {"median": statistics.median(ratios), "paired_95pct_bootstrap": [draws[99], draws[3899]],
            "median_by_input": {k: statistics.median(v) for k, v in bank.items()},
            "pairs": len(ratios)}


def comparison_effect(samples, cache_hashes, baseline, optimized):
    observed = pair_stats(samples, baseline, optimized)
    same_cache = cache_hashes[baseline] == cache_hashes[optimized]
    return {
        "baseline": baseline,
        "optimized": optimized,
        "byte_identical_sc": same_cache,
        **observed,
        "attribution": {
            "status": "not_attributable" if same_cache else "eligible",
            "reason": "byte_identical_sc" if same_cache else "different_sc_bytes",
            "speed_ratio": None if same_cache else observed["median"],
            "paired_95pct_bootstrap": None if same_cache else observed["paired_95pct_bootstrap"],
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("pipeline", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--params", type=Path)
    parser.add_argument("--selftest", type=Path)
    parser.add_argument("--post-splice", type=Path,
                        help="compose_program output containing a splice artifact and its reimport")
    parser.add_argument("--jobs", type=int, default=2, help="parallel arm preparation workers")
    parser.add_argument("--pairs", type=int, default=5)
    parser.add_argument("--prefix-only", action="store_true")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    if args.pairs < 5:
        parser.error("at least five pairs per input required")
    w, out = args.baseline.resolve(), args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    m = load(w / "baseline.json")
    identity = Path(m["artifact"])
    config = w / "host-inline.toml"
    qemu = load(w / "v10/dynamic.json")["qemu"]
    env = {**os.environ, "SCOPT_ENVLAYOUT_PATH": str(w / "envlayouts"),
           "SCOPT_V6_EXES": m["qemu_sha256"]}
    ram_root = benchmark_ram_override(config, m.get("ram_root"), env)
    if ram_root is not None:
        env["SCOPT_RAM_ROOT"] = ram_root
    # Each subprocess must load the same version of the optimizer, validator and
    # pass registry while development continues in the checkout.
    frozen = out / "source" / "tools" / "scopt"
    shutil.copytree(HERE.parent, frozen, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
    env["PYTHONPATH"] = os.pathsep.join((str(frozen), env.get("PYTHONPATH", "")))
    package_digest = hashlib.sha256()
    for source in sorted((frozen / "scopt").rglob("*.py")):
        package_digest.update(str(source.relative_to(frozen)).encode() + b"\0" + source.read_bytes())
    pipeline = out / "pipeline.toml"
    shutil.copyfile(args.pipeline.resolve(), pipeline)
    names = [p.name for p in load_pipeline(pipeline).passes]
    if len(names) != len(set(names)):
        parser.error("a prefix benchmark requires distinct pass names")
    sequence = [(f"after-{name}", names[:i + 1]) for i, name in enumerate(names)]
    if not args.prefix_only:
        sequence.extend((f"without-{name}", [p for p in names if p != name]) for name in names)
    native_names = {"host-inline", *HOST_INLINE_ALIASES}
    if args.post_splice and any(name in native_names for name in names):
        parser.error("--post-splice requires a downstream pipeline without the native prefix")
    qualified, rows = [], {}
    optimization_snapshot = m["snapshot"]
    first_baseline = "identity"
    if args.post_splice:
        upstream = args.post_splice.resolve()
        if (upstream / "result.json").is_file():
            source = load(upstream / "result.json")["native_stage"]
            optimization_snapshot = source["snapshot_dir"]
        else:
            imported, = (upstream / "reimport/snapshots").iterdir()
            optimization_snapshot = str(imported)
            source = load(upstream / "host-inline-splice.json")
        # New ledgers use the configurable pass name. Keep the name measured
        # by archived upstream runs when reproducing their ablations.
        if source.get("passes"):
            native, = source["passes"]
            first_baseline = native["pass"]
            if first_baseline not in native_names:
                parser.error("the upstream native prefix must contain a host-inline pass")
        else:
            first_baseline = "host-inline-splice"
        if not args.prefix_only:
            sequence.append((f"without-{first_baseline}", names))
        d = out / first_baseline
        d.mkdir()
        save(d / "result.json", source)
        artifact = Path(source["artifact_dir"])
        cmd = ["scopt", "validate", str(artifact), "--dynamic", "-c", str(config),
               "--qemu", qemu, "--baseline", m["snapshot"], "--out", str(d / "v10")]
        for i in range(len(m["oracle"]["heldout"])):
            cmd += ["--inputs", str(w / f"heldout-{i}.bin")]
        with (d / "v10.log").open("w") as log:
            subprocess.run(cmd, env=env, stdout=log, stderr=log, check=True)
        rows[first_baseline] = {"artifact_id": source["artifact_id"], "v10_status": 0,
                                "sc_sha256": sha256_sparse(artifact / "cache.sc")}
        qualified.append(first_baseline)
    def prepare_arm(stage):
        name, enabled = stage
        d = out / name
        d.mkdir()
        remove_prefix = args.post_splice and name == f"without-{first_baseline}"
        stage_input = m["snapshot"] if remove_prefix else optimization_snapshot
        cmd = [sys.executable, str(HERE / "measure_command.py"), str(d / "offline.json"),
               "scopt", "optimize", stage_input, "--pipeline", str(pipeline),
               "--profile", m["profile"], "--store", str(d), "--runtime-identity",
               str(w / "runtime-identity.v2.json"), "--elf", str(w / "guest.elf"),
               "--prep", str(w / "prep"), "--json"]
        if args.selftest:
            # The CLI may refresh a stale oracle. Keep the user-supplied witness immutable.
            witness = d / "selftest.json"
            shutil.copyfile(args.selftest, witness)
            cmd += ["--selftest", str(witness)]
        if args.params:
            cmd += ["--params", str(args.params.resolve())]
        # Default-disabled passes run only when requested: ask for exactly this arm.
        cmd += policy_arguments(names, enabled)
        with (d / "result.json").open("w") as result, (d / "optimize.log").open("w") as log:
            run = subprocess.run(cmd, env=env, stdout=result, stderr=log)
        row = {"enabled": enabled, "optimize_status": run.returncode,
                            "offline": load(d / "offline.json")}
        if (d / "result.json").stat().st_size:
            result = load(d / "result.json")
            row.update({key: result.get(key) for key in ("artifact_id", "pass_byte_delta",
                                                          "validation", "reason", "path")})
        if run.returncode:
            return name, row
        # An arm that claims a stage must have run it (a stage that ran and changed
        # nothing is fine; one skipped by policy is a misconfigured arm).
        try:
            row["effective"] = check_requested(result, enabled)
        except PolicyMismatch as error:
            row.update({"optimize_status": 3, "reason": "policy_mismatch", "detail": str(error)})
            return name, row
        artifact = Path(result["artifact_dir"])
        cache = artifact / "cache.sc"
        image = read_sc6(cache)
        row["sc_bytes"] = cache.stat().st_size
        row["sc_allocated_bytes"] = cache.stat().st_blocks * 512
        row["base_native_bytes"] = sum(image.sc.records[i].tc_size for i in image.sc.valid_slots)
        row["variant_native_bytes"] = sum(v.tc_size for v in image.ext.variants) if image.ext else 0
        row["sc_sha256"] = sha256_sparse(cache)
        row["pass_outcomes"] = [{k: p.get(k) for k in ("pass", "outcome", "reason", "n_edits")}
                                for p in result.get("passes", [])]
        # A manager can publish the remaining candidate after a failed pass. Such an
        # arm is recorded but cannot measure the requested composition.
        if any(p.get("outcome") == "failed" for p in result.get("passes", [])):
            row["refused"] = "pass_failed"
            return name, row
        validate = ["scopt", "validate", str(artifact), "--dynamic", "-c", str(config),
                    "--qemu", qemu, "--baseline", m["snapshot"], "--out", str(d / "v10")]
        for i in range(len(m["oracle"]["heldout"])):
            validate += ["--inputs", str(w / f"heldout-{i}.bin")]
        with (d / "v10.log").open("w") as log:
            row["v10_status"] = subprocess.run(validate, stdout=log, stderr=log, env=env).returncode
        return name, row
    # Each worker uses its own artifact store, witness file and V10 run directories.
    # Timed trials start after all workers have finished and use a common lock.
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        for name, row in executor.map(prepare_arm, sequence):
            rows[name] = row
            if row.get("v10_status") == 0:
                qualified.append(name)
            save(out / "stages.json", rows)
    save(out / "stages.json", rows)
    if not qualified:
        raise SystemExit("no qualified benchmark arms")
    target = out / "timing"
    cmd = [sys.executable, str(HERE / "measure_program.py"), str(w), str(target),
           "--pairs", str(args.pairs)]
    for name in qualified:
        cmd.extend(["--arm", f"{name}={out / name}"])
    subprocess.run(cmd, env=env, check=True)
    samples = load(target / "ratios.json")["samples_ns"]
    cache_hashes = {"identity": sha256_sparse(identity / "cache.sc")}
    cache_hashes.update({name: rows[name]["sc_sha256"] for name in qualified})
    comparisons = {}
    if args.post_splice:
        comparisons[f"incremental/{first_baseline}"] = comparison_effect(
            samples, cache_hashes, "identity", first_baseline)
    full = "after-" + names[-1]
    if args.post_splice and f"without-{first_baseline}" in qualified and full in qualified:
        comparisons[f"ablation/{first_baseline}"] = comparison_effect(
            samples, cache_hashes, f"without-{first_baseline}", full)
    for i, name in enumerate(names):
        after = f"after-{name}"
        prev = first_baseline if i == 0 else "after-" + names[i - 1]
        for kind, baseline, optimized in (("incremental", prev, after),
                                          ("ablation", f"without-{name}", full)):
            if optimized not in qualified or (baseline != "identity" and baseline not in qualified):
                continue
            comparisons[f"{kind}/{name}"] = comparison_effect(
                samples, cache_hashes, baseline, optimized)
    unqualified = [name for name, _ in sequence if name not in qualified]
    save(out / "pass-effects.json", {"pipeline": str(pipeline), "snapshot": m["snapshot"],
                                     "prefix_pass": first_baseline if args.post_splice else None,
                                     "optimization_snapshot": optimization_snapshot,
                                     "optimizer_source_sha256": package_digest.hexdigest(),
                                     "effects": comparisons, "stages": rows,
                                     "complete": not unqualified, "unqualified": unqualified})
    if unqualified:
        raise SystemExit(f"unqualified benchmark arms: {unqualified}")


if __name__ == "__main__":
    main()

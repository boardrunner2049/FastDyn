#!/usr/bin/env python3
"""Run an isolated, known-inventory Pye campaign with the scopt Python environment."""
import argparse
import json
import os
import random
import re
import shutil
import tarfile
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
LIB = HERE.parents[1] / "lib"
sys.path.insert(0, str(LIB))

from campaign import write_json
from candidates import build
from final_evaluation import evaluate
from joint_selection import ApplicationTrial, InvalidObservation, require, select
from scopt.run.launcher import RunDir, file_sha256
from scopt.run.warm_selection import _paired_evidence, _paired_orders
from session import stage


def read(path):
    return json.loads(Path(path).read_text())


def pin(path):
    path = Path(path).resolve(strict=True)
    return {"path": str(path), "sha256": file_sha256(path)}


def command(log_dir, name, argv, timeout=1800):
    """Keep the command and both output streams for every attempt."""
    write_json(log_dir / (name + ".argv.json"), list(map(str, argv)))
    with (log_dir / (name + ".log")).open("x") as log:
        subprocess.run(list(map(str, argv)), stdout=log, stderr=subprocess.STDOUT, check=True, timeout=timeout)


def stage_cases(product, out, policy):
    cases = {}
    frozen = read(HERE / "qualification/case-identities.json")
    for role, names in (policy["scenarios"] | policy["prechange_scenarios"]).items():
        cases[role] = []
        for name in names:
            dest = out / name
            record = stage(product, name, dest)
            require(record["protocol"] == policy["transport"], f"wrong UART protocol: {name}")
            require(all(record[key] == val for key, val in frozen[name].items()) and record["role"] == role,
                    f"scenario manifest changed: {name}")
            expected = Path(record["expected"])
            oracle = {"completed_work": 1, "observations": {
                "returned_sha256": record["expected_output_sha256"], "returned_bytes": expected.stat().st_size}}
            write_json(dest / "oracle.json", oracle)
            rom = pin(record["romfs"])
            peer = {"program": pin(HERE / "session.py"), "stage": pin(dest / "stage.json"),
                    "bank": "psram", "input_sha256": rom["sha256"]}
            cases[role].append({"id": name, "weight": 1, "bank_inputs": {"psram": rom},
                                "expected": pin(dest / "oracle.json"), "peer": peer})
    return cases


def prepare_image(product, dest, elf, qemu, plugin, policy):
    dest.mkdir()
    scopt = Path(sys.executable).parent / "scopt"
    runner = [sys.executable, HERE / "fastdyn_runner.py"]
    common = ["--work", product, "--run", dest, "--qemu", qemu, "--plugin", plugin, "--elf", elf]
    all_roles = policy["scenarios"] | policy["prechange_scenarios"]
    frozen = policy.get("cache_track") == "training-frozen"
    inventory_roles = {"training": policy["scenarios"]["training"]} if frozen else all_roles
    inventory = [n for names in inventory_roles.values() for n in names] * policy["inventory_sweeps"]
    write_json(dest / "inventory-plan.json", {"track": policy["track"], "roles": list(inventory_roles),
                                               "cases_in_order": inventory})
    captures = [x for name in inventory for x in ("--case", name)]
    train, = policy["scenarios"]["training"]
    prep = dest / "prep"
    cfg = dest / "pye.toml"
    manifest = prep / "cache/fw.sc.fastdyn.json"
    if policy.get("stable_sweeps"):
        require(policy["stable_sweeps"] == 2, "this capture supports two unchanged sweeps")
        captures += ["--stable-sweep-size", str(len(inventory) // policy["inventory_sweeps"])]
    command(dest, "capture", [*runner, "capture", *common, *captures, "--out", prep],
            timeout=policy["timeout_seconds"] * len(inventory) + 120)
    command(dest, "bundle", [scopt, "bundle", "identity", "-c", prep / "run.toml", "--manifest", manifest,
                             "--out", dest / "bundle.json"])
    command(dest, "runtime", [scopt, "bundle", "runtime-identity", "--run-dir", prep / "oracle",
                              "--sc", prep / "cache/fw.sc", "--scm", prep / "capture.scm",
                              "--out", dest / "runtime.json"])
    command(dest, "import", [scopt, "import", prep, "--bundle", dest / "bundle.json", "--runtime-identity",
                             dest / "runtime.json", "--store", dest / "store", "--no-progress"])
    snapshot, = (dest / "store/snapshots").iterdir()
    command(dest, "admission", [scopt, "admission", "identity", "--snapshot", snapshot, "--config", cfg,
                                "--out", dest / "admission-runtime.json"])
    failures = {}
    for check in (("frozen" if frozen else "warm"), "profile"):
        try:
            command(dest, check, [*runner, check, *common, "--case", train, "--snapshot", snapshot,
                                  "--out", dest / check])
        except subprocess.SubprocessError as exc:
            failures[check] = str(exc)
    require(not failures, f"ordinary replay or exact training profile failed: {failures}")
    return dict(firmware=str(elf), config=str(cfg), runtime=str(qemu), snapshot=str(snapshot),
                prep=str(prep), profile=str(dest / "profile/merged.profile.json"), manifest=str(manifest),
                admission_runtime=str(dest / "admission-runtime.json"))


def supplied_firmware(args, out, product, qemu, train):
    """Requalify an externally built deployment on this host and the common job."""
    from guest_layout import check

    dest = out / "firmbolt-guest"
    dest.mkdir()
    source_bin = product / "micropython/ports/qemu/build-MPS2_AN385/firmware.bin"
    original = source_bin.with_name("firmware-relocs.elf")
    require(file_sha256(source_bin) == "sha256:" + args.firmbolt_origin_sha256, "FirmBOLT parent payload differs")
    candidate = dest / "firmware.elf"
    shutil.copyfile(args.firmbolt_elf, candidate)
    require(file_sha256(candidate) == "sha256:" + args.firmbolt_sha256, "supplied FirmBOLT ELF hash differs")
    layout = check(original, candidate)
    write_json(dest / "layout.json", layout)
    argv = [sys.executable, HERE / "cold.py", "--qemu", qemu, "--stage", train["peer"]["stage"]["path"],
            "--elf", candidate]
    command(dest, "cold", [*argv, "--out", dest / "cold"])
    trace = dest / "exec.trace"
    command(dest, "diagnostic", [*argv, "--out", dest / "diagnostic", "--diagnostic-exec-trace", trace])
    # The second address in the exec trace is the guest PC. Chaining can bypass
    # this tracepoint, so report observed starts and no exact dynamic edge count.
    pattern = re.compile(rb"^Trace [0-9]+: 0x[0-9a-f]+ \[([0-9a-f]+)/([0-9a-f]+)/")
    moved = layout["relocated_sections"]
    pcs = set()
    with trace.open("rb") as stream:
        for line in stream:
            match = pattern.match(line)
            if match:
                pc = int(match[2], 16)
                if any(region["start"] <= pc < region["end"] for region in moved):
                    pcs.add(pc)
    require(pcs, "training execution trace did not enter relocated candidate code")
    write_json(dest / "executed-layout.json", {"role": "training diagnostic only", "trace": pin(trace),
               "observed_relocated_tb_starts": sorted(pcs), "firmware": pin(candidate),
               "source_binary": pin(source_bin)})
    return candidate


def optimizer_bindings(out, qemu):
    scopt = Path(sys.executable).parent / "scopt"
    env_dir = out / "envlayout"
    env_dir.mkdir()
    helper_dir = out / "helper-effects"
    helper_dir.mkdir()
    os.environ.update(SCOPT_ENVLAYOUT_PATH=str(env_dir), SCOPT_HELPER_EFFECTS_PATH=str(helper_dir))
    source = ROOT / "third_party/common/qemu"
    sha = file_sha256(qemu).removeprefix("sha256:")
    command(out, "envlayout", [scopt, "envlayout", "produce", "--exe", qemu, "--out", env_dir / (sha + ".json")])
    command(out, "helper-effects", [sys.executable, LIB / "rebind_helper_effects.py", qemu, source])


def campaign(args):
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    policy_name = "frozen-policy.json" if args.training_frozen else "campaign-policy.json"
    if args.eligible_history_only:
        require(args.training_frozen and args.firmbolt_elf, "supplemental historical control requires the frozen track and a qualified fixed guest")
        policy_name = "historical-eligible-policy.json"
    if args.promotion_only:
        require(args.training_frozen and not args.eligible_history_only and args.firmbolt_elf,
                "promotion-only comparison requires training-frozen preparation and a prequalified fixed guest")
        policy_name = "rebuilt-promotion-policy.json"
    policy = read(HERE / policy_name)
    write_json(out / "policy.json", policy)
    receipt = {"state": "running", "images": {}, "policy": pin(HERE / policy_name),
               "timing_cpu": args.cpu, "errors": {}}
    write_json(out / "campaign.json", receipt)
    phase = "initialization"
    start = time.monotonic()
    try:
        require(args.cpu in os.sched_getaffinity(0), "requested CPU outside allowed affinity")
        os.sched_setaffinity(0, {args.cpu})
        product = args.product.resolve()
        qemu = args.qemu.resolve(strict=True)
        plugin = args.plugin.resolve(strict=True)
        os.environ.update(FASTDYN_ROOT=str(out / "scopt"), SCOPT_FASTDYN_SRC=str(ROOT),
                          TREBUCHET_QEMU=str(qemu))
        # Keep disk-backed data private per launch on small hosted /dev/shm.
        os.environ.pop("SCOPT_RAM_ROOT", None)
        os.environ["PATH"] = str(ROOT / "fastdyn-env/bin") + os.pathsep + os.environ["PATH"]
        receipt.update(qemu=pin(qemu), plugin=pin(plugin), host=list(os.uname()))
        command(out, "personality", ["setarch", "x86_64", "-R", "true"])
        phase = "product build"
        for action in ("fetch", "build"):
            command(out, action, [HERE / "prepare.sh", action, product])
        phase = "scenario contract"
        cases = stage_cases(product, out / "cases", policy)
        train, = cases["training"]
        contract = out / "contract.json"
        write_json(contract, {"protocol": policy["transport"], "sources": [
            pin(HERE / p) for p in ("manifest.json", "promotion.json", "prechange.json", "protocol-queued.json",
                                     "qualification/case-identities.json",
                                     "session.py", "observe.py", policy_name)],
            "cases": cases})
        images = {"original": product / "micropython/ports/qemu/build-MPS2_AN385/firmware.elf"}
        phase = "FirmBOLT image"
        try:
            if args.firmbolt_elf:
                images["firmbolt"] = supplied_firmware(args, out, product, qemu, train)
            elif not os.environ.get("FIRMBOLT_REPO"):
                receipt["images"]["firmbolt"] = {"status": "unavailable",
                    "reason": "no prequalified ELF or local FirmBOLT toolchain supplied"}
            else:
                profile = out / "firmbolt-guest-profile"
                optimized = out / "firmbolt-guest"
                for action, argv in (("profile", [profile]), ("optimize", [optimized, profile])):
                    command(out, "firmbolt-" + action, [HERE / "firmbolt.sh", action, product,
                                                       train["peer"]["stage"]["path"], *argv])
                require(read(optimized / "executed-layout.json")["relocated_tb_executions"] > 0, "no relocated TB")
                images["firmbolt"] = optimized / "firmware.elf"
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            receipt["errors"][phase] = str(exc)
            receipt["images"]["firmbolt"] = {"status": "unqualified", "error": str(exc)}
        phase = "optimizer bindings"
        try:
            optimizer_bindings(out, qemu)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            # Ordinary and transformations without this dependency remain eligible.
            receipt["errors"][phase] = str(exc)
        phase = "per-image preparation"
        specs = {}
        if args.eligible_history_only:
            require("firmbolt" in images, "supplemental historical fixed guest failed qualification")
            images = {"firmbolt": images["firmbolt"]}
        for name, elf in images.items():
            try:
                specs[name] = prepare_image(product, out / name, elf, qemu, plugin, policy)
                receipt["images"][name] = {"status": "prepared", **specs[name]}
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                receipt["images"][name] = {"status": "failed", "error": str(exc)}
            write_json(out / "campaign.json", receipt)
        require(("firmbolt" if args.eligible_history_only else "original") in specs, "original ordinary-warm preparation failed; see image logs")
        phase = "registry"
        plan = {k: policy[k] for k in ("application", "configuration", "objective", "recipes", "budget")}
        plan.update(scenario=policy["track"], admission=str(HERE / "admission.json"),
                    contract=str(contract), images=specs)
        write_json(out / "materialization-plan.json", plan)
        if args.promotion_only:
            from replay_promotion import compare_rebuilt
            phase = "supplemental comparison of previously frozen recipes"
            compare_rebuilt(out, plan, policy, cases, PyeTrial)
            receipt.update(state="completed", scope=phase)
            return
        if args.eligible_history_only:
            from scopt.formats.profile_v1 import read_profile
            image = specs["firmbolt"]
            protocol = dict(policy, scenarios=cases, images={"firmbolt": {
                "config_sha256": file_sha256(image["config"]), "firmware_sha256": file_sha256(image["firmware"]),
                "profile_id": read_profile(image["profile"]).profile_id, "observer": pin(HERE / "observe.py")}})
            phase = "supplemental historical host-cache control"
            prepare_incumbent(out, policy, plan, protocol, eligible_only=True)
            write_json(out / "protocol.json", protocol)
            receipt.update(state="completed", scope=phase)
            return
        registry_dir = out / "registry"
        registry = build(out / "materialization-plan.json", registry_dir)
        ready = {row["id"]: row for row in registry["candidates"] if row["status"] == "ready"}
        base = ready["original/warm"]
        protocol = {k: policy[k] for k in ("application", "configuration", "family", "objective", "seed",
                    "final_seed", "warmup_pairs", "pairs", "alpha", "minimum_gain", "timeout_seconds",
                    "measurement", "secondary_measurement")}
        protocol.update(cache_track=policy.get("cache_track", "fully-warm"), scenarios=cases, candidate_limit=policy["budget"]["candidate_limit"],
                        registry_sha256=file_sha256(registry_dir / "registry.json"),
                        fallback_identity=base["deployment_identity"], images={})
        for name in specs:
            if name + "/warm" not in ready:
                continue
            row = ready[name + "/warm"]
            protocol["images"][name] = {k: row[k] for k in ("config_sha256", "firmware_sha256", "profile_id")}
            protocol["images"][name]["observer"] = pin(HERE / "observe.py")
        phase = "pre-change incumbent"
        try:
            incumbent = prepare_incumbent(out, policy, plan, protocol)
        except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
            receipt["errors"][phase] = str(exc)
            protocol["promotion_enabled"] = False
            incumbent = None
        write_json(out / "protocol.json", protocol)
        phase = "selection"
        trials = PyeTrial(registry, protocol)
        choice = select(registry, protocol, trials, out / "selection", incumbent)
        phase = "final"
        evaluate(protocol, registry, choice, incumbent, trials, out / "final")
        receipt["state"] = "completed"
    except BaseException as exc:
        receipt.update(state="failed", phase=phase, error=str(exc))
        raise
    finally:
        receipt["campaign_wall_seconds"] = time.monotonic() - start
        write_json(out / "campaign.json", receipt)


class PyeTrial(ApplicationTrial):
    def __call__(self, row, case, root):
        record = super().__call__(row, case, root)
        # Discard just the private launch copies after verifying the immutable source
        # and the completed replay. All failed run directories stay intact.
        rd = RunDir.open(root)
        if rd.cache and rd.cache.is_file():
            rd.cache.unlink()
        ram = rd.ram / "psram.ram"
        if ram.is_file():
            ram.unlink()
        return record


def historical_optimizer(out, revision):
    dest = out / "pre-change"
    dest.mkdir()
    archive = dest / "scopt.tar"
    command(dest, "source", ["git", "-C", ROOT, "archive", "--format=tar", "-o", archive, revision, "tools/scopt"])
    with tarfile.open(archive) as source:
        source.extractall(dest, filter="data")
    src = dest / "tools/scopt"
    # Only the transformer process imports the pinned historical implementation.
    # The contemporary runner and strict receipt reader apply to both releases.
    optimizer = [sys.executable, "-I", "-c",
                 "import sys; sys.path.insert(0, sys.argv.pop(1)); from scopt.cli import main; main()", str(src)]
    return dest, src, archive, optimizer


def prepare_incumbent(out, policy, plan, protocol, eligible_only=False):
    revision = policy["incumbent_revision"]
    dest, src, archive, optimizer = historical_optimizer(out, revision)
    fixed = "firmbolt" if "firmbolt" in plan["images"] else "original"
    legacy_plan = dict(plan, recipes=policy["prechange_recipes"], budget=policy["prechange_budget"],
                       images={fixed: plan["images"][fixed]}, source_revision=revision, source_archive=pin(archive))
    write_json(dest / "materialization-plan.json", legacy_plan)
    registry = build(dest / "materialization-plan.json", dest / "registry", optimizer=optimizer,
                     pipeline_dir=src / "pipelines", fixed_image=fixed)
    rows = registry["candidates"]
    # An unknown or incomplete historical result could conceal a stronger incumbent.
    if eligible_only:
        from historical_eligibility import excluded_b4
        excluded = [excluded_b4(dest, r, plan["images"][fixed], revision)
                    for r in rows if r["status"] not in ("ready", "equivalent")]
        write_json(dest / "eligibility.json", {"excluded": excluded, "scope": "historical host-cache control"})
        rows = [r for r in rows if r["status"] in ("ready", "equivalent")]
    require(all(r["status"] in ("ready", "equivalent") for r in rows),
            "pre-change recipes did not all establish a materialized deployment; see pre-change registry")
    warm, = [r for r in rows if r["id"] == fixed + "/warm"]
    trials = PyeTrial(registry, protocol)
    rows = [r for r in rows if r["status"] == "ready" and r != warm]
    rng = random.Random(policy["prechange_seed"])
    order = list(range(len(rows)))
    rng.shuffle(order)
    seeds = [rng.getrandbits(32) for _ in rows]
    confirmation_seed = rng.getrandbits(32)
    write_json(dest / "selection-plan.json", {"revision": revision, "fixed_guest": fixed, "order": order,
               "candidates": [r["id"] for r in rows], "seeds": seeds, "confirmation_seed": confirmation_seed,
               "ranking_scenario": protocol["scenarios"]["pre-change-ranking"],
               "confirmation_scenario": protocol["scenarios"]["pre-change-confirmation"]})
    best, best_score, best_index = warm, 1.0, None
    scores = {}
    with (dest / "observations.jsonl").open("x") as log:
        def compare(candidate, role, seed):
            case, = protocol["scenarios"][role]
            samples = []
            orders = [("warm", "candidate"), *_paired_orders(12, random.Random(seed))]
            for i, arms in enumerate(orders, -1):
                sample = {"first": arms[0], "second": arms[1]}
                for arm in arms:
                    deployment = warm if arm == "warm" else candidate
                    name = candidate["id"].split("/")[1]
                    directory = dest / f"{role}-{name}-{i}-{arm}"
                    rec = {"arm": arm, "block": i, "role": role, "case": case["id"],
                           "order": arms, "deployment": deployment["deployment_identity"]}
                    try:
                        rec["result"] = trials(deployment, case, directory)
                        sample[arm] = rec["result"]["value"]
                    except Exception as exc:
                        rec["error"] = str(exc)
                        if isinstance(exc, InvalidObservation) and arm == "candidate" and candidate != warm:
                            return {"rejected": str(exc)}
                        raise
                    finally:
                        print(json.dumps(rec), file=log, flush=True)
                if i >= 0:
                    samples.append(sample)
            return _paired_evidence(samples, alpha=policy["alpha"])

        for idx in order:
            candidate = rows[idx]
            score = compare(candidate, "pre-change-ranking", seeds[idx])
            scores[candidate["id"]] = score
            gain = score.get("median_paired_speedup", 0)
            if gain > best_score or (best_index is not None and gain == best_score and idx < best_index):
                best, best_score, best_index = candidate, gain, idx
        write_json(dest / "frozen.json", {"winner": best, "control": warm, "ranking": scores})
        confirmation = compare(best, "pre-change-confirmation", confirmation_seed)
    incumbent = best if confirmation.get("passes") else warm
    write_json(dest / "selection.json", {"source_revision": revision, "state": "completed",
               "selected": incumbent, "confirmation": confirmation, "fixed_image": fixed})
    protocol.update(incumbent_registry=pin(dest / "registry/registry.json"),
                    incumbent_id=incumbent["id"], incumbent_identity=incumbent["deployment_identity"],
                    incumbent_image=protocol["images"][fixed], promotion_enabled=True)
    return registry


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", type=Path, required=True, help="directory for pinned upstream sources and build")
    parser.add_argument("--qemu", type=Path, required=True, help="PSRAM-corrected native-board QEMU binary")
    parser.add_argument("--plugin", type=Path, required=True, help="matching release FastDyn plugin")
    parser.add_argument("--out", type=Path, required=True, help="new campaign evidence directory")
    parser.add_argument("--cpu", type=int, required=True, help="idle CPU on the dedicated host")
    parser.add_argument("--firmbolt-elf", type=Path, help="optional prequalified ELF from the pinned FirmBOLT build")
    parser.add_argument("--firmbolt-sha256", help="pinned lowercase SHA-256 of the supplied ELF")
    parser.add_argument("--firmbolt-origin-sha256", help="SHA-256 of the source loadable binary",
                        default=read(HERE / "admission.json")["training_qualification"]["payload_sha256"])
    parser.add_argument("--training-frozen", action="store_true", help="seal a training-only inventory and allow checked late translations on private copies")
    parser.add_argument("--eligible-history-only", action="store_true")
    parser.add_argument("--promotion-only", action="store_true")
    args = parser.parse_args()
    if bool(args.firmbolt_elf) != bool(args.firmbolt_sha256):
        parser.error("--firmbolt-elf and --firmbolt-sha256 must be supplied together")
    campaign(args)

#!/usr/bin/env python3
"""Select a complete firmware/cache deployment against the original ordinary warm reference."""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import subprocess
import sys
from pathlib import Path

from application_peer import uart_peer
from campaign import write_json
from scopt.formats.bundle_v1 import read_bundle, read_runtime_identity
from scopt.formats.profile_v1 import read_profile
from scopt.run.deploy import deploy
from scopt.run.launcher import RunDirError, create_run_dir, file_sha256, launch
from scopt.run.warm_selection import DEFAULT_ALPHA, MIN_PAIRS, _fingerprint, _paired_evidence, _paired_orders
from scopt.run.warm_tuning import _artifact_files


def require(condition, message):
    if not condition:
        raise ValueError(message)


def pinned(spec):
    path = Path(spec["path"]).resolve(strict=True)
    require(file_sha256(path) == spec["sha256"], f"file identity mismatch: {path}")
    return path


def positive(value):
    return type(value) in (float, int) and math.isfinite(value) and value > 0


def prepare(registry, plan, incumbent_registry=None):
    require(registry["schema"] == "trebuchet.registry/1" and registry["state"] == "completed",
            "registry is incomplete")
    objective = registry["objective"]
    require(all(isinstance(objective.get(k), str) and objective[k].strip()
                for k in ("name", "unit", "direction", "useful_work", "boundaries")), "invalid objective")
    require(objective == plan["objective"] and objective["direction"] in ("minimize", "maximize"),
            "objective differs from protocol")
    require(plan["application"] == registry["application"] and plan["configuration"] == registry["configuration"],
            "application or configuration mismatch")
    ids = set()
    candidates = {}
    for row in registry["candidates"]:
        name = row["id"]
        require(name not in ids, f"duplicate candidate ID: {name}")
        ids.add(name)
        if row["status"] != "ready":
            continue
        require(row["application"] == registry["application"], f"wrong application: {name}")
        binding = plan["images"][row["image_variant"]]
        for key in ("firmware_sha256", "profile_id", "config_sha256"):
            require(binding[key] == row[key], f"{key} mismatch for {name}")
        require([g["stage"] for g in row["generations"]] == row["recipe"], f"incomplete stage receipts: {name}")
        require(row["deployment_identity"], f"missing deployment identity: {name}")
        candidates[name] = row
    base = candidates["original/warm"]
    require(base["image_variant"] == "original" and not base["recipe"], "invalid reference")
    require(base["deployment_identity"] == plan["fallback_identity"], "fallback identity mismatch")
    promotion_enabled = plan.get("promotion_enabled", True)
    require(type(promotion_enabled) is bool, "invalid promotion flag")
    if not promotion_enabled:
        require(incumbent_registry is None and not any(plan.get(k) for k in
                ("incumbent_id", "incumbent_identity", "incumbent_image", "incumbent_registry")),
                "diagnostic protocol has an incumbent")
        incumbent = None
    elif incumbent_registry is None:
        incumbent = candidates[plan["incumbent_id"]]
    else:
        require(incumbent_registry["state"] == "completed", "incomplete incumbent registry")
        for field in ("objective", "application", "configuration", "contract_sha256"):
            require(incumbent_registry[field] == registry[field], f"incumbent differs on {field}")
        matches = [row for row in incumbent_registry["candidates"]
                   if row["id"] == plan["incumbent_id"] and row["status"] == "ready"]
        require(len(matches) == 1, "missing or ambiguous incumbent deployment")
        incumbent = matches[0]
        require(all(incumbent[k] == plan["incumbent_image"][k]
                    for k in ("config_sha256", "firmware_sha256", "profile_id")), "incumbent binding mismatch")
    if incumbent is not None:
        require(incumbent["deployment_identity"] == plan["incumbent_identity"], "incumbent identity mismatch")
    seen = {base["deployment_identity"]}
    contenders = []
    for row in candidates.values():
        if row["deployment_identity"] not in seen:
            seen.add(row["deployment_identity"])
            contenders.append(row)
    require(len(ids) <= plan["candidate_limit"] <= registry["budget"]["candidate_limit"], "candidate budget exceeded")
    require(0 < plan["alpha"] <= DEFAULT_ALPHA and positive(plan["minimum_gain"])
            and plan["minimum_gain"] > 1, "invalid confirmation gain rule")
    require(type(plan["warmup_pairs"]) is int and plan["warmup_pairs"] >= 1, "specify warmups")
    for phase in ("ranking", "confirmation", "promotion"):
        count = plan["pairs"][phase]
        require(type(count) is int and count >= MIN_PAIRS and count % 2 == 0, f"invalid {phase} pair count")
    roles = set()
    stimuli = set()
    for phase in ("training", "ranking", "confirmation", "promotion", "final"):
        require(plan["scenarios"][phase], f"missing {phase} scenarios")
        for case in plan["scenarios"][phase]:
            require(case["id"] not in roles, f"scenario reused across protocol: {case['id']}")
            roles.add(case["id"])
            # An alias or renamed input file does not provide an independent scenario.
            stimulus = tuple(sorted((bank, spec["sha256"]) for bank, spec in case["bank_inputs"].items()))
            require(stimulus and stimulus not in stimuli, "empty or reused scenario stimulus")
            stimuli.add(stimulus)
            require(positive(case["weight"]), "invalid scenario weight")
    return base, contenders, incumbent


class InvalidObservation(ValueError):
    """A launch or the application Oracle rejected a measured observation."""


def validate_observations(expected, observed, tolerances):
    require(isinstance(expected, dict) and expected and set(expected) == set(observed),
            "application observation fields differ")
    require(set(tolerances).issubset(expected), "tolerance for unknown observation")
    for field, want in expected.items():
        got = observed[field]
        if field in tolerances:
            eps = tolerances[field]
            require(type(eps) in (int, float) and math.isfinite(eps) and eps >= 0, "invalid tolerance")
            require(type(want) in (int, float) and type(got) in (int, float)
                    and math.isfinite(want) and math.isfinite(got)
                    and abs(want - got) <= eps, f"oracle mismatch: {field}")
        else:
            require(json.dumps(want, sort_keys=True, allow_nan=False) ==
                    json.dumps(got, sort_keys=True, allow_nan=False), f"oracle mismatch: {field}")


class ApplicationTrial:
    """One proof-checked fully warm launch; the pinned application adapter extracts observations."""
    def __init__(self, registry, plan):
        self.registry = registry
        self.plan = plan
        self.timeout_s = plan["timeout_seconds"]
        require(positive(self.timeout_s), "invalid timeout")
        self.verified = {}
        admission = json.loads(pinned(registry["admission"]).read_text())
        require(admission["status"] == "admitted" and admission["application"] == registry["application"],
                "application has no operational admission")
        require(registry["application"] not in {"compute", "fwbench", "kernbench", "libbench", "algobench"},
                "retired synthetic corpus")
        expected_direction = {"operational_latency_ns": ("minimize", "ns"),
                              "throughput_per_second": ("maximize", "work_units/s"),
                              "process_cpu_ns": ("minimize", "ns")}
        direction, unit = expected_direction[plan["measurement"]]
        require(plan["objective"]["direction"] == direction and plan["objective"]["unit"] == unit,
                "metric units or direction differ from objective")
        for cases in plan["scenarios"].values():
            for case in cases:
                for spec in case["bank_inputs"].values():
                    pinned(spec)
                pinned(case["expected"])
                if peer := case.get("peer"):
                    pinned(peer["program"])
                    stage = json.loads(pinned(peer["stage"]).read_text())
                    require(stage["case"] == case["id"], "peer stage belongs to a different scenario")
                    require(case["bank_inputs"][peer["bank"]]["sha256"] == peer["input_sha256"],
                            "peer expects a different bank image")
        for binding in [*plan["images"].values(), *([plan["incumbent_image"]] if "incumbent_registry" in plan else [])]:
            pinned(binding["observer"])

        require(file_sha256(registry["contract"]) == registry["contract_sha256"], "scenario contract changed")

    def verify(self, row):
        from candidates import deployment_identity
        inventory = _artifact_files([Path(row["artifact"])]) | set(Path(row["artifact"]).glob("*.scm")) | {
            Path(row[key]) for key in ("config", "firmware", "runtime", "profile")
        } | {Path(self.registry["contract"]), Path(self.registry["admission"]["path"])}
        fingerprints = {p: _fingerprint(p) for p in inventory}
        key = row["deployment_identity"], row["profile_id"]
        if key in self.verified:
            require(fingerprints == self.verified[key], f"deployment sources changed: {row['id']}")
            return
        profile = read_profile(row["profile"])
        require(profile.profile_id == row["profile_id"] and profile.snapshot_id == row["ordinary_snapshot_id"],
                "profile disagrees with its ordinary snapshot")
        for key, digest in (("firmware", "firmware_sha256"), ("config", "config_sha256")):
            require(file_sha256(row[key]) == row[digest], f"changed {key}: {row['id']}")
        require(row["contract_sha256"] == self.registry["contract_sha256"], "candidate contract differs")
        current = deployment_identity(row["artifact"], row["config"], row["firmware"], row["runtime"],
                                      row["contract_sha256"])
        require(current == row["deployment_identity"], f"changed deployment: {row['id']}")
        bundle = read_bundle(Path(row["artifact"]) / "bundle.json")
        require(bundle["image_hash"] == row["firmware_sha256"] and bundle["bundle_id"] == profile.bundle_id,
                "cache bound to wrong guest or profile")
        runtime = read_runtime_identity(Path(row["artifact"]) / "runtime_identity.json")
        require(runtime["runtime_identity_id"] == row["runtime_identity"], "artifact runtime changed")
        self.verified[(row["deployment_identity"], row["profile_id"])] = fingerprints

    def __call__(self, row, case, root):
        self.verify(row)
        banks = {bank: pinned(spec) for bank, spec in case["bank_inputs"].items()}
        reference = pinned(case["expected"])
        binding = self.plan["images"].get(row["image_variant"])
        if "incumbent_registry" in self.plan and row["deployment_identity"] == self.plan["incumbent_identity"]:
            binding = self.plan["incumbent_image"]
        adapter = pinned(binding["observer"])
        runtime = Path(row["runtime"])
        rd = create_run_dir(Path(row["config"]), root, qemu=runtime, label="joint-fully-warm")
        identity = deploy(row["artifact"], rd, timeout_s=self.timeout_s)["identity"]
        require(identity["checked"] is True and not identity["reasons"] and identity["bundle_id"]
                and identity["runtime_identity_id"], "deployment identity not verified")
        peer = uart_peer(case["peer"]) if case.get("peer") else None
        try:
            doc = launch(rd, prove=True, timeout_s=self.timeout_s, inputs=banks, peer=peer)
        except RunDirError as error:
            if error.code != "application_peer_failed":
                raise
            self.verify(row)
            pinned(case["peer"]["stage"])
            pinned(case["peer"]["program"])
            raise InvalidObservation(f"application peer rejected launch in {root}: {error.detail}") from error
        if doc["exit_code"] != 0 or doc["timed_out"] or doc["verdict"] != "PASS" or doc["appended"] != 0:
            raise InvalidObservation(f"invalid warm launch: {root}")
        require(doc["qemu_sha256"] == file_sha256(runtime), "runtime changed")
        require({k: v["sha256"] for k, v in doc["inputs"].items()} ==
                {k: v["sha256"] for k, v in case["bank_inputs"].items()}, "scenario replay changed")
        args = [sys.executable, str(adapter), str(root / "run.json"), row["firmware"]]
        output = subprocess.run(args, capture_output=True, text=True, timeout=self.timeout_s)
        if output.returncode:
            pinned(binding["observer"])
            raise InvalidObservation(f"application observer rejected {root}: {output.stderr.strip()}")
        observation = json.loads(output.stdout)
        expected = json.loads(reference.read_text())
        try:
            validate_observations(expected["observations"], observation["observations"], expected.get("tolerances", {}))
            work = observation["completed_work"]
            require(positive(work) and work == expected["completed_work"], "unequal useful work")
        except ValueError as error:
            raise InvalidObservation(str(error)) from error
        timings = doc["timings"]
        metric = self.plan["measurement"]
        if metric == "operational_latency_ns":
            value = observation["operational_elapsed_ns"]
        elif metric == "throughput_per_second":
            elapsed = observation["operational_elapsed_ns"]
            require(positive(elapsed), "invalid operational duration")
            value = work * 1e9 / elapsed
        elif metric == "process_cpu_ns":
            value = (timings["children_user_s"] + timings["children_sys_s"]) * 1e9
        else:
            raise ValueError(f"unsupported operational metric: {metric}")
        secondary = self.plan.get("secondary_measurement", "guest_window_ns")
        phase_costs = {"ram_reset_ns": timings["ram_reset_ns"], "bank_stage_ns": timings["bank_stage_ns"],
                       "process_tree_cpu_ns": round((timings["children_user_s"] + timings["children_sys_s"]) * 1e9)}
        if secondary == "qemu_lifetime_ns":
            trace = rd.work / "fastdyn_timing.jsonl"
            spans = [json.loads(line) for line in trace.read_text().splitlines()]
            starts = {s["phase"]: s["time_monotonic_s"] for s in spans if s["event"] == "phase_start"}
            phase_costs["fastdyn_pre_qemu_ns"] = round((starts["machine0.qemu_execution"] -
                                                        starts["fastdyn.run.total"]) * 1e9)
            windows = [round(s["duration_s"] * 1e9) for s in spans
                       if s["phase"] == "machine0.qemu_execution" and s["event"] == "phase_end" and s["status"] == "ok"]
        elif secondary == "guest_window_ns":
            windows = [window["elapsed_ns"] for window in doc["bench"]]
        else:
            raise ValueError(f"unsupported secondary measurement: {secondary}")
        require(len(windows) == 1 and positive(windows[0]) and positive(value), "invalid measured interval")
        self.verify(row)
        pinned(case["expected"])
        pinned(binding["observer"])
        return {"value": value, secondary: windows[0], "wall_s": timings["wall_s"], "work": work, "phase_costs": phase_costs,
                "observations": observation["observations"], "bundle_id": identity["bundle_id"],
                "runtime_identity_id": identity["runtime_identity_id"], "proof": str(root / "run.json")}


def paired_summary(rows, maximize, alpha):
    # Reuse scopt's exact balanced-order randomization test. Ranks preserve ties
    # and winning order for fractional throughput and larger-is-better metrics.
    ordinal = []
    gains = []
    for row in rows:
        warm, value = row["warm"], row["candidate"]
        better = value > warm if maximize else value < warm
        ordinal.append(dict(row, warm=2, candidate=2 if value == warm else (1 if better else 3)))
        gains.append(value / warm if maximize else warm / value)
    evidence = _paired_evidence(ordinal, alpha=alpha)
    return {"median_gain": statistics.median(gains), "wins": evidence["wins"], "ties": evidence["ties"],
            "p_value": evidence["p_value"], "candidate_first": evidence["candidate_first"],
            "candidate_median": statistics.median(r["candidate"] for r in rows),
            "reference_median": statistics.median(r["warm"] for r in rows)}


def select(registry, plan, trial, output, incumbent_registry=None):
    """Rank absolutely, confirm one fixed deployment, then make a separate incumbent comparison."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "plan.json", plan)
    write_json(output / "registry.json", registry)
    receipt = {"schema": "trebuchet.joint-selection/1", "state": "running", "objective": plan["objective"],
               "ranking": {}, "selected": None, "promoted": None}
    receipt_file = output / "selection.json"
    write_json(receipt_file, receipt)
    sequence = 0
    rng = random.Random(plan["seed"])
    maximize = plan["objective"]["direction"] == "maximize"

    def compare(control, target, phase, log):
        nonlocal sequence
        summaries = {}
        for case in plan["scenarios"][phase]:
            rows = []
            warmups = plan["warmup_pairs"]
            orders = [_paired_orders(2, rng)[0] for _ in range(warmups)]
            orders += _paired_orders(plan["pairs"][phase], rng)
            for index, order in enumerate(orders):
                row = {"first": order[0], "second": order[1]}
                for arm in order:
                    item = control if arm == "warm" else target
                    record = {"phase": phase, "case": case["id"], "warmup": index < warmups,
                              "pair": index - warmups, "first": order[0], "arm": arm, "candidate": item["id"],
                              "deployment_identity": item["deployment_identity"], "objective": plan["objective"],
                              "comparator_identity": control["deployment_identity"]}
                    sequence += 1
                    try:
                        result = trial(item, case, output / f"trial-{sequence}")
                        require(positive(result["value"]), "invalid objective measurement")
                    except Exception as error:
                        record["error"] = str(error)
                        print(json.dumps(record, allow_nan=False), file=log, flush=True)
                        if isinstance(error, InvalidObservation) and arm == "candidate":
                            return {"rejected": str(error)}
                        raise
                    record["result"] = result
                    print(json.dumps(record, allow_nan=False), file=log, flush=True)
                    row[arm] = result["value"]
                if index >= warmups:
                    rows.append(row)
            summaries[case["id"]] = paired_summary(rows, maximize, plan["alpha"])
        return summaries

    def passes(summaries):
        return "rejected" not in summaries and all(s["median_gain"] >= plan["minimum_gain"] and s["p_value"] <= plan["alpha"]
                   for s in summaries.values())

    try:
        base, contenders, incumbent = prepare(registry, plan, incumbent_registry)
        receipt["fallback"] = base
        rng.shuffle(contenders)
        with (output / "observations.jsonl").open("x") as log:
            winner = base
            best = 0.0
            for item in contenders:
                summaries = compare(base, item, "ranking", log)
                if "rejected" in summaries:
                    receipt["ranking"][item["id"]] = summaries
                    continue
                # Weighted geometric mean of absolute objective values over the same scenarios.
                cases = plan["scenarios"]["ranking"]
                weights = sum(c["weight"] for c in cases)
                def score(field):
                    return math.exp(sum(c["weight"] * math.log(summaries[c["id"]][field]) for c in cases) / weights)
                absolute, reference = score("candidate_median"), score("reference_median")
                receipt["ranking"][item["id"]] = {"absolute_score": absolute, "reference_score": reference,
                                                  "scenarios": summaries}
                if (absolute > reference if maximize else absolute < reference) and (
                    winner is base or (absolute > best if maximize else absolute < best)
                ):
                    winner, best = item, absolute
            receipt["ranking_winner"] = winner
            write_json(output / "frozen.json", {"deployment": winner, "fallback": base, "objective": plan["objective"],
                                               "minimum_gain": plan["minimum_gain"], "alpha": plan["alpha"]})
            selected = base
            if winner is base:
                receipt["confirmation"] = compare(base, base, "confirmation", log)
                require("rejected" not in receipt["confirmation"], "fallback control failed confirmation")
                receipt["confirmation_reason"] = "no_ranking_winner_identical_control"
            else:
                receipt["confirmation"] = compare(base, winner, "confirmation", log)
                if passes(receipt["confirmation"]):
                    selected = winner
            receipt["selected"] = selected
            receipt["incumbent"] = incumbent
            if incumbent is None:
                receipt["promotion_reason"] = "pre_change_incumbent_unavailable"
            elif selected["deployment_identity"] == incumbent["deployment_identity"]:
                receipt["promotion_reason"] = "identical_to_incumbent"
                receipt["promoted"] = False
            else:
                receipt["promotion"] = compare(incumbent, selected, "promotion", log)
                receipt["promoted"] = passes(receipt["promotion"])
        receipt["state"] = "completed"
    except BaseException as error:
        receipt.update(state="failed", error=str(error))
        raise
    finally:
        write_json(receipt_file, receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("registry", type=Path)
    parser.add_argument("plan", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    registry, plan = (json.loads(path.read_text()) for path in (args.registry, args.plan))
    require(file_sha256(args.registry) == plan["registry_sha256"], "registry differs from protocol")
    incumbent_registry = json.loads(pinned(plan["incumbent_registry"]).read_text()) if "incumbent_registry" in plan else None
    select(registry, plan, ApplicationTrial(registry, plan), args.output, incumbent_registry)


if __name__ == "__main__":
    main()

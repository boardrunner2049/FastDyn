"""Recheck candidate promotion against the recorded paired campaign."""

from __future__ import annotations

import json
import os
from pathlib import Path
import random

from ..formats.sc_v5 import data_extents, sha256_sparse
from .deploy import resolve_artifact
from .launcher import file_sha256, norm_b0, norm_guest, parse_bench
from .warm_selection import ARMS, DEFAULT_ALPHA, MIN_PAIRS, _paired_evidence, _paired_orders


def _require(ok, detail):
    if not ok:
        raise ValueError(detail)


def _read(path):
    return json.loads(path.read_text())


def _same_cache(left, right):
    """Compare full logical bytes, avoiding simultaneous sparse holes."""
    with open(left, "rb") as lhs, open(right, "rb") as rhs:
        src, dst = lhs.fileno(), rhs.fileno()
        size = os.fstat(src).st_size
        if os.fstat(dst).st_size != size:
            return False
        extents = sorted([*data_extents(src, size), *data_extents(dst, size)])
        end = 0
        for lo, hi in extents:
            for off in range(max(lo, end), hi, 1 << 20):
                count = min(hi - off, 1 << 20)
                a = os.pread(src, count, off)
                b = os.pread(dst, count, off)
                if len(a) != count or a != b:
                    return False
            end = max(end, hi)
        return True


def _arm_receipt(arm, receipt):
    artifact = resolve_artifact(receipt["path"])
    _require(artifact["sc"] == receipt["cache"], f"{arm} cache path changed")
    _require(receipt["cache_sha256"] == "sha256:" + sha256_sparse(artifact["sc"]),
             f"{arm} cache bytes changed")
    sidecar = Path(artifact["sc"] + ".scopt.json")
    for key, source in (
        ("bundle", artifact["bundle"]), ("runtime_identity", artifact["runtime_identity"]),
        ("sidecar", str(sidecar) if sidecar.is_file() else None),
        ("restore_hints", artifact.get("restore_hints")),
    ):
        recorded = receipt.get(key)
        if source is None:
            _require(key not in ("bundle", "runtime_identity") and recorded is None,
                     f"{arm} lacks {key}")
        else:
            _require(recorded is not None and source == recorded["path"]
                     and file_sha256(source) == recorded["sha256"], f"{arm} {key} changed")
    return artifact


def _case_inputs(case, excluded):
    config = Path(case["config"])
    expected = Path(case["expected_guest"])
    _require(file_sha256(config) == case["config_sha256"]
             and file_sha256(expected) == case["expected_guest_sha256"],
             "timing configuration or guest oracle changed")
    guest = expected.read_text(encoding="utf-8").splitlines()
    _require(bool(guest), "empty guest output oracle")
    banks = {}
    for bank, item in case["bank_inputs"].items():
        digest = file_sha256(item["path"])
        _require(digest == item["sha256"] and digest not in excluded,
                 f"timing input {bank} changed or overlaps earlier input")
        banks[bank] = digest
    _require(case["input_kind"] == ("bank-backed" if banks else "fixed-work")
             and (not banks or bool(excluded)), "invalid timing input declaration")
    return banks, guest


def _trial_evidence(path, artifact, case, expected_inputs, expected_guest, runtime, bundle_id):
    deploy = _read(path / "deploy.json")
    run = _read(path / "run.json")
    rundir = _read(path / "rundir.json")
    _require(deploy["run_dir"] == run["run_dir"] and Path(run["run_dir"]).name == path.name
             and all(deploy["artifact"].get(key) == value for key, value in artifact.items())
             and not deploy.get("refused"),
             f"{path.name}: deployment does not match the measured arm")
    ids = deploy["identity"]
    _require(ids["checked"] is True and not ids["reasons"] and ids["bundle_id"]
             and ids["expected_bundle_id"] == ids["bundle_id"] == bundle_id
             and ids["runtime_identity_id"],
             f"{path.name}: deployment identity was not proved")
    _require(rundir["base_toml_sha256"] == case["config_sha256"]
             and file_sha256(path / "run.toml") == rundir["run_toml_sha256"]
             and rundir.get("ram_storage", "as-configured") == runtime["ram_storage"]
             and run["qemu_sha256"] == runtime["qemu_sha256"],
             f"{path.name}: timing configuration or runtime changed")
    cache_name = rundir["cache_name"]
    _require(cache_name and Path(cache_name).name == cache_name
             and Path(deploy["state"]["sc_path"]).name == cache_name,
             f"{path.name}: invalid deployed cache path")
    actual_cache = path / "cache" / cache_name
    _require(_same_cache(artifact["sc"], actual_cache), f"{path.name}: measured cache differs")
    hint = artifact.get("restore_hints")
    actual_hint = actual_cache.with_name(actual_cache.name + ".restore-hints")
    _require(bool(hint) == actual_hint.is_file()
             and (not hint or deploy["restore_hint_sha256"] == file_sha256(actual_hint)
                  == file_sha256(hint)), f"{path.name}: measured hint differs")
    proof = run["proof"]
    checks = {row["check"]: row["ok"] for row in proof["checks"]}
    required = {"sc_exists", "header", "records", "inode", "manifest"}
    if deploy["state"]["file_version"] == 6:
        required.update(("v6_header", "ext"))
    _require(run["exit_code"] == 0 and run["timed_out"] is False
             and run["verdict"] == proof["verdict"] == "PASS"
             and type(run["appended"]) is int and run["appended"] == proof["appended"] == 0
             and not proof["failed"] and all(checks.get(key) is True for key in required)
             and proof["n"] == proof["record_count_after"] == deploy["state"]["n"],
             f"{path.name}: warm cache use or zero appends not proved")
    _require({bank: record["sha256"] for bank, record in run["inputs"].items()} == expected_inputs,
             f"{path.name}: measured inputs disagree")
    stdout = (path / "stdout.log").read_text(encoding="utf-8", errors="replace")
    _require(norm_guest(stdout) == expected_guest == run["guest_norm"]
             == (path / "guest.norm").read_text(encoding="utf-8").splitlines()
             and norm_b0(stdout) == (path / "output.norm").read_text(encoding="utf-8").splitlines()
             and file_sha256(path / "output.norm") == run["output_norm_sha256"],
             f"{path.name}: guest output disagrees")
    timers = parse_bench(stdout)
    _require(timers == run["bench"] and len(timers) == 1
             and type(timers[0]["elapsed_ns"]) is int and timers[0]["elapsed_ns"] > 0,
             f"{path.name}: missing or inconsistent benchmark timer")
    return timers[0], (ids["bundle_id"], ids["runtime_identity_id"])


def _restore_probe(path, index, case, document, runtime, artifacts, expected_inputs, expected_guest,
                   bundle_id):
    skips = {}
    for arm in ARMS:
        trial = path / f"probe-{index}-{arm}"
        _trial_evidence(trial, artifacts[arm], case, expected_inputs, expected_guest, runtime, bundle_id)
        stats = _read(trial / "restore-stats.json")
        attempted, nominated, skipped = (stats["benchmark"][k] for k in
                                         ("attempted", "nominated", "skipped"))
        probe = {"attempted": attempted, "nominated": nominated, "skipped": skipped,
                 "hint_file_valid": stats["hint_file_valid"]}
        _require(stats["schema"] == "fastdyn.supercache_restore_stats/1"
                 and stats["hint_file_valid"] is bool(artifacts[arm].get("restore_hints"))
                 and all(type(n) is int for n in (attempted, nominated, skipped))
                 and 0 <= skipped <= nominated <= attempted and probe == document[arm],
                 "restore hint probe does not match the recorded outcome")
        skips[arm] = skipped
    _require(skips["warm"] != skips["candidate"], "restore hints had no measured effect")


def verify_candidate_receipt(path, doc, config):
    """Recompute a candidate decision from the plan and complete trial evidence.

    Warm selections continue to use the original digest-only fallback. This
    consumes local experiment records and does not claim remote attestation.
    """
    _require(doc["status"] == "ok" and doc["reason"] == "speedup_established",
             "candidate lacks a measured speedup")
    cases = doc["plan"]["timing_inputs"]
    _require(bool(cases) and set(cases) == set(doc["inputs"]),
             "insufficient performance evidence")
    config_digest = file_sha256(config)
    _require(any(case["config_sha256"] == config_digest for case in cases.values()),
             "configuration was not measured")

    root = path.parent
    plan = _read(root / "plan.json")
    pairs, seed, alpha = plan["pair_count_per_input"], plan["seed"], plan["alpha"]
    _require(plan == doc["plan"] and doc["alpha"] == alpha
             and type(alpha) in (float, int) and 0 < alpha <= DEFAULT_ALPHA
             and type(pairs) is int and pairs >= MIN_PAIRS and pairs % 2 == 0
             and type(seed) is int and plan["warmups_per_arm"] == 1
             and plan["test"] == "one-sided-exact-balanced-paired-wins",
             "invalid paired timing plan")
    _require(set(cases) == set(plan["orders"]), "timing plan cases and orders disagree")
    runtime = doc["runtime"]
    _require(set(runtime) == {"qemu_sha256", "ram_storage"} and runtime["qemu_sha256"],
             "missing measured runtime")
    artifacts = {arm: _arm_receipt(arm, doc["artifacts"][arm]) for arm in ARMS}
    bundle_ids = {_read(Path(a["bundle"]))["bundle_id"] for a in artifacts.values()}
    _require(len(bundle_ids) == 1, "artifact bundles differ")
    bundle_id = bundle_ids.pop()
    builds = [_read(Path(artifact["runtime_identity"])) for artifact in artifacts.values()]
    build_ids = {(build["qemu_sha256"], build["plugin_sha256"]) for build in builds}
    _require(len(build_ids) == 1 and next(iter(build_ids))[0] == runtime["qemu_sha256"],
             "compared artifacts have different runtime builds")
    cache_hashes = {doc["artifacts"][arm]["cache_sha256"] for arm in ARMS}
    hints = {doc["artifacts"][arm].get("restore_hints", {}).get("sha256") for arm in ARMS}
    same = len(cache_hashes) == 1
    _require(doc["byte_identical"] is same and (not same or len(hints) == 2),
             "candidate deployment inputs did not differ")
    excluded = set()
    for source, digest in plan["excluded_inputs"].items():
        _require(file_sha256(source) == digest, "earlier input changed")
        excluded.add(digest)
    row_count = len(cases) * (pairs + 1) * len(ARMS)
    lines = (root / "samples.jsonl").read_text().splitlines()
    _require(len(lines) == row_count, "missing or extra paired trial rows")
    rows = iter(map(json.loads, lines))
    width = len(str(row_count - 1))
    rng = random.Random(seed)
    used_cases, used_inputs = set(), set()
    i = 0
    for index in range(len(cases)):
        first = next(rows)
        name = first["input"]
        _require(name in cases and name not in used_cases, "missing or repeated timing case")
        used_cases.add(name)
        case = cases[name]
        banks, guest = _case_inputs(case, excluded)
        signature = (case["config_sha256"], tuple(sorted(banks.items())))
        _require(signature not in used_inputs, "repeated timing inputs")
        used_inputs.add(signature)
        if same:
            _restore_probe(root, index, case, doc["restore_probes"][name], runtime, artifacts,
                           banks, guest, bundle_id)
        orders = _paired_orders(pairs, rng)
        _require([list(order) for order in orders] == plan["orders"][name], "timing orders changed")
        samples = []
        ids = {}
        tags = set()
        for block, order in enumerate([ARMS, *orders], start=-1):
            sample = {"first": order[0], "second": order[1]}
            for arm in order:
                row = first if block == -1 and arm == ARMS[0] else next(rows)
                trial = f"trial-{i:0{width}d}"
                i += 1
                _require((row["input"], row["block"], row["arm"], row["trial"])
                         == (name, block, arm, trial), "paired trial does not match the plan")
                timer, identity = _trial_evidence(root / trial, artifacts[arm], case, banks, guest, runtime, bundle_id)
                _require(all(row[key] == value for key, value in runtime.items())
                         and (row["bundle_id"], row["runtime_identity_id"]) == identity
                         and row["elapsed_ns"] == timer["elapsed_ns"]
                         and ids.setdefault(arm, identity) == identity, "sample disagrees with launch proof")
                tags.add(timer["tag"])
                sample[arm] = timer["elapsed_ns"]
            if block >= 0:
                samples.append(sample)
        _require(len(tags) == 1 and ids["warm"][0] == ids["candidate"][0],
                 "compared arms have different workloads")
        evidence = _paired_evidence(samples, alpha=alpha)
        _require(evidence["passes"] and doc["inputs"][name] == {
            **evidence, "bundle_id": ids["warm"][0],
            "runtime_identity_ids": {arm: pair[1] for arm, pair in ids.items()},
        }, "paired trials do not establish the recorded speedup")

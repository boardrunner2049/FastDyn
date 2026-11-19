"""Select a changed SuperCache only with paired evidence on new inputs."""

from __future__ import annotations

import fcntl
import json
import math
import os
import random
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from ..formats.sc_v5 import sha256_sparse
from .deploy import DeployError, deploy, resolve_artifact
from .launcher import RunDirError, create_run_dir, file_sha256, launch, write_json

ARMS = ("warm", "candidate")
MIN_PAIRS = 12
DEFAULT_ALPHA = 0.05


@dataclass(frozen=True)
class TimingInput:
    name: str
    config: Path
    bank_inputs: Mapping[str, Path]
    expected_guest: Path


@dataclass(frozen=True)
class _Case:
    config: Path
    bank_inputs: dict[str, Path]
    bank_digests: dict[str, str]
    guest_lines: list[str]


def _fingerprint(path: Path) -> tuple[int, ...]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _sources_unchanged(frozen: Mapping[Path, tuple[int, ...]]) -> bool:
    try:
        return all(_fingerprint(path) == info for path, info in frozen.items())
    except OSError:
        return False


def _preflight(
    inputs: Sequence[TimingInput], training_inputs: Sequence[Path], frozen: dict[Path, tuple[int, ...]],
) -> tuple[dict[str, _Case], dict[str, Any]]:
    def bind(path: Path) -> Path:
        path = Path(path).resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"not a file: {path}")
        frozen.setdefault(path, _fingerprint(path))
        return path

    excluded = {path: file_sha256(path) for path in map(bind, training_inputs)}
    excluded_digests = set(excluded.values())
    if not inputs:
        raise ValueError("supply timing cases")
    used: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    cases, definitions = {}, {}
    for item in inputs:
        if not item.name or item.name in cases:
            raise ValueError("each timing case needs a unique name")
        if item.bank_inputs and not excluded:
            raise ValueError("supply the earlier inputs to exclude for bank-backed timing cases")
        config, expected = bind(item.config), bind(item.expected_guest)
        config_digest = file_sha256(config)
        banks = {bank: bind(path) for bank, path in sorted(item.bank_inputs.items())}
        digests = {bank: file_sha256(path) for bank, path in banks.items()}
        signature = (config_digest, tuple(digests.items()))
        if any(digest in excluded_digests for digest in digests.values()) or signature in used:
            raise ValueError(f"timing input is not independent: {item.name}")
        used.add(signature)
        guest = expected.read_text(encoding="utf-8").splitlines()
        if not guest:
            raise ValueError(f"missing expected guest output: {item.name}")
        cases[item.name] = _Case(config, banks, digests, guest)
        definitions[item.name] = {
            "input_kind": "bank-backed" if banks else "fixed-work",
            "config": str(config), "config_sha256": config_digest,
            "expected_guest": str(expected), "expected_guest_sha256": file_sha256(expected),
            "bank_inputs": {bank: {"path": str(path), "sha256": digests[bank]}
                            for bank, path in banks.items()},
        }
    return cases, {"timing_inputs": definitions,
                   "excluded_inputs": {str(p): digest for p, digest in excluded.items()}}


def _paired_orders(pairs: int, rng: random.Random) -> list[tuple[str, str]]:
    orders = [ARMS, ARMS[::-1]] * (pairs // 2)
    rng.shuffle(orders)
    return orders


def _paired_evidence(samples: list[dict[str, int | str]], *,
                     alpha: float = DEFAULT_ALPHA) -> dict[str, Any]:
    """Exact one-sided balanced-order randomization test on pairwise wins.

    Under the sharp null, the order assignment does not affect either time.
    Condition on the total number of candidate-first pairs. Enumerate how
    many candidate-first assignments land on first-faster, first-slower and
    tied pairs; binomial coefficients count assignments in each category.
    """
    pairs = len(samples)
    wins = sum(int(row["candidate"]) < int(row["warm"]) for row in samples)
    ties = sum(row["candidate"] == row["warm"] for row in samples)
    first_count = sum(row["first"] == "candidate" for row in samples)
    first_faster = sum(int(row[str(row["first"])]) < int(row[str(row["second"])]) for row in samples)
    first_slower = pairs - first_faster - ties
    total = math.comb(pairs, first_count)
    tail = 0
    for faster in range(first_faster + 1):
        for slower in range(first_slower + 1):
            tied = first_count - faster - slower
            if 0 <= tied <= ties and faster + first_slower - slower >= wins:
                tail += math.comb(first_faster, faster) * math.comb(first_slower, slower) * math.comb(ties, tied)
    speedups = [int(row["warm"]) / int(row["candidate"]) for row in samples]
    median_speedup = statistics.median(speedups)
    return {"pairs": pairs, "wins": wins, "ties": ties, "losses": pairs - wins - ties,
            "candidate_first": first_count, "median_paired_speedup": median_speedup,
            "p_value": tail / total, "p_value_numerator": tail, "p_value_denominator": total,
            "passes": (wins > pairs / 2 and median_speedup > 1
                       and Fraction(tail, total) <= Fraction(str(alpha)))}


def _trial(
    artifact: str, case: _Case, root: Path, *, qemu: Path | None, timeout_s: float,
    restore_probe: bool = False,
) -> dict[str, Any]:
    rd = create_run_dir(case.config, root, qemu=qemu, label="trial")
    deployment = deploy(artifact, rd, timeout_s=timeout_s)
    identity = deployment["identity"]
    if (identity["checked"] is not True or not identity.get("bundle_id")
            or not identity.get("runtime_identity_id") or identity.get("reasons")):
        raise RunDirError("identity_unchecked", str(root))
    stats_path = root / "restore-stats.json"
    extra = {"extra_env": {"QEMU_SC_RESTORE_STATS": str(stats_path)}} if restore_probe else {}
    doc = launch(rd, timeout_s=timeout_s, prove=True, inputs=case.bank_inputs, **extra)
    if doc["exit_code"] != 0 or doc["timed_out"]:
        raise RunDirError("launch_failed", str(root))
    if doc["verdict"] != "PASS" or doc["appended"] != 0:
        raise RunDirError("cache_proof_failed", str(root))
    if doc["guest_norm"] != case.guest_lines:
        raise RunDirError("guest_output_mismatch", str(root))
    if {bank: rec["sha256"] for bank, rec in doc["inputs"].items()} != case.bank_digests:
        raise RunDirError("input_digest_mismatch", str(root))
    timers = doc["bench"]
    if len(timers) != 1 or type(timers[0]["elapsed_ns"]) is not int or timers[0]["elapsed_ns"] <= 0:
        raise RunDirError("invalid_benchmark_timer", str(root))
    if not doc.get("qemu_sha256"):
        raise RunDirError("runtime_digest_missing", str(root))
    result = {"elapsed_ns": timers[0]["elapsed_ns"], "qemu_sha256": doc["qemu_sha256"],
            "ram_storage": rd.meta.get("ram_storage", "as-configured"),
            "bundle_id": identity["bundle_id"], "runtime_identity_id": identity["runtime_identity_id"]}
    if restore_probe:
        result["restore"] = None
        if stats_path.is_file():
            try:
                stats = json.loads(stats_path.read_text())
                measured = stats["benchmark"]
                attempted, nominated, skipped = (measured[k] for k in ("attempted", "nominated", "skipped"))
                if (stats["schema"] == "fastdyn.supercache_restore_stats/1"
                        and type(stats["hint_file_valid"]) is bool
                        and all(type(n) is int for n in (attempted, nominated, skipped))
                        and 0 <= skipped <= nominated <= attempted
                        and (stats["hint_file_valid"] or nominated == 0)):
                    result["restore"] = {"hint_file_valid": stats["hint_file_valid"],
                                         "attempted": attempted, "nominated": nominated, "skipped": skipped}
            except (KeyError, TypeError, ValueError):
                pass
    return result


def select_warm_cache(
    warm: Path, candidate: Path, output: Path, *, inputs: Sequence[TimingInput],
    training_inputs: Sequence[Path] = (), qemu: Path | None = None, pairs: int = MIN_PAIRS,
    seed: int = 193483, timeout_s: float = 90, alpha: float = DEFAULT_ALPHA,
    training_only: bool = False, expected_artifacts: Mapping[str, Any] | None = None,
    expected_cases: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a predeclared paired campaign; retain warm on weak or invalid candidate evidence.

    Each case uses at least 12 balanced pairs after two discarded warmups.
    All case-level tests must support a speedup at the specified alpha. Earlier input
    files are compared by content against timing inputs. A failure of the warm
    control or a changed source invalidates the campaign. Identical logical
    deployment inputs return the original warm path without deploying either arm.
    Hint-only changes must exercise a different restore path in the measured interval.
    """
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {"schema": "scopt.warm_selection/1", "status": "pending",
                              "selected": None, "selected_artifact": None, "inputs": {}}
    if training_only:
        report["schema"] = "scopt.warm_training/1"
    artifacts: dict[str, dict] = {}
    frozen: dict[Path, tuple[int, ...]] = {}

    def finish(status: str, selected: str | None, reason: str, **info: Any) -> dict[str, Any]:
        report.update(status=status, selected=selected, reason=reason, **info)
        report["selected_artifact"] = artifacts[selected]["path"] if selected else None
        write_json(output / "selection.json", report)
        return report

    try:
        if type(alpha) not in (float, int) or not 0 < alpha <= DEFAULT_ALPHA:
            raise ValueError(f"alpha must be greater than zero and at most {DEFAULT_ALPHA}")
        report["alpha"] = alpha
        artifacts = {arm: resolve_artifact(path) for arm, path in zip(ARMS, (warm, candidate))}
        caches = {arm: Path(art["sc"]) for arm, art in artifacts.items()}
        for path in caches.values():
            frozen[path] = _fingerprint(path)
        report["artifacts"] = {
            arm: {"path": artifact["path"], "cache": str(caches[arm]),
                  "cache_sha256": "sha256:" + sha256_sparse(caches[arm])}
            for arm, artifact in artifacts.items()
        }
        same = (report["artifacts"]["warm"]["cache_sha256"] ==
                report["artifacts"]["candidate"]["cache_sha256"])
        report["byte_identical"] = same
        hints = {}
        for arm, artifact in artifacts.items():
            hints[arm] = None
            if artifact.get("restore_hints"):
                hint = Path(artifact["restore_hints"])
                frozen[hint] = _fingerprint(hint)
                hints[arm] = file_sha256(hint)
                report["artifacts"][arm]["restore_hints"] = {"path": str(hint), "sha256": hints[arm]}
        report["deployment_inputs_identical"] = same and hints["warm"] == hints["candidate"]
        if not _sources_unchanged(frozen):
            return finish("error", None, "source_changed")
        if report["deployment_inputs_identical"]:
            if expected_artifacts is not None:
                return finish("error", None, "frozen_candidate_changed")
            return finish("ok", "warm", "identical_cache_bytes")
        if type(pairs) is not int or pairs < MIN_PAIRS or pairs % 2:
            raise ValueError(f"pairs must be even and at least {MIN_PAIRS}")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout must be positive and finite")
        for arm, art in artifacts.items():
            if not all(art[k] for k in ("bundle", "runtime_identity")):
                raise ValueError(f"timed artifact lacks deploy identity: {art['path']}")
            sidecar = Path(art["sc"] + ".scopt.json")
            documents = {name: Path(art[name]) for name in ("bundle", "runtime_identity")}
            if sidecar.is_file():
                documents["sidecar"] = sidecar
            for name, path in documents.items():
                frozen[path] = _fingerprint(path)
                report["artifacts"][arm][name] = {"path": str(path), "sha256": file_sha256(path)}
        if expected_artifacts is not None and report["artifacts"] != expected_artifacts:
            return finish("error", None, "frozen_candidate_changed")
        if qemu is not None:
            qemu = Path(qemu).resolve(strict=True)
            frozen[qemu] = _fingerprint(qemu)
        cases, specifications = _preflight(inputs, training_inputs, frozen)
        if expected_cases is not None and specifications != expected_cases:
            return finish("error", None, "frozen_timing_inputs_changed")
    except (OSError, ValueError) as exc:
        return finish("error", None, "preflight_failed", detail=str(exc))

    rng = random.Random(seed)
    orders = {name: _paired_orders(pairs, rng) for name in cases}
    plan = {"pair_count_per_input": pairs, "seed": seed, "orders": orders, "warmups_per_arm": 1,
            "test": "one-sided-exact-balanced-paired-wins", "alpha": alpha, **specifications}
    if training_only:
        plan["purpose"] = "training"
    report["plan"] = plan
    write_json(output / "plan.json", plan)
    trial_id = 0
    width = len(str(len(cases) * (pairs + 1) * len(ARMS) - 1))
    arm = None
    case_name = None
    runtime: dict[str, str] | None = None
    case_ids: dict[str, tuple[str, str]] = {}
    lock = Path(f"/tmp/scopt-fixed-work-{os.getuid()}.lock")
    try:
        with lock.open("a") as guard, (output / "samples.jsonl").open("x") as log:
            fcntl.flock(guard, fcntl.LOCK_EX)
            # Keep the discarded control first so a candidate refusal has a
            # live warm reference on the affected input.
            for case_index, (case_name, case) in enumerate(cases.items()):
                samples = []
                case_ids.clear()
                if same:
                    probes = {}
                    for arm in ARMS:
                        probe = _trial(artifacts[arm]["path"], case, output / f"probe-{case_index}-{arm}",
                                       qemu=qemu, timeout_s=timeout_s, restore_probe=True)
                        observed_runtime = {key: probe[key] for key in ("qemu_sha256", "ram_storage")}
                        if runtime is None:
                            runtime = observed_runtime
                            report["runtime"] = runtime
                        if observed_runtime != runtime:
                            return finish("error", None, "runtime_changed", trial_error=observed_runtime)
                        case_ids[arm] = (probe["bundle_id"], probe["runtime_identity_id"])
                        probes[arm] = probe["restore"]
                    report.setdefault("restore_probes", {})[case_name] = probes
                    if case_ids["warm"][0] != case_ids["candidate"][0]:
                        return finish("error", None, "comparison_bundle_mismatch", input=case_name)
                    if not _sources_unchanged(frozen):
                        return finish("error", None, "source_changed")
                    if (any(p is None or p["hint_file_valid"] != bool(hints[a]) for a, p in probes.items())
                            or not any(p["skipped"] for p in probes.values())):
                        return finish("fallback", "warm", "restore_hints_inactive", input=case_name)
                    if probes["warm"]["skipped"] == probes["candidate"]["skipped"]:
                        return finish("fallback", "warm", "restore_effect_not_distinguished", input=case_name)
                for block, order in enumerate([ARMS, *orders[case_name]], start=-1):
                    sample: dict[str, int | str] = {"first": order[0], "second": order[1]}
                    for arm in order:
                        run_path = output / f"trial-{trial_id:0{width}d}"
                        trial_id += 1
                        trial = _trial(artifacts[arm]["path"], case, run_path,
                                       qemu=qemu, timeout_s=timeout_s)
                        observed_runtime = {key: trial[key] for key in ("qemu_sha256", "ram_storage")}
                        if runtime is None:
                            runtime = observed_runtime
                            report["runtime"] = runtime
                        if runtime != observed_runtime:
                            return finish("error", None, "runtime_changed", trial_error=observed_runtime)
                        observed_ids = (trial["bundle_id"], trial["runtime_identity_id"])
                        if case_ids.setdefault(arm, observed_ids) != observed_ids:
                            return finish("error", None, "identity_changed", input=case_name, arm=arm)
                        sample[arm] = trial["elapsed_ns"]
                        print(json.dumps({"input": case_name, "block": block, "arm": arm,
                                          "trial": run_path.name, **trial}),
                              file=log, flush=True)
                    if block >= 0:
                        samples.append(sample)
                if case_ids["warm"][0] != case_ids["candidate"][0]:
                    return finish("error", None, "comparison_bundle_mismatch", input=case_name)
                report["inputs"][case_name] = {
                    **_paired_evidence(samples, alpha=alpha), "bundle_id": case_ids["warm"][0],
                    "runtime_identity_ids": {arm: ids[1] for arm, ids in case_ids.items()},
                }
    except (DeployError, RunDirError) as exc:
        if not _sources_unchanged(frozen):
            return finish("error", None, "source_changed")
        error = {"arm": arm, "input": case_name, "code": exc.code, "detail": str(exc)}
        if arm == "candidate":
            return finish("fallback", "warm", "candidate_invalid", trial_error=error)
        return finish("error", None, "warm_control_invalid", trial_error=error)
    except OSError as exc:
        return finish("error", None, "campaign_failed", detail=str(exc))

    if not _sources_unchanged(frozen):
        return finish("error", None, "source_changed")
    if all(result["passes"] for result in report["inputs"].values()):
        return finish("ok", "candidate", "speedup_established")
    return finish("fallback", "warm", "insufficient_speedup_evidence")

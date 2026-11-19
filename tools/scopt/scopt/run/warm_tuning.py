"""Rank complete cache candidates on training cases, then confirm one frozen candidate."""
from __future__ import annotations

from collections.abc import Sequence
import math
from pathlib import Path
import random

from .launcher import write_json
from .deploy import resolve_artifact
from .warm_selection import (DEFAULT_ALPHA, MIN_PAIRS, TimingInput, _fingerprint, _preflight,
                             _sources_unchanged, select_warm_cache)

def _artifact_files(paths):
    files = set()
    for path in paths:
        art = resolve_artifact(path)
        cache = Path(art["sc"])
        files.add(cache)
        possible = [Path(str(cache) + ".scopt.json")]
        if art["kind"] == "artifact_dir":
            possible.extend(cache.parent.glob("*.scm"))
        possible.extend(Path(art[k]) for k in ("restore_hints", "bundle", "runtime_identity") if art.get(k))
        files.update(p for p in possible if p.is_file())
    return files


def tune_warm_cache(
    warm: Path, candidates: Sequence[Path], output: Path, *, training: Sequence[TimingInput],
    confirmation: Sequence[TimingInput], earlier_inputs: Sequence[Path] = (), qemu: Path | None = None,
    confirmation_exclusions: Sequence[Path] = (),
    training_pairs: int = MIN_PAIRS, pairs: int = MIN_PAIRS, seed: int = 193483,
    timeout_s: float = 90, alpha: float = DEFAULT_ALPHA,
) -> dict:
    """Keep warm when each changed candidate loses a training case or the final comparison."""
    warm = Path(warm).resolve(strict=True)
    paths = [Path(p).resolve(strict=True) for p in candidates]
    if not paths or len(set(paths)) != len(paths):
        raise ValueError("supply one or more distinct candidate paths")
    if any(type(n) is not int or n < MIN_PAIRS or n % 2 for n in (training_pairs, pairs)):
        raise ValueError(f"training and confirmation need even pair counts of at least {MIN_PAIRS}")
    if not 0 < alpha <= DEFAULT_ALPHA or not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("invalid selection alpha or timeout")
    earlier = list(dict.fromkeys(earlier_inputs))
    training_banks = [p for case in training for p in case.bank_inputs.values()]
    held_out_exclusions = list(dict.fromkeys([*earlier, *confirmation_exclusions, *training_banks]))
    frozen_files = {}
    _, train_inputs = _preflight(training, earlier, frozen_files)
    _, confirm_inputs = _preflight(confirmation, held_out_exclusions, frozen_files)
    if qemu is not None:
        qemu = Path(qemu).resolve(strict=True)
        frozen_files[qemu] = _fingerprint(qemu)
    input_artifacts = (warm, *paths)
    inventory = _artifact_files(input_artifacts)
    frozen_files.update({p: _fingerprint(p) for p in inventory})

    def check_sources():
        if inventory != _artifact_files(input_artifacts) or not _sources_unchanged(frozen_files):
            raise ValueError("a tuning input or artifact changed")

    rng = random.Random(seed)
    schedule = list(range(len(paths)))
    rng.shuffle(schedule)
    seeds = [rng.getrandbits(32) for _ in paths]
    confirmation_seed = rng.getrandbits(32)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    plan = {"schema": "scopt.warm_tuning/1", "warm": str(warm.resolve()), "candidates": list(map(str, paths)),
            "order": schedule, "seeds": seeds, "confirmation_seed": confirmation_seed,
            "training_pairs": training_pairs, "pairs": pairs, "alpha": alpha, "seed": seed,
            "training_inputs": train_inputs, "confirmation_inputs": confirm_inputs,
            "rank": "largest minimum per-case median paired speedup above one; ties use argument order"}
    write_json(output / "plan.json", plan)
    summaries = {}
    common_warm = None
    best_score, best, best_artifacts = 1.0, None, None
    for index in schedule:
        check_sources()
        result = select_warm_cache(warm, paths[index], output / "training" / str(index),
                                   inputs=training, training_inputs=earlier, qemu=qemu,
                                   pairs=training_pairs, seed=seeds[index], timeout_s=timeout_s,
                                   training_only=True, expected_cases=train_inputs)
        summaries[str(index)] = {"reason": result["reason"], "score": None}
        if result["status"] == "error":
            raise ValueError(f"training comparison {index} failed: {result['reason']}")
        if result["reason"] not in ("speedup_established", "insufficient_speedup_evidence"):
            continue
        observed_warm = result["artifacts"]["warm"]
        if common_warm is not None and common_warm != observed_warm:
            raise ValueError("the ordinary warm reference changed between candidates")
        common_warm = observed_warm
        score = min(row["median_paired_speedup"] for row in result["inputs"].values())
        summaries[str(index)]["score"] = score
        if score > best_score or (best is not None and score == best_score and index < best):
            best_score, best, best_artifacts = score, index, result["artifacts"]
    check_sources()
    write_json(output / "frozen.json", {"winner": best, "artifacts": best_artifacts, "scores": summaries})
    result = select_warm_cache(warm, warm if best is None else paths[best], output / "confirmation",
                               inputs=confirmation, training_inputs=held_out_exclusions, qemu=qemu,
                               pairs=pairs, seed=confirmation_seed, timeout_s=timeout_s, alpha=alpha,
                               expected_artifacts=best_artifacts, expected_cases=confirm_inputs)
    report = {"schema": "scopt.warm_tuning/1", "status": result["status"], "training_winner": best,
              "reason": "no_training_winner" if best is None and result["status"] == "ok" else result["reason"],
              "selected": result["selected"], "selected_artifact": result["selected_artifact"],
              "selection": str(output / "confirmation" / "selection.json")}
    write_json(output / "tuning.json", report)
    return report

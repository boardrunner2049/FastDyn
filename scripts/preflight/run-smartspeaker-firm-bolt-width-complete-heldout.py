#!/usr/bin/env python3
"""Run the frozen held-out width-complete Firm-BOLT comparison."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "scripts/preflight/run-smartspeaker-firm-bolt-arithmetic-precision.py"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    run = ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
    wrapper = ROOT / "build/preflight/bolt-firmware-wrapper-width-complete-cmp-2026-11-17"
    plan_path = run / "firm-bolt-width-complete-cmp-heldout-plan.json"
    training_selection = run / "firm-bolt-width-complete-cmp-selection.json"
    training_raw = run / "firm-bolt-width-complete-cmp-training.json"
    plan = json.loads(plan_path.read_text())
    if sha256(training_selection) != plan["training_selection_sha256"]:
        raise RuntimeError("training selection differs from frozen held-out plan")
    if sha256(training_raw) != plan["training_raw_sha256"]:
        raise RuntimeError("training raw data differs from frozen held-out plan")
    if json.loads(training_selection.read_text())["status"] != "selected_for_held_out":
        raise RuntimeError("training did not authorize held-out evaluation")

    spec = importlib.util.spec_from_file_location("firm_bolt_precision", SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load policy runner: {SOURCE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report_path = run / "firm-bolt-width-complete-cmp-heldout-report.json"
    module.PLAN = plan_path
    module.RAW = run / "firm-bolt-width-complete-cmp-heldout.json"
    module.LOG = run / "firm-bolt-width-complete-cmp-heldout.log"
    module.REPORT = report_path
    module.CANDIDATE = wrapper / "SmartSpeaker.bolt-qualified-compact"
    module.CANDIDATE_LABEL = "firm_bolt_width_complete_cmp"
    module.main()

    report = json.loads(report_path.read_text())
    comparison = report["stock_bolt_over_firm_bolt"]
    accepted = bool(
        report["all_exact_work_and_trace_checks_match"]
        and comparison["target_cpu_ns"]["bootstrap_95_percent"][0] > 1.0
        and comparison["wall_ns"]["bootstrap_95_percent"][0] >= 1.0
    )
    report["status"] = "held_out_accepted" if accepted else "held_out_rejected"
    report["useful_effect_cleared"] = bool(
        comparison["target_cpu_ns"]["bootstrap_95_percent"][0] > 1.005
    )
    report["training_selection_sha256"] = sha256(training_selection)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())

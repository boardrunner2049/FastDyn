#!/usr/bin/env python3
"""Run and classify the frozen Firm-BOLT inverse-copy held-out set."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "scripts/preflight/run-smartspeaker-firm-bolt-arithmetic-precision.py"


def main() -> int:
    spec = importlib.util.spec_from_file_location("firm_bolt_precision", SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load policy runner: {SOURCE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    run = ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
    wrapper = ROOT / "build/preflight/bolt-firmware-wrapper-copy-cleanup-2026-11-17"
    module.PLAN = run / "firm-bolt-copy-cleanup-heldout-plan.json"
    module.RAW = run / "firm-bolt-copy-cleanup-heldout-100.json"
    module.LOG = run / "firm-bolt-copy-cleanup-heldout-100.log"
    module.REPORT = run / "firm-bolt-copy-cleanup-heldout-report.json"
    module.CANDIDATE = wrapper / "SmartSpeaker.bolt-firmware-candidate"
    module.CANDIDATE_LABEL = "firm_bolt_copy_cleanup"
    result = module.main()

    report = json.loads(module.REPORT.read_text())
    comparison = report["stock_bolt_over_firm_bolt"]
    accepted = bool(
        report["all_exact_work_and_trace_checks_match"]
        and comparison["target_cpu_ns"]["bootstrap_95_percent"][0] > 1.005
        and comparison["wall_ns"]["bootstrap_95_percent"][0] >= 1.0
    )
    report["status"] = "held_out_accepted" if accepted else "held_out_rejected"
    report["selection_and_precision_samples_excluded"] = True
    module.REPORT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if accepted else (result or 2)


if __name__ == "__main__":
    raise SystemExit(main())

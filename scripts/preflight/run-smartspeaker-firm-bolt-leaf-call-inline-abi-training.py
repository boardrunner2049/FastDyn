#!/usr/bin/env python3
"""Run the frozen 100-block exact leaf-call-boundary experiment."""

from __future__ import annotations

import importlib.util
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
    wrapper = (
        ROOT / "build/preflight/bolt-firmware-wrapper-leaf-call-inline-abi-2026-11-17"
    )
    module.PLAN = run / "firm-bolt-leaf-call-inline-abi-plan.json"
    module.RAW = run / "firm-bolt-leaf-call-inline-abi-training-100.json"
    module.LOG = run / "firm-bolt-leaf-call-inline-abi-training-100.log"
    module.REPORT = run / "firm-bolt-leaf-call-inline-abi-training-report.json"
    module.CANDIDATE = wrapper / "SmartSpeaker.bolt-firmware-candidate"
    module.CANDIDATE_LABEL = "firm_bolt_leaf_call_inline_abi"
    return module.main()


if __name__ == "__main__":
    raise SystemExit(main())

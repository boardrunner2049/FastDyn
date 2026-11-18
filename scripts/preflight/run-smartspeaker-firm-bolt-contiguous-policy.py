#!/usr/bin/env python3
"""Train the frozen contiguous-hot Firm-BOLT policy."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "scripts/preflight/run-smartspeaker-firm-bolt-arithmetic-policy.py"


def main() -> int:
    spec = importlib.util.spec_from_file_location("firm_bolt_policy", SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load policy runner: {SOURCE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    run = ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
    wrapper = ROOT / "build/preflight/bolt-firmware-wrapper-contiguous-hot-2026-11-17"
    module.PLAN = run / "firm-bolt-contiguous-hot-training-plan.json"
    module.RAW = run / "firm-bolt-contiguous-hot-training.json"
    module.LOG = run / "firm-bolt-contiguous-hot-training.log"
    module.SELECTION = run / "firm-bolt-contiguous-hot-selection.json"
    module.CANDIDATE = wrapper / "SmartSpeaker.bolt-qualified-compact"
    module.SMOKE = run / "firm-bolt-contiguous-hot-smoke.json"
    module.CANDIDATE_LABEL = "firm_bolt_contiguous_hot"
    return module.main()


if __name__ == "__main__":
    raise SystemExit(main())

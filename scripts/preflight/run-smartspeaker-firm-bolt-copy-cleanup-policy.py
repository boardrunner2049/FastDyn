#!/usr/bin/env python3
"""Train the frozen Firm-BOLT inverse-copy cleanup policy."""

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
    wrapper = ROOT / "build/preflight/bolt-firmware-wrapper-copy-cleanup-2026-11-17"
    module.PLAN = run / "firm-bolt-copy-cleanup-training-plan.json"
    module.RAW = run / "firm-bolt-copy-cleanup-training.json"
    module.LOG = run / "firm-bolt-copy-cleanup-training.log"
    module.REPORT = run / "firm-bolt-copy-cleanup-selection.json"
    module.CANDIDATE = wrapper / "SmartSpeaker.bolt-firmware-candidate"
    module.CANDIDATE_LABEL = "firm_bolt_copy_cleanup"
    return module.main()


if __name__ == "__main__":
    raise SystemExit(main())

"""Capture and qualify the Cortex-M3 ordinary-edge specialization fixture."""

from __future__ import annotations

import argparse
from pathlib import Path

import bootstrap_host_inline as bootstrap


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--fixture", choices=("global-specialization", "global-transitive", "state-forward", "region-dse"),
                        default="global-specialization")
    args = parser.parse_args()
    work = args.work_dir.resolve()
    work.mkdir(parents=True)
    bootstrap.FIXTURE = bootstrap.ROOT / "tools/scopt/fixtures" / args.fixture
    bootstrap.qualify_images(work, args.qemu.resolve(strict=True), args.plugin.resolve(strict=True),
                             work / "bootstrap.log")


if __name__ == "__main__":
    main()

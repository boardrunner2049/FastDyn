"""Command-line entry point for the phase-two FastDyn bundle adapter."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Sequence

from .driver import create_identity_bundle


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fastdyn.preflight",
        description="Phase two: create an unchanged-image FastDyn ExecutionBundle v1.",
    )
    parser.add_argument("--config", required=True, type=Path, help="FastDyn TOML configuration")
    parser.add_argument("--output", required=True, type=Path, help="new bundle directory")
    parser.add_argument("--machine", default="machine0", help="parsed FastDyn machine name")
    parser.add_argument("--cpu-index", default=0, type=int, help="CPU index in the machine")
    parser.add_argument("--svd", type=Path, help="CMSIS-SVD file or catalog directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse one FastDyn configuration and publish its identity bundle."""

    args = _parser().parse_args(argv)

    # These modules load optional configuration dependencies; keep --help usable
    # even in a minimal artifact-reader environment.
    from fastdyn import paths, toml_parser
    from fastdyn.fastdyn import compile_device_routing

    with tempfile.TemporaryDirectory(prefix="fastdyn-preflight-") as preparation_dir:
        handle = toml_parser.parser(
            preparation_dir,
            machine_name=args.machine,
            toml_config=str(args.config.resolve()),
            svd_path=str(args.svd.resolve()) if args.svd else paths.default_svd_dir(),
            fmu_name=None,
        )
        try:
            machine = handle.machines[args.machine]
        except KeyError as exc:
            raise ValueError(f"configuration did not create machine {args.machine!r}") from exc
        compile_device_routing(
            machine.devices,
            machine.parsed_device,
            models=machine.models,
        )
        bundle = create_identity_bundle(machine, args.output, cpu_index=args.cpu_index)

    print(
        json.dumps(
            {"bundle_id": bundle.bundle_id, "path": str(bundle.path)},
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

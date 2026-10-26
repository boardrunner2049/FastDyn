#!/usr/bin/env python3
"""Build the FMI 3.0 FMU selected by a FastDyn TOML config."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import subprocess


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastdyn import fmu_build  # noqa: E402


DEFAULT_CONFIG = Path("configs/copter462.toml")


def path_arg(value: str) -> Path:
    return Path(value).expanduser()


def parser() -> argparse.ArgumentParser:
    details = f"""defaults:
  config: {DEFAULT_CONFIG}

The script reads [FMU] from the same TOML file used by `fastdyn run`.
Relative paths are resolved from the FastDyn repository root.

examples:
  ./utils/build_fmi3_fmu.py
  ./utils/build_fmi3_fmu.py --fmu quadrotor
  ./utils/build_fmi3_fmu.py --config configs/my_vehicle.toml --fmu quadrotor
  ./utils/build_fmi3_fmu.py --no-build
  ./utils/build_fmi3_fmu.py --output /tmp/Quadrotor
"""
    arg_parser = argparse.ArgumentParser(
        prog="utils/build_fmi3_fmu.py",
        description="Generate the FMI 3.0 FMU selected by a FastDyn TOML config.",
        epilog=details,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    arg_parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        type=path_arg,
        metavar="PATH",
        help="FastDyn TOML config containing an [FMU] table",
    )
    arg_parser.add_argument(
        "--fmu", default=None, metavar="NAME", help="Named [FMU.models] entry"
    )
    arg_parser.add_argument(
        "--model", default=None, metavar="NAME", help="Override the Modelica model"
    )
    arg_parser.add_argument(
        "--model-file",
        default=None,
        type=path_arg,
        metavar="PATH",
        help="Override the Modelica source file",
    )
    arg_parser.add_argument(
        "--source-root",
        default=None,
        type=path_arg,
        metavar="PATH",
        help="Override the Modelica source root",
    )
    arg_parser.add_argument(
        "--output",
        default=None,
        type=path_arg,
        metavar="PATH",
        help="Override the generated artifact directory",
    )
    arg_parser.add_argument(
        "--build", dest="package", action="store_true", default=None, help="Package the FMU"
    )
    arg_parser.add_argument(
        "--no-build", dest="package", action="store_false", help="Generate sources only"
    )
    arg_parser.add_argument(
        "--release", dest="release", action="store_true", default=None, help="Use cargo --release"
    )
    arg_parser.add_argument(
        "--dev", dest="release", action="store_false", help="Use the cargo dev profile"
    )
    arg_parser.add_argument(
        "--skip-submodules",
        action="store_true",
        help="Do not initialize or update pinned submodules before running Rumoca",
    )
    arg_parser.add_argument("--compose-only", action="store_true", help="Validate and emit FIRE Modelica without compiling an FMU")
    return arg_parser


def main() -> int:
    args = parser().parse_args()
    overrides = fmu_build.FmuOverrides(
        model=args.model,
        model_file=args.model_file,
        source_root=args.source_root,
        output=args.output,
        package=args.package,
        release=args.release,
    )
    try:
        build = fmu_build.resolve(args.config, args.fmu, overrides, repo_root=ROOT)
        if args.compose_only:
            if build.compose_config is None:
                raise fmu_build.FmuConfigError("--compose-only requires an FMU physics table or compose path")
            from fastdyn.physics_composition import compose
            print(f"Generated manifest: {compose(build)}")
            return 0
        print(
            f"Generating FMI 3.0 target for {build.name!r} with Rumoca at {build.rumoca_dir}...",
            flush=True,
        )
        fmu_build.build_fmu(build, update_submodules_first=not args.skip_submodules)
    except (fmu_build.FmuConfigError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from exc

    print()
    print(f"Generated source tree: {build.output}")
    if build.package:
        print(f"Generated FMU: {build.fmu_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

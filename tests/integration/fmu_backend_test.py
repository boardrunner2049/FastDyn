"""Check native FMI initialization and actuator response for all three vehicles."""

import argparse
import os
import shlex
from pathlib import Path
import subprocess

from fastdyn import fmu_build
from fastdyn.fmu_runtime import prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu-path", type=Path, default=Path("../qemu"))
    parser.add_argument("--vehicle", choices=("copter", "rover", "plane"), action="append",
                        help="Check only selected vehicles (repeatable)")
    for vehicle in ("copter", "rover", "plane"):
        parser.add_argument(f"--{vehicle}-config", type=Path,
                            default=Path(f"configs/{vehicle}462.toml"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = root / "out/ci/fmu_backend_test"
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-o",
            str(output),
            str(Path(__file__).with_suffix(".c")),
            str(root / "virtuals/physics/physics_engines/fmu/fmu.c"),
            "-I" + str(root / "include"),
            "-I" + str(root / "include/virtuals"),
            "-I" + str(args.qemu_path.resolve() / "include"),
            *shlex.split(
                subprocess.check_output(
                    ["pkg-config", "--cflags", "glib-2.0"], text=True
                )
            ),
            "-ldl",
            "-lm",
        ],
        check=True,
    )
    for vehicle in args.vehicle or ("copter", "rover", "plane"):
        build = fmu_build.resolve(root / getattr(args, f"{vehicle}_config"), repo_root=root)
        refs = fmu_build.value_references(build)
        runtime = prepare(build.fmu_path)
        subprocess.run(
            [
                str(output),
                vehicle,
                f"fmu={runtime.library}",
                f"fmu_instantiation_token={runtime.instantiation_token}",
                f"fmu_resource_path={runtime.resources}",
                *[f"fmu_vr_{name}={value}" for name, value in refs.items()],
                *[
                    f"fmu_param_{name}={fmu_build.parameter_argument(value)}"
                    for name, value in build.parameters.items()
                ],
            ],
            check=True,
        )


if __name__ == "__main__":
    main()

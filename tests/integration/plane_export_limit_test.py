"""Keep the current Plane exporter limitation explicit until event support lands."""
import argparse
from pathlib import Path
import subprocess

from fastdyn import fmu_build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/plane462.toml"))
    args = parser.parse_args()
    build = fmu_build.resolve(args.config)
    command = fmu_build.cargo_command(build)
    output = Path("out/ci").resolve()
    output.mkdir(parents=True, exist_ok=True)
    lowered = command[:command.index("--output")] + [
        "--emit", "dae-json", "--output", str(output / "plane-dae.json")]
    subprocess.run(lowered, cwd=build.rumoca_dir, check=True)
    result = subprocess.run(command, cwd=build.rumoca_dir, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (output / "plane-export-limit.log").write_text(result.stdout)
    if result.returncode == 0:
        raise RuntimeError("Plane now exports: enable its native, swarm, and mission checks before removing this limitation test")
    expected = ("state-event", "state event", "assertion depends on time, a continuous state")
    if not any(marker in result.stdout.lower() for marker in expected):
        raise RuntimeError(f"Unexpected Plane failure:\n{result.stdout}")
    print("Plane source lowers successfully; FMI export rejects its unsupported contact events. Plane flight is NOT validated.")


if __name__ == "__main__":
    main()

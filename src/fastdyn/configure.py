#!/usr/bin/env python3
"""Generate a portable FastDyn run TOML from a base and ordered overlays."""

import argparse
from pathlib import Path
import re
import shutil
import tomllib

import tomli_w


def defaults(value):
    """Materialize defaults from the shared configs without reading the environment."""
    if isinstance(value, str):
        return re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}", r"\1", value)
    if isinstance(value, list):
        return [defaults(item) for item in value]
    if isinstance(value, dict):
        return {key: defaults(item) for key, item in value.items()}
    return value


def merge(base, overlay):
    """Merge TOML tables; replace arrays and scalar values in overlay order."""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=Path("configs/copter462.toml"))
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--overlay", type=Path, action="append", default=[],
                        help="Merge a TOML overlay; repeat to apply files in order")
    for option in ("qemu", "plugin", "compiler"):
        parser.add_argument("--" + option, help="Override the executable/library location")
    args = parser.parse_args()
    with args.base.open("rb") as handle:
        config = defaults(tomllib.load(handle))
    for path in args.overlay:
        with path.open("rb") as handle:
            merge(config, tomllib.load(handle))
    directory = args.output.parent / args.output.stem
    directory.mkdir(parents=True, exist_ok=True)
    config["Machine"]["qemu_path"] = (args.qemu or config["Machine"].get("qemu_path")
                                      or shutil.which("qemu-system-arm"))
    config["Machine"]["qmp_socket"] = str(directory.resolve() / "qmp.sock")
    config["Machine"]["log_file"] = str(directory / "qemu.log")
    for group in config["CPU"].values():
        for cpu in group if isinstance(group, list) else [group]:
            if args.plugin:
                cpu["plugin_library"] = args.plugin
    for name, group in config["Memory"].items():
        for bank in group if isinstance(group, list) else [group]:
            if bank.get("backend") == "file":
                bank["memory_file"] = str(directory / (bank.get("id", name) + ".ram"))
    if "FMU" in config:
        config["FMU"]["compiler"] = (args.compiler or config["FMU"].get("compiler")
                                      or shutil.which("rumoca")
                                      or "third_party/common/rumoca/target/debug/rumoca")
    proxy = config.get("Run", {}).get("processes", {}).get("mavproxy")
    if proxy:
        proxy["command"] = [
            f"--logfile={directory.resolve() / 'mission.tlog'}" if arg.startswith("--logfile=") else arg
            for arg in proxy["command"]
        ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("wb") as handle:
        tomli_w.dump(config, handle)
    print(f"Created {args.output}; run: fastdyn run -c {args.output} -o {directory / 'work'}")


if __name__ == "__main__":
    main()

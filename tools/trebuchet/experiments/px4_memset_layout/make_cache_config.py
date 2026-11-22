#!/usr/bin/env python3
"""Add an explicit non-flat SuperCache to a generated PX4 runtime config."""
from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", default="cache/px4.sc")
    parser.add_argument(
        "--freeze-fmu",
        action="store_true",
        help="disable automatic FMU rebuilding after a packaged FMU has been built",
    )
    parser.add_argument(
        "--unpacked-fmu",
        type=Path,
        help="fixed unpacked FMU tree containing modelDescription.xml and binaries/",
    )
    args = parser.parse_args()

    text = args.input.read_text(encoding="utf-8")
    document = tomllib.loads(text)
    machine = document.get("Machine") or {}
    if machine.get("supercache") not in (None, ""):
        raise SystemExit("input already selects a SuperCache")
    marker = "[Machine]\n"
    if text.count(marker) != 1:
        raise SystemExit("expected exactly one [Machine] table")
    insertion = (
        marker
        + f'supercache = "{args.cache}"\n'
        + "supercache_flat = false\n"
    )
    rendered = text.replace(marker, insertion)
    if args.freeze_fmu or args.unpacked_fmu:
        auto_build = "auto_build = true"
        if rendered.count(auto_build) != 1:
            raise SystemExit("expected exactly one enabled FMU auto-build setting")
        rendered = rendered.replace(auto_build, "auto_build = false")
    if args.unpacked_fmu:
        model = next(iter(document["FMU"]["models"].values()))
        old_output = f'output = {json.dumps(model["output"])}'
        new_output = f'output = {json.dumps(str(args.unpacked_fmu.resolve()))}'
        if rendered.count(old_output) != 1 or rendered.count("build = true") != 1:
            raise SystemExit("expected one packaged FMU output and build setting")
        rendered = rendered.replace(old_output, new_output)
        rendered = rendered.replace("build = true", "build = false")
    parsed = tomllib.loads(rendered)
    if parsed["Machine"]["supercache"] != args.cache:
        raise SystemExit("rendered cache path did not round-trip")
    if parsed["Machine"]["supercache_flat"] is not False:
        raise SystemExit("rendered cache must be non-flat")
    if (args.freeze_fmu or args.unpacked_fmu) and parsed["FMU"]["auto_build"] is not False:
        raise SystemExit("rendered FMU must be frozen")
    if args.unpacked_fmu:
        parsed_model = next(iter(parsed["FMU"]["models"].values()))
        if parsed_model["build"] is not False:
            raise SystemExit("rendered FMU must use the unpacked tree")
        if Path(parsed_model["output"]) != args.unpacked_fmu.resolve():
            raise SystemExit("rendered unpacked FMU path did not round-trip")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()

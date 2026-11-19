"""Qualify the native-program CLI pipeline against the original firmware capture."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from scopt.formats.sc_v6 import read_sc6
from scopt.formats.scm_v1 import read_scm


def load(path):
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path, help="qualified ordinary-edge or state-forward fixture")
    parser.add_argument("out", type=Path)
    parser.add_argument("--selftest", type=Path)
    args = parser.parse_args()
    base, out = args.baseline.resolve(), args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    inputs = load(base / "baseline.json")
    os.environ["SCOPT_ENVLAYOUT_PATH"] = str(base / "envlayouts")
    os.environ["SCOPT_V6_EXES"] = inputs["qemu_sha256"]
    qemu = load(base / "v10/dynamic.json")["qemu"]
    cli = [sys.executable, "-m", "scopt"]

    # A fixture of this size needs more than its default 10% budget to hold one variant.
    params = out / "params.json"
    params.write_text(json.dumps({"specialize-edges": {"max_code_growth_bytes": 65536}}))
    cmd = [*cli, "optimize", inputs["snapshot"], "--pipeline", "native-program",
           "--store", str(out), "--profile", inputs["profile"], "--runtime-identity",
           str(base / "runtime-identity.v2.json"), "--elf", str(base / "guest.elf"),
           "--prep", str(base / "prep"), "--params", str(params), "--json"]
    if args.selftest:
        cmd += ["--selftest", str(args.selftest.resolve())]
    with (out / "result.json").open("w") as stream:
        subprocess.run(cmd, stdout=stream, check=True)
    doc = load(out / "result.json")
    result = Path(doc["artifact_dir"])
    image = read_sc6(result / "cache.sc")
    certs = [tb for f in result.glob("cache*.scm") for tb in read_scm(f).tbs.values()
             if tb.native_fragments]
    moves = load(result / "cache.sc.scopt.json").get("move_map")
    if not (image.loadable and image.ext and image.ext.links and image.ext.variants and certs
            and any(row["delta"] for row in (moves or {}).values())):
        raise ValueError("combined artifact lacks a certified splice, a variant/link or a physical move")
    validate = [*cli, "validate", str(result), "--dynamic", "-c", str(base / "host-inline.toml"),
                "--baseline", inputs["snapshot"], "--qemu", qemu, "--out", str(out / "v10")]
    for i in range(len(inputs["oracle"]["heldout"])):
        validate += ["--inputs", str(base / f"heldout-{i}.bin")]
    subprocess.run(validate, check=True)
    print(result)


if __name__ == "__main__":
    main()

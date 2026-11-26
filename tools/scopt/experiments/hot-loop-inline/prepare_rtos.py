"""Prepare ordinary or sealed compiled launches for a single marker-instrumented FreeRTOS ELF."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from scopt.canon import canonical_json
from scopt.formats.bundle_v1 import run_info_fields, runtime_identity
from scopt.formats.sc_records import ScHeader
from scopt.formats.scm_v1 import read_scm


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("elf", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--qemu", required=True, type=Path)
    parser.add_argument(
        "--sealed-training-input",
        type=Path,
        action="append",
        help="prepare the trusted no-icount/no-recovery capture and import it",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[4]
    out, qemu, elf = (
        p.resolve(strict=strict)
        for p, strict in ((args.out, False), (args.qemu, True), (args.elf, True))
    )
    out.mkdir(parents=True, exist_ok=False)
    gen = root / "tools/trebuchet/lib/gen_toml.py"
    config = out / "image.toml"
    env = dict(os.environ, TREBUCHET_FASTDYN=str(root))

    def run(*command):
        subprocess.run(command, check=True, env=env, cwd=root)

    run(
        sys.executable,
        gen,
        elf,
        config,
        "fw",
        *([] if args.sealed_training_input else ["--no-cache"]),
    )
    lines = config.read_text().splitlines()
    config.write_text(
        "\n".join(
            f"qemu_path = {json.dumps(str(qemu))}" if s.startswith("qemu_path =") else s
            for s in lines
            if not (args.sealed_training_input and s.startswith("icount ="))
        )
        + "\n"
    )
    if args.sealed_training_input:
        prep = out / "prep"
        run(
            sys.executable,
            root / "tools/scopt/scripts/prepare_sealed_firmware.py",
            "--config",
            config,
            "--out",
            prep,
            "--qemu",
            qemu,
            "--trust-user-no-recovery",
            *(
                arg
                for path in args.sealed_training_input
                for arg in ("--input", path.resolve(strict=True))
            ),
        )
        config = prep / "run.toml"
        run(
            sys.executable,
            "-m",
            "scopt",
            "bundle",
            "identity",
            "-c",
            config,
            "--manifest",
            prep / "cache/fw.sc.fastdyn.json",
            "--out",
            out / "bundle.json",
        )
    run("fastdyn", "compile-launch", "-c", config, "-o", out / "launch")
    if args.sealed_training_input:
        argv = json.loads((out / "launch/launch.json").read_text())["argv"]
        with (prep / "cache/fw.sc").open("rb") as fp:
            header = ScHeader.from_bytes(fp.read(ScHeader.SIZE))
        identity = runtime_identity(
            qemu=argv[0],
            plugin=argv[argv.index("--plugin") + 1].split(",")[0],
            config_text=config.read_text(),
            sc_header=header,
            fastdyn_src=root,
            run_info=run_info_fields(read_scm(prep / "capture.scm").run_info),
        )
        (out / "runtime.json").write_bytes(canonical_json(identity) + b"\n")
        run(
            sys.executable,
            "-m",
            "scopt",
            "import",
            prep,
            "--bundle",
            out / "bundle.json",
            "--runtime-identity",
            out / "runtime.json",
            "--store",
            out / "store",
        )


if __name__ == "__main__":
    main()

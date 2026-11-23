"""Stage and time the FreeRTOS fixed-work replay after hot-loop compilation."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from fastdyn.compiled_launch import emit_bundle
from scopt.formats.sc_v5 import sparse_copy
from scopt.run.helper_pgo import _launch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("launch", "snapshot", "artifact", "out"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--pairs", type=int, default=8)
    parser.add_argument("--seed", type=int, default=5449)
    parser.add_argument("--cpu", type=int, required=True)
    args = parser.parse_args()
    launch = json.loads(args.launch.read_text())
    if any(a.startswith("memory-backend-file,") and "share=off" not in a.split(",")
           for a in launch["argv"]):
        parser.error("this fixed-work timing driver requires private RAM mappings")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    for name, source in (("ordinary", args.snapshot), ("candidate", args.artifact)):
        arm = out / name
        arm.mkdir()
        cache = arm / "cache.sc"
        sparse_copy(source / "cache.sc" if source.is_dir() else source, cache)
        argv, env, _ = _launch(launch, arm, cache)
        frozen = {k: env[k] for k in set(launch["env_set"]) | {"QEMU_SC_SEALED"} if k in env}
        emit_bundle(arm, argv, launch["cwd"], frozen, disable_aslr=True, allow_existing=True)
    driver = Path(__file__).resolve().parents[2] / "scripts/measure_sealed_whopr.py"
    subprocess.run([sys.executable, str(driver), "--baseline", str(out / "ordinary/run.sh"),
                    "--candidate", str(out / "candidate/run.sh"), "--out", str(out / "pairs"),
                    "--cpu", str(args.cpu), "--pairs", str(args.pairs), "--seed", str(args.seed)],
                   check=True)


if __name__ == "__main__":
    main()

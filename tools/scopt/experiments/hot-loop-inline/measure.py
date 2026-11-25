"""Stage and time the FreeRTOS fixed-work replay after hot-loop compilation.

Besides ``ordinary`` and ``candidate`` it stages ``ordinary-aa``, a second
bundle of the same snapshot cache, which the paired driver times as the A/A
noise-floor control.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from fastdyn.compiled_launch import emit_bundle
from scopt.formats.sc_v5 import sparse_copy
from scopt.run import paired_stats
from scopt.run.helper_pgo import _launch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("launch", "snapshot", "artifact", "out"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--pairs", type=int, default=8)
    parser.add_argument("--seed", type=int, default=5449)
    parser.add_argument("--cpu", type=int, required=True)
    paired_stats.add_protocol_arguments(parser)
    parser.add_argument("--no-aa-control", action="store_true",
                        help="do not stage and time the ordinary-aa control")
    args = parser.parse_args()
    try:
        paired_stats.check_min_blocks(args.pairs, args.min_blocks, args.allow_few_blocks)
    except ValueError as exc:
        parser.error(str(exc))
    launch = json.loads(args.launch.read_text())
    if any(a.startswith("memory-backend-file,") and "share=off" not in a.split(",")
           for a in launch["argv"]):
        parser.error("this fixed-work timing driver requires private RAM mappings")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    arms = [("ordinary", args.snapshot), ("candidate", args.artifact)]
    if not args.no_aa_control:
        arms.append(("ordinary-aa", args.snapshot))
    for name, source in arms:
        arm = out / name
        arm.mkdir()
        cache = arm / "cache.sc"
        sparse_copy(source / "cache.sc" if source.is_dir() else source, cache)
        argv, env, _ = _launch(launch, arm, cache)
        frozen = {k: env[k] for k in set(launch["env_set"]) | {"QEMU_SC_SEALED"} if k in env}
        emit_bundle(arm, argv, launch["cwd"], frozen, disable_aslr=True, allow_existing=True)
    driver = Path(__file__).resolve().parents[2] / "scripts/measure_sealed_whopr.py"
    command = [sys.executable, str(driver), "--baseline", str(out / "ordinary/run.sh"),
               "--candidate", str(out / "candidate/run.sh"), "--out", str(out / "pairs"),
               "--cpu", str(args.cpu), "--pairs", str(args.pairs), "--seed", str(args.seed),
               "--min-blocks", str(args.min_blocks),
               "--aa-control", ("none" if args.no_aa_control
                                else str(out / "ordinary-aa/run.sh"))]
    if args.allow_few_blocks:
        command.append("--allow-few-blocks")
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()

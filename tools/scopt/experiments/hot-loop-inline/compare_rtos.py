"""Compare compiled FreeRTOS launches against the same per-input output oracle."""

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import random
import re
import statistics
import subprocess
import sys

from fastdyn.compiled_launch import emit_bundle
from scopt.formats.qprof_v1 import read_qprof
from scopt.formats.sc_v5 import ScHeader, sparse_copy
from scopt.formats.sc_v7 import read_sc7
from scopt.run.v7_stats import counter_problems

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools/trebuchet/lib"))
import stock
from result_schema import stats_mismatches


def header(path):
    with path.open("rb") as stream:
        return ScHeader.from_bytes(stream.read(ScHeader.SIZE))


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def stage(spec, input_path, directory, cache):
    directory.mkdir()
    launch = json.loads(Path(spec["launch"]).read_text())
    argv = launch["argv"].copy()
    banks = [arg for arg in argv if arg.startswith("memory-backend-file,")]
    inputs = [arg for arg in banks if "id=input" in arg.split(",")]
    if len(inputs) != 1 or any("share=off" not in arg.split(",") for arg in banks):
        raise ValueError("requires private RAM banks and one input bank")
    data = Path(input_path).read_bytes()
    size = re.search(r",size=(\d+)M(?:,|$)", inputs[0])
    if size is None or len(data) > int(size[1]) << 20:
        raise ValueError("input does not fit bank")
    bank = directory / "input.ram"
    with bank.open("wb") as fp:
        fp.write(data)
        fp.truncate(int(size[1]) << 20)
    argv[argv.index(inputs[0])] = re.sub(
        r"mem-path=[^,]+", f"mem-path={bank}", inputs[0]
    )
    for i, arg in enumerate(argv):
        if arg.startswith("unix:"):
            argv[i] = f"unix:{directory}/qmp,server,nowait"
    env = dict(launch["env_set"], FASTDYN_TIMING_FILE=str(directory / "window.jsonl"))
    if cache is not None:
        if "-accel" in argv:
            i = argv.index("-accel") + 1
            accel = [x for x in argv[i].split(",") if not x.startswith("supercache=")]
            argv[i] = ",".join([*accel, f"supercache={cache}"])
        else:
            argv.extend(["-accel", f"tcg,supercache={cache}"])
        env["QEMU_SC_TRUSTED"] = "1"
    elif any("supercache=" in arg for arg in argv):
        raise ValueError("uncached variant has a cached launch")
    if spec.get("sealed"):
        if cache is None or "-icount" in argv:
            raise ValueError("sealed variants require a matching icount-off cache")
        env["QEMU_SC_SEALED"] = "1"
    emit_bundle(
        directory, argv, launch["cwd"], env, allow_existing=True, disable_aslr=True
    )


def execute(directory, label, extra=None):
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith(("QEMU_SC_", "QEMU_SCOPT_")):
            del env[key]
    env.update(extra or {})
    proc = subprocess.run(
        [directory / "run.sh"], capture_output=True, text=True, env=env, timeout=60
    )
    log = proc.stdout + proc.stderr
    (directory / f"{label}.log").write_text(log)
    words = [int(x, 16) for x in re.findall(r"^Reg [0-3]:0x([a-fA-F0-9]+)$", log, re.M)]
    times = re.findall(r"^\[BENCH\]\[fw\] elapsed_ns=(\d+)", log, re.M)
    if proc.returncode or len(words) != 16 or len(times) != 1:
        raise RuntimeError(
            f"{directory}/{label}.log: exit {proc.returncode}, received {words}"
        )
    return int(times[0]), words


def replay(directory, expected, label, extra=None):
    elapsed, words = execute(directory, label, extra)
    if words != expected:
        raise RuntimeError(
            f"{directory}/{label}.log: received {words}, expected {expected}"
        )
    return elapsed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    plan = json.loads(args.manifest.read_text())
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    os.sched_setaffinity(0, {plan["cpu"]})
    policy = Path(f"/sys/devices/system/cpu/cpu{plan['cpu']}/cpufreq/scaling_governor")
    host = dict(
        kernel=platform.release(),
        machine=platform.machine(),
        cpu=plan["cpu"],
        affinity=sorted(os.sched_getaffinity(0)),
        load_at_start=os.getloadavg(),
        frequency_policy=policy.read_text().strip() if policy.exists() else None,
    )
    (out / "host.json").write_text(json.dumps(host, indent=2) + "\n")
    rng = random.Random(plan["seed"])
    cases, arms = plan["cases"], plan["arms"]
    for case, sample in cases.items():
        if "words" not in sample:
            directory = out / f"oracle-{case}"
            stage({"launch": plan["oracle_launch"]}, sample["input"], directory, None)
            _, sample["words"] = execute(directory, "unmodified")
    (out / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    calibration, caches, identities = {}, {}, {}
    hashes = {}

    def identity(path):
        path = Path(path).resolve(strict=True)
        if path not in hashes:
            hashes[path] = digest(path)
        return {"path": str(path), "sha256": hashes[path]}

    for name, spec in arms.items():
        folder = out / name
        folder.mkdir()
        if "stock" in spec:
            identities[name] = {
                "elf": identity(spec["stock"]),
                "qemu": identity(
                    os.environ.get("STOCK_QEMU", "/usr/bin/qemu-system-arm")
                ),
            }
            continue
        argv = json.loads(Path(spec["launch"]).read_text())["argv"]
        identities[name] = {
            "launch": identity(spec["launch"]),
            "qemu": identity(argv[0]),
            "elf": identity(argv[argv.index("-kernel") + 1]),
            "plugin": identity(argv[argv.index("--plugin") + 1].split(",")[0]),
        }
        cache = None
        if spec.get("cache"):
            cache = folder / "cache.sc"
            sparse_copy(spec["cache"], cache)
            caches[name] = (cache, header(cache), cache.stat().st_size)
            identities[name]["cache"] = identity(spec["cache"])
        for case, sample in cases.items():
            directory = folder / case
            stage(spec, sample["input"], directory, cache)
            if cache:
                prof = directory / "warm.qprof"
                replay(
                    directory, sample["words"], "public", {"QEMU_SC_PROFILE": str(prof)}
                )
                qh = read_qprof(prof).header
                h = caches[name][1]
                if not (
                    qh.clean_exit
                    and qh.warm
                    and not qh.overflow
                    and (qh.loaded_record_count, qh.loaded_header_checksum)
                    == (h.record_count, h.checksum)
                ):
                    raise RuntimeError(f"{directory}: warm cache identity")
                if h.reserved and spec.get("sealed"):
                    path = directory / "regions.json"
                    replay(
                        directory,
                        sample["words"],
                        "regions",
                        {"QEMU_SC_V7_STATS": str(path)},
                    )
                    evidence = json.loads(path.read_text())
                    if not (
                        evidence["loaded"]
                        and evidence["user_trusted_input"]
                        and evidence["artifact_id8"]
                        == read_sc7(cache).artifact_id8.hex()
                        and evidence["public_record_checksum_checks"]
                        == evidence["guest_hash_checks"]
                        == 0
                    ):
                        raise RuntimeError(f"{directory}: private region identity")
                    problems = counter_problems(evidence)
                    if problems:
                        raise RuntimeError(f"{directory}: v7 counters {problems}")
            else:
                replay(directory, sample["words"], "warmup")

    (out / "identities.json").write_text(json.dumps(identities, indent=2) + "\n")

    def run(name, case, label):
        sample = cases[case]
        spec = arms[name]
        if "stock" not in spec:
            return replay(out / name / case, sample["words"], label)
        packet = plan["application"] == "packet"
        result = stock.run(
            spec["stock"],
            sample["input"],
            icount=True,
            expect_ns=calibration.get((name, case)),
            result=("xStats", 20 if packet else 16),
        )
        mismatch = (
            stats_mismatches(result["stats"], sample["words"])
            if packet
            else result["stats"] != sample["words"]
        )
        if mismatch:
            raise RuntimeError(f"stock {case}: {mismatch}")
        return result["ns"]

    for name, spec in arms.items():
        if "stock" in spec:
            for case in cases:
                calibration[name, case] = run(name, case, "warmup")
    times = {}
    with (out / "observations.jsonl").open("w") as fp:
        for block in range(plan["blocks"]):
            order = [(name, case) for name in arms for case in cases]
            rng.shuffle(order)
            for name, case in order:
                ns = run(name, case, f"block-{block}")
                if name in caches:
                    path, h, size = caches[name]
                    if path.stat().st_size != size or header(path) != h:
                        raise RuntimeError(f"{name}: cache changed during replay")
                times[block, name, case] = ns
                print(
                    json.dumps(dict(block=block, arm=name, case=case, ns=ns)),
                    file=fp,
                    flush=True,
                )
    blocks = range(plan["blocks"])
    by_case = {
        case: {
            a: statistics.median(times[b, a, case] for b in blocks) / 1e6 for a in arms
        }
        for case in cases
    }
    ratios = {}
    for ref, arm in plan["comparisons"]:
        values = [
            sum(times[b, ref, c] for c in cases) / sum(times[b, arm, c] for c in cases)
            for b in blocks
        ]
        boot = sorted(
            statistics.median(rng.choices(values, k=len(values))) for _ in range(10000)
        )
        ratios[f"{ref}/{arm}"] = dict(
            median=statistics.median(values),
            interval=[boot[250], boot[9750]],
            blocks=values,
        )
    report = dict(
        case_ms=by_case,
        mean_median_ms={
            a: statistics.mean(v[a] for v in by_case.values()) for a in arms
        },
        paired=ratios,
        input_sha256={c: digest(x["input"]) for c, x in cases.items()},
    )
    (out / "summary.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()

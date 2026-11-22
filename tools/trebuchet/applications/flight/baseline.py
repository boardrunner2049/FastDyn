#!/usr/bin/env python3
"""Qualify the checked-in ArduCopter or PX4 mission on its matched FMI plant."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import tomllib
import time

from oracle import validate
from elftools.elf.elffile import ELFFile
from fastdyn import fmu_build, fmu_runtime

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
COURBET = ROOT / "virtuals/physics/flight_controllers/courbet"
QEMU = ROOT / "third_party/common/qemu/build/qemu-system-arm"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def pin(path, expected):
    actual = digest(path)
    if actual != expected:
        raise ValueError(f"{path}: expected {expected}, received {actual}")
    return actual


def replace_one(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"unsupported config, expected one occurrence of {old!r}")
    return text.replace(old, new)


def freeze_inventory(cache, sidecar, work):
    """Seal the inventory before observing any replay, preserving sparse holes."""
    frozen = work / "frozen"
    frozen.mkdir()
    copy = frozen / cache.name
    subprocess.run(["cp", "--reflink=auto", "--sparse=always", str(cache), str(copy)], check=True)
    shutil.copyfile(sidecar, frozen / sidecar.name)
    identity = {"cache_sha256": digest(copy), "logical_bytes": copy.stat().st_size,
                "fastdyn_manifest_sha256": digest(frozen / sidecar.name)}
    if digest(cache) != identity["cache_sha256"] or digest(sidecar) != identity["fastdyn_manifest_sha256"]:
        raise ValueError("deployment does not match frozen preparation")
    for path in (copy, frozen / sidecar.name):
        path.chmod(0o444)
    (frozen / "identity.json").write_text(json.dumps(identity, indent=2) + "\n")
    return identity


def checkpoint(work, receipt):
    dest = work / "receipt.json"
    with tempfile.NamedTemporaryFile("w", dir=work, delete=False) as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    os.replace(stream.name, dest)


def run(args, receipt):
    manifest = json.loads((HERE / "manifest.json").read_text())
    family = manifest["families"][args.family]
    mission = ROOT / manifest["mission"]
    inputs = {key: pin(ROOT / family[key], family[key + "_sha256"])
              for key in ("elf", "config", "params", "profile")}
    inputs["mission"] = pin(mission, manifest["mission_sha256"])
    receipt.update(family=args.family, scenario=manifest["scenario"], role=manifest["role"],
                   source_revision=family["revision"], inputs=inputs,
                   fastdyn_revision=subprocess.check_output(
                       ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip())
    work = args.work.resolve()
    hooks = work / "hooks"
    image = ROOT / family["elf"]
    if args.release_pair:
        from release.compare import inspect, same_load, symbol_delta

        directory = args.release_pair.resolve()
        comparison = json.loads((directory / "comparison.json").read_text())
        image = directory / "relocs.elf"
        normal = inspect(directory / "normal.elf")
        relocated = inspect(image)
        if (comparison["source_revision"] != family["revision"] or comparison["family"] != args.family
                or comparison["images"]["reference"]["sha256"] != inputs["elf"]
                or comparison["images"]["normal"] != normal or comparison["images"]["relocs"] != relocated
                or not same_load(normal, relocated) or not relocated["code_relocations"]
                or not symbol_delta(directory / "normal.elf", image)["preserved_with_only_local_aliases"]):
            raise ValueError("release pair does not match the named source, layout, symbols and retained relocations")
        inputs["shipped_reference_elf"] = inputs["elf"]
        inputs["elf"] = relocated["sha256"]
        receipt["release_pair"] = {"normal_sha256": normal["sha256"],
                                   "comparison_sha256": digest(directory / "comparison.json")}
    def prepare(elf_path, dest):
        subprocess.run([sys.executable, str(COURBET / "prepare_execution_configuration.py"),
                        "--elf", str(elf_path), "--profile", str(ROOT / family["profile"]),
                        "--output-dir", str(dest)], cwd=ROOT, check=True)

    if args.candidate_elf:
        from release.px4_placement import examine, verify_layout

        parent = image
        source_hooks = work / "source-hooks"
        prepare(parent, source_hooks)
        image = args.candidate_elf.resolve(strict=True)
        prepare(image, hooks)
        check = examine(parent, image, source_hooks, hooks)
        receipt["candidate"] = {"layout_sha256": digest(args.candidate_layout), "placement": check}
        if not check["admissible"]:
            raise ValueError("candidate fails PX4 board placement or hook contract")
        receipt["candidate"]["moves"] = verify_layout(parent, image, args.candidate_layout)
        inputs["parent_elf"], inputs["elf"] = inputs["elf"], digest(image)
    else:
        prepare(image, hooks)
    receipt["hook_files"] = {p.name: digest(p) for p in sorted(hooks.iterdir()) if p.is_file()}
    with image.open("rb") as stream:
        elf = ELFFile(stream)
        # .ARM.exidx and debug relocations alone cannot relocate executable text.
        text_relocations = [section.name for section in elf.iter_sections()
                            if section["sh_type"] in ("SHT_REL", "SHT_RELA")
                            and elf.get_section(section["sh_info"])["sh_flags"] & 4]
        receipt["elf_metadata"] = {"machine": elf["e_machine"], "class": elf.elfclass,
                                   "hard_float_abi": bool(elf["e_flags"] & 0x400),
                                   "has_symbols": elf.get_section_by_name(".symtab") is not None,
                                   "has_dwarf": elf.get_section_by_name(".debug_info") is not None,
                                   "code_relocation_sections": text_relocations}
    receipt["profile_validated"] = True
    if args.step == "inspect":
        return

    if shutil.which("lsof") is None:
        raise ValueError("lsof is required for FastDyn to shut QEMU down on mission completion")
    dependencies = {}
    for directory, expected in manifest["simulation_dependencies"].items():
        if args.family == "ardupilot" and directory in (
                "third_party/courbet_deps/fire_modelica_models", "out/toolchains/ModelicaStandardLibrary"):
            continue
        actual = subprocess.check_output(["git", "-C", str(ROOT / directory),
                                          "rev-parse", "HEAD"], text=True).strip()
        if actual != expected:
            raise ValueError(f"{directory}: expected {expected}, received {actual}")
        dependencies[directory] = actual
    receipt["simulation_dependencies"] = dependencies
    receipt["runtime"] = {name: digest(path) for name, path in
                          (("plugin", args.plugin.resolve()), ("qemu", QEMU), ("plant", ROOT / family["plant"]))}
    config = (ROOT / family["config"]).read_text()
    if args.release_pair:
        config = replace_one(config, json.dumps(family["elf"]), json.dumps(str(image)))
    config = replace_one(config, '"build/libfastdyn.so"', json.dumps(str(args.plugin.resolve())))
    original = tomllib.loads(config)
    hook_path = original["CPU"]["cpu0"][0]["existing_config_path"]
    config = replace_one(config, json.dumps(hook_path), json.dumps(str(hooks)))
    if args.family == "ardupilot":
        for name in ("my_m4_ram", "my_m4_ram3"):
            config = replace_one(config, f'"../qemu/ws/{name}"', json.dumps(str(work / name)))
        # MAVProxy expands the child work directory in the original PX4 command.
        # The helper terminates FastDyn at its last telemetry event. Flush the
        # diagnostic tlog per packet so this observed event survives shutdown.
        launcher = ['sh', '-c', 'exec mavproxy.py --logfile="$FASTDYN_WORK_DIR/mav.tlog" "$@"',
                    'copter-mavproxy', '--cmd=set flushlogs True']
        config = replace_one(config, '"mavproxy.py",', ", ".join(map(json.dumps, launcher)) + ",")
    if args.px4_tlog == "flush-each-packet-v2":
        # The helper can receive the disarm heartbeat and stop FastDyn before
        # MAVProxy's periodic flush. Keep the historical protocol selectable.
        config = replace_one(config, '"px4-mavproxy",',
                             '"px4-mavproxy", "--cmd=set flushlogs True",')
    if args.family == "px4":
        receipt["runtime"]["px4_tlog_contract"] = args.px4_tlog
    runtime = work / "runtime.toml"
    runtime.write_text(config)
    receipt["runtime"]["launch_config"] = digest(runtime)
    # FastDyn normally freshly unpacks a packaged FMU on each launch. Reuse
    # the exact extracted package for this comparison so the existing cache
    # check sees the same native library path and mtime across cold/warm runs.
    build = fmu_build.resolve(ROOT / family["config"], repo_root=ROOT)
    if not build.package or build.fmu_path.resolve() != (ROOT / family["plant"]).resolve():
        raise ValueError("an inherited FMU override conflicts with the pinned case")
    extracted = fmu_runtime.prepare_runtime(build, work / "plant")
    plant_root = extracted.library_path.parents[2]
    receipt["runtime"]["plant_library"] = digest(extracted.library_path)
    receipt["runtime"]["plant_description"] = digest(plant_root / "modelDescription.xml")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FASTDYN_", "QEMU_SC_"))}
    env.update(FASTDYN_QEMU=str(QEMU), FASTDYN_MISSION_FILE=str(mission),
               FASTDYN_PARAM_FILE=str(ROOT / family["params"]), FASTDYN_FMI_BUILD="false",
               FASTDYN_FMI_OUTPUT=str(plant_root))
    if args.training_profiler:
        profiler = args.training_profiler.resolve(strict=True)
        output = work / "session.tbprof"
        wrapper = work / "profile-qemu"
        option = f"{profiler},out={output}"
        if "," in str(profiler) or "," in str(output):
            raise ValueError("commas are unsupported in QEMU plugin and profile paths")
        wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(str(QEMU))} "$@" -plugin {shlex.quote(option)}\n')
        wrapper.chmod(0o755)
        env["FASTDYN_QEMU"] = str(wrapper)
        receipt["training_profiler"] = {"plugin_sha256": digest(profiler), "launch_sha256": digest(wrapper)}
    env["PATH"] = str(ROOT / "fastdyn-env/bin") + os.pathsep + env["PATH"]
    receipt["cold"] = launch(args, manifest, mission, work / "cold", runtime, env)
    if args.training_profiler:
        output = work / "session.tbprof"
        if not output.is_file() or not output.stat().st_size:
            raise ValueError("training plugin returned no profile")
        receipt["training_profiler"]["profile_sha256"] = digest(output)
        if args.candidate_elf:
            from release.px4_placement import executed
            receipt["candidate"]["execution"] = executed(output, receipt["candidate"]["moves"])
            receipt["transformed_deployment"] = True
    receipt["operational_baseline"] = True
    checkpoint(work, receipt)
    if not (args.ordinary_warm or args.frozen_preparation or args.scopt_training):
        return
    # Import only for the optional warm proof; the source is the same pinned checkout.
    sys.path.insert(0, str(ROOT / "tools/scopt"))
    from scopt.run.proof import deployed_state, prove

    cache = work / "mission.sc"
    sidecar = work / "mission.sc.fastdyn.json"
    warm_config = work / "cache.toml"
    warm_config.write_text(replace_one(config, "[Machine]", "[Machine]\nsupercache = "
                                       + json.dumps(str(cache)) + "\nsupercache_flat = false"))
    receipt["runtime"]["cache_config"] = digest(warm_config)
    if args.scopt_training:
        from release.scopt_training import train
        train(args, receipt, work, manifest, mission, warm_config, env, cache, sidecar,
              launch=launch, freeze=freeze_inventory, checkpoint=checkpoint, qemu=QEMU)
        return
    receipt["warm_attempt_budget"] = args.warm_attempts if args.ordinary_warm else 1
    checkpoint(work, receipt)
    receipt["capture"] = launch(args, manifest, mission, work / "capture", warm_config, env, no_aslr=True)
    if args.frozen_preparation:
        receipt["frozen_preparation"] = {
            "identity": freeze_inventory(cache, sidecar, work),
            "population_scenario": receipt["scenario"], "evaluation_scenario": receipt["scenario"],
            "previously_unseen_input": False, "mission_sha256": receipt["inputs"]["mission"],
            "qualified": False,
        }
    receipt["warm_attempts"] = []
    # One population pass followed by a predeclared number of checks. Appends are
    # recorded; only a proof with zero appends establishes a fully warm mission.
    checkpoint(work, receipt)
    for i in range(receipt["warm_attempt_budget"]):
        state = deployed_state(cache, sidecar)
        if not state["n"] or state["n"] != state["header"]["record_count"]:
            raise ValueError("empty or incomplete captured record inventory")
        attempt = {"deployed_state": state}
        receipt["warm_attempts"].append(attempt)
        checkpoint(work, receipt)
        try:
            attempt["mission"] = launch(args, manifest, mission, work / f"replay-{i + 1}",
                                        warm_config, env, no_aslr=True)
        finally:
            proof = prove(state)
            attempt["proof"] = proof
            checkpoint(work, receipt)
        if proof["verdict"] != "PASS":
            raise ValueError(f"cache artifact was not used: {proof['failed']}")
        if args.frozen_preparation and i == 0:
            frozen = receipt["frozen_preparation"]
            if digest(work / "frozen" / cache.name) != frozen["identity"]["cache_sha256"]:
                raise ValueError("sealed inventory changed during replay")
            frozen.update(qualified=True, replay="replay-1", initial_records=state["n"],
                          final_records=proof["record_count_after"],
                          appended_records=proof["appended"], post_cache_sha256=digest(cache))
            checkpoint(work, receipt)
            if not args.ordinary_warm:
                return
        if proof["appended"] == 0:
            receipt["ordinary_warm"] = True
            return
    raise ValueError(f"warm mission still appends blocks after {args.warm_attempts} checked replays")


def launch(args, manifest, mission, work, runtime, env, no_aslr=False):
    work.mkdir()
    env = dict(env, FASTDYN_FMU_PLANT_TRACE=str(work / "plant.csv"))
    if args.measure_cpu:
        env["FASTDYN_FMU_CPU_MARKS"] = str(work / "cpu-marks.csv")
    prefix = ["setarch", "x86_64", "-R"] if no_aslr or args.measure_cpu else []
    with (work / "console.log").open("x") as log:
        started = time.monotonic()
        process = subprocess.Popen([*prefix, str(ROOT / "fastdyn-env/bin/fastdyn"), "run", "-c", str(runtime),
                                    "--no-build-fmu", "-o", str(work / "run")], env=env, cwd=ROOT,
                                   start_new_session=True, stdout=log, stderr=subprocess.STDOUT)
        try:
            rc = process.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
    wall = time.monotonic() - started
    if rc:
        raise ValueError(f"{work.name}: mission process exited {rc}")
    tlog = work / "run/mav.tlog"
    result = validate(args.family, mission, manifest, work / "plant.csv", tlog,
                      (work / "console.log").read_text(errors="replace"))
    outputs = {str(p.relative_to(work)): digest(p)
               for p in (work / "console.log", work / "plant.csv", tlog)}
    outcome = {"returncode": rc, "oracle": result, "raw_outputs": outputs}
    if args.measure_cpu:
        marks = work / "cpu-marks.csv"
        rows = [[float(v) for v in line.split(",")] for line in marks.read_text().splitlines()]
        if (len(rows) != 2 or rows[0][0] != 0.0 or not 44.999999 <= rows[1][0] <= 45.002001
                or rows[1][1] <= rows[0][1]):
            raise ValueError(f"invalid FMI CPU measurement {rows}")
        outcome["process_cpu_seconds_at_45s"] = rows[1][1] - rows[0][1]
        outcome["cpu_progress_end_s"] = rows[1][0]
        outcome["full_fastdyn_wall_seconds"] = wall
        outputs[marks.name] = digest(marks)
    return outcome


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=["inspect", "run"])
    parser.add_argument("family", choices=["ardupilot", "px4"])
    parser.add_argument("work", type=Path, help="new output directory")
    parser.add_argument("--plugin", type=Path, default=ROOT / "build/libfastdyn.so")
    parser.add_argument("--release-pair", type=Path, help="directory with checked normal.elf, relocs.elf and comparison.json")
    parser.add_argument("--candidate-elf", type=Path, help="FirmBOLT deployment to check against the PX4 release pair")
    parser.add_argument("--candidate-layout", type=Path, help="linker-resolved layout report for --candidate-elf")
    parser.add_argument("--training-profiler", type=Path, help="run a diagnostic mission with a FirmBOLT TB profiler plugin")
    parser.add_argument("--timeout", type=float, default=420)
    parser.add_argument("--px4-tlog", choices=("buffered-v1", "flush-each-packet-v2"), default="buffered-v1",
                        help="PX4 telemetry recorder contract (default reproduces the original timing campaign)")
    parser.add_argument("--measure-cpu", action="store_true", help="collect QEMU CPU through 45 simulated seconds under fixed ASLR")
    parser.add_argument("--ordinary-warm", action="store_true", help="qualify cache capture and replay after cold success")
    parser.add_argument("--warm-attempts", type=int, default=2, metavar="1..16",
                        help="maximum checked replays after population (default 2)")
    parser.add_argument("--frozen-preparation", action="store_true",
                        help="seal the capture and qualify its first replay with exact late-translation counts")
    parser.add_argument("--scopt-training", action="store_true",
                        help="qualify an instrumented release PX4 training replay and one host rewrite")
    args = parser.parse_args()
    if args.scopt_training and (args.step != "run" or args.family != "px4" or not args.release_pair
                                or args.training_profiler or args.measure_cpu or args.ordinary_warm
                                or args.frozen_preparation):
        parser.error("scopt-training requires a separate PX4 release run")
    if bool(args.candidate_elf) != bool(args.candidate_layout) or (args.candidate_elf and
                                                                  (not args.release_pair or args.family != "px4")):
        parser.error("candidate-elf and candidate-layout require together a PX4 release pair")
    if args.px4_tlog != "buffered-v1" and args.family != "px4":
        parser.error("px4-tlog applies only to the PX4 slice")
    if args.measure_cpu and (args.step != "run" or args.training_profiler or args.ordinary_warm or args.frozen_preparation):
        parser.error("measure-cpu requires an uninstrumented single cacheless mission")
    if args.training_profiler and (args.step != "run" or args.ordinary_warm or args.frozen_preparation):
        parser.error("training-profiler requires a single uncached diagnostic run")
    if not 1 <= args.warm_attempts <= 16:
        parser.error("warm-attempts must be from 1 through 16")
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    args.work.mkdir(parents=True, exist_ok=False)
    receipt = {"profile_validated": False, "operational_baseline": False,
               "transformed_deployment": False, "ordinary_warm": False, "state": "running"}
    dest = args.work / "receipt.json"
    dest.write_text(json.dumps(receipt) + "\n")
    try:
        run(args, receipt)
        receipt["state"] = "complete"
    except Exception as error:
        # Preserve the negative result even for an unexpected telemetry/parser error.
        receipt.update(state="failed", error_type=type(error).__name__, error=str(error))
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
    checkpoint(args.work, receipt)
    print(dest)
    return int(receipt["state"] != "complete")


if __name__ == "__main__":
    sys.exit(main())

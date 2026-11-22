"""Collect and qualify one SCM/QPROF training pair on the release PX4 mission."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from scopt.run.proof import deployed_state, prove


def write(path, doc):
    path.write_text(json.dumps(doc, indent=2) + "\n")


def train(args, receipt, work, manifest, mission, config, env, cache, sidecar, *,
          launch, freeze, checkpoint, qemu):
    report = {"role": "training", "phase_symbol": "_ZN9Commander3runEv", "recipe": "compact-nops"}
    receipt["scopt_training"] = report

    def stage(name):
        report["stage"] = name
        checkpoint(work, receipt)

    def run(name, extra):
        return launch(args, manifest, mission, work / name, config, dict(env, **extra), no_aslr=True)

    def replay(name, extra):
        state = deployed_state(cache, sidecar)
        result = report[name] = {"deployed_state": state}
        try:
            result["mission"] = run(name, extra)
        finally:
            result["proof"] = prove(state)
            checkpoint(work, receipt)
        if result["proof"]["verdict"] != "PASS":
            raise ValueError(f"{name}: cache deployment failed: {result['proof']['failed']}")

    stage("SCM population")
    scm = work / "training.scm"
    report["population"] = run("population", {"QEMU_SC_CAPTURE": str(scm)})
    report["frozen"] = freeze(cache, sidecar, work)
    frozen = work / "frozen" / cache.name
    captured_manifest = work / "frozen" / sidecar.name
    nm = subprocess.check_output(["arm-none-eabi-nm", "-P", str(args.candidate_elf or
                                 args.release_pair / "relocs.elf")], text=True)
    pc, = [int(row.split()[2], 16) & ~1 for row in nm.splitlines()
           if row.split()[:2] == [report["phase_symbol"], "T"]]
    report["phase_pc"] = hex(pc)
    qprof = work / "training.qprof"
    stage("QPROF replay")
    replay("profiled", {"QEMU_SC_PROFILE": str(qprof), "QEMU_SC_PROFILE_PHASES": f"steady@{pc:#x}"})
    stage("profile admission")
    python = Path(__file__).resolve().parents[5] / "scopt-env/bin/python"
    try:
        subprocess.run([str(python), str(Path(__file__).resolve()), str(work), str(qemu),
                        str(args.plugin.resolve()), str(mission), receipt["inputs"]["mission"]], check=True)
    finally:
        offline = work / "training-admission.json"
        if offline.is_file():
            report.update(json.loads(offline.read_text()))
        checkpoint(work, receipt)
    candidate = work / "compacted"
    stage("rewritten uninstrumented mission")
    subprocess.run(["cp", "--sparse=always", str(candidate / "cache.sc"), str(cache)], check=True)
    shutil.copyfile(captured_manifest, sidecar)
    replay("transformed", {})
    stage("qualified")

def admit(work, qemu, plugin, mission, mission_hash):
    from scopt.commands.compact_nops import generate
    from scopt.formats.bundle_v1 import identity_bundle, run_info_fields, runtime_identity
    from scopt.formats.profile_v1 import check_flow, ingest
    from scopt.formats.qprof_v1 import read_qprof
    from scopt.formats.sc_v5 import read_sc
    from scopt.formats.scm_v1 import read_scm
    from scopt.importer.import_cache import import_snapshot
    report = {}
    cache = work / "mission.sc"
    frozen = work / "frozen/mission.sc"
    captured_manifest = work / "frozen/mission.sc.fastdyn.json"
    scm, qprof, config = work / "training.scm", work / "training.qprof", work / "cache.toml"
    def stage(name):
        report["stage"] = name
        write(work / "training-admission.json", report)
    try:
        for key, path in (("capture", scm), ("qprof", qprof)):
            with path.open("rb") as stream:
                report[key] = {"sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
                               "bytes": path.stat().st_size}
        header = read_qprof(qprof).header
        report["profile_header"] = {k: getattr(header, k) for k in (
            "loaded_record_count", "loaded_header_checksum", "clean_exit", "tb_overflow", "edge_overflow")}
        stage("profile admission")
        bundle = identity_bundle(config, captured_manifest)
        captured = read_scm(scm)
        if not captured.clean or captured.incomplete or captured.run_info is None:
            raise ValueError("population SCM is incomplete or lacks clean RUN_INFO")
        ri = run_info_fields(captured.run_info)
        report["captured_tbs"] = len(captured.tbs)
        del captured
        sc = read_sc(frozen)
        runtime = runtime_identity(qemu=qemu, plugin=plugin.resolve(), config_text=config.read_text(),
                                   sc_header=sc.header, run_info=ri, fastdyn_src=Path(__file__).resolve().parents[5])
        write(work / "bundle.json", bundle)
        write(work / "runtime.json", runtime)
        profile = ingest(qprof, cache, sc, bundle_id=bundle["bundle_id"], contract_id=bundle["contract_id"],
                         runtime_identity=runtime, inputs=[{"name": mission.name, "split": "training",
                                                             "sha256": "sha256:" + mission_hash}])
        (work / "profile.json").write_bytes(profile.to_bytes())
        report["profile_id"] = profile.profile_id
        report["flow"] = check_flow(profile).to_json()
        if not report["flow"]["ok"] or not report["flow"]["exact"]:
            raise ValueError("host rewrite requires complete exact profile flow")
        del sc, profile
        stage("snapshot import")
        result = import_snapshot(sc=str(frozen), scms=[str(scm)], manifest=str(captured_manifest),
                                 bundle=bundle, runtime_identity=runtime, store=work / "store")
        snapshot = Path(result.store_dir)
        del result
        stage("host rewrite")
        candidate = work / "compacted"
        report["rewrite"] = generate(snapshot, work / "profile.json", candidate)
        stage("host rewrite generated")
    except Exception as error:
        report.update(error_type=type(error).__name__, error=str(error))
        write(work / "training-admission.json", report)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("work", "qemu", "plugin", "mission"):
        parser.add_argument(name, type=Path)
    parser.add_argument("mission_hash")
    admit(**vars(parser.parse_args()))

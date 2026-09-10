#!/usr/bin/env python3
"""Compile and fly seeded payload-weight variants with fixed vehicle properties and gains."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tomllib

import numpy as np
import tomli_w

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from fastdyn.mission_report import log_messages  # noqa: E402
from experiment_process import ExperimentProcess  # noqa: E402
from monte_carlo_report import collect, write_report  # noqa: E402


def flight_metrics(path, limits):
    armed, airborne, ever_armed = False, False, False
    tilt_start = None
    max_tilt, max_alt, excessive = 0.0, 0.0, False
    max_armed_tilt = None
    samples = 0
    if not path.exists():
        return {"armed": False, "airborne": False, "attitude_samples": 0}, None
    for msg in log_messages(path):
        if msg.get_srcSystem() != 1:
            continue
        kind = msg.get_type()
        if kind == "HEARTBEAT":
            armed = bool(msg.base_mode & 128)
            ever_armed = ever_armed or armed
        elif kind == "GLOBAL_POSITION_INT" and armed:
            altitude = msg.relative_alt / 1000
            max_alt = max(max_alt, altitude)
            airborne = airborne or altitude > 2
        elif kind == "ATTITUDE" and armed:
            tilt = math.degrees(math.acos(max(-1, min(1, math.cos(msg.roll)*math.cos(msg.pitch)))))
            max_armed_tilt = max(max_armed_tilt or 0.0, tilt)
            if not airborne:
                continue
            samples += 1
            max_tilt = max(max_tilt, tilt)
            now = msg.time_boot_ms / 1000
            if tilt > limits["max_tilt_deg"]:
                if tilt_start is None:
                    tilt_start = now
                excessive = excessive or now - tilt_start >= limits["tilt_duration_s"]
            else:
                tilt_start = None
    reason = None
    if excessive:
        reason = f"Tilt exceeded {limits['max_tilt_deg']:g} degrees for {limits['tilt_duration_s']:g} s"
    elif max_alt > limits["max_altitude_m"]:
        reason = f"Altitude exceeded {limits['max_altitude_m']:g} m"
    return {"armed": armed, "ever_armed": ever_armed, "airborne": airborne, "attitude_samples": samples,
            "max_tilt_deg": max_tilt, "max_altitude_m": max_alt,
            **({"max_armed_tilt_deg": max_armed_tilt} if max_armed_tilt is not None else {})}, reason


def setup_sources(settings, output):
    """Snapshot the same Modelica sources used by the preceding load exercise."""
    source = output / "modelica/FastDyn"
    shutil.copytree(ROOT / "modelica/FastDyn", source, dirs_exist_ok=True)
    library = output / "library"
    shutil.copytree(ROOT / "third_party/common/modelica_models", library,
                    ignore=shutil.ignore_patterns(".git", "artifacts", "__pycache__"),
                    dirs_exist_ok=True)
    return source.parent, library


def run_trial(name, payload_mass, base, settings, output, sources, compiler):
    directory = output / name
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "PayloadTrial.mo"
    attachment = ", ".join(format(v, ".17g") for v in settings["study"]["attachment_b_m"])
    source.write_text(f"""within FastDyn;
model PayloadTrial
  import Vehicles;
  import Geodesy;
  import RigidBody;
  extends QavrSidePayload(
    bare_mass={settings["study"]["vehicle_mass_kg"]:.17g},
    payload_mass={payload_mass:.17g},
    attachment_b={{{attachment}}});
end PayloadTrial;
""")
    result = {"id": name, "payload_mass_kg": payload_mass,
              "log": str(directory / "mission.tlog"), "console": str(directory / "console.log"),
              "model_file": str(source), "status": "run_error", "reason": ""}
    try:
        with (directory / "build.log").open("w") as build_log:
            subprocess.run([compiler, "compile", str(source), "--model", "FastDyn.PayloadTrial",
                            "--source-root", str(sources[0]), "--source-root", str(sources[1]),
                            "--target", "fmi3", "--output", str(directory / "fmu")],
                           stdout=build_log, stderr=subprocess.STDOUT, check=True, timeout=120)
        fmu = directory / "fmu/FastDyn_PayloadTrial.fmu"
        result["fmu_sha256"] = hashlib.sha256(fmu.read_bytes()).hexdigest()
        config = copy.deepcopy(base)
        config["FMU"]["auto_build"] = False
        model = config["FMU"]["models"][config["FMU"]["active"]]
        model.update(model="FastDyn.PayloadTrial", model_file=str(source),
                     source_roots=[str(path) for path in sources], output=str(directory / "fmu"))
        # Vehicle and load defaults are compiled into each variant. Retain only
        # the geographic origin from the base; never overwrite the QAV-R with
        # the baseline's mass, motor, or inertia parameter overrides.
        model["parameters"] = {key: value for key, value in model.get("parameters", {}).items()
                               if key in {"lat0", "lon0", "ground_alt_wgs84"}}
        config["Machine"].update(monitor_port=settings["execution"]["monitor_port"],
                                 qmp_socket=str(directory.resolve() / "qmp.sock"),
                                 log_file=str(directory / "qemu.log"))
        for group in config["Memory"].values():
            for bank in group if isinstance(group, list) else [group]:
                if bank.get("backend") == "file":
                    bank["memory_file"] = str(directory / (bank["id"] + ".ram"))
        proxy = config["Run"]["processes"]["mavproxy"]
        proxy["command"] = [f"--logfile={directory.resolve() / 'mission.tlog'}"
                            if arg.startswith("--logfile=") else arg for arg in proxy["command"]]
        mission = config["Run"]["processes"]["mission"]["command"]
        mission[0] = sys.executable
        params = directory / "controller.param"
        params.write_text("".join(f"{key} {value}\n" for key, value in settings["parameters"].items()))
        mission[-2] = str(params)
        mission[-1] = settings["study"]["mission"]
        config_path = directory / "fastdyn.toml"
        config_path.write_text(tomli_w.dumps(config))
        command = [sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); from fastdyn.main import cli; cli()",
                   "run", "-c", str(config_path), "-o", str(directory / "work")]
        started = time.monotonic()
        with Path(result["console"]).open("w") as console:
            process = ExperimentProcess(command, cwd=ROOT, stdout=console, stderr=subprocess.STDOUT)
            try:
                while time.monotonic() - started < settings["execution"]["wall_timeout_s"]:
                    process.poll()
                    text = Path(result["console"]).read_text(errors="replace")
                    metrics, failure = flight_metrics(Path(result["log"]), settings["execution"])
                    result["metrics"] = metrics
                    if failure:
                        result.update(status="flight_failure", reason=failure)
                        break
                    if "[mission] final landing confirmed near ground" in text:
                        result.update(status="reference" if name == "reference" else "passed",
                                      reason="Final mission item and landing confirmed")
                        break
                    if "Crash: Disarming" in text:
                        result.update(status="flight_failure", reason="Firmware crash check disarmed the vehicle")
                        break
                    if process.poll() is not None or "helper.mission.exited exit_code=" in text:
                        result.update(status="mission_incomplete" if metrics.get("ever_armed") else "run_error",
                                      reason="Simulation or mission helper exited before completion")
                        break
                    time.sleep(2)
                else:
                    result.update(status="mission_incomplete" if result.get("metrics", {}).get("ever_armed") else "run_error",
                                  reason="Wall-clock deadline reached before mission completion")
            finally:
                process.stop()
        result["wall_time_s"] = time.monotonic() - started
        result["metrics"], _ = flight_metrics(Path(result["log"]), settings["execution"])
    except (subprocess.SubprocessError, OSError) as error:
        result.update(status="run_error", reason=str(error))
    (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/monte-carlo/payload.toml"))
    parser.add_argument("--run-config", type=Path, required=True)
    parser.add_argument("--compiler", default="rumoca")
    parser.add_argument("--limit", type=int, help="Run only the first N entries, including the reference")
    args = parser.parse_args()
    settings = tomllib.loads(args.config.read_text())
    base = tomllib.loads(args.run_config.read_text())
    version = subprocess.check_output([args.compiler, "--version"], text=True).strip()
    if settings["study"]["rumoca_version"] not in version:
        raise ValueError(f"This study requires Rumoca {settings['study']['rumoca_version']}; got {version}")
    output = Path(settings["execution"]["output"])
    output.mkdir(parents=True, exist_ok=True)
    compiler_path = Path(shutil.which(args.compiler) or args.compiler).resolve()
    identity = hashlib.sha256(json.dumps({"settings": settings, "base": base}, sort_keys=True).encode())
    for source in (Path(__file__), ROOT / "utils/experiment_process.py", compiler_path):
        identity.update(source.read_bytes())
    for tree in (ROOT / "modelica", ROOT / "third_party/common/modelica_models"):
        for source in sorted(tree.rglob("*.mo")):
            identity.update(str(source.relative_to(ROOT)).encode())
            identity.update(source.read_bytes())
    identity.update((ROOT / "virtuals/physics/flight_controllers/courbet/mavlink/mav_command_and_control.py").read_bytes())
    fingerprint = output / "experiment.sha256"
    if fingerprint.exists() and fingerprint.read_text().strip() != identity.hexdigest():
        raise ValueError("Experiment inputs changed; use a fresh output directory")
    fingerprint.write_text(identity.hexdigest() + "\n")
    (output / "study.toml").write_text(tomli_w.dumps(settings))
    sources = setup_sources(settings["study"], output)
    rng = np.random.default_rng(settings["study"]["seed"])
    study = settings["study"]
    ratio = study["max_payload_to_vehicle_mass_ratio"]
    if not math.isfinite(ratio) or ratio <= 0 or not math.isfinite(study["vehicle_mass_kg"]) or study["vehicle_mass_kg"] <= 0:
        raise ValueError("Payload-to-vehicle mass ratio and vehicle mass must be finite and positive")
    max_mass = study["vehicle_mass_kg"] * ratio
    masses = rng.uniform(0.0, max_mass, study["samples"])
    cases = [("reference", 0.0), ("payload-limit", max_mass),
             *[(f"run-{i:03d}", float(value)) for i, value in enumerate(masses)]]
    results = []
    for name, payload_mass in cases[:args.limit]:
        print(f"[study] {name}: payload {1000 * payload_mass:.1f} g", flush=True)
        result_file = output / name / "result.json"
        if result_file.exists():
            result = json.loads(result_file.read_text())
            if not math.isclose(result["payload_mass_kg"], payload_mass, rel_tol=1e-12):
                raise ValueError(f"Existing {name} has a different payload mass; choose a new output directory")
        else:
            result = run_trial(name, payload_mass, base, settings, output, sources, args.compiler)
        results.append(result)
        if result["status"] == "run_error":
            raise RuntimeError(f"{name} could not run: {result['reason']}; inspect {output / name / 'build.log'} and its console log")
        manifest = {"study": study, "mission": study["mission"], "runs": results}
        (output / "runs.toml").write_text(tomli_w.dumps(manifest))
        print(f"[study] {name}: {result['status']} — {result['reason']}", flush=True)
        if any(Path(run["log"]).exists() for run in results):
            data = collect(manifest)
            write_report(data, output / "report")
            if publish := settings["execution"].get("publish"):
                destination = Path(publish)
                destination.mkdir(parents=True, exist_ok=True)
                for filename in ("trajectories.png", "trajectories.svg", "trajectories.json"):
                    shutil.copyfile(output / "report" / filename, destination / filename)
                shutil.copyfile(output / "study.toml", destination / "study.toml")
                shutil.copyfile(settings["study"]["mission"], destination / "mission.waypoints")
                portable = copy.deepcopy(manifest)
                portable["mission"] = "mission.waypoints"
                for run in portable["runs"]:
                    for field in ("log", "console", "model_file"):
                        run[field] = f"{run['id']}/{Path(run[field]).name}"
                (destination / "runs.toml").write_text(tomli_w.dumps(portable))
                for filename in ("Copter.mo", "Qavr.mo", "QavrSidePayload.mo", "QuadrotorWithExternalWrench.mo", "package.mo"):
                    shutil.copyfile(sources[0] / "FastDyn" / filename, destination / filename)
                for filename in ("mission.tlog", "console.log", "PayloadTrial.mo", "controller.param"):
                    path = output / name / filename
                    if path.exists():
                        target = destination / name / filename
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, target)
                lines = [f"**{len(results)} of {len(cases)} runs completed** (including the nominal reference).", "",
                         "| Run | Payload mass (g) | Result | Peak armed tilt |", "| --- | ---: | --- | ---: |"]
                for run in results:
                    tilt = run.get("metrics", {}).get("max_armed_tilt_deg")
                    label = f"{tilt:.1f}°" if tilt is not None else "No flight data"
                    lines.append(f"| {run['id']} | {1000 * run['payload_mass_kg']:.1f} | {run['status'].replace('_', ' ')} | {label} |")
                (destination / "results.md").write_text("\n".join(lines) + "\n")
    print(f"Results: {output / 'runs.toml'}", flush=True)


if __name__ == "__main__":
    main()

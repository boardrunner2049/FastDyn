"""Foreground FastDyn helper. Snapshot the FMU layout and the uploaded mission."""

import argparse
import json
import os
import shutil
from pathlib import Path
from xml.etree import ElementTree

from fastdyn import fmu_build

TOOLS = Path(__file__).resolve().parent
MAVLINK = TOOLS.parents[1] / "virtuals/physics/flight_controllers/courbet/mavlink"


def prepare(vehicle, config, run):
    model = fmu_build.resolve(config)
    if model.model != f"FastDyn.{vehicle.title()}":
        raise ValueError(f"Expected {vehicle} in {config}, got {model.model}")
    xml = ElementTree.fromstring(fmu_build._model_description_xml(model))
    layout = {"vehicle": vehicle, "signals": [], "parameters": []}
    for var in xml.findall("./{*}ModelVariables/{*}Float64"):
        role = var.get("causality", "local")
        if role == "independent":
            continue
        dims = [int(dim.attrib["start"]) for dim in var.findall("{*}Dimension")]
        key = "parameters" if role in ("parameter", "calculatedParameter") else "signals"
        layout[key].append(
            {
                "name": var.attrib["name"],
                "vr": int(var.attrib["valueReference"]),
                "shape": dims,
                "unit": var.get("unit"),
            }
        )
    (run / "layout.json").write_text(json.dumps(layout, indent=2) + "\n")
    # OptiFuzz can supply its own trace path. Keep the same CSV accessible in the capture.
    trace = run / "trace.csv"
    if trace.is_symlink():
        trace.unlink()
    external = os.environ.get("FASTDYN_TRACE_FILE")
    if external and Path(external).resolve() != trace.resolve():
        trace.symlink_to(Path(external).resolve())
    proxy = run / "mavproxy"
    proxy.mkdir(exist_ok=True)
    shutil.copy2(MAVLINK / "fastdyn_cesium.py", proxy)
    if vehicle == "rover":
        mission, params = TOOLS / "rover_auto.waypoints", TOOLS / "rover_auto.params"
    else:
        mission, params = MAVLINK / "copter_mission.waypoints", MAVLINK / "copter_init.param"
    shutil.copyfile(os.environ.get("FASTDYN_MISSION_FILE") or mission, run / "mission.waypoints")
    shutil.copyfile(os.environ.get("FASTDYN_PARAM_FILE") or params, run / "mission.params")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vehicle", choices=("rover", "copter"))
    args = parser.parse_args()
    prepare(args.vehicle, Path(os.environ["FASTDYN_CONFIG"]), Path(os.environ["FASTDYN_WORK_DIR"]))

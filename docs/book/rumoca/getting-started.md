# Run your first mission

First choose [Nix, Docker, or a manual installation](../general/environment.md).
Run these commands inside that environment, from the repository root.

## Select the mission

This exercise uses the compiler and array-based model library pinned by the
checkout. The selected source is the same model you will read and edit in this
walkthrough.

```bash
fastdyn-config --base configs/copter462.toml --output out/copter.toml
```

Expect `Created out/copter.toml`. The generated TOML records tool locations, model
sources, separate RAM files, a QMP socket, and the mission log path.
Its `model_file` is `modelica/FastDyn/Copter.mo`; the FMU is
`out/fmi3/Copter/FastDyn_Copter.fmu`. Keep your settings in source TOML overlays,
because rerunning `fastdyn-config` replaces the generated run TOML.

## Fly the mission

```bash
fastdyn run -c out/copter.toml -o out/copter/work
```

The first run compiles the FMU. The mission helper loads the ArduPilot simulation
parameters, waits for GPS and the state estimator (EKF) to become ready, uploads
the waypoints, arms, flies, and exits after landing. Look for:

```text
[mission] ArduPilot ready with GPS and EKF initialized
[mission] armed
[mission] final landing confirmed near ground
```

Open **http://127.0.0.1:5000/mavcesium/** for the live map. In Docker, port 5000
must be published as shown in the setup chapter. Run one vehicle or experiment
at a time: these examples share MAVLink ports and the viewer port.

The MAVLink log is `out/copter/mission.tlog`. A successful mission reaches the
final waypoint **and** receives the firmware's on-ground report near the ground;
being below one meter while still descending is insufficient. An armed vehicle
or a moving map alone does not establish completion. Ctrl-C stops a run you want
to interrupt.

## Save and inspect the result

```bash
python -m fastdyn.mission_report --log out/copter/mission.tlog \
  --mission virtuals/physics/flight_controllers/courbet/mavlink/copter_mission.waypoints \
  --vehicle copter --output out/copter-summary.png
```

Expect trajectory and altitude plots plus machine-readable report files.
Compare them with [the recorded Copter result](mission-reports.md#arducopter).
Keep the TOML and telemetry together when comparing model or gain changes.

Regenerate the run TOML after changing tool builds. Keep your own settings in
a versioned [TOML overlay](configuration.md) and pass it with `--overlay`.
Next, [read the Modelica model](modelica.md) that produced this flight. For
additional vehicle examples, see [other models](running-models.md).

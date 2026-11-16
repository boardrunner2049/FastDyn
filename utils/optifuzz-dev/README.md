# FastDyn 4.6.2 mission capture and reports

Run the commands from the FastDyn repository root in the configured WSL/Linux environment. The two mission configs use the existing ArduPilot firmware, FMU build and MAVCesium setup.

## Prepare once

```bash
source fastdyn-env/bin/activate
ninja -C build libfastdyn_mission.so
python -m pip install matplotlib 'plotly>=6'
```

The mission recorder shares the standard plugin's objects and reads the selected FMU's Float64 layout at startup. The existing build must include FMU, flight controller and trace support. Re-run the Ninja command after changing plugin sources.

## Run a mission

Choose a fresh work directory for each capture. FastDyn normally clears an existing directory passed to `-o`. Run these missions separately when using the default network ports.

```bash
fastdyn run -c configs/rover462.mission.toml  -o out/rover-run-01
fastdyn run -c configs/copter462.mission.toml -o out/copter-run-01
```

The live viewer is at [MAVCesium](http://127.0.0.1:5000/mavcesium/) during the run. Rover uploads [the waypoint plan](rover_auto.waypoints) from [auto.py](auto.py) and drives WP5 and WP6 in reverse. Its left/right motor interface initializes at 1500 µs. Copter uses the existing ArduCopter mission helper and waypoint file. Both configs copy the uploaded plan and parameter file into the work directory. To choose other inputs, set `FASTDYN_MISSION_FILE` and `FASTDYN_PARAM_FILE` before the run.

The Rover FMU records wheel angular speeds. Its firmware wheel-encoder hook requests joint positions and skips the update when those positions are unavailable. A single warning during startup is expected.

## Run one Copter mission with STL monitoring

From the FastDyn root, use the configured virtual environment and the sibling Banquo checkout prepared by `setup.sh`. Its predicate-sign patch is needed for quantitative bounds with constants. The validated Rust toolchain is `nightly-2026-02-27` (install it once with `rustup toolchain install nightly-2026-02-27 --profile minimal` if needed).

```bash
source fastdyn-env/bin/activate
ninja -C build libfastdyn_mission.so
root=$PWD
crate="$root/virtuals/fuzzer/libafl_phi"
cargo +nightly-2026-02-27 build --locked --manifest-path "$crate/Cargo.toml" --bin baby_fuzzer
run=$(mktemp -d "$root/out/copter-optifuzz-once-XXXXXX")
cp "$crate/stl_formulas.txt" "$run/"
cd "$run"
export EXECUTION=0 FASTDYN_OPTIFUZZ_WORK_ROOT="$run"
export FASTDYN_OPTIFUZZ_SMOKE=1 FASTDYN_OPTIFUZZ_DRY_RUN=0 FASTDYN_OPTIFUZZ_SWARM_INSTANCES=1
export FASTDYN_OPTIFUZZ_BACKEND=fmuv3 FASTDYN_OPTIFUZZ_VEHICLE=copter
export FASTDYN_OPTIFUZZ_CONFIG=configs/copter462.mission.toml FASTDYN_OPTIFUZZ_VERBOSE=1
"$crate/target/debug/baby_fuzzer" > optifuzz.log 2>&1 && cat robustness_logs/robustness_0.csv
python "$root/utils/optifuzz-dev/check_copter_envelope.py" trace_logs/trace_0.csv robustness_logs/robustness_0.csv
```

Smoke mode runs one fixed midpoint input. The monitor loads the formula file in the run directory. It computes 3-D speed from the three NED velocity components and checks FRD roll and pitch and world-frame kinematic acceleration from startup through landing. A negative robustness marks a violation. The last log line is the minimum over the selected formulas, including earlier active requirements. The checker independently compares the six motion bounds against the raw CSV with an absolute tolerance of `1e-9`.

To re-evaluate the same recorded mission after editing the formulas, run the following in the same shell and run directory. The evaluator replaces the robustness log for that execution.

```bash
cp "$crate/stl_formulas.txt" .
"$crate/target/debug/baby_fuzzer" --evaluate-trace
```

## Build artifacts from saved data

After the run finishes, use its work directory as `--run`. You can repeat this step offline.

```bash
python utils/optifuzz-dev/artifacts.py rover  --run out/rover-run-01  --out artifacts/rover-run-01  --zip
python utils/optifuzz-dev/artifacts.py copter --run out/copter-run-01 --out artifacts/copter-run-01 --zip
```

Open **`fmu_explorer_en.html`** for the interactive ground track and FMU signal comparison. The PDF and `trajectory_en.png` show the mission and coordinate frames. The output directory also contains full-resolution `fmu_signals.npz`, the compressed OptiFuzz CSV, coordinate checks and mission metrics. Rover adds per-leg distances and motion-sign checks. Use `--baseline PATH` on the Rover command to compare a saved conventional-steering capture of the same route. `--zip` adds a shareable archive.

Keep `layout.json`, `signals.bin`, `parameters.bin`, `trace.csv`, `mission.waypoints`, `mission.params` and Rover's `events.csv` together. Keep `mavproxy/mav.tlog` for the firmware position overlay. Earlier capture folders using the bundled missions are also supported. A standalone OptiFuzz CSV contains the published outputs. The internal-variable plots also need the FMI binary captures.


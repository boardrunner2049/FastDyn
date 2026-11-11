# Betaflight Reference Flight Replay

FastDyn can keep a reference flight as small text files in GitHub and recreate
its browser replay or a shareable video later. The bundled reference is the
MATEKF405CTR hardware-sensor flight: 418 position/attitude samples over 69.1
seconds, three waypoints at 15 m, return, landing and disarm.

The Python entry point is
`virtuals/physics/flight_controllers/courbet/mavlink/replay_betaflight.py`.
It separates visual playback of saved telemetry from a new firmware mission.
Visual playback does not start QEMU or the FMU. A new mission uses the saved
configuration and route; host wall-clock RC scheduling means its trajectory can
differ from the reference.

## Files to keep in GitHub

The reference directory is
[`courbet/betaflight/matekf405ctr/reference_flight`](../virtuals/physics/flight_controllers/courbet/betaflight/matekf405ctr/reference_flight/manifest.json).
Its three files total approximately 90 KiB:

| File | Content |
| --- | --- |
| `flight.json` | Recorded firmware telemetry, timestamps, position, attitude, speeds and final mission state |
| `runtime.toml` | Frozen runtime configuration used to reproduce the mission setup |
| `manifest.json` | SHA-256 checksums, mission parameters/results, firmware and source identities, physics parameters and timing limitations |

The recording's source is firmware MAVLink telemetry. It is not an independent
ground-truth trajectory or a recording of video pixels. Browser playback
interpolates between saved samples; it does not rerun flight dynamics.
MP4 files are generated under ignored `out/` directories and do not need to be
committed. The firmware ELF and build prerequisites remain in the existing
hardware target and repository setup.

The bundled reference predates automatic recording provenance. Its manifest
marks `captured_during_run=false` and records the configuration/source identities
inspected at archive time; the archive also verified that the run's slicing
rules matched the selected hardware configuration. Future recordings capture
component identities when the viewer starts and retain them in the saved JSON.

## Replay the reference

From the FastDyn root, with the normal viewer dependencies installed:

```bash
REPLAY=virtuals/physics/flight_controllers/courbet/mavlink/replay_betaflight.py
fastdyn-env/bin/python "$REPLAY" play
```

Open the printed URL:
`http://127.0.0.1:15500/mavcesium/?replay=1&camera=overview`.
The saved flight starts playing automatically. Pause, seek, change speed, or
choose Overview, Top down or Follow quad. Stop the replay server with Ctrl+C.

For an SSH session, keep this tunnel running on your local computer:

```bash
ssh -N -L 15500:127.0.0.1:15500 your-ssh-host
```

Then open the URL in your local browser. If a live viewer is already using
15500, stop it or select a different port with `play --port 15501` and forward
that port instead. A reference directory, its `manifest.json`, or a raw
`betaflight_flight.json` can be supplied after `play`.

## Verify the reference

```bash
fastdyn-env/bin/python "$REPLAY" verify
fastdyn-env/bin/python "$REPLAY" verify --components
```

The first command checks the recording/configuration checksums and mission
metadata. The second also compares the firmware, profile, sensor rules, adapter
sources and physics source with files in the current checkout. Visual replay
needs the saved telemetry and viewer assets; it can still display an old flight
when the current firmware has changed. The bundled development reference also
predates the release-sweep adapters, so `--components` reports source drift in
this checkout. That does not invalidate saved playback. Use the current
`configs/betaflight_f405.toml` for a new maintained-profile mission, or review
the listed differences before using `rerun --allow-differences`.

## Archive a future flight

Run a normal mission and wait for completion:

```bash
fastdyn-env/bin/fastdyn run -c configs/betaflight_f405.toml
```

Before starting another run, archive its telemetry and the matching config:

```bash
fastdyn-env/bin/python "$REPLAY" archive fastdyn_work/betaflight_flight.json \
  --config configs/betaflight_f405.toml \
  --output virtuals/physics/flight_controllers/courbet/betaflight/matekf405ctr/reference_flights/my-run
```

The command creates all three files and refuses to overwrite an existing
reference. Archive immediately after the run so its work directory and selected
configuration still correspond. A recording with captured provenance is rejected
if the supplied configuration has a different hash. When run slicing rules are
available, they must match the selected configuration too. `fastdyn_work` itself
is temporary and is recreated on the next normal run.

For a custom `fastdyn run -o DIR`, use `DIR/betaflight_flight.json` and the actual
runtime config. Include the reference directory with the code review; no MP4 or
large peripheral debug log is needed.

## Run the mission again

Inspect the proposed invocation first, then launch a new mission:

```bash
fastdyn-env/bin/python "$REPLAY" rerun --dry-run
fastdyn-env/bin/python "$REPLAY" rerun
```

This action starts FastDyn/QEMU with the frozen `runtime.toml` and recorded
altitude/waypoints. It saves new results under `out/betaflight/reproductions/`.
An existing output directory is not overwritten. Component differences stop the
rerun before a process starts; `--allow-differences` is available after reviewing
the listed differences. It does not turn a changed run into an identical replay.

Build FastDyn and the FMU with the normal repository setup before rerunning.
Compiler, host and scheduling differences can affect a fresh trajectory. Compare
waypoint completion, altitude, landing, disarm and sensor evidence as validation
results; do not require identical video frames or position samples.

## Generate a video when needed

In the browser, **Download video** exports the saved flight with a field view,
aircraft, trail, height/speed and elapsed time. Chrome/Edge can produce MP4;
other supported browsers may produce WebM. Keep the tab active during export.
**Open recording** loads another flight JSON without starting a new simulation.

For headless export on a server with Google Chrome and the optional Playwright
Python package installed:

```bash
fastdyn-env/bin/python -m pip install playwright
fastdyn-env/bin/python "$REPLAY" video \
  --output out/betaflight/replays/reference-flight.mp4
```

Use `--camera top` or `--camera follow` to change the view. Export uses the saved
samples and takes approximately the recording's duration. Playwright/Chrome are
not required on the server for ordinary browser replay. The standalone
`export_betaflight_video.py` also accepts a raw recording JSON.

## Verification

```bash
fastdyn-env/bin/python -m pytest -q \
  tests/unit/test_betaflight_replay.py \
  tests/unit/test_betaflight_video.py \
  tests/unit/test_betaflight_flight_view.py
```

Validation covers archive round trips, corrupted checksums, unsafe file paths,
firmware drift, dry-run behavior, preserved output directories, invalid samples
and recording provenance. Headless Chrome has also exercised the bundled replay,
all camera modes, JSON import and MP4 download with external requests blocked.

See the [hardware rehosting guide](BetaflightHardwareRehosting.md) for the actual
firmware/sensor boundary and the [version sweep](BetaflightVersionSweep.md)
for measured release-specific challenges and validation history.

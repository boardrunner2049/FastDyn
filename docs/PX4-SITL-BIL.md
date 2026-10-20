# FastDyn with PX4 SITL

FastDyn's PX4 BIL adapter runs the FastDyn FMI 3 plant as a MAVLink simulator
endpoint. PX4 SITL remains a separate process and connects to the simulator
over TCP port 4560. The
adapter sends `HIL_SENSOR` and `HIL_GPS` and feeds received
`HIL_ACTUATOR_CONTROLS` back into the plant. This is the PX4 Simulator MAVLink
API, not a GCS MAVLink connection. PX4's [simulation guide](https://docs.px4.io/main/en/simulation/)
documents this external-simulator flow: the simulator listens on TCP 4560 and
PX4 initiates the connection.

## Build and run the FastDyn endpoint

On the machine that will run the FastDyn model:

```sh
python -m pip install -r requirements-px4-sitl.txt
./utils/build_fmi3_fmu.py --config configs/copter462.toml --fmu quadrotor
python utils/px4_sitl_bil.py \
  --fmu out/fmi3/Copter/FastDyn_Copter.fmu \
  --host 0.0.0.0 --port 4560 \
  --geoid-separation-m "$GEOID_SEPARATION_M"
```

Set `GEOID_SEPARATION_M` from the selected simulation origin (WGS84 ellipsoid
height minus mean-sea-level height); it is intentionally not guessed. The
source-only FMU is compiled for the FastDyn host on first use and cached next
to the FMU in an ignored `.runtime` directory.

## Start the PX4 side

Once FastDyn is listening, start a compatible PX4 SITL build with the FastDyn
host's reachable address. For example, an existing launcher can export:

```sh
PX4_SIM_MODEL=none_iris \
PX4_SIM_HOST_ADDR=192.0.2.10 \
PX4_NO_LOCKSTEP=1 \
./path/to/px4-sitl-launcher
```

Replace `192.0.2.10` with the actual IP address. Allow TCP 4560 from the PX4
host to the FastDyn host. Select a launcher and airframe compatible with the
PX4 build under test. Keep both processes in the foreground for the initial
test; Ctrl-C stops the PX4 process, then the FastDyn server.

The adapter paces the model at the requested fixed step (default 2 ms), does
not catch up in an unbounded burst if the FMU runs late, and sends disarmed PWM
after actuator output has been absent for 100 ms. By default, armed motor
controls must be in [0,1] and are mapped linearly to 1100–1900 µs; use
`--control-min` / `--control-max` only after checking the exact PX4 SITL output
semantics. The motor channel order and PWM scaling are explicit CLI options;
verify these against the PX4 airframe before interpreting trajectories.
The server reports achieved step rate and late-step fraction once per second;
do not interpret a run that cannot sustain the target rate as real-time BIL.
TCP sensor writes are nonblocking and have a bounded queue. If PX4 stops
reading long enough to fill it, the session ends rather than freezing the plant
loop or retaining unbounded stale sensor output.

## Current limits

This implementation is an integration starting point, not mission validation.
It supplies IMU, magnetometer, barometer, and GPS messages. Optical-flow and
rangefinder fusion, noise/bias calibration, sensor delay, and external system
behavior are not modeled by this adapter. The generic `none_iris` airframe and
the included FastDyn copter model must not be treated as a calibrated replica
of a physical vehicle. Validate axes, motor ordering, PWM semantics,
initial conditions, time pacing, and failsafe behavior before using results for
vehicle claims. PX4's non-lockstep mode makes wall-clock throughput important;
the offline test probe verifies plant/message behavior only and is not a PX4
closed-loop test.

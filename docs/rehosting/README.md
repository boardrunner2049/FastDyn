# Firmware rehosting reference

Use this guide when adding a firmware family, hardware board or release to
FastDyn. It distills the Betaflight F405 and PX4 FMUv6C bring-up experience into
a workflow. Their hooks, offsets and timing values are examples to investigate,
not settings that automatically apply to a different platform.

## Establish the target before implementation

Record the firmware source commit, board/configuration commit, compiler version,
build flags, MCU, memory map and actual sensor connections. State the required
substitution boundary and the observable behavior that will count as success.

For hardware-driver rehosting, the intended path can be:

```text
Plant -> sensor register responses -> original firmware driver
      -> estimator/controller/mixer -> actuator output -> plant
```

A message/topic bridge is another possible boundary. Specify which processing
remains in the firmware. A virtual-sensor board build does not demonstrate that
the hardware board's sensor drivers execute. An SVD or QEMU machine with a
matching CPU also does not establish complete support for that board.

## Work in this order

1. **Find the nearest working target.** Read its profile, labeled rules, native
   adapter and run configuration. Compare hardware and driver APIs before
   copying hooks. Reuse the existing setup, rule compiler and plant backend.
2. **Obtain a clean reference build.** Use the source release's compiler and
   dependencies. Retain the unstripped ELF and debug information for symbols
   and layout checks. Build in a separate directory; stale optimized/LTO objects
   can invalidate a debug build. Record intentional changes to build flags.
   Resolve compilation failures before debugging emulation.
3. **Establish boot and time.** Check reset/vector addresses, flash, RAM, stacks,
   clock assumptions and interrupt delivery. At a hang, capture the PC, stack,
   wait condition and missing hardware event. Substitute the smallest necessary
   operation and document the behavior that operation would have provided.
4. **Connect the selected boundary.** Supply device identity, configuration,
   calibration and data responses coherently. Check units, ranges, byte order,
   board rotation, axis signs and actuator ordering against the firmware source.
   Preserve the firmware processing required by the chosen boundary.
5. **Validate every memory assumption.** Pin the ELF in `profile.toml`, resolve
   labeled symbols and check instruction bytes where a hook depends on an
   instruction/register. Keep consumed structure-layout checks in the firmware
   adapter, as the PX4 and Betaflight adapters do. Rebuilds need fresh validation;
   a matching function name alone does not establish compatible offsets.
6. **Qualify behavior incrementally.** Use the stages below. Diagnose original
   calibration, arming and failsafe reports before changing timing or settings.
   Do not manufacture readiness or completion to pass a mission helper.

For targets using the Courbet pipeline, prepare bindings from the repository
root after [FastDyn setup](../Building.md). Replace the example profile path:

```bash
fastdyn-env/bin/python \
  virtuals/physics/flight_controllers/courbet/prepare_execution_configuration.py \
  --profile path/to/target/profile.toml
```

The profile describes the ELF, slicing rules and validation. The runtime TOML
describes the machine, memory, plant and helper processes. Existing version
launchers may derive a runtime TOML from a shared configuration; check their
implementation before creating another copy or assuming `profile.toml` is a
standalone `fastdyn run` configuration.

## State what has actually passed

| Stage | Evidence to collect before advancing |
| --- | --- |
| Build | Correct board and source identity; expected physical drivers present; compiler and ELF hash recorded. |
| Boot | Firmware reaches its normal scheduler/loop; time advances; no recurring fault or unsupported wait. |
| Sensors | Original required driver functions execute; decoded values follow controlled plant changes; calibration completes normally. |
| Actuators | A controlled output produces the expected plant response, including ordering, direction and units. |
| Closed loop | The firmware stabilizes the plant using the intended inputs; reported readiness and control modes agree with the experiment. |
| Mission/task | Defined targets and tolerances are satisfied, the final state is observed, and the run exits cleanly. For a flight: takeoff, waypoints, landing and confirmed disarm. |

Record partial support when a later stage fails. PX4 v1.13/v1.14 demonstrate why
boot, sensors and motor output must be distinguished from a completed mission.
A visually plausible flight trail is supporting evidence; inspect telemetry,
driver execution and final state as well. Define acceptable tracking error and
report overshoot instead of expecting every release to follow an identical path.

## Troubleshooting patterns from the existing ports

| Symptom | Investigation and demonstrated fix |
| --- | --- |
| Compiler warning stops an older build | Check the release's expected compiler first. Betaflight 4.4 required a scoped `-O0` build of `flash_m25p16.c`; `-Werror` remained enabled. Record such exceptions rather than applying them to the entire firmware. |
| Crash after reusing a hook | Inspect that ELF's instruction and live register. PX4's HRT hook changed register and instruction offset between release lines. Resolve and validate each image's hook. |
| Serial settings silently reset | Inspect the guest's configuration validator and defaults. Betaflight required retaining USB MSP while assigning another MSP port to UART2. |
| Heartbeat arrives but control does not | Check receiver protocol support, UART identifiers and command semantics separately from telemetry. Older Betaflight required MSP receiver frames; heartbeat and ANGLE identifiers also varied. |
| Telemetry reports zero velocity | Check the actual message implementation and timestamp units. Older Betaflight supplied positions but zero velocities and a microsecond timestamp; the host pilot derived velocity from successive telemetry samples. |
| Attitude diverges or rotates the wrong way | Check mounting rotation, full-scale registers, units and signs. The MPU6000 model needed inverse board rotation before the original firmware applied its alignment. Check motor ordering independently. |
| Barometer responds but altitude is wrong | Check calibration coefficients together with raw ADC values. The BMP280 model had to encode a coherent pair so the original ARM compensation recovered the plant pressure and temperature. |
| GPS bytes arrive without a usable fix | Check framing, checksum and message support in that release's parser. Betaflight 4.2 required older UBX SOL/POSLLH/VELNED packets rather than NAV-PVT. |
| Firmware refuses to arm because tasks are late | Check simulated clock, interrupt rate and configured loop workload. The F405 debug build passed with `icount.shift=3` and PID denominator 8; original arming checks remained active. Measure again for another target. |
| Browser says connection refused | Check the viewer helper, listening port, SSH forwarding and run lifetime. Betaflight's server is a run helper; the postflight viewing window keeps it available after landing. Diagnose this separately from firmware boot. |

## Extend to another version or board

First qualify one image. For a version sweep, keep the board and plant fixed
where the release supports them, then compare compiler requirements, symbols,
instruction hooks, structure layouts, configuration validation and wire protocols.
If the board did not exist in an older release, choose and document a new board
port; do not relabel a different target as support for the original board.

Keep build directories separate and qualify one release at a time. Measure
shared setup, builds/retries, the first passing run and the rehosting window
separately. Include start/end timestamps, failed attempts, acceptance results and
limitations in one compact table or CSV. Overlapping windows are elapsed time,
not exclusive engineering effort. Revalidate the original working target after
changing shared adapters.

## Keep the handoff small and useful

Keep board/version behavior in the target configuration or firmware adapter;
follow the [ownership conventions](../CodingConventions.md) before changing
shared FastDyn code. Use a family-level runtime configuration when possible.

The reference package should contain the firmware image, required runtime
configuration and parameter inputs, labeled rules, profile, shared adapter,
setup/run instructions, concise results and applicable source/license information.
Commit generated bindings only when the runtime workflow needs portable copies.
Machine-dependent bindings should be generated locally. Preserve the symbols
and debug information needed by preparation when packaging the ELF.

Keep full logs, preprocessor dumps, large traces, recordings and chronological
working notes local. For each unresolved problem, leave a short handoff stating
the exact image, last passing stage, reproduction command, failure observation,
attempted fixes and next distinguishing experiment. Promote reusable findings
into an issue/cause/fix/result summary, rather than copying the whole journal.

## Working examples

- [Betaflight hardware boundary, setup and run](../BetaflightHardwareRehosting.md)
- [Betaflight release differences and measured results](../../virtuals/physics/flight_controllers/courbet/betaflight/version_sweep/README.md)
- [PX4 FMUv6C profile, hooks and launcher](../../virtuals/physics/flight_controllers/courbet/px4/fmu_v6c/README.md)
- [Shared profile and rule preparation](../../virtuals/physics/flight_controllers/courbet/README.md)
- [Betaflight register-response implementation](../../virtuals/physics/flight_controllers/betaflight/hardware.c)

Checkouts with the PX4 release sweep also contain its detailed report at
`virtuals/physics/flight_controllers/courbet/px4/version_sweep/README.md`.

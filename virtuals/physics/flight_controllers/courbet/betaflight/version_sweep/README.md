# Betaflight F405 release sweep

Six physical-sensor firmware releases completed the host-piloted mission on
2026-11-11: 15 m target, three 30 m corners, return, landing and disarm.
The firmware runs its MPU6000 SPI and BMP280 I2C drivers; bus responses come from
the Copter FMU. This is firmware slicing, not complete STM32 peripheral emulation
or onboard waypoint execution. The development-snapshot default is unchanged.

## Setup and run

Install the dependencies in the [hardware guide](../../../../../../docs/BetaflightHardwareRehosting.md#setup-and-run),
then run from the FastDyn root:

```bash
source ./setup.sh --build-qemu --with-rumoca --skip-optifuzz
BF_SWEEP=virtuals/physics/flight_controllers/courbet/betaflight/version_sweep
fastdyn-env/bin/python "$BF_SWEEP/run.py" 4.2.11
```

Replace `4.2.11` with any release in the table below. The included firmware ELF
is checked against its profile before FastDyn starts; no firmware rebuild is needed.
The terminal streams the flight stages and should end with
`landed and disarmed: mission complete; waypoints=3/3`.

For SSH viewing: `ssh -N -L 18500:127.0.0.1:18500 your-ssh-host`, then open
`http://127.0.0.1:18500/mavcesium/`. The viewer stays available for 120 s after
landing. Set `FASTDYN_BF_VIEW_SECONDS=0` to exit immediately, or use
`run.py 4.2.11 --port 18600` to choose another port.

## Measured results

| Release | GCC | Build s | Passing run s | Rehosting window min | Peak height m |
| --- | --- | ---: | ---: | ---: | ---: |
| [2026.6.2](2026.6.2/README.md) | 13.3.1 | 7.868 | 77.889 | 8.35 | 15.1 |
| [2025.12.5](2025.12.5/README.md) | 13.3.1 | 7.512 | 78.118 | 16.13 | 15.0 |
| [4.5.5](4.5.5/README.md) | 10.3.1 | 6.779 | 69.499 | 17.03 | 16.2 |
| [4.4.3](4.4.3/README.md) | 10.3.1 | 6.735 | 73.969 | 22.77 | 15.5 |
| [4.3.2](4.3.2/README.md) | 9.3.1 | 12.283 | 77.365 | 22.82 | 17.0 |
| [4.2.11](4.2.11/README.md) | 9.2.1 | 12.274 | 86.086 | 22.97 | 16.9 |

[results.csv](results.csv) retains firmware identities, timing boundaries,
failed-attempt counts, landing results and sensor read/pressure/gyro statistics.
Build time is the command sequence producing the validated image; for 4.4.3 it
includes completing HEX output after a missing-directory failure. Passing-run
time includes helpers, QEMU, FMU and smoke checks, without the viewing delay.
Rehosting windows overlap and include investigation, retries, interleaved work
and a brief interruption; they are not exclusive engineering time.

Existing FastDyn/QEMU/FMU infrastructure was reused. Shared compiler downloads
and extraction took 65.1/56.1/46.8 s for GCC 10.3.1/9.3.1/9.2.1, concurrently;
GCC 13.3.1 was installed already. Full diagnostics stay in local ignored output.
All runs completed 3/3 and disarmed at ground level. Older releases overshot the
15 m height target, so completion does not imply identical flight tracking.

## Version differences and fixes

| Release | Main adaptation |
| --- | --- |
| 2026.6.2 | Moved serial settings to function masks; accepted GENERIC heartbeat. |
| 2025.12.5 | Selected GCC through ARM_SDK_DIR; handled ANGLE mode identifier 0. |
| 4.5.5 | Used real MSP receiver frames; derived velocity because telemetry sends zeros. |
| 4.4.3 | Applied official unified board settings; built flash_m25p16.c at -O0 with -Werror retained; used clean non-LTO objects and linear ANGLE mapping. |
| 4.3.2 | Used legacy motor/UART layouts, MSP receiver and linear ANGLE mapping. |
| 4.2.11 | Added legacy bus transport and older UBX packets; NAV-PVT was unsupported. |

## Firmware and runtime files

[results.csv](results.csv) identifies source commits, board configurations and compilers.
2026.6/2025.12/4.5 use MATEKF405CTR config headers; 4.4 uses STM32F405 plus
MTKS-MATEKF405CTR unified settings; 4.3/4.2 use the built-in MATEKF405 target.
All select MPU6000 on SPI1/PC2 with CW270 alignment and BMP280 on I2C1/PB6/PB7.

Each version keeps its ELF, profile, labeled rules and runtime parameter writes
in `parameters.txt`. Layout and parameter checks live in the shared Betaflight
adapter, following the PX4 approach. Resolved sweep rules are generated
locally because they contain the local parameter-file path. Only 4.4 needs the
additional unified board configuration file. Firmware sources are unpatched;
[the shared GPL license](../LICENSE-BETAFLIGHT) covers firmware/configuration.

Sources: [Betaflight](https://github.com/betaflight/betaflight),
[board configs](https://github.com/betaflight/config) and
[unified configs](https://github.com/betaflight/unified-targets).
Images use `DEBUG=GDB`, `-Og`, no LTO and the compiler in the results table.
Build selection is `CONFIG=MATEKF405CTR` for 4.5 and newer, `TARGET=STM32F405`
for 4.4, and `TARGET=MATEKF405` for 4.3/4.2; goals are `fwo` for 2025/2026
and `hex` for 4.x. Only 4.4's flash driver uses `-O0` as described above.
Changing a firmware image requires new bindings and a fresh qualification run.

Physical driver symbols and compile selections were checked for every image;
virtual IMU/barometer/GPS targets are absent. Calibration, stabilization, mixer
and normal receiver checks remain in firmware. GPS fix/accuracy are ideal;
DMA/EXTI timing, compass, noise and cycle accuracy are outside this validation.

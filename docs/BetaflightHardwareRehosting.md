# Betaflight F405 hardware rehosting

`configs/betaflight_f405.toml` runs the MATEKF405CTR hardware target with the
original MPU6000 SPI and BMP280 I2C drivers. Sensor register responses come from
`FastDyn.Copter`; firmware decoding, calibration, estimation, PID and mixing run
in ARM code. No custom virtual-sensor board is selected.

## Setup and run

Install the [system dependencies](../README.md#build-from-source), Rust/Cargo
for Rumoca, and Python-enabled GDB (`sudo apt-get install gdb-multiarch`).
From the repository root:

```bash
source ./setup.sh --build-qemu --with-rumoca --skip-optifuzz
fastdyn-env/bin/fastdyn run -c configs/betaflight_f405.toml
```

Setup builds FastDyn and its patched QEMU; the first run builds the Copter FMU.
The hardware-target firmware ELF is included. Later runs need only the run command.

The host RC pilot takes off to 15 m, visits `(30,0)`, `(30,30)`, `(0,30)` m N/E,
returns, lands and disarms. Arrival checks use a 3 m horizontal radius,
1 m altitude tolerance and 0.75 m/s horizontal speed. This is host-controlled
navigation through ordinary receiver frames, not an onboard mission executor.

Open `http://127.0.0.1:15500/mavcesium/` for the offline ground/quad/path view.
For SSH, run locally:

```bash
ssh -N -L 15500:127.0.0.1:15500 your-ssh-host
```

The viewer remains open for 120 s after landing while QEMU pauses.
`FASTDYN_BF_VIEW_SECONDS=0` exits immediately. Private defaults are UDP 15551
(firmware), 15552 (mission link), 15560 (MSP), and TCP 15500 (viewer).

## Firmware identity and build

| Item | Default hardware image |
| --- | --- |
| Version / source | 2026.12.0 development, `5ce286c9676348494224837959b1bf742041279f` |
| Board configuration | MATEKF405CTR, config commit `cacb3a06d7d90abd9c71255318ee95a59fa333f7` |
| CPU / compiler | Cortex-M4 hard float; GCC 13.2.1 |
| Build | `DEBUG=GDB`, `-Og`, `LTO=no` |
| Exact ELF hash / vectors | [profile.toml](../virtuals/physics/flight_controllers/courbet/betaflight/matekf405ctr/profile.toml) |

The firmware and board sources are unchanged. With the Betaflight source beside
FastDyn and its config/MAVLink dependencies initialized:

```bash
BF_SOURCE="$PWD/../betaflight"
BF_OUTPUT="$PWD/out/betaflight/hardware_f405/MATEKF405CTR"
mkdir -p "$BF_OUTPUT/obj/main"
make -C "$BF_SOURCE" CONFIG=MATEKF405CTR CONFIG_DIR="$BF_SOURCE/src/config" \
  DEBUG=GDB LTO=no GCC_REQUIRED_VERSION=13.2.1 \
  OBJECT_DIR="$BF_OUTPUT/obj/main" BIN_DIR="$BF_OUTPUT/obj" -j16 fwo
```

Checkout the pinned source and board configuration commits before building.
Rebuilds can change addresses and hashes; review and regenerate bindings for a
new image before running it. Python-enabled `gdb-multiarch` or `arm-none-eabi-gdb`
is required for the consumed-layout checks.

## Slicing boundary

| Component | Firmware retained | Host substitution |
| --- | --- | --- |
| MPU6000, SPI1/PC2, CW270 | Detection, register decoding, calibration, alignment and filtering | Raw register responses at SPI transport |
| BMP280, I2C1/PB6/PB7 | Detection, calibration-coefficient reads, ADC decode and compensation | I2C register responses |
| GPS, UART3 | Original UBX parser and navigation state | Encoded UBX packets |
| Receiver / motors | Protocol parser, stabilization, PID and mixer | UART datagrams; PWM values passed to the plant |

Clock/RTC, flash storage and absent internal ADC setup use explicit substitutions.
Runtime settings select MAVLink RX on UART1, MSP on UART2, UBX on UART3, PWM,
AUX1 ARM and AUX2 ANGLE. Storage, OSD and battery/current sources are disabled.
Guest settings reset each run. There is no forced calibration or arming bypass.
The real accelerometer calibration runs at startup.

The shared Cortex-M4 QEMU machine uses STM32F427 SVD routing; the compiled target
is STM32F405. Synchronous buses, ideal GPS fix/accuracy and `icount.shift=3` do
not establish complete peripherals, DMA/EXTI timing, compass/noise or cycle accuracy.

## Validation and references

The baseline hardware mission completed 3/3 waypoints, peak 15.0 m, landing
0.10 m from home and confirmed disarm. The [six-release sweep](BetaflightVersionSweep.md) records version-specific
metrics and overshoot rather than claiming identical tracking.

The run command above streams the mission stages and should finish with:

```text
landed and disarmed: mission complete; waypoints=3/3
```

Startup logs should report MPU6000 raw SPI samples, BMP280 raw I2C samples and
real accelerometer calibration. The local viewer shows the planned route and
actual flight trail. Tracking and duration vary with the firmware and host.

[Target files](../virtuals/physics/flight_controllers/courbet/betaflight/matekf405ctr/README.md)

[General workflow and troubleshooting for new platforms](rehosting/README.md)

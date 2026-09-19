# FastDyn Table 8 Docker reproduction

This is the self-contained runbook for reproducing the real-world firmware
and fuzzing measurements in Table 8 from the supplied Docker image. It
contains no source-build steps.

## Host requirements

Use an x86-64 Linux host with Docker Engine and permission to run Docker. The
software-only measurements need only a writable host directory. Hardware
passthrough additionally needs native Linux Docker access to the board's
ST-LINK USB device and VCP; Docker Desktop USB virtual machines are not a
supported replacement.

Every launcher requires a fresh output directory. Its final `SIGINT` /
timeout is the intended wall-clock cutoff, not a fuzzer failure.

## Run the software-only evaluations

Pull the published image, then run from a directory where `results/` may be
created:

```sh
docker pull pseclab/fastdyn-fuzzer:1.0.0

mkdir -p results
IMAGE=pseclab/fastdyn-fuzzer:1.0.0

# QEMU-only cold-start baseline: MQTT, lwIP, SD-card, and Modbus.
docker run --rm -it -v "$PWD/results:/results" "$IMAGE" \
  ./virtuals/fuzzer/run_qemu_coverage.sh all --work-dir /results/qemu

# AFLNet campaigns. Each command uses a distinct result directory.
docker run --rm -it -v "$PWD/results:/results" "$IMAGE" \
  ./virtuals/fuzzer/run_aflnet_coverage.sh lwip /results/lwip-aflnet
docker run --rm -it -v "$PWD/results:/results" "$IMAGE" \
  ./virtuals/fuzzer/run_aflnet_coverage.sh mqtt /results/mqtt-aflnet
docker run --rm -it -v "$PWD/results:/results" "$IMAGE" \
  ./virtuals/fuzzer/run_aflnet_coverage.sh modbus /results/modbus-aflnet

# Non-fuzzing Modbus Elder baseline; no physical board is required.
docker run --rm -it -v "$PWD/results:/results" "$IMAGE" \
  ./virtuals/fuzzer/run_board_coverage.sh modbus \
    --work-dir /results/modbus-elder
```

The QEMU-only launcher writes `summary.tsv` and one `bbl.txt` per target.
The other launchers write `bbl.txt` in their specified result directory.

## Reference results

These are unique-basic-block (BBL) counts from the checked-in image inputs.
AFLNet and hardware-assisted results can vary slightly with timing. The
QEMU-only values are cold-start references, not application-exercise results.

| Measurement | Target | Reference BBL count |
| --- | --- | ---: |
| AFLNet | lwIP | 2,587 measured; about 2,600 for a longer run |
| AFLNet | MQTT | 902 measured; about 920 for a longer run |
| AFLNet | Modbus | 611 measured; about 620 for a longer run |
| Elder baseline | MQTT (NUCLEO-H753ZI board) | 869 |
| Elder baseline | lwIP (STM32F769I-EVAL board) | 1,814 |
| Elder baseline | SD-card (STM32F769I-EVAL board) | 1,393 |
| Elder baseline | Modbus | 521 |
| QEMU-only | MQTT / lwIP / SD-card / Modbus | 196 / 124 / 103 / 123 |

The lwIP AFLNet campaign is the only enforced threshold: it exits nonzero
unless its combined six-phase result exceeds 2,500 BBLs. For MQTT and Modbus,
confirm that a `bbl.txt` is produced and compare the count with the reference
while allowing normal timing variation.

## Run the hardware-assisted baselines

The following optional Elder baselines require a physical board. Do not attach
a NUCLEO-F103 for this section: the Modbus baseline above is intentionally
software-only.

| Run | Required board | Idle image to flash |
| --- | --- | --- |
| MQTT | NUCLEO-H753ZI | `tests/idle_firmwares/prebuilt/idlefirmware_h7.axf` |
| lwIP and SD-card | STM32F769I-EVAL | `tests/idle_firmwares/prebuilt/idleloop_firmware_f4_f7.axf` |

Flash the idle image, not the demo firmware, before running the matching
baseline. This overwrites the board flash. Connect one board, select its
ST-LINK VCP, and set `PORT` to it:

```sh
ls -l /dev/serial/by-id/
PORT=/dev/ttyACM0                 # replace with the selected ST-LINK VCP
test -c "$PORT"
```

Flash a NUCLEO-H753ZI for MQTT:

```sh
docker run --rm -it \
  --device-cgroup-rule='c 189:* rmw' \
  -v /dev/bus/usb:/dev/bus/usb \
  "$IMAGE" \
  openocd -f interface/stlink.cfg -f target/stm32h7x.cfg \
    -c 'program tests/idle_firmwares/prebuilt/idlefirmware_h7.axf verify reset exit'
```

Flash an STM32F769I-EVAL for lwIP or SD-card:

```sh
docker run --rm -it \
  --device-cgroup-rule='c 189:* rmw' \
  -v /dev/bus/usb:/dev/bus/usb \
  "$IMAGE" \
  openocd -f interface/stlink.cfg -f target/stm32f7x.cfg \
    -c 'program tests/idle_firmwares/prebuilt/idleloop_firmware_f4_f7.axf verify reset exit'
```

Reconnect or reset the board after flashing. Then run the matching command.

### MQTT (NUCLEO-H753ZI)

```sh
docker run --rm -it \
  --device="$PORT" \
  --device-cgroup-rule='c 189:* rmw' \
  -v /dev/bus/usb:/dev/bus/usb \
  -v /dev/serial/by-id:/dev/serial/by-id:ro \
  -v "$PWD/results:/results" \
  "$IMAGE" \
  ./virtuals/fuzzer/run_board_coverage.sh mqtt \
    --board-port "$PORT" --work-dir /results/mqtt-board
```

### lwIP (STM32F769I-EVAL)

Create `tap0` once on the host, then run the container with host networking:

```sh
sudo ip tuntap add dev tap0 mode tap user "$(id -un)"
sudo ip addr add 192.168.8.1/24 dev tap0
sudo ip link set dev tap0 up

docker run --rm -it \
  --network=host --cap-add=NET_ADMIN --device=/dev/net/tun \
  --device="$PORT" \
  --device-cgroup-rule='c 189:* rmw' \
  -v /dev/bus/usb:/dev/bus/usb \
  -v /dev/serial/by-id:/dev/serial/by-id:ro \
  -v "$PWD/results:/results" \
  "$IMAGE" \
  ./virtuals/fuzzer/run_board_coverage.sh lwip \
    --board-port "$PORT" --work-dir /results/lwip-board
```

### SD-card (STM32F769I-EVAL)

```sh
docker run --rm -it \
  --device="$PORT" \
  --device-cgroup-rule='c 189:* rmw' \
  -v /dev/bus/usb:/dev/bus/usb \
  -v /dev/serial/by-id:/dev/serial/by-id:ro \
  -v "$PWD/results:/results" \
  "$IMAGE" \
  ./virtuals/fuzzer/run_board_coverage.sh sdcard \
    --board-port "$PORT" --work-dir /results/sdcard-board
```

If a rootless Docker daemon cannot apply the USB device-cgroup rule, use a
daemon that can. `--privileged` is a broader fallback. Each hardware command
writes its `bbl.txt` to the selected result directory.

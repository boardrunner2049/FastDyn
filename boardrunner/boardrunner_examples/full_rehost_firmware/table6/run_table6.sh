#!/usr/bin/env bash
# Measure one cell of Table 6: unique basic blocks executed by a firmware
# under one peripheral-modeling mode.
#
#   ./run_table6.sh <gpio|uart|i2c|spi|adc> <passthrough|null|replay|boardrunner|record> [seconds]
#
# Run from the FastDyn repo root with the fastdyn CLI on PATH. Prints the
# block count; the raw list is left in fastdyn_work_table6_<fw>_<mode>/bbl.txt.
# passthrough and record need a NUCLEO-F103RB running the idle firmware; the
# other modes need no hardware. record leaves a new tape in the work dir
# (copy it over tapes/<fw>/record.trace to use it for replay).
set -euo pipefail

FW=${1:?firmware: gpio|uart|i2c|adc|spi}
MODE=${2:?mode: passthrough|null|replay|boardrunner|record}
SECS=${3:-10}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../../.." && pwd)
cd "$ROOT"

declare -A EXAMPLE=([spi]=SPI [gpio]=GPIO_with_GPIO_INT [uart]=UART [i2c]=I2C [adc]=ADC_with_DMA)
declare -A TOML=([spi]=spi_config.toml [gpio]=gpio.toml [uart]=uart_config.toml [i2c]=i2c_config.toml [adc]=adc_with_dma_config.toml)
EXDIR="boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/${EXAMPLE[$FW]}"

case $MODE in
  boardrunner) CFG="$EXDIR/${TOML[$FW]}" ;;
  passthrough|null|replay|record) CFG="$HERE/configs/$FW/$MODE.toml" ;;
  *) echo "unknown mode $MODE" >&2; exit 2 ;;
esac

# null and boardrunner load the example's models: stage them in the SDK model
# dir and build (configuring the SDK build dir on first use)
if [[ $MODE == boardrunner || $MODE == null ]]; then
  SDK=boardrunner/boardrunner_sdk
  cp "$EXDIR"/generated_models/*.c "$SDK/model/"
  if [[ ! -f $SDK/build/CMakeCache.txt ]]; then
    cmake -S "$SDK" -B "$SDK/build" \
      -DFASTDYN_INCLUDE_DIR="$ROOT/include" -DQEMU_INCLUDE_DIR="$ROOT/../qemu/include" >/dev/null
  fi
  cmake --build "$SDK/build" -j >/dev/null
fi

WORK="fastdyn_work_table6_${FW}_${MODE}"
rm -rf "$WORK" io.log
LOG=$(mktemp)
fastdyn run -c "$CFG" -o "./$WORK" >"$LOG" 2>&1 &
for _ in $(seq 120); do grep -q "Current CPU type\|ran out of records\|divergence" "$LOG" 2>/dev/null && break; sleep 0.5; done
sleep 3

# drive the firmware's input where it has one
case "$FW:$MODE" in
  gpio:boardrunner)
    for _ in $(seq 10); do printf 0 >/tmp/gpioc_pc13; sleep 0.25; printf 1 >/tmp/gpioc_pc13; sleep 0.25; done ;;
  gpio:passthrough|gpio:record)
    echo "press the blue user button a few times now (${SECS}s)..." ;;
  uart:boardrunner)
    # the USART2 model needs /tmp/usart2_pty; create a pair if the evaluator has none
    if [[ ! -e /tmp/usart2_pty ]]; then echo "no /tmp/usart2_pty - start 'socat PTY,link=/tmp/usart2_pty,raw,echo=0 PTY,link=/tmp/host_usart2,raw,echo=0' first" >&2; fi
    for c in H e l l o W o r l d; do printf "$c" >/tmp/host_usart2 2>/dev/null || true; sleep 0.2; done ;;
  uart:passthrough|uart:record)
    stty -F /dev/ttyACM0 115200 raw -echo
    for c in H e l l o W o r l d; do printf "$c" >/dev/ttyACM0; sleep 0.5; done ;;
esac

sleep "$SECS"
pkill -INT -x qemu-system-arm 2>/dev/null || true
sleep 3
grep -E "smash|Couldn|divergence|HW .* Failed" "$LOG" | head -3 >&2 || true
[[ -f io.log ]] && mv io.log "$WORK/io.log"
echo "$FW $MODE: $(wc -l <"$WORK/bbl.txt") unique basic blocks   ($WORK/bbl.txt)"
[[ $MODE == record ]] && echo "tape recorded: $WORK/io.log  (cp it to $HERE/tapes/$FW/record.trace to replay it)"

#!/bin/bash
# Entrypoint of the pseclab/boardrunner image: NUCLEO-F103RB SDK workloads
# fully rehosted (Tables 5/7) and the naive-baseline comparison (Table 6).
#
#   table6 all [secs]                     every firmware x {null, replay, boardrunner}, then the table
#   table6 <fw> <mode> [secs]             one cell; fw: gpio uart i2c spi adc; mode: null replay boardrunner
#   example <name> [secs]                 one fully rehosted workload: GPIO_with_GPIO_INT UART SPI I2C
#                                         ADC_with_DMA TIMER PWM (or `example all`)
#   verify <fw>                           trace verification (TV) of the boardrunner run of <fw>
# Hardware (NUCLEO-F103RB on USB; SPI and I2C also need a BME280 breakout, see README):
#   check | flash                         openocd sanity check | flash the idle firmware (once)
#   table6 <fw> passthrough|record [secs] Passthrough column / re-record a replay tape
#
# Every run is a fresh QEMU process, stopped after <secs> (default 10).
# Outputs go to /results when mounted (-v "$PWD:/results"): table6/<fw>_<mode>.{txt,log}
# (unique-block list, I/O log), examples/<name>.log, table6.md.
set -u
trap 'kill -9 -1 2>/dev/null; exit 130' INT TERM
FD=/workspace/FastDyn; cd "$FD"
R=$FD/boardrunner/boardrunner_examples/full_rehost_firmware
T=$R/table6
OUT=/results; mountpoint -q "$OUT" 2>/dev/null || OUT=/tmp/results
mkdir -p "$OUT/table6" "$OUT/examples"

declare -A EXAMPLE=([gpio]=GPIO_with_GPIO_INT [uart]=UART [i2c]=I2C [spi]=SPI [adc]=ADC_with_DMA [timer]=TIMER [pwm]=PWM)
declare -A TOML=([gpio]=gpio.toml [uart]=uart_config.toml [i2c]=i2c_config.toml [spi]=spi_config.toml
                 [adc]=adc_with_dma_config.toml [timer]=timer_config.toml [pwm]=pwm_config.toml)
PRESSES=10          # GPIO: button press/release cycles the driver performs
TYPED="BRunner123"  # UART: the 10 characters the driver types (differ from the recording)

stage_models() {   # $1 = fw key: copy the example's generated models into the SDK and build
    rm -f boardrunner/boardrunner_sdk/model/*.c
    cp "$R/Nucleo-F103RB/${EXAMPLE[$1]}"/generated_models/*.c boardrunner/boardrunner_sdk/model/
    cmake -S boardrunner/boardrunner_sdk -B boardrunner/boardrunner_sdk/build \
        -DFASTDYN_INCLUDE_DIR=$FD/include -DQEMU_INCLUDE_DIR=/workspace/qemu/include >/dev/null
    cmake --build boardrunner/boardrunner_sdk/build -j >/dev/null 2>&1 || { echo "model build failed" >&2; exit 1; }
}

uart_pty() {       # the USART2 model opens /tmp/usart2_pty; give it a pty pair and capture the other end
    pkill -x socat 2>/dev/null; rm -f /tmp/usart2_pty /tmp/host_usart2
    socat PTY,link=/tmp/usart2_pty,raw,echo=0 PTY,link=/tmp/host_usart2,raw,echo=0 2>/dev/null &
    for _ in $(seq 20); do [ -e /tmp/host_usart2 ] && break; sleep 0.1; done
    : > /tmp/uart_out; (cat /tmp/host_usart2 > /tmp/uart_out 2>/dev/null) &
}

run_fw() {         # $1 = fw key, $2 = mode, $3 = seconds; leaves $WORK/{bbl.txt,io.log}
    local fw=$1 mode=$2 secs=$3 cfg
    case $mode in
        boardrunner|example) cfg="$R/Nucleo-F103RB/${EXAMPLE[$fw]}/${TOML[$fw]}"; stage_models "$fw" ;;
        null)                cfg="$T/configs/$fw/$mode.toml"; stage_models "$fw" ;;
        passthrough|replay|record) cfg="$T/configs/$fw/$mode.toml" ;;
        *) echo "unknown mode $mode" >&2; exit 2 ;;
    esac
    [ -f "$cfg" ] || { echo "no config for $fw/$mode ($cfg)" >&2; exit 2; }
    WORK="fastdyn_work_${fw}_${mode}"; rm -rf "$WORK" io.log /tmp/gpioc_pc13 /tmp/gpioa_out; : > /tmp/uart_out
    [ "$fw" = uart ] && [ "$mode" != null ] && uart_pty
    LOG=$(mktemp)
    fastdyn run -c "$cfg" -o "./$WORK" >"$LOG" 2>&1 &
    for _ in $(seq 120); do grep -q "Current CPU type\|ran out of records\|divergence" "$LOG" 2>/dev/null && break; sleep 0.5; done
    sleep 3
    case "$fw:$mode" in
        gpio:boardrunner|gpio:example)
            for _ in $(seq $PRESSES); do printf 0 >/tmp/gpioc_pc13; sleep 0.25; printf 1 >/tmp/gpioc_pc13; sleep 0.25; done ;;
        gpio:passthrough|gpio:record) echo "  press the blue user button (B1) a few times now (${secs}s)..." ;;
        uart:boardrunner|uart:example|uart:replay)
            for c in $(echo "$TYPED" | fold -w1); do printf "$c" >/tmp/host_usart2 2>/dev/null; sleep 0.2; done ;;
        uart:passthrough|uart:record)   # the firmware's USART2 is the board's VCP: type there and capture its output
            stty -F /dev/ttyACM0 115200 raw -echo && (cat /dev/ttyACM0 > /tmp/uart_out 2>/dev/null) &
            sleep 0.5; for c in $(echo "$TYPED" | fold -w1); do printf "$c" >/dev/ttyACM0; sleep 0.5; done ;;
    esac
    sleep "$secs"
    pkill -INT -x qemu-system-arm 2>/dev/null; sleep 3; pkill -9 -x qemu-system-arm 2>/dev/null
    pkill -x socat 2>/dev/null; pkill -f "cat /tmp/host_usart2" 2>/dev/null; pkill -f "cat /dev/ttyACM0" 2>/dev/null
    grep -E "smash|Couldn|HW .* Failed|Traceback" "$LOG" | head -3 >&2
    [ -f io.log ] && mv io.log "$WORK/io.log"
    cp "$LOG" "$WORK/run.log"; rm -f "$LOG"
    [ -f "$WORK/bbl.txt" ] || { echo "no bbl.txt produced (see $WORK/run.log)" >&2; return 1; }
}

fc_check() {       # $1 = fw, $2 = io.log path, $3 = mode -> prints "pass"/"fail" and a reason (Table 7 operation)
    local fw=$1 log=$2 mode=${3:-} n
    case $fw in
        gpio)  n=$(grep -c "Interrupt Taken.*Vector = 0x00000038" "$log")
               case $mode in
                   passthrough|record)   # the human pressed the button; the board enters the ISR twice per edge
                       [ "$n" -ge 2 ] && echo "pass: callback ran on $((n/2)) button edges" || echo "fail: no button interrupt seen" ;;
                   *)  [ "$n" -eq $((2*PRESSES)) ] && echo "pass: $n EXTI interrupts for $PRESSES press/release cycles" \
                                                   || echo "fail: $n EXTI interrupts for $PRESSES press/release cycles" ;;
               esac ;;
        uart)  grep -q "$TYPED" /tmp/uart_out 2>/dev/null && echo "pass: firmware echoed the typed '$TYPED'" \
                                                          || echo "fail: typed '$TYPED' not echoed (output: $(tr -d '\r' </tmp/uart_out 2>/dev/null | tail -c 60 | tr '\n' ' '))" ;;
        i2c)   n=$(grep "Read.*address = 0x40005410" "$log" | grep -cE "value = 0x0*60\b"); local t=$(grep -c "0x40010810" "$log")
               [ "$n" -ge 1 ] && [ "$t" -ge 3 ] && echo "pass: BME280 chip id 0x60 read, $t LED toggles (measurement cycles)" \
                                                || echo "fail: chip id reads=$n, LED toggles=$t" ;;
        spi)   grep -i "Read.*address = 0x4000380C" "$log" | grep -qiE "value = 0x0*1c\b" && [ "$(grep -ic '0x40010810.*value = 0x0*20\b' "$log")" -ge 1 ] \
                   && echo "pass: config register read back 0x1c, LED turned on" || echo "fail: no 0x1c read-back / LED" ;;
        adc)   n=$(grep -c "Interrupt Taken.*Vector = 0x0000001B" "$log")
               [ "$n" -ge 100 ] && echo "pass: $n DMA1 transfer-complete interrupts" || echo "fail: $n DMA1 interrupts" ;;
        timer) n=$(grep -c "Interrupt Taken.*Vector = 0x0000002D" "$log")
               [ "$n" -ge 3 ] && echo "pass: $n TIM3 update interrupts, LED toggled $(grep -c 0x40010810 "$log") times" || echo "fail: $n TIM3 interrupts" ;;
        pwm)   n=$(grep -c "Read.*address = 0x40000424" "$log")
               [ "$n" -ge 100 ] && echo "pass: $n TIM3->CNT samples, counter advancing" || echo "fail: $n CNT reads" ;;
    esac
}

table6_cell() {    # $1 = fw, $2 = mode, $3 = secs
    run_fw "$1" "$2" "$3" || return 1
    cp "$WORK/bbl.txt" "$OUT/table6/$1_$2.txt"; cp "$WORK/io.log" "$OUT/table6/$1_$2.log" 2>/dev/null
    [ "$2" = record ] && cp "$WORK/io.log" "$OUT/table6/$1_record.trace" && echo "  tape: $OUT/table6/$1_record.trace (replaces table6/tapes/$1/record.trace)"
    local n=$(wc -l < "$WORK/bbl.txt") fc; fc=$(fc_check "$1" "$WORK/io.log" "$2")
    printf "%-5s %-12s %4d unique basic blocks   FC %s\n" "$1" "$2" "$n" "$fc"
    echo "$1,$2,$n,${fc%%:*}" >> "$OUT/table6/cells.csv"
}

table6_table() {
    local f m n c; local -A BB FC
    while IFS=, read -r f m n c; do BB[$f,$m]=$n; FC[$f,$m]=$c; done < "$OUT/table6/cells.csv"
    { echo "| Firmware | Null | Replay | BoardRunner |"; echo "|---|---|---|---|"
      for f in gpio uart i2c spi adc; do printf "| %s | %s | %s | %s |\n" "$f" "${BB[$f,null]:--}" "${BB[$f,replay]:--}" "${BB[$f,boardrunner]:--}"; done
      printf "| FC | %s | %s | %s |\n" "$(fcmark null)" "$(fcmark replay)" "$(fcmark boardrunner)"
    } | tee "$OUT/table6.md"
    echo "(FC = every firmware passes its functional check; per-firmware results above. Paper: Null 101/352/304/262/265, Replay = BoardRunner = Passthrough 191/415/438/291/378; FC pass only for BoardRunner.)"
}
fcmark() { local m=$1 f; for f in gpio uart i2c spi adc; do [ "${FC[$f,$m]:-fail}" = pass ] || { echo "fail"; return; }; done; echo "pass"; }

CMD="${1:-help}"; shift || true
case "$CMD" in
    table6)
        FW="${1:-all}"; MODE="${2:-}"; SECS="${3:-10}"
        if [ "$FW" = all ]; then
            : > "$OUT/table6/cells.csv"; SECS="${2:-10}"
            for f in gpio uart i2c spi adc; do for m in null replay boardrunner; do table6_cell "$f" "$m" "$SECS"; done; done
            table6_table
        else
            [ -n "$MODE" ] || { echo "usage: table6 <fw> <mode> [secs]" >&2; exit 2; }
            table6_cell "$FW" "$MODE" "$SECS"
        fi ;;
    example)
        NAME="${1:-all}"; SECS="${2:-10}"
        for key in "${!EXAMPLE[@]}"; do [ "${EXAMPLE[$key]}" = "$NAME" ] && NAME=$key; done
        if [ "$NAME" = all ]; then LIST="gpio uart i2c spi adc timer pwm"; else LIST="$NAME"; fi
        for f in $LIST; do
            [ -n "${EXAMPLE[$f]:-}" ] || { echo "unknown example $f" >&2; exit 2; }
            run_fw "$f" example "$SECS" || continue
            cp "$WORK/io.log" "$OUT/examples/${EXAMPLE[$f]}.log" 2>/dev/null
            printf "%-18s %4d unique basic blocks   FC %s\n" "${EXAMPLE[$f]}" "$(wc -l < "$WORK/bbl.txt")" "$(fc_check "$f" "$WORK/io.log")"
        done ;;
    verify)
        FW="${1:?fw}"; EM="$OUT/table6/${FW}_${2:-boardrunner}.log"; EX="$R/Nucleo-F103RB/${EXAMPLE[$FW]}"
        [ -f "$EM" ] || { echo "run 'table6 $FW boardrunner' first (no $EM)" >&2; exit 2; }
        M=boardrunner/boardrunner_sdk/model; HW="$EX/hardware_log/io.trace"
        case $FW in
            gpio) fastdyn verifier -hw "$HW" -em "$EM" -b STM32F103xx -p GPIOC -p EXTI -mname GPIOC -mname EXTI -d GPIOC:$EX/generated_models/gpioc_model.c -d EXTI:$EX/generated_models/exti_model.c -o ./fastdyn_work ;;
            uart) fastdyn verifier -hw "$HW" -em "$EM" -b STM32F103xx -p USART2 -mname USART2 -d USART2:$EX/generated_models/usart2_model.c -o ./fastdyn_work ;;
            i2c)  fastdyn verifier -hw "$HW" -em "$EM" -b STM32F103xx -p I2C1 -mname I2C1 -d I2C1:$EX/generated_models/i2c1_model.c -d BME280:$EX/generated_models/bme280_i2c_slave.c -o ./fastdyn_work ;;
            spi)  fastdyn verifier -hw "$HW" -em "$EM" -b STM32F103xx -p SPI2 -p GPIOB -mname SPI2 -d SPI2:$EX/generated_models/spi2_model.c -d BME280:$EX/generated_models/bme280_slave.c -o ./fastdyn_work ;;
            adc)  fastdyn verifier -hw "$HW" -em "$EM" -b STM32F103xx -p ADC1 -p DMA1 -mname ADC1 -mname DMA1 -d ADC1:$EX/generated_models/adc1_model.c -d DMA1:$EX/generated_models/dma1_model.c -o ./fastdyn_work ;;
        esac 2>&1 | grep -iE "matched|passing|mismatch|warning|error" ;;
    check) exec openocd -f board/st_nucleo_f103rb.cfg -c "init; targets; shutdown" ;;
    flash) # no `verify`: the idle ELF carries a .data segment loaded at its RAM address, which a
           # post-reset verify re-reads and rejects; flash itself is programmed (as in tests/idle_firmwares/flash_scripts/flash.sh)
           openocd -f board/st_nucleo_f103rb.cfg -c "program tests/idle_firmwares/prebuilt/idlefirmware_f103.axf reset exit" 2>&1 \
               | grep -E "Programming|Error|rror:" ; exit "${PIPESTATUS[0]}" ;;
    help|*) sed -n '2,20p' "$0" ;;
esac

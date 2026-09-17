#!/usr/bin/env bash
# flash_firmware.sh
# Fast flash for STM32F469 Discovery using OpenOCD (no verify)

set -e

OPENOCD="/usr/bin/openocd"
BOARD_CFG="/usr/share/openocd/scripts/board/st_nucleo_f103rb.cfg"
# BOARD_CFG="/usr/share/openocd/scripts/board/stm32f469discovery.cfg"
# BOARD_CFG="/usr/local/share/openocd/scripts/board/stm32f7discovery.cfg"
# BOARD_CFG="/usr/share/openocd/scripts/board/st_nucleo_h743zi.cfg"  #From the official stm comment, all h743 are compatible with h753, let's use this.
FLASH_ADDR="0x08000000"
FIRMWARE="${1:-build/firmware.elf}"

if [ ! -f "$FIRMWARE" ]; then
    echo "Error: Firmware '$FIRMWARE' not found"
    echo "Usage: $0 <firmware.elf|firmware.bin>"
    exit 1
fi

echo "Flashing $FIRMWARE (no verify)..."
if [[ "$FIRMWARE" == *.bin ]]; then
    $OPENOCD -f "$BOARD_CFG" \
      -c "program $FIRMWARE $FLASH_ADDR reset exit"
else
    $OPENOCD -f "$BOARD_CFG" \
      -c "program $FIRMWARE reset exit"
fi

echo "✅ Flash done (skipped verification)."



# Idle (interrupt-server) firmware for HITL

Passthrough needs this firmware on the real board: it keeps the MCU's CPU idle and turns every peripheral IRQ into a `bkpt` that LibHW picks up and forwards to QEMU.

## Flash a prebuilt

| Board | File |
|---|---|
| NUCLEO-F103RB | `prebuilt/idlefirmware_f103.axf` |
| STM32F429I-DISC1 | `prebuilt/idlefirmware_f429.axf` |
| STM32F769I-EVAL | `prebuilt/idlefirmware_f769.axf` |
| NUCLEO-H753ZI | `prebuilt/idlefirmware_h7.axf` |
| MAX78000FTHR | `prebuilt/idle_firmware_max78000.elf` |

```bash
# set BOARD_CFG in flash_scripts/flash.sh to your board's OpenOCD config first
tests/idle_firmwares/flash_scripts/flash.sh tests/idle_firmwares/prebuilt/idlefirmware_f103.axf
```

## IRQ id contract (read this before rebuilding)

Each handler does `mov r0, <id>; bkpt`. `<id>` **must be the NVIC IRQn** (e.g. EXTI15_10 = 40, USART2 = 38), not the exception vector (IRQn + 16). `hw_session.c` adds 16 itself (`IRQ_QEMU_VECTOR_BASE`). A firmware built with vector numbers halts the board on the first interrupt and QEMU raises the wrong vector — passthrough looks alive (MMIO still flows) but no interrupt ever reaches the emulated firmware.

Check any binary before flashing:

```bash
arm-none-eabi-objdump -d <fw.axf> | grep -A3 '<EXTI15_10_IRQHandler>:' | grep -oE '#[0-9]+'   # must print #40
```

## Rebuild from source

```bash
sudo apt install -y clang llvm lld
# GCC sysroot: https://developer.arm.com/-/media/Files/downloads/gnu/13.3.rel1/binrel/arm-gnu-toolchain-13.3.rel1-x86_64-arm-none-eabi.tar.xz
cd tests/idle_firmwares/<board dir>          # Nucleo_F103RB | STM32F429i_disc1 | STM32F769i_eval | NucleoH753zi
# set GCC_INSTALL in the Makefile to the unpacked toolchain
make bc && make exec                         # -> build/idlefirmware.axf
```

Copy the result over the matching file in `prebuilt/` so the two never drift again.

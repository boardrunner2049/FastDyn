# ESCape32 admission

**Blocked at boot.** The released ESCAPE1 rev17.3 controller and BOOT2_PA2 rev4 loader target the STM32G071 on the Axen 60A and Hybrid 60A boards. The published binaries and a source rebuild both reached the bootloader clock initialization on FastDyn’s generic Cortex-M machine. Four QMP samples per image found PC `0x080000f4` testing the PLL-ready bit. The preceding load read zero from RCC_CR at `0x40021000`. The source rebuild maps this instruction to `boot/mcu/STM32G0/config.c:28`. [Upstream target table](https://github.com/neoxic/ESCape32/wiki/Targets) · [Boot initialization at the tested revision](https://github.com/neoxic/ESCape32/blob/1d718c143380c3eb7e6581478990e1496632e5b8/boot/mcu/STM32G0/config.c#L20-L29)

## Reproduce the reset diagnosis

Run `prepare.sh fetch` with Git, curl and sha256sum. Run the build and probes with Python 3.11+, CMake, GNU Make, the Arm bare-metal GCC/binutils/newlib toolchain, and the FastDyn QEMU build. From the FastDyn checkout in the shared workspace:

```sh
export ESC_WORK=/workspace/trebuchet-work/escape32
tools/trebuchet/applications/escape32/prepare.sh fetch "$ESC_WORK"
/workspace/trebuchet-shell tools/trebuchet/applications/escape32/prepare.sh build "$ESC_WORK"
for image in release rebuild; do
    /workspace/trebuchet-shell python3 tools/trebuchet/applications/escape32/probe.py \
        "$ESC_WORK" third_party/common/qemu/build/qemu-system-arm --image "$image"
done
```

The fetch step checks the release binary hashes. The build step uses the original upstream CMake board targets with the default `-Os -g` options, pins libopencm3, and supplies no application source changes. The release assets and the rebuilt images have different bytes. A second application ELF retains relocations for FirmBOLT. The build compares its entire flash payload with the default source build. That comparison passed. The older mixed-O0 ESCape32 ELF in FirmBOLT’s qualification corpus has independent provenance.

Each probe creates a fresh output directory containing the QEMU command, both input hashes, machine register observations, translated-block trace and process log. It returns after four diagnostic samples. A successful diagnostic run carries no operational performance result. Both image pairs contain the matching bootloader at `0x08000000` and the application configuration at `0x08001000`. The application vectors begin at `0x08001800`. The release probe wraps each published flash image in Intel HEX to preserve those load addresses within the generic loader’s size limits.

## Operational admission boundary

The upstream RC-car scenario uses forward/brake/reverse mode. Throttle entering reverse first applies the brake. The motor controller must receive real command edges, drive the motor phases, and respond to changing rotor speed, load and current. The upstream documentation describes command behavior and inertial loading, but supplies no timestamped motor/ESC capture for this target in the tested sources. [Operating scenarios](https://github.com/neoxic/ESCape32/wiki/TipsAndTricks) · [Configuration interface](https://github.com/neoxic/ESCape32/wiki/Configuration)

The generic machine runs a Cortex-M0 at a fixed 168 MHz. ESCAPE1 targets a Cortex-M0+ with a 64 MHz operating clock. The bootloader needs a responding clock controller. The application then needs the TIM6 one-pulse timer, ADC calibration and conversions, TIM15 input capture and DMA, TIM1 phase outputs, TIM2 comparator crossings or the hall path, and the enabled interrupts. The repository’s existing flight-controller plant does not supply this board interface. [Board initialization](https://github.com/neoxic/ESCape32/blob/1d718c143380c3eb7e6581478990e1496632e5b8/mcu/STM32G071/config.c) · [Target clock and timers](https://github.com/neoxic/ESCape32/blob/1d718c143380c3eb7e6581478990e1496632e5b8/mcu/STM32G071/config.h)

A future admission requires independently checked command acceptance, phase commutation order and duty, telemetry, and rotor response to the declared load, brake and reversal. A closed-loop evaluation requires a qualified electrical/mechanical plant. A replay evaluation requires a board trace with event ordering and a separate output oracle. Neither baseline has been demonstrated here. Ordinary-warm capture, optimization, ranking and final timing therefore remain open.

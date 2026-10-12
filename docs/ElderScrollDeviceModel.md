# Elder Scroll Device Model

The **elder scroll device model** serves each configured device from its own
model library (its "scroll"), a shared library written in C. The models can be
written by hand or generated, for example by
[BoardRunner](https://github.com/boardrunner2049/BoardRunner), which learns them
from a hardware trace, or by PhantomSmith; elder loads and runs them the same
way.

## Writing a model for elder

Each device handled by elder names a shared library, its model:

```toml
[Device.usart2]
    ranges = [["0x40004400", "0x400047FF"]]
    irq = ['USART2']                    # the CMSIS-SVD name, or its exception number (IRQn + 16)
    pty = "usart2"                      # anything else belongs to the model
    [[Device.usart2.handlers]]
        model   = "elder"
        enabled = true
        scroll  = "build/models/stm32_usart.so"
```

The model includes `<fastdyn/device_model.h>` and exports one descriptor,
`fastdyn_model`, that points at its functions (see `tests/models/elder_echo.c`):

```c
static void *usart_init(const FdDevice *dev, const char *config_json, const FdHost *host);
static uint64_t usart_read(void *opaque, hwaddr addr, unsigned size, uint64_t pc);
static void usart_write(void *opaque, hwaddr addr, uint64_t value, unsigned size, uint64_t pc);

const FdModel fastdyn_model = {
    FASTDYN_MODEL_ABI_FIELDS,
    .init = usart_init,
    .read = usart_read,
    .write = usart_write,
    /* optional: .interrupt and .serve, called when the CPU takes and
       returns from one of the device's own IRQs */
};
```

* Elder calls `init` once per device that uses the library, so one model can
  serve several devices (e.g. USART2 and USART3). `dev` gives the device's
  name, address ranges and IRQs; `config_json` is its own TOML settings as a
  JSON object (everything except `handlers`, `ranges`, `irq`, `description`,
  `connections` and `read_priority`), e.g. the `slaves` of a LibVIO bus.
* `init` returns the instance state, which every other call receives as
  `opaque`. Keep all state there, not in globals. Returning NULL refuses the
  configuration and FastDyn stops.
* `read` and `write` receive absolute addresses; decode registers as
  `addr - dev->ranges[i].base`. Range ends in the TOML are inclusive.
* `irq` lists the interrupts the model hears (`dev->irqs`, `.interrupt`,
  `.serve`). Numbers are Cortex-M exception numbers, which is how the CPU
  reports them: device IRQ n is n + 16 (USART2 on an STM32F4 is IRQ 38, so
  54). Interrupt names from the CMSIS-SVD (`USART2`) are converted for you.
  This is separate from raising an interrupt, where models pass IRQn + 16 to
  `qemu_plugin_raise_irq`.
* The emulator's host functions (`qemu_plugin_raise_irq`, timers, guest
  memory and registers) are declared by `<fastdyn/device_model.h>`, which
  includes QEMU's `<qemu/qemu-plugin.h>`; models do not include it themselves.
* `host->log(level, dev->name, ...)` writes to the FastDyn log as
  `fastdyn.<device>|LEVEL|`; LibVIO's messages appear as `fastdyn.vio.*`.
* Build models with `fastdyn build-model` (see [Running FastDyn](RunningFastDyn.md)).
  A wrong function signature is a compile error.

FastDyn stops at startup if the descriptor is missing a required function or
was built for another interface version. A library without `fastdyn_model`
(an older model exporting `<device>_init/_read/_write`) is refused at startup;
port it to the interface above.

## Learning models from a hardware trace

Learning a model from the I/O trace of a run on the board (capturing the trace,
generating the model with an LLM, and verifying it against the trace) is
[BoardRunner](https://github.com/boardrunner2049/BoardRunner)'s workflow; its README
describes it. The models it produces are elder models as described above.

[Back to Main Page](FastDynDeviceModel.md)



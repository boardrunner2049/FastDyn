# Virtual Instructions and Modifiers

FastDyn can alter an emulated firmware run at a selected guest program
counter (PC) without changing the firmware image. Both features are configured
inside a `[[CPU.<name>]]` TOML table, but they serve different purposes:

- A **virtual instruction** invokes named FastDyn behavior at a PC: for
  example, raise an IRQ, start a timer, or emit a diagnostic.
- A **modifier** patches guest register or memory state at a PC: for example,
  force a register value or redirect control flow.

## TOML form

```toml
[CPU]

[[CPU.cpu0]]
arch = "arm"
machine = "cortexm"
cpu = "cortex-m4"
binary = "firmware.elf"

[[CPU.cpu0.virtuals]]
at = "0x08001234"
instruction = "raiseirq"
args = ["42"]

[[CPU.cpu0.modifiers]]
at = "0x08005678"
patch = "r0 <- 1"
```

At run time FastDyn turns these entries into the following generated files
under `<work-dir>/virtuals/`:

```text
# virtuals.txt
0x08001234 raiseirq 42

# modifiers.txt
0x08005678 r0 <- 1
```

`at` is the PC of the guest instruction that triggers the entry. Numeric
addresses may be decimal or hexadecimal. In the TOML form, `args` is an array
of argument tokens (optional, empty when left out); FastDyn joins those tokens
with spaces before passing the resulting text to the selected virtual.
Existing `virtuals.txt` and `modifiers.txt` files may also be supplied through
`existing_config_path`.

## What happens at the trigger PC

For this virtual:

```toml
[[CPU.cpu0.virtuals]]
at = "0x08001234"
instruction = "raiseirq"
args = ["42"]
```

when QEMU executes the guest instruction at PC `0x08001234`, FastDyn invokes
the `raiseirq` callback and raises interrupt vector 42. The address is the
trigger; it is not the interrupt vector.

For this modifier:

```toml
[[CPU.cpu0.modifiers]]
at = "0x08005678"
patch = "r15 <- 0x08006000"
```

when QEMU executes the guest instruction at `0x08005678`, FastDyn updates ARM
register `r15` (the PC) to `0x08006000`. This is useful for bypassing or
redirecting a firmware path.

## Underlying difference

Both entries are installed while QEMU translates the relevant guest basic
block, and both run every time that guest PC executes. The difference is how
they act:

```text
virtual:  guest PC hit -> named C callback -> behavioral action
modifier: guest PC hit -> specialized inline operation -> state assignment
```

A virtual is a normal plugin callback from the built-in callback registry. It
can perform arbitrary behavior implemented in C, including actions that happen
to change guest state. A modifier is a declarative assignment parsed once and
registered as FastDyn/QEMU's specialized inline register or memory update.

There is therefore some capability overlap, but use the feature that expresses
the intent:

- Use a **virtual** for events and behavior: interrupts, timers, logging,
  randomization, loading data, or custom callback logic.
- Use a **modifier** for a direct deterministic register or memory patch. It
  requires no new callback and avoids normal callback dispatch.

## Built-in virtual instructions

The plugin's built-in registry currently provides the following names. They
are case-sensitive.

To browse the user-configurable virtual instructions and run-wide plugins from
the terminal, including a ready-to-copy TOML example for each selection, run:

```bash
fastdyn help virtuals
```

`fastdyn help plugins` is an alias. Use `--no-browse` to print a
script-friendly catalog. Browse modifier forms with `fastdyn help modifiers`.

| Instruction | Arguments | Effect when its trigger PC executes |
|---|---|---|
| `raiseirq` | `<irq>` | Raise IRQ `<irq>` immediately. Example: `args = ["42"]`. |
| `pulseirq` | `<irq>` | Pulse IRQ `<irq>`. |
| `raise_periodic_irq` | `<irq>[,<period_ns>]` | Register a periodic IRQ. The period defaults to `timer_irq_period_ns` (1 ms unless configured). Example: `args = ["15,1000000"]`. |
| `updatemem` | `<address>:<r\|w>:<length>:<byte,...>` | Read (`r`) or write (`w`) guest memory. Both forms require exactly `<length>` comma-separated byte values; read-mode values are only a parser placeholder. |
| `randstate` | comma-separated register numbers or memory addresses | Randomize a register when the number is below 100, or write one random byte to a memory address otherwise. Do not use ARM register 15/PC here. |
| `printreg` | `<register-number>` | Print the selected QEMU register. |
| `debug_log` | free-form message | Print the message with CPU and virtual-time information. |
| `benchmark_start` | none | Start a host monotonic-time benchmark region and reset its tick counter. |
| `bench_tick` | none | Increment the benchmark tick counter. |
| `benchmark_end` | optional tag | Print elapsed benchmark time and terminate QEMU. This is deliberately a terminal action. |
| `timer_start` | ignored | Start the legacy fixed-period virtual-clock timer. |
| `start_budgeting` | ignored | Enter QEMU plugin budget waiting. |
| `dyninst` | `<address>:<file>` | Load a host file and write its bytes into guest memory at `<address>`. |
| `dyninst_lib` | `<elf-file>` | Load an ELF dynamically through the QEMU plugin API. |
| `dumplog` | `<logger-index>:<file>` | Dump an internal logger buffer to a host file. |

The callback registry can also be extended in C with `virtual_register()`;
such additions are not automatically available in an unmodified build.

For virtuals that need host-side argument preparation, or for firmware-wide
modules that generate internal virtuals, see the
[Virtual and Run Preprocessing SDK](VirtualPreprocessing.md).

### Virtual examples

Raise IRQ 42 at the firmware's selected trigger address:

```toml
[[CPU.cpu0.virtuals]]
at = "0x08001234"
instruction = "raiseirq"
args = ["42"]
```

Register a 1 ms periodic SysTick-style interrupt when initialization reaches
the trigger:

```toml
[[CPU.cpu0.virtuals]]
at = "0x08001234"
instruction = "raise_periodic_irq"
args = ["15,1000000"]
```

Write four bytes to guest RAM:

```toml
[[CPU.cpu0.virtuals]]
at = "0x08001234"
instruction = "updatemem"
args = ["0x20001000:w:4:0xde,0xad,0xbe,0xef"]
```

## Modifiers

The canonical modifier grammar is:

```text
<trigger-address> <target> <- <value>
```

The parser also accepts `=`, `:=`, and `->` in place of `<-`; use `<-` in
new configuration for clarity.

### Targets and values

| Form | Meaning |
|---|---|
| `rN` or `xN` | Target or source register by QEMU register slot. |
| `[rN]` or `[xN]` | Target/source through the address held in that register. |
| `0xADDRESS` on the left | Absolute guest-memory target. The current native modifier path rejects targets above `0x40000000`; use this only for code/RAM addresses. |
| integer or `0x...` on the right | Immediate value. |
| `rip`, `rsp` | Explicit x86-64 instruction and stack-pointer slots. |
| `riscv_pc` or `pc32` | Explicit RISC-V PC slot. |

For portability, prefer explicit architecture-specific register names instead
of ambiguous aliases such as `pc` and `sp`: use `r15`/`r13` for 32-bit ARM,
`rip`/`rsp` for x86-64, and `riscv_pc`/`xN` for RISC-V.

### Modifier examples

Set an ARM general-purpose register:

```toml
[[CPU.cpu0.modifiers]]
at = "0x08001234"
patch = "r0 <- 1"
```

Redirect ARM control flow:

```toml
[[CPU.cpu0.modifiers]]
at = "0x08001234"
patch = "r15 <- 0x08004567"
```

Set x86-64 RIP:

```toml
[[CPU.cpu0.modifiers]]
at = "0x18008"
patch = "rip <- 0x18004"
```

Write an immediate through an ARM register-held pointer:

```toml
[[CPU.cpu0.modifiers]]
at = "0x08001234"
patch = "[r0] <- 0x42"
```

## Operational notes

- Trigger addresses must be instruction addresses for the active firmware and
  architecture. A trigger that is never executed has no effect.
- Virtuals and modifiers are de-duplicated when FastDyn builds the QEMU
  command, including entries loaded through `existing_config_path`.
- A virtual callback receives the space-joined TOML `args` array as its
  argument string; choose the tokens and syntax from the table above.
- Validate a new rule with a short run and `debug_log` or QEMU logging before
  relying on it in a rehosting or fuzzing campaign.

ENTRY(Reset_Handler)
SECTIONS {
  .isr_vector 0x08000000 : { KEEP(*(.isr_vector)) }
  .text 0x08000100 : {
    *(.text.reset)
    *(.text.spacer)
    *(.text.worker)
  }
  .rodata 0x08001000 : { *(.rodata) }
}

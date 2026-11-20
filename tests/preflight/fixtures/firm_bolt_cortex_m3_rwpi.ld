ENTRY(Reset_Handler)

PHDRS {
  flash PT_LOAD FLAGS(5);
  ram PT_LOAD FLAGS(6);
}

SECTIONS {
  .isr_vector 0x08000000 : { KEEP(*(.isr_vector)) } :flash
  .text 0x08000100 : { *(.text) } :flash
  .data 0x20000000 : { *(.data) } :ram
}

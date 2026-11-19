ENTRY(Reset_Handler)
SECTIONS {
  .isr_vector 0x08000000 : { *(.isr_vector) }
  .text 0x08000100 : { *(.text) }
  .dispatch 0x08000200 : { *(.dispatch) }
}

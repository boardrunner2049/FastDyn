ENTRY(Reset_Handler)

MEMORY
{
  FLASH_CODE (rx) : ORIGIN = 0x08000000, LENGTH = 32K
  FLASH_DATA (r)  : ORIGIN = 0x08008000, LENGTH = 32K
  RAM (rwx)       : ORIGIN = 0x20000000, LENGTH = 16K
}

SECTIONS
{
  .isr_vector ORIGIN(FLASH_CODE) :
  {
    KEEP(*(.isr_vector))
  } > FLASH_CODE

  .text :
  {
    *(.text*)
  } > FLASH_CODE

  /* A distinct, allocated, non-writable object with retained relocation
     records.  It is deliberately outside the rewritten function range. */
  .rodata :
  {
    KEEP(*(.rodata.detached_switch))
    *(.rodata*)
  } > FLASH_DATA

  .data :
  {
    *(.data*)
  } > RAM AT > FLASH_DATA

  .bss (NOLOAD) :
  {
    *(.bss*)
    *(COMMON)
  } > RAM
}

ENTRY(Reset_Handler)

MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 64K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 64K
}

SECTIONS
{
  .isr_vector ORIGIN(FLASH) :
  {
    KEEP(*(.isr_vector))
  } > FLASH

  .text :
  {
    *(.text.Reset_Handler)
    *(.text.vListInitialiseItem)
    *(.text*)
    *(.rodata*)
  } > FLASH

  .bss (NOLOAD) :
  {
    *(.bss*)
    *(COMMON)
  } > RAM
}

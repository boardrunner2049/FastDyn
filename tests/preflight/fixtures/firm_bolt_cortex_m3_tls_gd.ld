ENTRY(Reset_Handler)

MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 64K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 16K
}

SECTIONS
{
  .isr_vector ORIGIN(FLASH) :
  {
    KEEP(*(.isr_vector))
  } > FLASH

  .text ORIGIN(FLASH) + 0x100 :
  {
    *(.text*)
  } > FLASH

  .got ORIGIN(RAM) + 0x100 :
  {
    *(.got*)
  } > RAM AT > FLASH

  .tdata ORIGIN(RAM) + 0x200 :
  {
    *(.tdata*)
  } > RAM AT > FLASH

  .data ORIGIN(RAM) + 0x300 :
  {
    *(.data*)
  } > RAM AT > FLASH

  .bss ORIGIN(RAM) (NOLOAD) :
  {
    *(.bss*)
    *(COMMON)
  } > RAM
}

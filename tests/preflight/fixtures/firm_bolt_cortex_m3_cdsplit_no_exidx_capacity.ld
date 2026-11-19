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
    . = . + 8;
  } > FLASH

  .ARM.exidx :
  {
    *(.ARM.exidx*)
  } > FLASH

  /* Negative control: the following byte immediately owns the next address,
     so an additional fragment range record cannot be installed safely. */
  .firmbolt.exidx.end :
  {
    BYTE(0)
  } > FLASH

  .data :
  {
    *(.data*)
  } > RAM AT > FLASH

  .bss (NOLOAD) :
  {
    *(.bss*)
    *(COMMON)
  } > RAM
}

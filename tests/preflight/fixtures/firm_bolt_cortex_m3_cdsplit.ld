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
    /* Linker-owned capacity for cross-fragment branch widening/alignment. */
    . = . + 8;
  } > FLASH

  .ARM.exidx :
  {
    *(.ARM.exidx*)
  } > FLASH

  /* One additional EHABI range record is linker-owned capacity.  The
     following allocated marker keeps the gap file- and memory-backed. */
  .firmbolt.exidx.end ADDR(.ARM.exidx) + SIZEOF(.ARM.exidx) + 8 :
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

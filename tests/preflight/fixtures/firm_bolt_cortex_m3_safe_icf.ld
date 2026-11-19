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

  .rodata :
  {
    *(.rodata*)
  } > FLASH

  /* BOLT's relocation writer aligns its replacement text to 64 bytes.  Keep
     that requirement in the declared linker layout rather than inventing
     post-link flash outside the original image. */
  .text 0x08000100 :
  {
    *(.text*)
  } > FLASH
}

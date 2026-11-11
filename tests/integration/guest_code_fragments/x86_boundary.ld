ENTRY(_start)
SECTIONS
{
  . = 0x00100000;
  .multiboot : { *(.multiboot) }
  .text : { *(.text) }
  . = 0x00101fff;
  .boundary : { *(.boundary) }
}

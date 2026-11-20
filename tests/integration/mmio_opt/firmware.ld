ENTRY(reset)
SECTIONS {
  . = 0;
  .vectors : { *(.vectors) }
  . = 0x100;
  .text : { *(.text*) }
}

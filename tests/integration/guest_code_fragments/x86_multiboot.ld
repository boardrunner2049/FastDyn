ENTRY(_start)
SECTIONS {
  . = 0x100000;
  .multiboot : { *(.multiboot) }
  .text : { *(.text*) }
  .bss : { *(.bss*) }
}

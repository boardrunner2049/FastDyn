ENTRY(boot)
SECTIONS {
    . = 0x08000000;
    .vectors : { KEEP(*(.vectors)) }
    . = 0x08000100;
    .text : { *(.text) }
    /DISCARD/ : { *(.ARM.attributes) }
}

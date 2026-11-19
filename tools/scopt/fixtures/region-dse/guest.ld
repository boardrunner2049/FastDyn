ENTRY(start)
SECTIONS {
    . = 0x08000000;
    .vectors : { KEEP(*(.vectors)) }
    . = 0x08000100;
    .text : { *(.start_code) *(.coverage_code) }
    . = 0x08001000;
    .source : { *(.source_code) }
    . = 0x08002000;
    .consumer : { *(.consumer_code) }
    /DISCARD/ : { *(.ARM.attributes) }
}

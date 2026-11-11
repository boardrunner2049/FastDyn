ENTRY(reset)
SECTIONS {
  . = 0; .vectors : { *(.vectors) }
  . = 8; .text : { *(.text*) }
}

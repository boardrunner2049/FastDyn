.section .debug_aranges,"",%progbits
.Laranges_start:
  .long .Laranges_end - .Laranges_header
.Laranges_header:
  .short 2
  .long 0
  .byte 4
  .byte 0
  .long 0
  .long worker
  .long 16
  .long 0
  .long 0
.Laranges_end:

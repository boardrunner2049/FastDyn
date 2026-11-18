/* Executed Cortex-M3 fixture for native TBB hot-index promotion. */
  .syntax unified
  .cpu cortex-m3
  .thumb

  .section .isr_vector,"a",%progbits
  .word 0x20001000
  .word Reset_Handler

  .text
  .p2align 2
  .global Reset_Handler
  .type Reset_Handler,%function
  .thumb_func
Reset_Handler:
  movs r0, #0
  bl jump_table_worker
  ldr r1, =0x20000000
  str r0, [r1]
  .global fixture_halt
fixture_halt:
  b fixture_halt
  .size Reset_Handler, .-Reset_Handler

  .p2align 2
  .global jump_table_worker
  .type jump_table_worker,%function
  .thumb_func
jump_table_worker:
  cmp r0, #2
  bhi .Ldefault
  tbb [pc, r0]
.Ltable:
  .byte (.Lcase0 - .Ltable) / 2
  .byte (.Lcase1 - .Ltable) / 2
  .byte (.Lcase2 - .Ltable) / 2
  .byte 0
.Lcase0:
  movs r0, #10
  bx lr
.Lcase1:
  movs r0, #20
  bx lr
.Lcase2:
  movs r0, #30
  bx lr
.Ldefault:
  movs r0, #0
  bx lr
  /* Proven unreachable capacity for the checked in-place expansion. */
  .rept 16
  nop
  .endr
  .size jump_table_worker, .-jump_table_worker

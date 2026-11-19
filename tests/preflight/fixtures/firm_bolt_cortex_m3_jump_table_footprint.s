/* Executed Cortex-M3 fixture for checked TBH-to-TBB footprint reduction. */
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
  ldr r4, =0x20000000
  movs r0, #0
  bl odd_byte_range_switch
  str r0, [r4, #0]
  movs r0, #1
  bl odd_byte_range_switch
  str r0, [r4, #4]
  movs r0, #2
  bl odd_byte_range_switch
  str r0, [r4, #8]
  movs r0, #0
  bl halfword_range_switch
  str r0, [r4, #12]
  .global fixture_halt
fixture_halt:
  b fixture_halt
  .size Reset_Handler, .-Reset_Handler

  .p2align 2
  .global odd_byte_range_switch
  .type odd_byte_range_switch,%function
  .thumb_func
odd_byte_range_switch:
  cmp r0, #2
  bhi .Lodd_default
  tbh [pc, r0, lsl #1]
.Lodd_table:
  .hword (.Lodd_case0 - .Lodd_table) / 2
  .hword (.Lodd_case1 - .Lodd_table) / 2
  .hword (.Lodd_case2 - .Lodd_table) / 2
.Lodd_case0:
  movs r0, #10
  bx lr
.Lodd_case1:
  movs r0, #20
  bx lr
.Lodd_case2:
  movs r0, #30
  bx lr
.Lodd_default:
  movs r0, #0
  bx lr
  .size odd_byte_range_switch, .-odd_byte_range_switch

  .p2align 2
  .global halfword_range_switch
  .type halfword_range_switch,%function
  .thumb_func
halfword_range_switch:
  cmp r0, #0
  bhi .Lhalf_default
  tbh [pc, r0, lsl #1]
.Lhalf_table:
  .hword (.Lhalf_case0 - .Lhalf_table) / 2
  /* The valid target is beyond TBB's unsigned-byte displacement. */
  .space 510
.Lhalf_case0:
  movs r0, #42
  bx lr
.Lhalf_default:
  movs r0, #0
  bx lr
  .size halfword_range_switch, .-halfword_range_switch

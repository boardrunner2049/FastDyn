.syntax unified
.cpu cortex-m3
.thumb

.equ ITERATIONS, 25000000

.section .isr_vector, "a", %progbits
.align 2
.word 0x20010000
.word Reset_Handler + 1

.section .text.Reset_Handler, "ax", %progbits
.align 2
.global Reset_Handler
.type Reset_Handler, %function
.thumb_func
Reset_Handler:
  ldr r2, =ITERATIONS
benchmark_start_anchor:
  nop
1:
  ldr r0, =list_item
  bl vListInitialiseItem
  subs r2, r2, #1
  bne 1b
benchmark_end_anchor:
  nop
2:
  b 2b
.size Reset_Handler, .-Reset_Handler

.section .text.vListInitialiseItem, "ax", %progbits
.align 2
.global vListInitialiseItem
.type vListInitialiseItem, %function
.thumb_func
vListInitialiseItem:
  sub sp, #4
  str r0, [sp]
  ldr r1, [sp]
  movs r0, #0
  str r0, [r1, #16]
  add sp, #4
  bx lr
.size vListInitialiseItem, .-vListInitialiseItem

.section .bss.list_item, "aw", %nobits
.align 2
list_item:
  .space 32

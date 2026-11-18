.syntax unified
.cpu cortex-m0plus
.thumb

.section .isr_vector,"a",%progbits
.align 2
.global g_pfnVectors
g_pfnVectors:
  .word 0x20001000
  .word Reset_Handler

.text
.align 2
.global Reset_Handler
.type Reset_Handler,%function
.thumb_func
Reset_Handler:
  bl relocated_pool
  bx lr
  nop
.size Reset_Handler, .-Reset_Handler

.global relocated_pool
.type relocated_pool,%function
.thumb_func
relocated_pool:
  ldr r0, .Lliteral
  b .Lreturn
.Lreturn:
  bx lr
  .align 2
.Lliteral:
  .word external_object
.size relocated_pool, .-relocated_pool

.align 2
.global external_object
.type external_object,%object
external_object:
  .word 0x12345678
.size external_object, .-external_object

.syntax unified
.cpu cortex-m3
.thumb

.section .isr_vector, "a", %progbits
.align 2
.global g_pfnVectors
.type g_pfnVectors, %object
g_pfnVectors:
  .word 0x20001000
  .word Reset_Handler
  .word Default_Handler
  .word Default_Handler
.size g_pfnVectors, . - g_pfnVectors

.section .text, "ax", %progbits
.align 2
.global Reset_Handler
.type Reset_Handler, %function
.thumb_func
Reset_Handler:
  ldr r0, =narrow_values
  ldrb r1, [r0]
  ldrh r2, [r0, #2]
  adds r1, r1, r2
  movw r0, #0
  movt r0, #0x2000
  str r1, [r0]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.global Default_Handler
.type Default_Handler, %function
.thumb_func
Default_Handler:
  b Default_Handler
.size Default_Handler, . - Default_Handler

.section .rodata.narrow, "a", %progbits
.align 1
.global narrow_values
.type narrow_values, %object
narrow_values:
  .byte answer8
  .byte 0
  .short answer16
.size narrow_values, . - narrow_values

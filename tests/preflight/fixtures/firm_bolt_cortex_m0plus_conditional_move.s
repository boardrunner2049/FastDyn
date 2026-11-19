.syntax unified
.cpu cortex-m0plus
.thumb
.eabi_attribute Tag_CPU_arch, 12
.eabi_attribute Tag_CPU_arch_profile, 'M'

/* Armv6-M retention qualification for BOLT's CMOVConversion pass.  R12 is
   caller-saved and the high-register MOV encoding is flag-neutral, but
   Cortex-M0+ has no IT instruction.  Firm-BOLT must therefore retain this
   branch even when the firmware contract otherwise admits conversion. */
.section .isr_vector, "a", %progbits
.align 2
.global g_pfnVectors
.type g_pfnVectors, %object
g_pfnVectors:
  .word 0x20001000
  .word Reset_Handler
.size g_pfnVectors, . - g_pfnVectors

.section .text, "ax", %progbits
.align 2
.global Reset_Handler
.type Reset_Handler, %function
.thumb_func
Reset_Handler:
  ldr r4, =0x20000000

  movs r0, #0
  movs r1, #7
  movs r2, #42
  mov r12, r1
  bl select_value
  str r0, [r4]

  movs r0, #1
  movs r1, #7
  movs r2, #42
  mov r12, r1
  bl select_value
  str r0, [r4, #4]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.global select_value
.type select_value, %function
.thumb_func
select_value:
  cmp r0, #0
  bne 1f
  mov r12, r2
1:
  mov r0, r12
  bx lr
.size select_value, . - select_value

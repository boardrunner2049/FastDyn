.syntax unified
.cpu cortex-m3
.thumb
.eabi_attribute Tag_CPU_arch, 10
.eabi_attribute Tag_CPU_arch_profile, 'M'

/* Executed qualification for BOLT's CMOVConversion pass on Thumb. The
   admitted contract permits one branch event to become an explicit IT block;
   the selected register move remains tied to the same incoming NZCV state. */
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
  movw r4, #0
  movt r4, #0x2000

  movs r0, #0
  movs r1, #7
  movs r2, #42
  bl select_value
  str r0, [r4]

  movs r0, #1
  movs r1, #7
  movs r2, #42
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
  mov.w r1, r2
1:
  mov r0, r1
  bx lr
.size select_value, . - select_value

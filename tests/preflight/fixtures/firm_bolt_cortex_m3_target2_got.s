.syntax unified
.cpu cortex-m3
.thumb
.eabi_attribute Tag_CPU_arch, 10
.eabi_attribute Tag_CPU_arch_profile, 'M'

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
  bl target2_got_worker
  movw r1, #0
  movt r1, #0x2000
  str r0, [r1]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.align 2
.global target2_got_worker
.type target2_got_worker, %function
.thumb_func
target2_got_worker:
  ldr r0, .Ltarget2_delta
.Ltarget2_pc:
  add r0, pc
  adds r0, #6
  ldr r0, [r0]
  ldr r0, [r0]
  bx lr
.align 2
.Ltarget2_delta:
  // Under --target2=got-rel this is G(value)+A-P, but the retained ELF
  // relocation identity must remain platform-defined R_ARM_TARGET2.
  .long value(TARGET2)
.size target2_got_worker, . - target2_got_worker

.section .rodata, "a", %progbits
.align 2
.global value
.type value, %object
value:
  .long 42
.size value, . - value

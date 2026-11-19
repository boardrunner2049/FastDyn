.syntax unified
.cpu cortex-m3
.thumb
.eabi_attribute Tag_CPU_arch, 10
.eabi_attribute Tag_CPU_arch_profile, 'M'

.section .isr_vector, "a", %progbits
.p2align 2
.global g_pfnVectors
.type g_pfnVectors, %object
g_pfnVectors:
  .word 0x20001000
  .word Reset_Handler
.size g_pfnVectors, . - g_pfnVectors

.section .text, "ax", %progbits
.p2align 2
.global Reset_Handler
.type Reset_Handler, %function
.thumb_func
Reset_Handler:
  .fnstart
  ldr r4, =0x20000000
  bl external_call_a
  str r0, [r4]
  bl external_call_b
  str r0, [r4, #4]
.global fixture_halt
fixture_halt:
  b fixture_halt
  .cantunwind
  .fnend
.size Reset_Handler, . - Reset_Handler

.weak __aeabi_unwind_cpp_pr1

/* LLVM MC/LLD emits separate .ARM.extab records for these two functions even
 * though their complete personality-index-1 compact models are byte-identical.
 * Safe ICF may fold the code only after comparing both complete records. */
.p2align 2
.global external_call_a
.type external_call_a, %function
.thumb_func
external_call_a:
  .fnstart
  .personalityindex 1
  movs r0, #17
  bx lr
  .fnend
.size external_call_a, . - external_call_a

.p2align 2
.global external_call_b
.type external_call_b, %function
.thumb_func
external_call_b:
  .fnstart
  .personalityindex 1
  movs r0, #17
  bx lr
  .fnend
.size external_call_b, . - external_call_b

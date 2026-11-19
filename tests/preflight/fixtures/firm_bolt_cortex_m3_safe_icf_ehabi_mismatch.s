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
  bl direct_call_a
  bl direct_call_b
.global fixture_halt
fixture_halt:
  b fixture_halt
  .cantunwind
  .fnend
.size Reset_Handler, . - Reset_Handler

/* The machine bodies are identical, but the admitted unwind models are not.
 * ICF may discover the code equivalence; final EHABI validation must reject
 * publication rather than silently selecting either metadata contract. */
.p2align 2
.global direct_call_a
.type direct_call_a, %function
.thumb_func
direct_call_a:
  .fnstart
  movs r0, #42
  bx lr
  .cantunwind
  .fnend
.size direct_call_a, . - direct_call_a

.p2align 2
.global direct_call_b
.type direct_call_b, %function
.thumb_func
direct_call_b:
  .fnstart
  .personalityindex 0
  movs r0, #42
  bx lr
  .fnend
.size direct_call_b, . - direct_call_b

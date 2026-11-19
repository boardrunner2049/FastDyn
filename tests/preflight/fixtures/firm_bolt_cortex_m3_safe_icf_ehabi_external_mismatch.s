.syntax unified
.cpu cortex-m3
.thumb
.eabi_attribute Tag_CPU_arch, 10
.eabi_attribute Tag_CPU_arch_profile, 'M'

.section .isr_vector, "a", %progbits
.p2align 2
.word 0x20001000
.word Reset_Handler

.section .text, "ax", %progbits
.p2align 2
.global Reset_Handler
.type Reset_Handler, %function
.thumb_func
Reset_Handler:
  .fnstart
  bl external_call_a
  bl external_call_b
fixture_halt_external_mismatch:
  b fixture_halt_external_mismatch
  .cantunwind
  .fnend
.size Reset_Handler, . - Reset_Handler

.weak __aeabi_unwind_cpp_pr1
.weak __aeabi_unwind_cpp_pr2

/* The machine code is identical but the complete external compact models use
 * different ARM personality indexes.  Publication must reject the ICF pair. */
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
  .personalityindex 2
  movs r0, #17
  bx lr
  .fnend
.size external_call_b, . - external_call_b

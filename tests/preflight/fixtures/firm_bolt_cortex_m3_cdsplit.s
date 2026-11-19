.syntax unified
.cpu cortex-m3
.thumb
.eabi_attribute Tag_CPU_arch, 10
.eabi_attribute Tag_CPU_arch_profile, 'M'

/* Three-way splitting fixture.  The frequent input returns through the hot
   path, the less frequent input traverses the warm chain, and the held-out
   input executes the zero-count cold block.  Reset_Handler records all three
   results so the emitted fragments are independently execution checked. */
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
.global cdsplit_worker
.type cdsplit_worker, %function
.thumb_func
cdsplit_worker:
  .fnstart
  .cantunwind
  cmp r0, #2
  bge .Lchain_start
.Lfast:
  movs r0, #5
  b .Lexit
.Lchain_start:
  mov.w r1, #10
  bgt .Lchain1
.Lcold:
  adds r1, #1
  adds r1, #1
  adds r1, #1
  adds r1, #1
  b .Lexit
.Lchain1:
  .rept 8
  adds r1, #1
  .endr
  b .Lchain2
.Lchain2:
  .rept 8
  adds r1, #1
  .endr
  b .Lchain3
.Lchain3:
  .rept 8
  adds r1, #1
  .endr
  b .Lchain4
.Lchain4:
  .rept 8
  adds r1, #1
  .endr
  mov r0, r1
  b .Lexit
.Lexit:
  bx lr
  .fnend
.size cdsplit_worker, . - cdsplit_worker

.weak __aeabi_unwind_cpp_pr1

.align 2
.global Reset_Handler
.type Reset_Handler, %function
.thumb_func
Reset_Handler:
  .fnstart
  .personalityindex 1
  movw r4, #0
  movt r4, #0x2000
  movs r0, #1
.global hot_callsite
hot_callsite:
  bl cdsplit_worker
  str r0, [r4]
  movs r0, #4
.global warm_callsite
warm_callsite:
  bl cdsplit_worker
  str r0, [r4, #4]
  movs r0, #2
.global cold_callsite
cold_callsite:
  bl cdsplit_worker
  str r0, [r4, #8]
.global fixture_halt
fixture_halt:
  b fixture_halt
  .fnend
.size Reset_Handler, . - Reset_Handler

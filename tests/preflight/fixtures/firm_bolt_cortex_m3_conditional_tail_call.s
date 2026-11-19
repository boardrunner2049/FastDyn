.syntax unified
.cpu cortex-m3
.thumb
.eabi_attribute Tag_CPU_arch, 10
.eabi_attribute Tag_CPU_arch_profile, 'M'

/* Executed qualification for BOLT's SimplifyConditionalTailCalls pass. The
   admitted contract permits one conditional branch plus one tail branch to
   become a conditional tail branch while preserving incoming LR and all
   explicitly mapped original instruction identities. */
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
  bl conditional_tail
  str r0, [r4]

  movs r0, #1
  bl conditional_tail
  str r0, [r4, #4]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.global conditional_tail
.type conditional_tail, %function
.thumb_func
conditional_tail:
  cmp r0, #0
  beq 1f
  movs r0, #7
  bx lr
1:
  b tail_target
.size conditional_tail, . - conditional_tail

.global tail_target
.type tail_target, %function
.thumb_func
tail_target:
  movs r0, #42
  bx lr
.size tail_target, . - tail_target

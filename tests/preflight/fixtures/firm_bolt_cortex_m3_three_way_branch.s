.syntax unified
.cpu cortex-m3
.thumb
.eabi_attribute Tag_CPU_arch, 10
.eabi_attribute Tag_CPU_arch_profile, 'M'

/* Executed qualification for BOLT's native ThreeWayBranch pass. The pass
   rewrites two branches that consume one CMP under a frozen profile while
   retaining all original instruction identities. The admitted contract
   explicitly permits changed branch identity/path order and modeled timing. */
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
  bl classify
  str r0, [r4]

  movs r0, #1
  bl classify
  str r0, [r4, #4]

  movs r0, #2
  bl classify
  str r0, [r4, #8]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.global classify
.type classify, %function
.thumb_func
classify:
  cmp r0, #1
  blt 3f
2:
  beq 4f
1:
  movs r0, #30
  bx lr
3:
  movs r0, #10
  bx lr
4:
  movs r0, #20
  bx lr
.size classify, . - classify

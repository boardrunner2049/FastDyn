.syntax unified
.cpu cortex-m4
.fpu fpv4-sp-d16
.thumb

.eabi_attribute Tag_CPU_arch, 13
.eabi_attribute Tag_CPU_arch_profile, 'M'
.eabi_attribute Tag_FP_arch, 6
.eabi_attribute Tag_ABI_HardFP_use, 1

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
  /* Enable CP10/CP11 before executing the scalar VFP fixture. */
  movw r0, #0xed88
  movt r0, #0xe000
  ldr r1, [r0]
  orr.w r1, r1, #0x00f00000
  str r1, [r0]
  dsb sy
  isb sy

  /* 1.0f is carried through the stack spill/reload and written to RAM. */
  movw r0, #0x0000
  movt r0, #0x3f80
  vmov s0, r0
  bl vfp_frame_worker
  vmov r0, s1
  movw r1, #0x0000
  movt r1, #0x2000
  str r0, [r1]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.global vfp_frame_worker
.type vfp_frame_worker, %function
.thumb_func
vfp_frame_worker:
  sub sp, #8
  vstr s0, [sp]
  vldr s1, [sp]
  add sp, #8
  bx lr
.size vfp_frame_worker, . - vfp_frame_worker

.global Default_Handler
.type Default_Handler, %function
.thumb_func
Default_Handler:
  b Default_Handler
.size Default_Handler, . - Default_Handler

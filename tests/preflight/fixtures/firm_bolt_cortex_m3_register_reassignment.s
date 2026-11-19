.syntax unified
.cpu cortex-m3
.thumb
.eabi_attribute Tag_CPU_arch, 10
.eabi_attribute Tag_CPU_arch_profile, 'M'

/* Executed qualification for target-aware BOLT register reassignment. The
   admitted contract treats R4/R8 as AAPCS callee-saved internal state and
   separately admits R2/R3/R12 caller-saved exchange only when ABI-aware
   liveness proves both exact names dead at every entry. An ordinary direct
   call is admitted only when its exact transitive callee-use summary observes
   neither exchanged name and both values are dead after the call. There is no
   instruction-boundary register observer or interrupt inside an accepted
   worker. Final values and live-in, callee-observed, unknown-call and
   live-after-call rejection controls are checked. */
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
  .fnstart
  movw r5, #0
  movt r5, #0x2000
  movs r0, #42
  str r0, [r5, #32]
  movs r4, #4
  movs r0, #8
  mov r8, r0
  add r1, r5, #32
  bl worker
  str r0, [r5]
  str r4, [r5, #4]
  mov r0, r8
  str r0, [r5, #8]
  add r1, r5, #32
  bl aggressive_worker
  str r0, [r5, #12]
  movs r2, #5
  movs r3, #7
  add r1, r5, #32
  bl aggressive_live_in_worker
  str r0, [r5, #16]
  add r1, r5, #32
  bl call_worker
  str r0, [r5, #20]
  movs r2, #5
  movs r3, #7
  add r1, r5, #32
  bl arg_call_worker
  str r0, [r5, #24]
  add r1, r5, #32
  bl live_after_call_worker
  str r0, [r5, #28]
.global fixture_halt
fixture_halt:
  b fixture_halt
  .cantunwind
  .fnend
.size Reset_Handler, . - Reset_Handler

.global worker
.type worker, %function
.thumb_func
worker:
  .fnstart
  sub sp, #8
  str.w r8, [sp, #4]
  mov.w r8, r1
  ldr.w r0, [r8, #0]
  str.w r0, [r8, #4]
  ldr.w r8, [sp, #4]
  add sp, #8
  bx lr
  .cantunwind
  .fnend
.size worker, . - worker

.global aggressive_worker
.type aggressive_worker, %function
.thumb_func
aggressive_worker:
  .fnstart
  mov.w r12, r1
  ldr.w r0, [r12, #0]
  str.w r0, [r12, #4]
  bx lr
  .cantunwind
  .fnend
.size aggressive_worker, . - aggressive_worker

/* Both available low caller-saved names are live at entry, so neither may be
   exchanged with R12 even though doing so would shorten the load. */
.global aggressive_live_in_worker
.type aggressive_live_in_worker, %function
.thumb_func
aggressive_live_in_worker:
  .fnstart
  add.w r0, r2, r3
  mov.w r12, r1
  ldr.w r1, [r12, #0]
  add r0, r1
  bx lr
  .cantunwind
  .fnend
.size aggressive_live_in_worker, . - aggressive_live_in_worker

/* The callee reads only r0. R2 and R12 are dead after the call because the
   selected base is redefined before the store, so R2/R12 exchange is safe. */
.global call_worker
.type call_worker, %function
.thumb_func
call_worker:
  .fnstart
  push {lr}
  mov.w r12, r1
  ldr.w r0, [r12, #0]
  bl no_args_callee
  mov.w r12, r1
  str.w r0, [r12, #4]
  pop {pc}
  .cantunwind
  .fnend
.size call_worker, . - call_worker

.global no_args_callee
.type no_args_callee, %function
.thumb_func
no_args_callee:
  .fnstart
  adds r0, #1
  bx lr
  .cantunwind
  .fnend
.size no_args_callee, . - no_args_callee

/* Both candidate low names are transitively read by the direct callee. */
.global arg_call_worker
.type arg_call_worker, %function
.thumb_func
arg_call_worker:
  .fnstart
  push {lr}
  mov.w r12, r1
  ldr.w r0, [r12, #0]
  bl uses_r2_r3_callee
  mov.w r12, r1
  str.w r0, [r12, #4]
  pop {pc}
  .cantunwind
  .fnend
.size arg_call_worker, . - arg_call_worker

.global uses_r2_r3_callee
.type uses_r2_r3_callee, %function
.thumb_func
uses_r2_r3_callee:
  .fnstart
  add.w r0, r2, r3
  bx lr
  .cantunwind
  .fnend
.size uses_r2_r3_callee, . - uses_r2_r3_callee

/* An indirect call has no exact transitive register summary. This function is
   not executed; it is an installation-time retention control. */
.global indirect_call_worker
.type indirect_call_worker, %function
.thumb_func
indirect_call_worker:
  .fnstart
  push {lr}
  mov.w r12, r1
  ldr.w r0, [r12, #0]
  blx r3
  mov.w r12, r1
  str.w r0, [r12, #4]
  pop {pc}
  .cantunwind
  .fnend
.size indirect_call_worker, . - indirect_call_worker

/* The known callee does not observe the candidate names, but R12 is used
   after the call. Its physical clobber behavior therefore cannot be exchanged
   safely without rewriting the callee as one semantic unit. */
.global live_after_call_worker
.type live_after_call_worker, %function
.thumb_func
live_after_call_worker:
  .fnstart
  push {lr}
  mov.w r12, r1
  ldr.w r0, [r12, #0]
  bl no_args_callee
  str.w r0, [r12, #4]
  pop {pc}
  .cantunwind
  .fnend
.size live_after_call_worker, . - live_after_call_worker

/* A direct tail call transfers the boundary state instead of returning to a
   point where local liveness can close the exchange. */
.global tail_call_worker
.type tail_call_worker, %function
.thumb_func
tail_call_worker:
  .fnstart
  mov.w r12, r1
  ldr.w r0, [r12, #0]
  b.w no_args_callee
  .cantunwind
  .fnend
.size tail_call_worker, . - tail_call_worker

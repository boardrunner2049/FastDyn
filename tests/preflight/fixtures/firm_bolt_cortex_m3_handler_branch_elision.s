.syntax unified
.cpu cortex-m3
.thumb

/* Execute a real SVCall entry and EXC_RETURN twice.  The handler's conditional
   branch targets the immediately following instruction, so the explicit
   Firm-BOLT handler contract may remove it while preserving the MRS state
   observation, RAM effect and BX LR exception return. */
.section .isr_vector, "a", %progbits
.align 2
.global g_pfnVectors
.type g_pfnVectors, %object
g_pfnVectors:
  .word 0x20001000
  .word Reset_Handler
  .word Default_Handler       /* NMI */
  .word Default_Handler       /* HardFault */
  .word Default_Handler       /* MemManage */
  .word Default_Handler       /* BusFault */
  .word Default_Handler       /* UsageFault */
  .word 0
  .word 0
  .word 0
  .word 0
  .word SVCall_Handler
.size g_pfnVectors, . - g_pfnVectors

.section .text, "ax", %progbits
.align 2
.global Reset_Handler
.type Reset_Handler, %function
.thumb_func
Reset_Handler:
  movs r4, #40
  movs r5, #0
  movs r0, #0
  svc #0
  movs r0, #1
  svc #0
  movw r6, #0
  movt r6, #0x2000
  str r4, [r6]
  str r5, [r6, #4]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.align 2
.global SVCall_Handler
.type SVCall_Handler, %function
.thumb_func
SVCall_Handler:
  mrs r5, ipsr
  cmp r0, #0
  beq .Lnext
.Lnext:
  adds r4, r4, #1
  bx lr
.size SVCall_Handler, . - SVCall_Handler

.align 2
.global Default_Handler
.type Default_Handler, %function
.thumb_func
Default_Handler:
  b Default_Handler
.size Default_Handler, . - Default_Handler

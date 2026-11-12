.syntax unified
.cpu cortex-m4
.thumb

.section .vectors, "a"
/*
 * These two halfwords are both the initial MSP word and a tiny Thumb
 * function at address zero.  reset installs a usable stack before calling
 * it.  The store forces a callback whose saved PC value is valid zero, then
 * returns through LR.
 */
.hword 0x6008                  /* str r0, [r1] */
.hword 0x4770                  /* bx lr */
.word reset + 1

.section .text, "ax"
.thumb_func
.global reset
reset:
    ldr r3, =0x20010000
    mov sp, r3
    movs r0, #'Z'
    ldr r1, =0x40004000
    adr r3, after_zero
    orr r3, r3, #1
    mov lr, r3
    movs r3, #1                  /* Thumb-state branch to address zero */
    bx r3
after_zero:
    ldr r0, =0x40004018
    ldr r2, [r0]
    sub r0, r0, #0x18
    movs r2, #'P'
    str r2, [r0]
    movs r0, #0x18
    movw r1, #0x26
    movt r1, #2
    bkpt #0xab
1:  b 1b

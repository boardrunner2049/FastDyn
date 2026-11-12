.syntax unified
.cpu cortex-m4
.thumb
.section .vectors,"a"
.word 0x20010000
.word reset + 1
.rept 14
.word 0
.endr
.section .text,"ax"
.thumb_func
.global reset
reset:
    ldr r4, =0x40010000
    ldr r5, [r4, #4]       /* read-to-clear: return old value */
    ldr r0, =0x12345678
    cmp r5, r0
    bne failed
    ldr r5, [r4, #8]       /* subsequent state must be cleared */
    cmp r5, #0
    bne failed
    ldr r5, [r4, #4]       /* second read-to-clear returns zero */
    cmp r5, #0
    bne failed
    ldr r5, [r4, #12]      /* unused volatile read stays ordinary */
    ldr r0, =0x11111111
    cmp r5, r0
    bne failed
    ldr r5, [r4, #16]      /* second array element, not first */
    ldr r0, =0x22222222
    cmp r5, r0
    bne failed
    ldr r5, [r4, #20]      /* WORD result: byte wrap must yield zero */
    cmp r5, #0
    bne failed
    ldr r5, [r4, #32]      /* initial movable selector */
    cmp r5, #0x99
    bne failed
    add r0, r4, #36
    str r0, [r4]          /* setter(&s->base, new_address) */
    ldr r5, [r4, #32]      /* old address no longer selects */
    cmp r5, #0
    bne failed
    ldr r5, [r4, #36]      /* new address selects */
    cmp r5, #0x99
    bne failed
    adr r1, passed
    b report
failed:
    adr r1, failure
report:
    movs r0, #4
    bkpt #0xab
    movs r0, #0x18
    ldr r1, =0x20026
    bkpt #0xab
1:  b 1b
.align 2
passed: .asciz "PASS safety clear_old=12345678 clear_after=0 array=22222222 wrap_word=0 old_base=0 new_base=99\n"
failure: .asciz "FAIL safety\n"

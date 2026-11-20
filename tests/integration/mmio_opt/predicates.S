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
    ldr r5, [r4]
    cmp r5, #0x55
    bne failed
    ldr r5, [r4, #24]
    cmp r5, #0
    bne failed
    ldr r5, [r4, #28]
    cmp r5, #0
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
passed: .asciz "PASS predicates both_mismatch=0\n"
failure: .asciz "FAIL predicates\n"

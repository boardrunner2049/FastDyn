.syntax unified
.cpu cortex-m4
.thumb
.section .vectors,"a"
.word 0x20010000
.word reset + 1
.word 0
.word hardfault + 1
.rept 12
.word 0
.endr
.section .text,"ax"
.thumb_func
.global reset
reset:
    ldr r0, =0xe000ed94    /* MPU disabled explicitly */
    movs r1, #0
    str r1, [r0]
    ldr r0, =0xe000ed24    /* SHCSR: UsageFault disabled -> HardFault */
    str r1, [r0]
    ldr r0, =0xe000ed14    /* CCR.UNALIGN_TRP */
    ldr r1, [r0]
    orr r1, r1, #8
    str r1, [r0]
    dsb sy
    isb sy
    ldr r4, =0x400100ff
    ldrh r5, [r4]
    b failed
.thumb_func
hardfault:
    ldr r0, =0xe000ed28    /* CFSR.UNALIGNED */
    ldr r1, [r0]
    tst r1, #0x01000000
    beq failed
    ldr r0, =0xe000ed2c    /* HFSR.FORCED */
    ldr r1, [r0]
    tst r1, #0x40000000
    beq failed
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
passed: .asciz "PASS alignment HardFault CFSR.UNALIGNED HFSR.FORCED\n"
failure: .asciz "FAIL alignment\n"

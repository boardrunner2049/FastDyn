.syntax unified
.cpu cortex-m4
.thumb

.section .vectors, "a"
.word 0x20010000
.word reset + 1
.rept 14
.word 0
.endr
.word irq0_handler + 1

.section .text, "ax"
.thumb_func
.global reset
reset:
    /* Literal-pool construction of the model's configured base. */
    ldr     r4, =0x40010000

    /* The same translated load runs once fast and once with the MPU enabled. */
    movs    r10, #0
mpu_loop:
    ldr     r5, [r4]
    ldr     r0, =0x11223344
    cmp     r5, r0
    bne     failed
    cmp     r10, #0
    bne     mpu_done
    ldr     r0, =0xe000ed94
    movs    r1, #5              /* ENABLE | PRIVDEFENA */
    str     r1, [r0]
    movs    r10, #1
    b       mpu_loop
mpu_done:
    ldr     r0, =0xe000ed94
    movs    r1, #0
    str     r1, [r0]

    /* Transfer width is independent of the encoded instruction length. */
    ldrb    r5, [r4]
    cmp     r5, #0x44
    bne     failed
    ldrh    r5, [r4]
    movw    r0, #0x3344
    cmp     r5, r0
    bne     failed

    /* A normal write changes the state observed by an already bound read. */
    ldr     r0, =0x55667788
    str     r0, [r4]
    ldr     r5, [r4]
    cmp     r5, r0
    bne     failed

    /* movw/movt construction and an automatically extracted state read. */
    movw    r6, #0x0004
    movt    r6, #0x4001
    ldr     r5, [r6]
    cmp     r5, #0
    bne     failed

    /* The IRQ callback advances a fast-polled field without a slow read. */
    ldr     r6, =0xe000e100     /* NVIC ISER0 */
    movs    r0, #1
    str     r0, [r6]
    ldr     r6, =0xe000ef00     /* NVIC STIR: request external IRQ 0 */
    movs    r0, #0
    str     r0, [r6]
    movw    r6, #0x0004
    movt    r6, #0x4001
poll_irq_progress:
    ldr     r5, [r6]
    cmp     r5, #0
    beq     poll_irq_progress

    /* A constant operation selected by the model's live last-address field. */
    add.w   r6, r4, #0xff
    ldrb    r5, [r6]
    cmp     r5, #0xaa
    bne     failed

    /* No generated operation exists at +0x0c: this remains original MMIO. */
    ldr     r5, [r4, #0x0c]
    cmp     r5, #0
    bne     failed

    /* One PC first sees MMIO, then RAM; its live guard must select each arm. */
    mov     r6, r4
    movs    r7, #0
guard_loop:
    ldr     r5, [r6]
    cmp     r7, #0
    bne     guard_done
    ldr     r6, =ram_word
    movs    r7, #1
    b       guard_loop
guard_done:
    ldr     r0, =0xa1b2c3d4
    cmp     r5, r0
    bne     failed

    /* Invalidate and rebuild a TB; its one registration must remain live. */
    bl      retranslation_read
    ldr     r0, =retranslation_patch
    ldrh    r1, [r0]
    strh    r1, [r0]
    dsb     sy
    isb     sy
    bl      retranslation_read
    ldr     r0, =0x55667788
    cmp     r5, r0
    bne     failed

    /* A fixed workload makes dispatch reduction and run time measurable. */
    ldr     r7, =1000000
benchmark_loop:
    ldr     r5, [r4]
    subs    r7, #1
    bne     benchmark_loop
    ldr     r0, =0x55667788
    cmp     r5, r0
    bne     failed

    adr     r1, pass_message
    b       report
failed:
    adr     r1, fail_message
report:
    movs    r0, #4
    bkpt    #0xab
    movs    r0, #0x18
    movw    r1, #0x26
    movt    r1, #2
    bkpt    #0xab
1:  b       1b

.thumb_func
irq0_handler:
    bx      lr

.thumb_func
retranslation_read:
    ldr     r5, [r4]
retranslation_patch:
    nop
    bx      lr

.align 2
ram_word:
    .word 0xa1b2c3d4
pass_message:
    .asciz "PASS automatic MMIO substitution\n"
fail_message:
    .asciz "FAIL automatic MMIO substitution\n"

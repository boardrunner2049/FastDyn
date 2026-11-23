.syntax unified
.cpu cortex-m3
.thumb

.section .vectors,"a",%progbits
.word 0x20010000
.word boot

.section .text,"ax",%progbits
.thumb_func
.global boot
boot:
    mov.w r3, #0x30000000
    ldr r0, [r3]
    adds r2, r0, #1
    beq .Lcold
    adds r2, r0, #2
    beq .Lrecursive
    bl init
    b print_result
.Lcold:
    bl cold_entry
    b print_result
.Lrecursive:
    movs r0, #2
    bl recursive

.global print_result
print_result:
    nop
.global benchmark_end
benchmark_end:
    nop
1:  b 1b

/* The conditional path and counted backedge force a captured multi-TB
 * callee.  The nested BL is a second firmware call site. */
.thumb_func
.global init
init:
    push {r4, lr}
    movs r4, #3
2:  bl leaf
    subs r4, #1
    bne 2b
    cmp r0, #0
    beq 3f
    adds r0, #1
3:  pop {r4, pc}

.thumb_func
.global leaf
leaf:
    eor r0, r0, r4
    adds r0, #7
    bx lr

/* A direct cold call site deliberately absent from the default capture.
 * Passing 0xffffffff reaches it, allowing the missing-capture receipt and
 * later exhaustive admission to be tested with the same image. */
.thumb_func
.global cold_entry
cold_entry:
    bl cold_leaf
    bx lr
.thumb_func
cold_leaf:
    adds r0, #11
    bx lr

/* Captured only by the recursion input used by the integration driver.  The
 * planner must retain the recursive edge instead of expanding forever. */
.thumb_func
.global recursive
recursive:
    cbz r0, 4f
    subs r0, #1
    bl recursive
4:  bx lr

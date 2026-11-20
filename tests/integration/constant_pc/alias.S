.syntax unified
.cpu cortex-a9
.arm

.equ ALIAS_DELTA, 0x00100000
.equ PAGE_TABLE, 0x00004000

.text
.global _start
.global alias_redirect_site
.global alias_redirect_target
.global alias_loop
_start:
    /* Two 1 MB sections map physical section zero at VA 0 and VA 0x100000. */
    ldr r0, =PAGE_TABLE
    mov r1, #0
    mov r2, #4096
1:  str r1, [r0], #4
    subs r2, r2, #1
    bne 1b
    ldr r0, =PAGE_TABLE
    ldr r1, =0x00000c02
    str r1, [r0, #0]
    str r1, [r0, #4]
    mcr p15, 0, r0, c2, c0, 0
    mov r1, #0
    mcr p15, 0, r1, c2, c0, 2
    mov r1, #3
    mcr p15, 0, r1, c3, c0, 0
    dsb sy
    mcr p15, 0, r1, c8, c7, 0
    mrc p15, 0, r1, c1, c0, 0
    orr r1, r1, #1
    mcr p15, 0, r1, c1, c0, 0
    isb

    mov r10, #0
    mov r4, #0
    mov r6, #0
    mov r5, #1
    b alias_loop

alias_loop:
    add r4, r4, #1
alias_redirect_site:
    add r6, r6, #1
    b failed

alias_redirect_target:
    subs r5, r5, #1
    bne alias_loop
    cmp r10, #0
    bne alias_complete

    /* Warm/link the original mapping, then enter the same physical TB by
       the second virtual-page alias. */
    mov r10, #1
    mov r5, #1
    ldr r0, =(alias_loop + ALIAS_DELTA)
    mov pc, r0

alias_complete:
    cmp r4, #2
    bne failed
    cmp r6, #0
    bne failed
    adr r1, pass_message
    b report
failed:
    adr r1, fail_message
report:
    mov r0, #4
    svc #0x123456
    mov r0, #0x18
    movw r1, #0x26
    movt r1, #2
    svc #0x123456
2:  b 2b

pass_message: .asciz "PASS constant-pc alias absolute-target fallback\n"
fail_message: .asciz "FAIL constant-pc alias\n"

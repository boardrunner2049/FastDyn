.syntax unified
.cpu cortex-m4
.thumb
.section .vectors, "a"
.word 0x20010000
.word reset + 1
.text
.thumb_func
.global reset
reset:
    movs r0, #1
fragment_site:
    mov r1, r0
    cmp r1, #8
    bne failed
    adr r1, pass_message
    b report
failed:
    adr r1, fail_message
report:
    movs r0, #4
    bkpt #0xab
    movs r0, #0x18
    movw r1, #0x26
    movt r1, #2
    bkpt #0xab
1:  b 1b
pass_message: .asciz "PASS fastdyn external fragment\n"
fail_message: .asciz "FAIL fastdyn external fragment\n"

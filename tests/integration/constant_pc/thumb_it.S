.syntax unified
.cpu cortex-m4
.thumb
.section .vectors, "a"
.word 0x20010000
.word reset + 1

.text
.thumb_func
.global reset
.global it_redirect_site
.global it_redirect_target
reset:
    movs r0, #0
    cmp r0, #0
    it eq
it_redirect_site:
    moveq r1, #1
    b failed
it_redirect_target:
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
pass_message: .asciz "PASS constant-pc IT fallback\n"
fail_message: .asciz "FAIL constant-pc IT fallback\n"

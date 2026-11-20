.text
.global _start
_start:
    mov w0, #1
fragment_site:
    b.ne failed
    b.lo failed
    b.mi failed
    b.vs failed
    mov w1, w0
    cmp w1, #94
    b.ne failed
    adr x1, pass_message
    b report
failed:
    adr x1, fail_message
report:
    mov x0, #4
    hlt #0xf000
    mov x1, #0x26
    movk x1, #2, lsl #16
    mov x0, #0x18
    hlt #0xf000
1:  b 1b
pass_message: .asciz "PASS a64\n"
fail_message: .asciz "FAIL a64\n"

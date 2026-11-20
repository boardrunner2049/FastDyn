.text
.global _start
_start:
    mov x0, #0x18
    movk x0, #0x900, lsl #16
    ldr w2, [x0]
    sub x0, x0, #0x18
    mov w2, #'P'
    str w2, [x0]
    mov x1, #0x26
    movk x1, #2, lsl #16
    mov x0, #0x18
    hlt #0xf000
1:  b 1b

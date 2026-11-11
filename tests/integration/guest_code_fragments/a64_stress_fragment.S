.text
.global fragment_start, fragment_end
fragment_start:
    mov x20, #0xffff
    lsl x20, x20, #48
    mov w20, w20
    add x20, x20, xzr
    .rept 94
    add x20, x20, #1
    .endr
    add x0, x20, xzr
    cmp x0, #94
fragment_end:

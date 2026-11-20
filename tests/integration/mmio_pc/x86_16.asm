BITS 16
ORG 0x7c00
    jmp 0x07c0:(nonzero_cs - 0x7c00)
nonzero_cs:
    mov dx, 0xe9
    mov al, 'P'
    out dx, al
    xor eax, eax
    mov dx, 0xf4
    out dx, eax
    cli
    hlt
times 510-($-$$) db 0
dw 0xaa55

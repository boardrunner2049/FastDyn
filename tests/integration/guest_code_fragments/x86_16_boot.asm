BITS 16
ORG 0x7c00
    mov ax, 1
fragment_site:
    jne failed
    jc failed
    js failed
    jo failed
    mov bx, ax
    cmp bx, 100
    jne failed
    xor eax, eax
    jmp finish
failed:
    mov eax, 1
finish:
    mov dx, 0xf4
    out dx, eax
    cli
    hlt
times 510-($-$$) db 0
dw 0xaa55

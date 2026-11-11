section .multiboot
align 4
    dd 0x1badb002
    dd 0
    dd -(0x1badb002)

section .text
BITS 32
global _start, fragment_site_order
_start:
    mov eax, 1
fragment_site_order:
    mov ebx, eax
    cmp ebx, 4
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

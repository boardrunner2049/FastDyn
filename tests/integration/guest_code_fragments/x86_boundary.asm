section .multiboot
align 4
    dd 0x1badb002
    dd 0
    dd -(0x1badb002)

section .text
BITS 32
global _start
_start:
    mov eax, 1
    jmp fragment_site_boundary

section .boundary progbits alloc exec nowrite align=1
global fragment_site_boundary
fragment_site_boundary:
    mov ebx, eax
    cmp ebx, 2
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

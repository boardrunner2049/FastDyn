section .multiboot
align 4
    dd 0x1badb002
    dd 0
    dd -(0x1badb002)

section .text
BITS 32
global _start
_start:
    mov dx, 0xe9
    mov al, 'P'
    out dx, al

    mov eax, pdpt
    or eax, 3
    mov [pml4], eax
    mov eax, pd
    or eax, 3
    mov [pdpt], eax
    mov dword [pd], 0x83
    mov eax, pml4
    mov cr3, eax
    mov eax, cr4
    or eax, 1 << 5
    mov cr4, eax
    mov ecx, 0xc0000080
    rdmsr
    or eax, 1 << 8
    wrmsr
    mov eax, cr0
    or eax, 1 << 31
    mov cr0, eax
    lgdt [gdt_ptr]
    jmp 0x08:long_mode

BITS 64
long_mode:
    mov ax, 0x10
    mov ds, ax
    mov ss, ax
    mov dx, 0xe9
    mov al, 'P'
    out dx, al
    xor eax, eax
    mov dx, 0xf4
    out dx, eax
    cli
    hlt

align 8
gdt:
    dq 0
    dq 0x00af9a000000ffff
    dq 0x00af92000000ffff
gdt_end:
gdt_ptr:
    dw gdt_end - gdt - 1
    dd gdt

section .bss
align 4096
pml4: resb 4096
pdpt: resb 4096
pd: resb 4096

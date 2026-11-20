section .multiboot
align 4
    dd 0x1badb002
    dd 0
    dd -(0x1badb002)

section .text
BITS 32
global _start, fragment_site32, fragment_site64
_start:
    mov eax, 1
fragment_site32:
    jne failed32
    jc failed32
    js failed32
    jo failed32
    mov ebx, eax
    cmp ebx, 100
    jne failed32

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

failed32:
    mov eax, 1
    jmp finish32
finish32:
    mov dx, 0xf4
    out dx, eax
    cli
    hlt

BITS 64
long_mode:
    mov ax, 0x10
    mov ds, ax
    mov ss, ax
    mov eax, 1
fragment_site64:
    jne failed64
    jc failed64
    js failed64
    jo failed64
    mov ebx, eax
    cmp ebx, 100
    jne failed64
    xor eax, eax
    jmp finish64
failed64:
    mov eax, 2
finish64:
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
pd:   resb 4096

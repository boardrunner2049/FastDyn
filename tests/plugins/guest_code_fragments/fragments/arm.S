.syntax unified
.text
.arm
.global fragment_a32
fragment_a32:
    and r0, r0, #255
    add r0, r0, #7
.thumb
.global fragment_thumb
.thumb_func
fragment_thumb:
    and.w r0, r0, #255
    add.w r0, r0, #7

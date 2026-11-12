"""LLVM's own data-layout queries for host-compiled model analysis.

Loaded lazily: ordinary firmware analysis does not require libLLVM.
"""
from __future__ import annotations

import ctypes as C
from ctypes.util import find_library


class LlvmLayoutError(ValueError):
    pass


def gep_offsets(ir: str) -> dict[tuple[str, str], tuple[str, int | None]]:
    """Map (function, SSA name) to (base SSA name, constant byte offset).

    LLVM parses the actual GEP source type and module data layout. Unknown
    indices/layouts produce None; TBAA metadata is never consulted.
    """
    library = next((path for name in ("LLVM-18", "LLVM-20", "LLVM-19", "LLVM-17", "LLVM")
                    if (path := find_library(name))), None)
    if not library:
        raise LlvmLayoutError("model analysis requires libLLVM (install llvm-18)")
    llvm = C.CDLL(library)

    def api(name, result, *args):
        fn = getattr(llvm, name)
        fn.restype, fn.argtypes = result, args
        return fn

    ptr, uint = C.c_void_p, C.c_uint
    context_new = api("LLVMContextCreate", ptr)
    context_free = api("LLVMContextDispose", None, ptr)
    buffer_new = api("LLVMCreateMemoryBufferWithMemoryRangeCopy", ptr, C.c_char_p, C.c_size_t, C.c_char_p)
    parse = api("LLVMParseIRInContext", C.c_int, ptr, ptr, C.POINTER(ptr), C.POINTER(C.c_char_p))
    module_free = api("LLVMDisposeModule", None, ptr)
    message_free = api("LLVMDisposeMessage", None, ptr)
    layout_get = api("LLVMGetModuleDataLayout", ptr, ptr)
    layout_string = api("LLVMGetDataLayoutStr", C.c_char_p, ptr)
    first_fn = api("LLVMGetFirstFunction", ptr, ptr)
    next_fn = api("LLVMGetNextFunction", ptr, ptr)
    first_block = api("LLVMGetFirstBasicBlock", ptr, ptr)
    next_block = api("LLVMGetNextBasicBlock", ptr, ptr)
    first_insn = api("LLVMGetFirstInstruction", ptr, ptr)
    next_insn = api("LLVMGetNextInstruction", ptr, ptr)
    opcode = api("LLVMGetInstructionOpcode", uint, ptr)
    name_get = api("LLVMGetValueName2", C.c_char_p, ptr, C.POINTER(C.c_size_t))
    operand = api("LLVMGetOperand", ptr, ptr, uint)
    operand_count = api("LLVMGetNumOperands", C.c_int, ptr)
    gep_type = api("LLVMGetGEPSourceElementType", ptr, ptr)
    kind = api("LLVMGetTypeKind", uint, ptr)
    element = api("LLVMGetElementType", ptr, ptr)
    struct_element = api("LLVMStructGetTypeAtIndex", ptr, ptr, uint)
    struct_count = api("LLVMCountStructElementTypes", uint, ptr)
    is_int = api("LLVMIsAConstantInt", ptr, ptr)
    int_value = api("LLVMConstIntGetSExtValue", C.c_longlong, ptr)
    abi_size = api("LLVMABISizeOfType", C.c_ulonglong, ptr, ptr)
    field_offset = api("LLVMOffsetOfElement", C.c_ulonglong, ptr, ptr, uint)
    sized = api("LLVMTypeIsSized", C.c_int, ptr)

    def name(value):
        length = C.c_size_t()
        return name_get(value, C.byref(length)).decode()

    context, module, error = context_new(), ptr(), C.c_char_p()
    data = ir.encode()
    try:
        if parse(context, buffer_new(data, len(data), b"model"), C.byref(module), C.byref(error)):
            detail = error.value.decode() if error.value else "invalid LLVM IR"
            if error:
                message_free(C.cast(error, ptr))
            raise LlvmLayoutError(detail)
        layout = layout_get(module)
        has_layout = bool(layout_string(module))
        result = {}
        fn = first_fn(module)
        while fn:
            block = first_block(fn)
            while block:
                insn = first_insn(block)
                while insn:
                    if opcode(insn) == 29:  # LLVMGetElementPtr
                        ty, offset = gep_type(insn), 0
                        for index in range(1, operand_count(insn)):
                            value = operand(insn, index)
                            if not is_int(value) or not sized(ty):
                                offset = None
                                break
                            n = int_value(value)
                            if index == 1:
                                offset += n * abi_size(layout, ty)
                            elif kind(ty) == 10:  # LLVMStructTypeKind
                                if not 0 <= n < struct_count(ty):
                                    offset = None
                                    break
                                offset += field_offset(layout, ty, n)
                                ty = struct_element(ty, n)
                            elif kind(ty) == 11:  # LLVMArrayTypeKind
                                ty = element(ty)
                                offset += n * abi_size(layout, ty)
                            else:
                                offset = None
                                break
                        result[(name(fn), "%" + name(insn))] = (
                            "%" + name(operand(insn, 0)), offset if has_layout else None,
                        )
                    insn = next_insn(insn)
                block = next_block(block)
            fn = next_fn(fn)
        return result
    finally:
        if module:
            module_free(module)
        context_free(context)

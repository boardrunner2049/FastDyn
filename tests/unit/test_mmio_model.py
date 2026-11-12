from pathlib import Path
import shlex
import subprocess
import ctypes
import json
import sys

import pytest

from fastdyn import mmio_model


REPO = Path(__file__).resolve().parents[2]


def ir_model(body, *, state_size=32, extra=""):
    return f'''
target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
%struct.FdModel = type {{ i32, i32, ptr, ptr, ptr }}
@fastdyn_model = constant %struct.FdModel {{ i32 2, i32 40, ptr @init, ptr @read, ptr null }}
define ptr @init() {{
entry:
  %p = call ptr @calloc(i64 1, i64 {state_size})
  ret ptr %p
}}
define i64 @read(ptr %s, i64 %addr) {{
entry:
  %ok = icmp eq i64 %addr, 1073807360
  br i1 %ok, label %yes, label %no
yes:
{body}
no:
  ret i64 0
}}
declare ptr @calloc(i64, i64)
{extra}
'''


@pytest.mark.parametrize("effect", [
    "store i32 0, ptr %s", " %unused = load volatile i32, ptr %s",
    " %unused = load atomic i32, ptr %s monotonic, align 4",
    "fence seq_cst", " %unused = freeze i32 7",
])
def test_effects_and_unadmitted_instructions_decline_entire_path(effect):
    ir = ir_model(f"  %v = load i32, ptr %s\n  {effect}\n"
                  "  %wide = zext i32 %v to i64\n  ret i64 %wide")
    with pytest.raises(mmio_model.MmioModelError, match="no pure"):
        mmio_model.analyze_ir(ir)


def test_gep_uses_actual_array_element_and_bounds_not_scalar_tbaa():
    body = '''  %p = getelementptr [2 x i32], ptr %s, i64 0, i64 1
  %v = load i32, ptr %p, !tbaa !0
  %wide = zext i32 %v to i64
  ret i64 %wide'''
    ir = ir_model(body, state_size=8, extra='''
!0 = !{!1, !1, i64 0}
!1 = !{!"int", !2, i64 0}
!2 = !{!"omnipotent char"}
''')
    assert mmio_model.analyze_ir(ir).operations[0].expression[0] == (
        "FD_MMIO_EXPR_LOAD_U32", 4, 0,
    )
    with pytest.raises(mmio_model.MmioModelError, match="no pure"):
        mmio_model.analyze_ir(ir.replace("i64 0, i64 1", "i64 0, i64 2"))


def test_narrow_wrap_and_conversion_are_explicit_before_word_result():
    ir = ir_model('''  %v = load i8, ptr %s
  %sum = add i8 %v, 1
  %wide = zext i8 %sum to i64
  ret i64 %wide''')
    expression = mmio_model.analyze_ir(ir).operations[0].expression
    stack = []
    for opcode, offset, value in expression:
        if opcode == "FD_MMIO_EXPR_LOAD_U8": stack.append(255)
        elif opcode == "FD_MMIO_EXPR_CONST": stack.append(value)
        else:
            rhs, lhs = stack.pop(), stack.pop()
            stack.append(lhs + rhs if opcode == "FD_MMIO_EXPR_ADD" else lhs & rhs)
    assert stack == [0]  # no final guest-byte masking


def test_all_address_predicates_are_retained():
    ir = ir_model('''  %p = getelementptr i8, ptr %s, i64 8
  %base = load i64, ptr %p
  %also = icmp eq i64 %addr, %base
  br i1 %also, label %both, label %no
both:
  ret i64 42''')
    operation = mmio_model.analyze_ir(ir).operations[0]
    assert len(operation.predicates) == 2
    assert operation.predicates[0][0] == "FD_MMIO_SELECTOR_ADDRESS_EQ_CONSTANT"
    assert operation.predicates[1][:3] == ("FD_MMIO_SELECTOR_ADDRESS_EQ_STATE", 8, 8)


@pytest.mark.parametrize("callback", ["write", "interrupt"])
def test_pointer_relative_helper_writes_cannot_prove_selector_immutable(callback):
    ir = ir_model('''  %p = getelementptr i8, ptr %s, i64 8
  %base = load i64, ptr %p
  %eq = icmp eq i64 %addr, %base
  br i1 %eq, label %selected, label %no
selected:
  ret i64 99''', extra=f'''
define void @{callback}(ptr %s) {{
entry:
  %field = getelementptr i8, ptr %s, i64 8
  call void @setter(ptr %field)
  ret void
}}
define void @setter(ptr %field) noinline {{
entry:
  store i64 1073807396, ptr %field
  ret void
}}
''')
    with pytest.raises(mmio_model.MmioModelError, match="no pure"):
        mmio_model.analyze_ir(ir)


@pytest.mark.parametrize("instruction", [
    "%v = sext i8 255 to i64",  # unsupported sign extension
    "%bad = shl i32 1, 32\n  %v = zext i32 %bad to i64",  # poison shift
    "%v = lshr i64 4294967296, 1",  # requires a wider evaluator
])
def test_unrepresentable_integer_forms_decline(instruction):
    with pytest.raises(mmio_model.MmioModelError, match="no pure"):
        mmio_model.analyze_ir(ir_model(f"  {instruction}\n  ret i64 %v"))


def test_actual_c_models_through_public_build_command(tmp_path):
    subprocess.run([sys.executable, "-m", "fastdyn.main", "build-model", "--mmio-opt",
                    "--no-vio", "tests/models/mmio_safety.c", "tests/models/mmio_predicates.c",
                    "-o", str(tmp_path)], check=True, cwd=REPO)
    report = json.loads((tmp_path / "mmio_safety.mmio.json").read_text())
    values = {op["selector_value"]: op for op in report["operations"]}
    assert 0x40010004 not in values  # read-to-clear
    assert 0x4001000c not in values  # unused volatile read
    assert all(op["selector_kind"] == "FD_MMIO_SELECTOR_ADDRESS_EQ_CONSTANT"
               for op in report["operations"])  # helper-mutated base is unproven
    assert values[0x40010010]["expression"][0] == ["FD_MMIO_EXPR_LOAD_U32", 28, 0]
    assert "FD_MMIO_EXPR_AND" in str(values[0x40010014])

    class Model(ctypes.Structure):
        _fields_ = [("abi", ctypes.c_uint32), ("size", ctypes.c_uint32),
                    *[(name, ctypes.c_void_p) for name in ("init", "read", "write", "irq", "serve")]]
    library = ctypes.CDLL(str(tmp_path / "mmio_safety.so"))
    model = Model.in_dll(library, "fastdyn_model")
    init = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p)(model.init)
    read = ctypes.CFUNCTYPE(ctypes.c_uint64, ctypes.c_void_p, ctypes.c_ulong,
                            ctypes.c_uint, ctypes.c_uint64)(model.read)
    state = init(None, b"{}", None)
    assert state
    try:
        assert read(state, 0x40010004, 4, 0) == 0x12345678
        assert read(state, 0x40010008, 4, 0) == 0
        assert read(state, 0x40010010, 4, 0) == 0x22222222
        assert read(state, 0x40010014, 4, 0) == 0
    finally:
        libc = ctypes.CDLL(None)
        libc.free.argtypes = [ctypes.c_void_p]
        libc.free(state)


def test_unmodified_echo_model_is_extracted_from_host_layout():
    system_cflags = tuple(shlex.split(subprocess.check_output(
        ["pkg-config", "--cflags", "glib-2.0"], text=True
    )))
    analysis = mmio_model.analyze_source(
        REPO / "tests/models/elder_echo.c",
        cflags=(),
        include_flags=(f"-I{REPO / 'include'}", f"-I{REPO / 'third_party/common/qemu/include'}",
                       *system_cflags),
    )

    assert analysis.state_size == 32
    assert len(analysis.operations) == 4
    state_loads = {
        instruction[1]
        for operation in analysis.operations
        for instruction in operation.expression
        if instruction[0].startswith("FD_MMIO_EXPR_LOAD")
    }
    assert state_loads == {16, 20, 24}
    assert any(
        operation.expression == (("FD_MMIO_EXPR_CONST", 0, 0xAA),)
        for operation in analysis.operations
    )


def test_effectful_call_is_not_reduced_to_following_load():
    ir = """
%struct.FdModel = type { i32, i32, ptr, ptr, ptr }
@fastdyn_model = constant %struct.FdModel { i32 2, i32 40, ptr @init, ptr @read, ptr null }
define ptr @init() {
entry:
  %p = call dereferenceable_or_null(4) ptr @calloc(i64 1, i64 4)
  ret ptr %p
}
define i64 @read(ptr %s, i64 %addr) {
entry:
  %ok = icmp eq i64 %addr, 1073741824
  br i1 %ok, label %yes, label %no
yes:
  call void @poll_backend(ptr %s)
  %v = load i32, ptr %s
  %wide = zext i32 %v to i64
  ret i64 %wide
no:
  ret i64 0
}
declare ptr @calloc(i64, i64)
declare void @poll_backend(ptr)
"""
    try:
        mmio_model.analyze_ir(ir)
    except mmio_model.MmioModelError as exc:
        assert "no pure" in str(exc)
    else:
        raise AssertionError("effectful read path was incorrectly accepted")


def test_supported_state_computation_becomes_postfix_expression():
    ir = """
%struct.FdModel = type { i32, i32, ptr, ptr, ptr }
@fastdyn_model = constant %struct.FdModel { i32 2, i32 40, ptr @init, ptr @read, ptr null }
define ptr @init() {
entry:
  %p = call dereferenceable_or_null(4) ptr @calloc(i64 1, i64 4)
  ret ptr %p
}
define i64 @read(ptr %s, i64 %addr) {
entry:
  %ok = icmp eq i64 %addr, 1073741824
  br i1 %ok, label %yes, label %no
yes:
  %v = load i32, ptr %s
  %sum = add i32 %v, 7
  %wide = zext i32 %sum to i64
  ret i64 %wide
no:
  ret i64 0
}
declare ptr @calloc(i64, i64)
"""
    analysis = mmio_model.analyze_ir(ir)
    assert analysis.operations[0].expression == (
        ("FD_MMIO_EXPR_LOAD_U32", 0, 0),
        ("FD_MMIO_EXPR_CONST", 0, 7),
        ("FD_MMIO_EXPR_ADD", 0, 0),
    )


def test_live_state_branch_is_not_frozen_during_binding():
    ir = """
%struct.FdModel = type { i32, i32, ptr, ptr, ptr }
@fastdyn_model = constant %struct.FdModel { i32 2, i32 40, ptr @init, ptr @read, ptr null }
define ptr @init() {
entry:
  %p = call dereferenceable_or_null(4) ptr @calloc(i64 1, i64 4)
  ret ptr %p
}
define i64 @read(ptr %s, i64 %addr) {
entry:
  %ok = icmp eq i64 %addr, 1073741824
  br i1 %ok, label %selected, label %no
selected:
  %v = load i32, ptr %s
  %live = icmp eq i32 %v, 0
  br i1 %live, label %zero, label %nonzero
zero:
  ret i64 1
nonzero:
  ret i64 2
no:
  ret i64 0
}
declare ptr @calloc(i64, i64)
"""
    try:
        mmio_model.analyze_ir(ir)
    except mmio_model.MmioModelError as exc:
        assert "no pure" in str(exc)
    else:
        raise AssertionError("live conditional state was frozen into one operation")

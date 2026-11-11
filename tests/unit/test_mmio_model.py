from pathlib import Path
import shlex
import subprocess

from fastdyn import mmio_model


REPO = Path(__file__).resolve().parents[2]


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

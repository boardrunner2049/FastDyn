"""Exercise the offline BOLT bridge against ELF functions linked from assembly."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from capstone import CS_ARCH_X86, CS_MODE_64, Cs
from elftools.elf.elffile import ELFFile
import pytest


@pytest.fixture(scope="module")
def bridge():
    path = os.environ.get("SCOPT_NATIVE_BRIDGE")
    if not path:
        pytest.skip("SCOPT_NATIVE_BRIDGE must point to the built bridge")
    assert Path(path).is_file()
    return path


@pytest.fixture(scope="module")
def image(tmp_path_factory):
    cc = shutil.which("cc")
    if not cc:
        pytest.skip("assembler toolchain required")
    directory = tmp_path_factory.mktemp("native-elf")
    source = directory / "leaves.S"
    source.write_text(r"""
.text
.macro entry name
.globl \name
.type \name,@function
\name:
.endm
.macro end name
.size \name,.-\name
.endm

entry leaf
  endbr64
  movl %edi,%eax
  bswapl %eax
  addl %esi,%eax
  ret
end leaf

entry choose
  cmpl %esi,%edi
  jne .Lright
  movl %edi,%eax
  ret
.Lright:
  movl %esi,%eax
  ret
end choose

entry loop
  movl %edi,%eax
.Lagain:
  subl $1,%eax
  jne .Lagain
  ret
end loop

entry stack
  movl -8(%rsp),%eax
  ret
end stack

entry memory
  movl (%rdi),%eax
  ret
end memory

entry nested
  call leaf@PLT
  ret
end nested

entry indirect
  jmp *%rdi
  ret
end indirect

entry trap
  int3
  ret
end trap
""")
    binary = directory / "leaves.so"
    subprocess.run([cc, "-shared", "-nostdlib", "-o", binary, source], check=True)
    return binary


def request(image, name):
    with image.open("rb") as stream:
        elf = ELFFile(stream)
        sym, = elf.get_section_by_name(".symtab").get_symbol_by_name(name)
        section = elf.get_section(sym["st_shndx"])
        offset = sym["st_value"] - section["sh_addr"]
        code = section.data()[offset:offset + sym["st_size"]]
    return {
        "version": 1,
        "image_path": str(image),
        "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        "address": sym["st_value"],
        "size": len(code),
        "code_sha256": hashlib.sha256(code).hexdigest(),
    }, code


def invoke(bridge, req):
    proc = subprocess.run([bridge], input=json.dumps(req), capture_output=True,
                          check=True, text=True, timeout=15)
    return json.loads(proc.stdout)


@pytest.mark.parametrize(("name", "returns"), [("leaf", 1), ("choose", 2), ("loop", 1)])
def test_fragment_origins_and_relocations(bridge, image, name, returns):
    req, source = request(image, name)
    result = invoke(bridge, req)
    assert result["status"] == "ok", result
    fragment = bytes.fromhex(result["code"])
    assert sum(row["source_size"] for row in result["origins"]) == len(source)
    assert sum(row["output_size"] for row in result["origins"]) == len(fragment)
    relocs = result["relocations"]
    assert len(relocs) == returns
    assert all(r["width"] == 4 and r["target"] == "continuation" for r in relocs)

    cs = Cs(CS_ARCH_X86, CS_MODE_64)
    original = {i.address: i for i in cs.disasm(source, 0)}
    emitted = {i.address: i for i in cs.disasm(fragment, 0)}
    origin = {row["source_off"]: row["output_off"] for row in result["origins"]}
    fields = {r["field_off"] for r in relocs}
    for source_off, output_off in origin.items():
        old = original[source_off]
        new = emitted[output_off]
        if old.mnemonic == "ret":
            assert new.mnemonic == "jmp"
            assert output_off + 1 in fields
        elif old.mnemonic.startswith("j"):
            assert new.mnemonic == old.mnemonic
            assert int(new.op_str, 0) == origin[int(old.op_str, 0)]
        else:
            assert (old.mnemonic, old.op_str) == (new.mnemonic, new.op_str)
    assert invoke(bridge, req) == result


@pytest.mark.parametrize(("name", "reason"), [
    ("stack", "unsupported_stack"),
    ("memory", "unsupported_memory"),
    ("nested", "unsupported_control_flow"),
    ("indirect", "unsupported_control_flow"),
    ("trap", "unsupported_instruction"),
])
def test_refused_machine_cases(bridge, image, name, reason):
    req, _ = request(image, name)
    result = invoke(bridge, req)
    assert (result["status"], result["reason"]) == ("refused", reason), result


@pytest.mark.parametrize("field", ["image_sha256", "code_sha256"])
def test_refuses_changed_digest(bridge, image, field):
    req, _ = request(image, "leaf")
    req[field] = "0" * 64
    result = invoke(bridge, req)
    assert (result["status"], result["reason"]) == ("refused", "native_image_mismatch")


def test_qemu_rbit_preserves_nonreturn_bytes(bridge):
    target = os.environ.get("SCOPT_NATIVE_QEMU")
    if not target:
        pytest.skip("SCOPT_NATIVE_QEMU must point to the source-locked QEMU executable")
    req, source = request(Path(target), "helper_rbit")
    result = invoke(bridge, req)
    assert result["status"] == "ok", result
    assert source[-1:] == b"\xc3"
    assert bytes.fromhex(result["code"]) == source[:-1] + b"\xe9\x00\x00\x00\x00"
    assert result["relocations"] == [
        {"field_off": len(source), "width": 4, "target": "continuation"}
    ]

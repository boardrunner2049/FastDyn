"""Exercise both complete linked qstr_find_strn bodies from a Pye continuity artifact."""
import argparse
import ctypes
import hashlib
import itertools
import json
import mmap
from pathlib import Path
import struct
import subprocess
import tempfile

from scopt.formats.sc_v6 import EXIT_DIRECT, KIND_SPEC, read_sc6

from continuity_witness import require

# Both training images emitted the same 160-byte public instruction stream.
# Normalize only the three rel32 fields that jump to the QEMU return prologue.
# The complete reviewed stream restricts native execution to bounded CPU-state
# accesses, local branches and the synthetic prologue return below.
PUBLIC_DIGEST = "e6ee092aef29c6828535735648d64ed0fef31048c44f131697d6d68b8d4f19e5"
RETURN_FIELDS = (116, 144, 156)

WRAPPER = """
.text
.globl run_body
.type run_body,@function
run_body:
    push %rbp
    push %rbx
    push %r12
    push %r13
    push %r14
    mov %rsi,%rbp
    mov %edx,%r13d
    mov $0x12345678,%ebx
    mov $0xabcdef,%r12d
    mov $0xfade,%r14d
    call *%rdi
    mov %rax,0(%rcx)
    mov %rbx,8(%rcx)
    mov %r12,16(%rcx)
    mov %r13,24(%rcx)
    mov %r14,32(%rcx)
    pushfq
    popq 40(%rcx)
    pop %r14
    pop %r13
    pop %r12
    pop %rbx
    pop %rbp
    ret
.section .note.GNU-stack,"",@progbits
"""


def check_body(code, tc, tb, private):
    require(len(code) == 160 and tc - tb == 192, "unrecognized TB extent")
    normalized = bytearray(code)
    for field in RETURN_FIELDS:
        target = tc + field + 4 + struct.unpack_from("<i", code, field)[0]
        require(target == 24, "return does not reach the cache prologue")
        normalized[field:field + 4] = bytes(4)
    if private:
        require(code[22:25] == bytes.fromhex("4489eb"), "missing carrier forwarding")
        normalized[22:25] = bytes.fromhex("8b5d14")
    require(hashlib.sha256(normalized).hexdigest() == PUBLIC_DIGEST, "unknown native instruction stream")


def exercise(cache):
    parsed = read_sc6(cache)
    require(parsed.problem is None and parsed.ext and len(parsed.ext.variants) == 1,
            "requires the validated single-variant SC6 artifact")
    slot, = parsed.ext.slots
    require(slot.exit_class == EXIT_DIRECT and slot.kind == KIND_SPEC and slot.target_vaddr == 0xde4,
            "unexpected private edge")
    private, = parsed.ext.variants
    index = next(i for i, r in enumerate(parsed.sc.records) if r and r.tb_offset == private.general_tb_offset)
    general = parsed.sc.records[index]
    records = [(general, parsed.sc.bodies[index]), (private, parsed.variant_bodies[0])]
    size = max(r.end_offset for r, _ in records)
    require(0 < size < 64 * 1024 * 1024, "test arena outside bounded Pye layout")
    with mmap.mmap(-1, size, prot=mmap.PROT_READ | mmap.PROT_WRITE) as arena, tempfile.TemporaryDirectory() as tmp:
        arena[24] = 0xc3  # QEMU return prologue ends the test call.
        for i, (record, body) in enumerate(records):
            code = body[:record.tc_size]
            check_body(code, record.tc_offset, record.tb_offset, bool(i))
            arena[record.tc_offset:record.tc_offset + len(code)] = code
        base = ctypes.addressof(ctypes.c_char.from_buffer(arena))
        libc = ctypes.CDLL(None, use_errno=True)
        require(libc.mprotect(ctypes.c_void_p(base), ctypes.c_size_t(size),
                              mmap.PROT_READ | mmap.PROT_EXEC) == 0, "cannot seal test arena")
        src, so = Path(tmp) / "entry.S", Path(tmp) / "entry.so"
        src.write_text(WRAPPER)
        subprocess.run(["cc", "-shared", "-fPIC", "-o", so, src], check=True)
        dll = ctypes.CDLL(str(so))
        fn = dll.run_body
        fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
        fn.restype = None

        def run(record, ticks, value, other, carrier):
            env = bytearray(bytes(range(256)) * 3)
            for offset, val in [(-8, ticks), (0x14, value), (0x18, other), (0x3c, 0xde4)]:
                struct.pack_into("<I", env, 16 + offset, val)
            buf = (ctypes.c_ubyte * len(env)).from_buffer(env)
            regs = (ctypes.c_uint64 * 6)()
            fn(base + record.tc_offset, ctypes.byref(buf, 16), carrier, regs)
            exit_index = regs[0] - base - record.tb_offset
            return bytes(env), exit_index, tuple(regs[1:])

        exits = set()
        trials = 0
        values = (0, 1, 0x7fffffff, 0x80000000, 0xffffffff)
        for ticks, value, other in itertools.product((0, 1, 2, 65535, 0x80000000), values, values):
            public = run(general, ticks, value, other, value)
            variant = run(private, ticks, value, other, value)
            require(variant == public, "linked private body changed CPU state, host state or exit")
            env, exit_index, _ = public
            if exit_index in (0, 1):
                require(run(general, ticks, value, other, value ^ 0xffffffff) == public,
                        "public entry depends on the incoming carrier")
            exits.add(exit_index)
            trials += 1
        require(exits == {0, 1, 3}, f"unexpected or incomplete block exits: {exits}")
    return {"trials": trials, "TB_exit_indices": sorted(exits),
            "public_tb_offset": general.tb_offset, "private_tb_offset": private.tb_offset,
            "resolved_return_prologue_offset": 24, "normalized_public_code_sha256": PUBLIC_DIGEST}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cache", type=Path)
    args = parser.parse_args()
    print(json.dumps(exercise(args.cache), indent=2))

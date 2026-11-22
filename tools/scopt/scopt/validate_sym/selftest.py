"""V18 self-test: native differential test of every concretely modelled form (§9.1 V18). WP19.

Each instruction form that :mod:`.semantics` models concretely is executed
on the host CPU through ``ctypes`` + ``mmap(PROT_EXEC)`` with random and
edge-case operands and random input flags, and the result registers and the
defined flags (ZF SF CF OF PF, AF where modelled) are compared with the
model. The harness::

    mov r11, rdi ; mov rax,[r11] ; mov rcx,[r11+8] ; mov rdx,[r11+16] ; push rdx ; popfq
    <form>
    pushfq ; pop rdx ; mov [r11],rax ; mov [r11+8],rcx ; mov [r11+16],rdx ; ret

:func:`run_selftest` also runs the synthetic part of the §9.3 mutation
suite (:func:`.mutants.synthetic_suite`) and returns one JSON document; V18
(:func:`scopt.validate.v18_rows`) accepts it only for the current scopt and
capstone versions.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import mmap
import os
import platform
import random
from collections.abc import Iterable
from typing import Any

import capstone

from .semantics import CONCRETE, Unsupported, execute
from .state import State, is_const

__all__ = ["forms", "native_check", "run_selftest", "SELFTEST_SCHEMA", "kernel_id",
           "load_or_run"]

SELFTEST_SCHEMA = "scopt.hostsym_selftest/1"
_HEAD = bytes.fromhex("4989fb" "498b03" "498b4b08" "498b5310" "52" "9d")
_TAIL = bytes.fromhex("9c" "5a" "498903" "49894b08" "49895310" "c3")
_FLAGBIT = {"CF": 0, "PF": 2, "AF": 4, "ZF": 6, "SF": 7, "OF": 11}
_EDGE = [0, 1, 2, 0x7F, 0x80, 0xFF, 0x7FFF, 0x8000, 0xFFFF, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF,
         0x7FFFFFFFFFFFFFFF, 0x8000000000000000, 0xFFFFFFFFFFFFFFFF, 0x100000000, 0x12345678]


def forms() -> list[bytes]:
    """Every concretely modelled form, on eax/ecx (rax/rcx, ax/cx, al/cl)."""
    out: list[str] = []
    rr = {"add": 0x01, "or": 0x09, "and": 0x21, "sub": 0x29, "xor": 0x31, "cmp": 0x39,
          "test": 0x85, "mov": 0x89}
    for opc in rr.values():
        out += [f"{opc:02x}c8", f"48{opc:02x}c8", f"66{opc:02x}c8", f"{opc - 1:02x}c8"]
    # Read/write bits 8..15 alongside their low-byte aliases, checking
    # untouched bits in the 64-bit destination and architectural flags.
    out += ["0fb6c5", "88cc", "88e8", "00ec", "80fc80", "0f94c4"]
    out += ["31c0", "85c0", "29c0", "4831c0"]
    for ext in (0, 1, 4, 5, 6, 7):
        m = 0xC0 | (ext << 3)
        for ib in (0x00, 0x01, 0x7F, 0x80, 0xFF, 0xF8):
            out += [f"83{m:02x}{ib:02x}", f"4883{m:02x}{ib:02x}", f"6683{m:02x}{ib:02x}",
                    f"80{m:02x}{ib:02x}"]
        out += [f"81{m:02x}78563412", f"81{m:02x}000000fc", f"4881{m:02x}000000f0"]
    for one in ("ffc0", "ffc8", "f7d0", "f7d8"):
        out += [one, "48" + one, "66" + one, "fe" + one[2:] if one.startswith("ff")
                else "f6" + one[2:]]
    for ext in (4, 5, 7):  # shl shr sar
        m = 0xC0 | (ext << 3)
        for ib in (1, 2, 5, 31):
            out += [f"c1{m:02x}{ib:02x}", f"48c1{m:02x}{ib:02x}"]
        out += [f"48c1{m:02x}3f", f"48c1{m:02x}20"]
    out += ["0fb6c1", "0fb7c1", "0fbec1", "0fbfc1", "480fb6c1", "480fbfc1", "4863c1",
            "660fb6c1", "660fbec1"]
    out += ["8d0408", "488d0408", "488d444810", "8d41fd", "488d84c878563412", "8d4401ff"]
    out += ["b878563412", "48b8efcdab8967452301", "48c7c0f0ffffff", "66b83412", "b0a5",
            "89c8", "4889c8"]
    for cc in range(16):
        out += [f"0f{0x90 + cc:02x}c0", f"0f{0x40 + cc:02x}c1", f"480f{0x40 + cc:02x}c1",
                f"660f{0x40 + cc:02x}c1"]
    return [bytes.fromhex(x) for x in out]


class _NoMem:
    def read(self, *a: Any) -> Any:
        raise Unsupported("memory")

    write = read
    rip_addr = read


class _Native:
    def __init__(self) -> None:
        self.m = mmap.mmap(-1, 1 << 16, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
        self.addr = ctypes.addressof(ctypes.c_char.from_buffer(self.m))
        self.ftype = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
        self.io = (ctypes.c_uint64 * 3)()

    def run(self, code: bytes, rax: int, rcx: int, flags: int) -> tuple[int, int, int]:
        prog = _HEAD + code + _TAIL
        self.m.seek(0)
        self.m.write(prog)
        self.io[0], self.io[1], self.io[2] = rax, rcx, flags
        self.ftype(self.addr)(ctypes.addressof(self.io))
        return self.io[0], self.io[1], self.io[2]


def _model(ci: Any, rax: int, rcx: int, flags: int) -> tuple[int, int, dict[str, Any]]:
    st = State.initial()
    st.regs["rax"] = rax
    st.regs["rcx"] = rcx
    for f, bit in _FLAGBIT.items():
        st.flags[f] = (flags >> bit) & 1
    execute(st, ci, _NoMem())
    return st.regs["rax"], st.regs["rcx"], st.flags


def native_check(trials: int = 48, seed: int = 20261117,
                 only: Iterable[bytes] | None = None) -> dict[str, Any]:
    """Run every form natively and in the model; return counts and the first failures."""
    if platform.machine() not in ("x86_64", "AMD64"):
        return {"status": "not_run", "reason": f"host {platform.machine()} is not x86-64"}
    try:
        nat = _Native()
    except (OSError, ValueError) as e:  # pragma: no cover - hardened hosts
        return {"status": "not_run", "reason": f"mmap(PROT_EXEC) failed: {e}"}
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    rnd = random.Random(seed)
    fails: list[dict[str, Any]] = []
    n_forms = n_trials = n_flag_checks = 0
    mnems: set[str] = set()
    for code in (list(only) if only is not None else forms()):
        ci = next(md.disasm(code, 0x1000), None)
        if ci is None or ci.size != len(code):
            fails.append({"form": code.hex(), "error": "decode"})
            continue
        m = ci.mnemonic
        base = m if m in CONCRETE else ("setcc" if m.startswith("set") else
                                        "cmovcc" if m.startswith("cmov") else None)
        if base is None:
            fails.append({"form": code.hex(), "error": f"{m} is not a concrete form"})
            continue
        mnems.add(base)
        n_forms += 1
        for t in range(trials):
            a = rnd.choice(_EDGE) if t % 3 == 0 else rnd.getrandbits(64)
            b = rnd.choice(_EDGE) if t % 4 == 1 else rnd.getrandbits(64)
            fl = 0x2 | sum(1 << bit for bit in _FLAGBIT.values() if rnd.random() < 0.5)
            ra, rc, rf = nat.run(code, a, b, fl)
            try:
                ma, mc, mf = _model(ci, a, b, fl)
            except Unsupported as e:
                fails.append({"form": code.hex(), "insn": f"{m} {ci.op_str}",
                              "error": f"unsupported {e.why}"})
                break
            n_trials += 1
            bad = []
            if not is_const(ma) or ma != ra:
                bad.append(f"rax model {ma} native {ra:#x}")
            if not is_const(mc) or mc != rc:
                bad.append(f"rcx model {mc} native {rc:#x}")
            for f, bit in _FLAGBIT.items():
                v = mf[f]
                if is_const(v):
                    n_flag_checks += 1
                    if v != (rf >> bit) & 1:
                        bad.append(f"{f} model {v} native {(rf >> bit) & 1}")
            if bad:
                fails.append({"form": code.hex(), "insn": f"{m} {ci.op_str}",
                              "in": [hex(a), hex(b), hex(fl)], "diff": bad})
                break
    return {"status": "pass" if not fails else "fail", "forms": n_forms, "trials": n_trials,
            "flag_checks": n_flag_checks, "mnemonics": sorted(mnems),
            "failures": fails[:20], "n_failures": len(fails)}


def kernel_id() -> str:
    """Content hash of the kernel sources (``scopt/validate_sym/*.py``, the analyses it
    imports and the shared LOG_REG matcher ``scopt/decode/logreg.py``): V18 binds a
    self-test to it, since the package version string does not change between commits."""
    from pathlib import Path

    here = Path(__file__).resolve().parent
    files = sorted(here.glob("*.py")) + [here.parent / "analysis" / "effects.py",
                                         here.parent / "analysis" / "obs_contract.py",
                                         here.parent / "decode" / "logreg.py"]
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode() + b"\0" + f.read_bytes())
    return "sha256:" + h.hexdigest()


def load_or_run(path: str | os.PathLike | None = None, **kw: Any) -> dict[str, Any]:
    """The cached V18 document at ``path`` if it is for the current kernel and
    capstone, else a fresh :func:`run_selftest` (written to ``path``)."""
    if path is not None and os.path.isfile(path):
        try:
            doc = json.loads(open(path).read())
        except (OSError, ValueError):
            doc = None
        if doc and doc.get("kernel_id") == kernel_id() and \
                doc.get("capstone_version") == capstone.__version__:
            return doc
    doc = run_selftest(**kw)
    if path is not None:
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w") as f:
            f.write(json.dumps(doc, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, path)
    return doc


def run_selftest(*, trials: int = 48, mutants: bool = True) -> dict[str, Any]:
    """The V18 document: native semantics check + synthetic mutation suite."""
    from .. import __version__
    from .mutants import synthetic_suite

    nat = native_check(trials)
    doc: dict[str, Any] = {"schema": SELFTEST_SCHEMA, "scopt_version": __version__,
                           "kernel_id": kernel_id(),
                           "capstone_version": capstone.__version__,
                           "host": platform.machine(), "native": nat}
    if mutants:
        doc["mutants"] = synthetic_suite().to_json()
    ok = nat.get("status") == "pass" and (not mutants or doc["mutants"]["all_rejected"])
    doc["status"] = "pass" if ok else "fail"
    return doc

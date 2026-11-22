"""Shared helpers of the B3 passes (WP21). Not a pass module (``_`` prefix: not discovered)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from ..passes.base import B3Reason, Outcome, PassResult

__all__ = ["nops", "xor_zero", "mov_imm32", "REG32", "refusal_result", "FLAG_SET",
           "NO_MEM_DHO", "has_mem_operand", "factref_of_row", "certified_facts"]

#: 32-bit register name -> number (x86-64 encoding)
REG32 = {"eax": 0, "ecx": 1, "edx": 2, "ebx": 3, "esp": 4, "ebp": 5, "esi": 6, "edi": 7,
         **{f"r{i}d": i for i in range(8, 16)}}
FLAG_SET = frozenset({"CF", "PF", "AF", "ZF", "SF", "OF", "DF"})
#: register-only mnemonics DHO may delete (no memory operand, no fault, no side effect)
NO_MEM_DHO = frozenset({
    "mov", "movzx", "movsx", "movsxd", "xor", "and", "or", "add", "sub", "test", "cmp",
    "inc", "dec", "neg", "not", "shl", "sal", "shr", "sar", "rol", "ror", "bswap",
    "seto", "setno", "setb", "setae", "sete", "setne", "setbe", "seta", "sets", "setns",
    "setp", "setnp", "setl", "setge", "setle", "setg",
    "cmovo", "cmovno", "cmovb", "cmovae", "cmove", "cmovne", "cmovbe", "cmova", "cmovs",
    "cmovns", "cmovp", "cmovnp", "cmovl", "cmovge", "cmovle", "cmovg",
    "andn", "imul",
})


def nops(n: int) -> bytes:
    """``n`` bytes of register-free nops (``66 90`` pairs, one ``90`` for an odd length).
    No multi-byte ``nop [mem]`` form: patched code keeps no memory operand that an
    analysis could mistake for an access."""
    if n < 0:
        raise ValueError(n)
    return b"\x66\x90" * (n // 2) + (b"\x90" if n % 2 else b"")


def xor_zero(reg32: str) -> bytes | None:
    r = REG32.get(reg32)
    if r is None:
        return None
    modrm = 0xC0 | ((r & 7) << 3) | (r & 7)
    if r >= 8:
        return bytes((0x45, 0x31, modrm))
    return bytes((0x31, modrm))


def mov_imm32(reg32: str, value: int) -> bytes | None:
    r = REG32.get(reg32)
    if r is None:
        return None
    imm = (value & 0xFFFFFFFF).to_bytes(4, "little")
    if r >= 8:
        return bytes((0x41, 0xB8 + (r & 7))) + imm
    return bytes((0xB8 + r,)) + imm


def has_mem_operand(ci: Any) -> bool:
    from capstone import x86 as X

    return any(o.type == X.X86_OP_MEM for o in ci.operands)


def refusal_result(refused: Counter, *, nothing: str, stats: Mapping[str, float] | None = None,
                   detail: str = "") -> PassResult:
    """The PassResult of a pass that proposed nothing: the most frequent §3.8 refusal
    (``skipped_unsupported`` / ``skipped_unknown`` per :meth:`B3Reason.outcome`; a
    ``failed`` class code keeps ``failed``), or ``no_change`` with ``nothing``."""
    st = dict(stats or {})
    st.update({f"refused:{k}": v for k, v in refused.items()})
    if not refused:
        return PassResult(Outcome.NO_CHANGE, nothing, detail, stats=st)
    code, _n = sorted(refused.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    st["no_applicable_sites"] = 1
    return PassResult(B3Reason.outcome(code), code,
                      detail or ", ".join(f"{k}={v}" for k, v in sorted(refused.items())),
                      stats=st)


def factref_of_row(row: Mapping[str, Any], f: Mapping[str, Any]) -> Any:
    from .base import FactRef

    v = f["value"]
    return FactRef(f["fact_id"], f["field"], int(v, 0) if isinstance(v, str) else int(v),
                   int(f["def_insn"]), f["source"], f["cert_hash"], row["row_hash"])


def certified_facts(row: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if row is None:
        return []
    return [f for f in row.get("facts", ()) if f.get("cert_hash") and
            ((f.get("status") == "certified" and f.get("source") in ("patch", "guest_code"))
             or (f.get("status") == "guarded" and f.get("source") == "guard"))]

"""Fixed-width known bits and signed/unsigned bounds for a single native result."""
from dataclasses import dataclass

from capstone import x86 as X


@dataclass(frozen=True, slots=True)
class IntegerResult:
    bits: int
    zero: int
    one: int
    # A sign extension has a tighter signed interval than independent bit masks imply.
    sign_width: int

    @property
    def mask(self):
        return (1 << self.bits) - 1

    def bounds(self, signed):
        lower, upper = self.one, self.mask ^ self.zero
        if not signed:
            return lower, upper
        sign = 1 << (self.bits - 1)
        if self.zero & sign:
            return lower, upper
        if self.one & sign:
            return lower - (1 << self.bits), upper - (1 << self.bits)
        # Tight signed bounds subject to the known low bits.
        low = (lower | sign) - (1 << self.bits)
        high = upper & (sign - 1)
        narrow = 1 << (self.sign_width - 1)
        return max(low, -narrow), min(high, narrow - 1)


def result_facts(ins):
    if ins is None or len(ins.operands) != 2 or any(ins.prefix):
        return None
    dst, src = ins.operands
    if dst.type != X.X86_OP_REG or dst.size not in (4, 8):
        return None
    bits = dst.size * 8
    mask = (1 << bits) - 1
    if src.type == X.X86_OP_REG and ins.mnemonic in ("movsx", "movsxd", "movzx"):
        n = src.size * 8
        if 0 < n < bits:
            if ins.mnemonic == "movzx":
                return IntegerResult(bits, mask ^ ((1 << n) - 1), 0, bits)
            return IntegerResult(bits, 0, 0, n)
    if src.type == X.X86_OP_IMM:
        imm = src.imm & mask
        if ins.mnemonic in ("mov", "movabs"):
            return IntegerResult(bits, mask ^ imm, imm, bits)
        if ins.mnemonic == "and":
            return IntegerResult(bits, mask ^ imm, 0, bits)
        if ins.mnemonic == "or":
            return IntegerResult(bits, 0, imm, bits)
    return None


def compare_outcome(facts, condition, immediate):
    signed = condition in ("jl", "jle", "jg", "jge")
    value = immediate & facts.mask
    if signed and value >> (facts.bits - 1):
        value -= 1 << facts.bits
    low, high = facts.bounds(signed)
    if condition in ("je", "jne"):
        if low == high == value:
            return condition == "je"
        if (facts.zero & value or facts.one & ~value or value < low or value > high):
            return condition == "jne"
        return None
    if condition in ("jb", "jl"):
        return True if high < value else False if low >= value else None
    if condition in ("jbe", "jle"):
        return True if high <= value else False if low > value else None
    if condition in ("ja", "jg"):
        return True if low > value else False if high <= value else None
    if condition in ("jae", "jge"):
        return True if low >= value else False if high < value else None
    return None


def pure_expression(ins):
    """Re-executable address/copy/extension expression with one full GPR result."""
    if ins is None or any(ins.prefix) or len(ins.operands) != 2:
        return False
    dst, src = ins.operands
    if dst.type != X.X86_OP_REG or dst.size not in (4, 8):
        return False
    if ins.mnemonic == "lea" and src.type == X.X86_OP_MEM:
        return not src.mem.segment and src.mem.base != X.X86_REG_RIP
    return (ins.mnemonic in ("mov", "movzx", "movsx", "movsxd")
            and src.type == X.X86_OP_REG)


def modeled_scalar(ins):
    if pure_expression(ins):
        return True
    return (ins is not None and not any(ins.prefix)
            and ins.mnemonic in ("and", "or", "cmp", "test")
            and all(a.type in (X.X86_OP_REG, X.X86_OP_IMM) for a in ins.operands))

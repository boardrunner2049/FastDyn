"""Target-specific instruction decoding and shared instruction facts."""
from __future__ import annotations

from dataclasses import dataclass

import capstone
from capstone.arm import ARM_OP_IMM, ARM_OP_MEM

from .errors import UnsupportedTargetError


@dataclass(frozen=True)
class DecodedInstruction:
    address: int
    size: int
    mnemonic: str
    operands: str
    has_memory_operand: bool
    memory_transfer: bool
    return_site: bool
    direct_target: int | None


class Decoder:
    def __init__(self, elf, architecture: str, instruction_mode: str):
        self._elf = elf
        self.architecture = architecture
        self.instruction_mode = instruction_mode
        if architecture.lower() != "arm" or instruction_mode != "thumb-mclass":
            raise UnsupportedTargetError(
                f"unsupported decoder target: architecture={architecture!r}, mode={instruction_mode!r}"
            )
        self._regions = None
        self._instruction_cache = {}
        self._native_cache = None

    @staticmethod
    def _new_capstone(*, detail: bool = True):
        decoder = capstone.Cs(capstone.CS_ARCH_ARM,
                              capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
        decoder.detail = detail
        decoder.skipdata = True
        return decoder

    @staticmethod
    def _facts(instruction) -> DecodedInstruction:
        mnemonic = instruction.mnemonic.lower()
        operands = instruction.op_str.lower()
        if instruction.id == 0:  # Capstone skip-data pseudo instruction.
            return DecodedInstruction(int(instruction.address) & ~1, int(instruction.size),
                                      mnemonic, instruction.op_str, False, False, False, None)
        has_mem = any(operand.type == ARM_OP_MEM for operand in instruction.operands)
        transfer = has_mem or mnemonic.startswith(("ldm", "stm", "push", "pop", "vld", "vst"))
        returned = ((mnemonic == "bx" and "lr" in operands)
                    or (mnemonic.startswith("pop") and "pc" in operands)
                    or (mnemonic.startswith("ldm") and "pc" in operands))
        direct = None
        if mnemonic in {"b", "bl", "blx"} and instruction.operands \
                and instruction.operands[0].type == ARM_OP_IMM:
            direct = int(instruction.operands[0].imm) & ~1
        return DecodedInstruction(int(instruction.address) & ~1, int(instruction.size),
                                  mnemonic, instruction.op_str, has_mem, transfer,
                                  returned, direct)

    def executable_regions(self) -> tuple[tuple[int, bytes], ...]:
        if self._regions is None:
            self._regions = tuple(
                (int(section["sh_addr"]), bytes(section.data()))
                for section in self._elf.iter_sections()
                if int(section["sh_flags"]) & 0x4 and int(section["sh_size"])
            )
        return self._regions

    def instructions(self, address: int | None = None,
                     size: int | None = None) -> tuple[DecodedInstruction, ...]:
        key = (address, size)
        if key in self._instruction_cache:
            return self._instruction_cache[key]
        decoder = self._new_capstone()
        result = []
        for base, data in self.executable_regions():
            region_start, region_end = base, base + len(data)
            start = region_start if address is None else max(region_start, address)
            end = region_end if address is None or size is None else min(region_end, address + size)
            if start >= end:
                continue
            for instruction in decoder.disasm(data[start - base:end - base], start):
                result.append(self._facts(instruction))
        decoded = tuple(result)
        self._instruction_cache[key] = decoded
        return decoded

    def native_instructions(self):
        """Internal escape hatch for specialized consumers needing operands."""
        if self._native_cache is None:
            decoder = self._new_capstone()
            self._native_cache = tuple(
                instruction
                for base, data in self.executable_regions()
                for instruction in decoder.disasm(data, base)
                if instruction.id != 0
            )
        yield from self._native_cache

    def return_sites(self, start: int, size: int) -> tuple[int, ...]:
        return tuple(item.address for item in self.instructions(start, size) if item.return_site)

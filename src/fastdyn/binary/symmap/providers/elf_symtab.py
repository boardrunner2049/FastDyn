from __future__ import annotations

from typing import Iterable

from fastdyn.binary.symmap.core import SymbolInfo, SymbolProvider
from fastdyn.host import AnalysisSession


class ElfSymtabProvider(SymbolProvider):
    @property
    def name(self) -> str:
        return "elf_symtab"

    def is_applicable(self, binary_path: str) -> bool:
        try:
            elf = AnalysisSession.open(
                binary_path, architecture="arm", instruction_mode="thumb-mclass"
            ).elf
            return any(section.header.sh_type in {"SHT_SYMTAB", "SHT_DYNSYM"}
                       for section in elf.iter_sections())
        except Exception:
            return False

    def get_symbols(self, binary_path: str) -> Iterable[SymbolInfo]:
        session = AnalysisSession.open(
            binary_path, architecture="arm", instruction_mode="thumb-mclass"
        )
        for symbol in session.symbols.all():
            yield SymbolInfo(
                name=symbol.name, address=symbol.code_address, size=symbol.size,
                kind="variable" if symbol.kind == "object" else symbol.kind,
                provider=self.name, confidence=0.9,
            )

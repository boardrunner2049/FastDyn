from __future__ import annotations
from typing import Iterable, List, Optional

from elftools.elf.elffile import ELFFile
from elftools.dwarf.descriptions import describe_form_class

from fastdyn.binary.symmap.core import SymbolInfo, SymbolProvider
from fastdyn.host import AnalysisSession


class DwarfProvider(SymbolProvider):
    """
    Symbol provider backed by DWARF debug information.

    Extracts:
      - Functions (DW_TAG_subprogram)
      - Simple global/static variables (DW_TAG_variable with DW_OP_addr)
      - Function epilogues (Return instructions appended as <name>_epi)
    """

    def __init__(self, include_variables: bool = True,
                 session: AnalysisSession | None = None) -> None:
        self._include_variables = include_variables
        self._session = session

    # -------------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "dwarf"

    def is_applicable(self, binary_path: str) -> bool:
        try:
            if self._session is not None:
                return self._session.elf.has_dwarf_info()
            with open(binary_path, "rb") as f:
                return ELFFile(f).has_dwarf_info()
        except Exception:
            return False

    def get_symbols(self, binary_path: str) -> Iterable[SymbolInfo]:
        session = self._session or AnalysisSession.open(
            binary_path, architecture="arm", instruction_mode="thumb-mclass"
        )
        elf = session.elf
        if not elf.has_dwarf_info():
            return []
        queries = session.dwarf
        symbols: List[SymbolInfo] = []

            # 1. Standard DWARF Extraction Pass
        for die in queries.dies():
            if die.tag == "DW_TAG_subprogram":
                symbol = self._from_subprogram(die)
            elif self._include_variables and die.tag == "DW_TAG_variable":
                symbol = self._from_variable(die, queries)
            else:
                symbol = None
            if symbol is not None:
                symbols.append(symbol)

            # 2. Epilogue Scanning Pass
            #TODO: This won't capture context switch routines as they have weird ways to return, right now not needed, but in future if need rises something to note
        epilogue_symbols: List[SymbolInfo] = []

        for sym in symbols:
            if sym.kind == "function" and sym.address is not None and sym.size:
                return_addresses = list(session.decoder.return_sites(sym.address & ~1, sym.size))
                if return_addresses:
                    epilogue_symbols.append(SymbolInfo(
                        name=f"{sym.name}_epi", address=return_addresses, size=0,
                        kind="epilogue", provider=self.name, confidence=sym.confidence,
                    ))

            
            # Append the newly discovered epilogues to the master symbol list
        symbols.extend(epilogue_symbols)
        return symbols

    def _die_name(self, die) -> Optional[str]:
        attr = die.attributes.get("DW_AT_name")
        if not attr:
            return None
        val = attr.value
        return val.decode(errors="replace") if isinstance(val, bytes) else str(val)

    def _from_subprogram(self, die) -> Optional[SymbolInfo]:
        name = self._die_name(die)
        if not name:
            return None

        low_pc = die.attributes.get("DW_AT_low_pc")
        if not low_pc:
            return None

        addr = int(low_pc.value)
        size = None

        high_pc = die.attributes.get("DW_AT_high_pc")
        if high_pc:
            cls = describe_form_class(high_pc.form)
            val = int(high_pc.value)
            size = val - addr if cls == "address" else val

        return SymbolInfo(
            name=name,
            address=addr,
            size=size,
            kind="function",
            provider=self.name,
            confidence=1.0,
        )

    def _from_variable(self, die, queries=None) -> Optional[SymbolInfo]:
        name = self._die_name(die)
        if not name:
            return None

        loc = die.attributes.get("DW_AT_location")
        if not loc or not isinstance(loc.value, (list, bytes, bytearray)):
            return None

        if queries is None:
            return None
        addr = queries.fixed_address(loc)
        if addr is None:
            return None

        return SymbolInfo(
            name=name,
            address=addr,
            size=None,
            kind="variable",
            provider=self.name,
            confidence=0.95,
        )

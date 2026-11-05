"""Canonical ELF symbol and function queries."""
from __future__ import annotations

from dataclasses import dataclass, replace

from elftools.elf.sections import SymbolTableSection

from .errors import AmbiguousSymbolError, SymbolNotFoundError


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    address: int
    code_address: int
    size: int | None
    aliases: tuple[str, ...]
    provider: str
    section: str | None


@dataclass(frozen=True)
class FunctionRange:
    name: str
    start: int
    end: int
    aliases: tuple[str, ...]
    provider: str
    range_provenance: str  # explicit | inferred
    section: str | None

    @property
    def size(self) -> int:
        return self.end - self.start


class SymbolIndex:
    def __init__(self, elf, *, normalize_thumb_functions: bool):
        symbols: list[Symbol] = []
        seen: set[tuple] = set()
        section_bounds: dict[int, tuple[int, int, str, bool]] = {}
        for index, section in enumerate(elf.iter_sections()):
            start = int(section["sh_addr"])
            section_bounds[index] = (
                start, start + int(section["sh_size"]), section.name,
                bool(int(section["sh_flags"]) & 0x4),
            )
        for table in elf.iter_sections():
            if not isinstance(table, SymbolTableSection):
                continue
            for entry in table.iter_symbols():
                if not entry.name or entry.entry.st_shndx == "SHN_UNDEF":
                    continue
                raw_kind = entry.entry.st_info.type
                kind = {"STT_FUNC": "function", "STT_OBJECT": "object",
                        "STT_NOTYPE": "label"}.get(raw_kind, "unknown")
                raw = int(entry.entry.st_value)
                code = raw & ~1 if normalize_thumb_functions and kind == "function" else raw
                shndx = entry.entry.st_shndx
                section_name = section_bounds.get(shndx, (0, 0, None, False))[2] \
                    if isinstance(shndx, int) else None
                key = (entry.name, raw, code, int(entry.entry.st_size), kind, section_name)
                if key in seen:
                    continue
                seen.add(key)
                symbols.append(Symbol(entry.name, kind, raw, code,
                                      int(entry.entry.st_size) or None, (),
                                      "elf-symbol-table", section_name))
        alias_map: dict[tuple[int, str], set[str]] = {}
        for symbol in symbols:
            alias_map.setdefault((symbol.code_address, symbol.kind), set()).add(symbol.name)
        self._symbols = tuple(replace(symbol, aliases=tuple(sorted(
            alias_map[(symbol.code_address, symbol.kind)] - {symbol.name}
        ))) for symbol in symbols)
        self._by_name: dict[str, list[Symbol]] = {}
        for symbol in self._symbols:
            self._by_name.setdefault(symbol.name, []).append(symbol)
        self._ranges = self._build_ranges(section_bounds)

    def all(self, *, kind: str | None = None) -> tuple[Symbol, ...]:
        return tuple(symbol for symbol in self._symbols if kind is None or symbol.kind == kind)

    def find(self, name: str, *, kind: str | None = None) -> tuple[Symbol, ...]:
        return tuple(symbol for symbol in self._by_name.get(name, ())
                     if kind is None or symbol.kind == kind)

    def lookup(self, name: str, *, kind: str | None = None,
               address: int | None = None) -> Symbol:
        matches = self.find(name, kind=kind)
        if address is not None:
            matches = tuple(item for item in matches
                            if item.address == address or item.code_address == address)
        distinct = {(item.address, item.code_address, item.kind) for item in matches}
        if not matches:
            raise SymbolNotFoundError(f"undefined or unknown firmware symbol: {name!r}")
        if len(distinct) != 1:
            choices = sorted({item.code_address for item in matches})
            raise AmbiguousSymbolError(f"{name}: expected one address, found {choices}")
        return matches[0]

    def _build_ranges(self, bounds) -> tuple[FunctionRange, ...]:
        grouped: dict[str | None, dict[int, list[Symbol]]] = {}
        for symbol in self._symbols:
            if symbol.kind == "function":
                grouped.setdefault(symbol.section, {}).setdefault(symbol.code_address, []).append(symbol)
        section_by_name = {value[2]: value for value in bounds.values()}
        ranges: list[FunctionRange] = []
        for section_name, starts_map in grouped.items():
            starts = sorted(starts_map)
            section = section_by_name.get(section_name)
            if section is None or not section[3]:
                continue
            section_end = section[1]
            for index, start in enumerate(starts):
                entries = starts_map[start]
                explicit = max((entry.size or 0 for entry in entries), default=0)
                if explicit:
                    end = start + explicit
                    if section_end is not None:
                        end = min(end, section_end)
                    provenance = "explicit"
                else:
                    next_start = starts[index + 1] if index + 1 < len(starts) else section_end
                    if next_start is None or next_start <= start:
                        continue
                    end, provenance = next_start, "inferred"
                if end <= start:
                    continue
                names = sorted({entry.name for entry in entries})
                ranges.append(FunctionRange(names[0], start, end, tuple(names[1:]),
                                            "elf-symbol-table", provenance, section_name))
        return tuple(sorted(ranges, key=lambda item: (item.start, item.end, item.name)))

    def function_ranges(self) -> tuple[FunctionRange, ...]:
        return self._ranges

    def containing_function(self, address: int) -> FunctionRange | None:
        return next((item for item in self._ranges if item.start <= address < item.end), None)

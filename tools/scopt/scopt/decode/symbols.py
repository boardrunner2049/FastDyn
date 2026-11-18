"""Host address symbolization: MAPS parser and ELF symbol tables (design §2.7 step 6). WP2.

* :class:`Maps` holds ``/proc/<pid>/maps``-style entries. It parses the text
  format (:meth:`Maps.parse`) and can also be built from entries (the ``.scm``
  MAPS chunk, WP4) or synthesized from ELF modules at known load biases
  (:meth:`Maps.from_modules`, used by the meta-less classifier, which has no
  MAPS of the B0 run).
* :class:`ElfImage` reads an ELF's PT_LOAD segments and its ``.symtab``
  (falling back to ``.dynsym``) with pyelftools.
* :class:`Symbolizer` maps an absolute host address to
  ``(module, symbol, off)``.

A module's *load bias* is what is added to its ELF virtual addresses: the
PIE base for ``qemu-system-arm`` (0x555555554000 under ``setarch -R``) or a
shared library's load base.
"""

from __future__ import annotations

import bisect
import os
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import NamedTuple

__all__ = [
    "MapEntry", "Maps", "ElfSymbol", "ElfSegment", "ElfImage", "Module", "Symbol",
    "Symbolizer", "load_elf", "PAGE_SIZE", "SETARCH_R_PIE_BASE",
]

PAGE_SIZE = 4096
#: Load bias of a PIE executable on x86-64 Linux with ASLR off (``setarch -R``):
#: ``ELF_ET_DYN_BASE`` = 2/3 of the 47-bit task size, page-aligned.
SETARCH_R_PIE_BASE = 0x555555554000


# --------------------------------------------------------------------------
# MAPS
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class MapEntry:
    """One mapping ``[start, end)``; ``path`` is '' for anonymous mappings."""

    start: int
    end: int
    perms: str = "r--p"
    offset: int = 0
    dev: str = "00:00"
    inode: int = 0
    path: str = ""

    def contains(self, addr: int) -> bool:
        return self.start <= addr < self.end

    @property
    def executable(self) -> bool:
        return "x" in self.perms

    def to_line(self) -> str:
        return (f"{self.start:x}-{self.end:x} {self.perms} {self.offset:08x} {self.dev} "
                f"{self.inode} {self.path}").rstrip()


class Maps:
    """An ordered, non-overlapping set of :class:`MapEntry` with address lookup."""

    def __init__(self, entries: Iterable[MapEntry] = ()) -> None:
        self.entries: tuple[MapEntry, ...] = tuple(sorted(entries, key=lambda e: e.start))
        self._starts = [e.start for e in self.entries]

    # -- construction ---------------------------------------------------
    @classmethod
    def parse(cls, text: str) -> "Maps":
        """Parse ``/proc/<pid>/maps`` text. Blank and malformed lines are rejected."""
        out = []
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            parts = line.split(maxsplit=5)
            if len(parts) < 5:
                raise ValueError(f"maps line {n}: {line!r}")
            lo, _, hi = parts[0].partition("-")
            out.append(MapEntry(int(lo, 16), int(hi, 16), parts[1], int(parts[2], 16), parts[3],
                                int(parts[4]), parts[5].strip() if len(parts) > 5 else ""))
        return cls(out)

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Maps":
        with open(path, encoding="utf-8", errors="replace") as f:
            return cls.parse(f.read())

    @classmethod
    def from_modules(cls, modules: Iterable["Module"]) -> "Maps":
        """Synthesize mappings from each module's PT_LOAD segments at its bias."""
        out: list[MapEntry] = []
        for m in modules:
            if m.image is None:
                continue
            for seg in m.image.segments:
                lo = (m.base + seg.vaddr) & ~(PAGE_SIZE - 1)
                hi = (m.base + seg.vaddr + seg.memsz + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1)
                out.append(MapEntry(lo, hi, seg.perms, seg.offset & ~(PAGE_SIZE - 1), "00:00", 0,
                                    m.path))
        return cls(out)

    # -- queries ----------------------------------------------------------
    def find(self, addr: int) -> MapEntry | None:
        i = bisect.bisect_right(self._starts, addr) - 1
        if i >= 0 and self.entries[i].contains(addr):
            return self.entries[i]
        return None

    def contains(self, addr: int) -> bool:
        return self.find(addr) is not None

    def __iter__(self) -> Iterator[MapEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def paths(self) -> list[str]:
        return list(dict.fromkeys(e.path for e in self.entries if e.path.startswith("/")))

    def load_bias(self, path: str, image: "ElfImage | None" = None) -> int | None:
        """Load bias of the file ``path``: the mapping of file offset 0, minus the
        page-aligned vaddr of the PT_LOAD at file offset 0 (0 for PIE/DSOs)."""
        for e in self.entries:
            if e.path == path and e.offset == 0:
                first = 0
                if image is not None and image.segments:
                    first = min(s.vaddr for s in image.segments if s.offset == 0) \
                        if any(s.offset == 0 for s in image.segments) else 0
                return e.start - (first & ~(PAGE_SIZE - 1))
        return None


# --------------------------------------------------------------------------
# ELF
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ElfSymbol:
    value: int
    size: int
    name: str
    type: str  # STT_FUNC, STT_OBJECT, ...


@dataclass(frozen=True, slots=True)
class ElfSegment:
    vaddr: int
    memsz: int
    offset: int
    filesz: int
    flags: int

    @property
    def perms(self) -> str:
        return (("r" if self.flags & 4 else "-") + ("w" if self.flags & 2 else "-")
                + ("x" if self.flags & 1 else "-") + "p")


@dataclass(frozen=True)
class ElfImage:
    """PT_LOAD segments and (FUNC/OBJECT/NOTYPE) symbols of one ELF file."""

    path: str
    segments: tuple[ElfSegment, ...]
    symbols: tuple[ElfSymbol, ...]  # sorted by value, only defined, non-zero
    is_pie: bool
    _values: list[int] = field(default_factory=list, repr=False, compare=False)
    _by_name: dict[str, ElfSymbol] = field(default_factory=dict, repr=False, compare=False)
    _max_end: list[int] = field(default_factory=list, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._values[:] = [s.value for s in self.symbols]
        hi = 0
        for s in self.symbols:  # running max of symbol ends, for lookup()'s early stop
            hi = max(hi, s.value + max(s.size, 1))
            self._max_end.append(hi)
        for s in self.symbols:
            # prefer FUNC/OBJECT over local aliases with the same name
            self._by_name.setdefault(s.name, s)

    @property
    def image_end(self) -> int:
        return max((s.vaddr + s.memsz for s in self.segments), default=0)

    def segment_for(self, vaddr: int) -> ElfSegment | None:
        for s in self.segments:
            if s.vaddr <= vaddr < s.vaddr + s.memsz:
                return s
        return None

    def symbol(self, name: str) -> ElfSymbol | None:
        return self._by_name.get(name)

    def lookup(self, vaddr: int) -> tuple[str, int] | None:
        """``(name, off)`` of the symbol containing ``vaddr`` (exact start for size-0 symbols).

        Among symbols covering ``vaddr``, the one starting closest wins; FUNC
        and OBJECT are preferred over NOTYPE.
        """
        i = bisect.bisect_right(self._values, vaddr) - 1
        best: ElfSymbol | None = None
        # walk back while some earlier symbol could still cover vaddr
        while i >= 0 and self._max_end[i] > vaddr:
            s = self.symbols[i]
            if best is not None and s.value < best.value:
                break
            if s.value + max(s.size, 1) > vaddr and (best is None or _rank(s) < _rank(best)):
                best = s
            i -= 1
        if best is None:
            return None
        return best.name, vaddr - best.value

    def is_func_start(self, vaddr: int) -> bool:
        i = bisect.bisect_left(self._values, vaddr)
        while i < len(self.symbols) and self.symbols[i].value == vaddr:
            if self.symbols[i].type == "STT_FUNC":
                return True
            i += 1
        return False

    def funcs(self) -> list[ElfSymbol]:
        return [s for s in self.symbols if s.type == "STT_FUNC"]


def _rank(s: ElfSymbol) -> int:
    return {"STT_FUNC": 0, "STT_OBJECT": 1, "STT_TLS": 3}.get(s.type, 2)


@lru_cache(maxsize=16)
def _load_elf_cached(path: str, mtime_ns: int, size: int) -> ElfImage:
    from elftools.elf.elffile import ELFFile
    from elftools.elf.sections import SymbolTableSection

    with open(path, "rb") as f:
        elf = ELFFile(f)
        segs = tuple(ElfSegment(seg["p_vaddr"], seg["p_memsz"], seg["p_offset"], seg["p_filesz"],
                                seg["p_flags"])
                     for seg in elf.iter_segments() if seg["p_type"] == "PT_LOAD")
        tab = elf.get_section_by_name(".symtab")
        if not isinstance(tab, SymbolTableSection):
            tab = elf.get_section_by_name(".dynsym")
        syms: dict[tuple[int, str], ElfSymbol] = {}
        if isinstance(tab, SymbolTableSection):
            for s in tab.iter_symbols():
                typ = s["st_info"]["type"]
                if (not s.name or s["st_shndx"] == "SHN_UNDEF" or s["st_value"] == 0
                        or typ in ("STT_SECTION", "STT_FILE")):
                    continue
                syms.setdefault((s["st_value"], s.name),
                                ElfSymbol(s["st_value"], s["st_size"], s.name, typ))
        is_pie = elf.header["e_type"] == "ET_DYN"
    ordered = tuple(sorted(syms.values(), key=lambda s: (s.value, _rank(s), s.name)))
    return ElfImage(path, segs, ordered, is_pie)


def load_elf(path: str | os.PathLike) -> ElfImage:
    """Load (and cache, keyed by path + mtime + size) an ELF's segments and symbols."""
    p = os.path.realpath(os.fspath(path))
    st = os.stat(p)
    return _load_elf_cached(p, st.st_mtime_ns, st.st_size)


# --------------------------------------------------------------------------
# Symbolizer
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Module:
    """A loaded ELF at load bias ``base`` (vaddr + base = host address)."""

    name: str
    path: str
    base: int
    image: ElfImage | None = None

    @classmethod
    def from_file(cls, path: str | os.PathLike, base: int, name: str | None = None) -> "Module":
        img = load_elf(path)
        return cls(name or os.path.basename(os.fspath(path)), img.path, base, img)

    @property
    def start(self) -> int:
        if self.image is None or not self.image.segments:
            return self.base
        return self.base + min(s.vaddr for s in self.image.segments)

    @property
    def end(self) -> int:
        return self.base + (self.image.image_end if self.image else 0)

    def contains(self, addr: int) -> bool:
        if self.image is None:
            return False
        return self.image.segment_for(addr - self.base) is not None

    def address_of(self, symbol: str) -> int | None:
        if self.image is None:
            return None
        s = self.image.symbol(symbol)
        return None if s is None else self.base + s.value


class Symbol(NamedTuple):
    """Result of :meth:`Symbolizer.symbolize`.

    ``symbol`` is None when the address is inside a module or mapping but not
    inside any symbol; ``off`` is then relative to the module base (load
    bias) or to the start of the anonymous mapping.
    """

    module: str
    symbol: str | None
    off: int

    def __str__(self) -> str:
        if self.symbol is None:
            return f"{self.module}+0x{self.off:x}"
        return f"{self.module}!{self.symbol}" + (f"+0x{self.off:x}" if self.off else "")


class Symbolizer:
    """``symbolize(addr) -> (module, symbol, off)`` over ELF modules and MAPS entries."""

    def __init__(self, modules: Sequence[Module] = (), maps: Maps | None = None) -> None:
        self.modules = tuple(modules)
        self.maps = maps if maps is not None else Maps.from_modules(self.modules)

    @classmethod
    def from_maps(cls, maps: Maps, *, names: dict[str, str] | None = None,
                  load_symbols: bool = True) -> "Symbolizer":
        """Build modules from every file-backed mapping that is a readable ELF."""
        mods = []
        for path in maps.paths():
            img = None
            if load_symbols and os.path.isfile(path):
                try:
                    img = load_elf(path)
                except Exception:  # not an ELF (e.g. a RAM backing file)
                    img = None
            if img is None:
                continue
            bias = maps.load_bias(path, img)
            if bias is None:
                continue
            mods.append(Module((names or {}).get(path, os.path.basename(path)), img.path, bias,
                               img))
        return cls(mods, maps)

    def module_for(self, addr: int) -> Module | None:
        for m in self.modules:
            if m.contains(addr):
                return m
        return None

    def contains(self, addr: int) -> bool:
        return self.module_for(addr) is not None or self.maps.contains(addr)

    def symbolize(self, addr: int) -> Symbol | None:
        m = self.module_for(addr)
        if m is not None:
            hit = m.image.lookup(addr - m.base) if m.image else None
            if hit is not None:
                return Symbol(m.name, hit[0], hit[1])
            return Symbol(m.name, None, addr - m.base)
        e = self.maps.find(addr)
        if e is not None:
            name = os.path.basename(e.path) if e.path else "[anon]"
            return Symbol(name, None, addr - e.start)
        return None

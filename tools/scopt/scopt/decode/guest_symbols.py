"""Guest firmware ELF reader: bytes, ``STT_FUNC`` symbols and ARM mapping symbols (B3/B4 §3.3). WP20.

scopt resolved only host symbols before B3 (``scopt.decode.symbols``). The
CalleeSummary (design §3.3, m-SP14) names the guest function that contains a
return instruction, and the return-edge analyses decode a unit's guest bytes
(Thumb) to classify its exit. Both come from the firmware ELF, which is an
**optional** input: a stripped image gives ``callee = null``, a missing image
gives no guest decode (every decode-dependent question fails closed).

* :class:`GuestImage` holds the ``PT_LOAD`` file bytes, the ``STT_FUNC``
  symbols (Thumb bit cleared, ``[start, start + size)``) and the ARM ELF
  mapping symbols ``$t`` / ``$a`` / ``$d`` (code vs literal data), plus the
  sha256 of the file (compared with the bundle's ``image_hash``).
* :func:`unit_guest_bytes` returns the guest bytes of a TB unit only when
  their FNV-1a 64 equals the record's ``guest_hash`` (the same check the
  importer uses for ``elf_verified``), so a decode never reads bytes that are
  not the ones QEMU translated.
"""

from __future__ import annotations

import bisect
import hashlib
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from ..canon import fnv1a64

__all__ = ["GuestFunc", "GuestImage", "load_guest_image", "unit_guest_bytes",
           "unit_vaddr", "GuestImageError"]


class GuestImageError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GuestFunc:
    name: str
    start: int  # Thumb bit cleared
    end: int  # exclusive
    source: str = "elf_symtab"

    @property
    def size(self) -> int:
        return self.end - self.start

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "start": f"0x{self.start:x}", "end": f"0x{self.end:x}",
                "source": self.source}


@dataclass(frozen=True)
class GuestImage:
    path: str
    sha256: str  # "sha256:<hex>" of the file bytes
    segments: tuple[tuple[int, bytes], ...]  # (vaddr, file bytes) of PT_LOAD with filesz
    exec_ranges: tuple[tuple[int, int], ...]  # [start, end) of executable PT_LOADs
    funcs: tuple[GuestFunc, ...]  # sorted by start
    mapping: tuple[tuple[int, str], ...]  # sorted (addr, "t"|"a"|"d")
    _starts: list[int] = field(default_factory=list, repr=False, compare=False)

    # Read-only allocated section or segment bytes. A writable initializer alone
    # is insufficient evidence for a value at a later registration.
    readonly_ranges: tuple[tuple[int, int], ...] = ()

    exception_entries: frozenset[int] = frozenset()
    thumb_entries: frozenset[int] = frozenset()
    text_sections: tuple[tuple[int, int], ...] = ()
    little_endian: bool = True
    # Only entries whose local function aliases agree on one ELF STT_FILE.
    local_function_sources: tuple[tuple[int, str], ...] = ()

    def __post_init__(self) -> None:
        self._starts[:] = [f.start for f in self.funcs]

    @property
    def stripped(self) -> bool:
        return not self.funcs

    def read(self, vaddr: int, n: int) -> bytes | None:
        for base, data in self.segments:
            if base <= vaddr and vaddr + n <= base + len(data):
                return data[vaddr - base:vaddr - base + n]
        return None

    def func_at(self, vaddr: int) -> GuestFunc | None:
        """The ``STT_FUNC`` whose ``[start, end)`` contains ``vaddr`` (closest start wins)."""
        i = bisect.bisect_right(self._starts, vaddr) - 1
        while i >= 0:
            f = self.funcs[i]
            if f.start <= vaddr < f.end:
                return f
            if vaddr - f.start > 1 << 20:  # no symbol covers this far
                break
            i -= 1
        return None

    def code_ranges(self) -> list[tuple[int, int]]:
        """``[start, end)`` ranges marked ``$t`` by the mapping symbols, clipped to the
        executable segments. Without mapping symbols: the executable segments."""
        out: list[tuple[int, int]] = []
        if not self.mapping:
            return list(self.exec_ranges)
        for k, (addr, kind) in enumerate(self.mapping):
            if kind != "t":
                continue
            end = self.mapping[k + 1][0] if k + 1 < len(self.mapping) else None
            for lo, hi in self.exec_ranges:
                if lo <= addr < hi:
                    out.append((addr, hi if end is None else min(end, hi)))
                    break
        return [(a, b) for a, b in out if b > a]


@lru_cache(maxsize=8)
def _load(path: str, mtime_ns: int, size: int) -> GuestImage:
    from elftools.elf.elffile import ELFFile
    from elftools.elf.sections import SymbolTableSection

    data = open(path, "rb").read()
    sha = "sha256:" + hashlib.sha256(data).hexdigest()
    import io

    elf = ELFFile(io.BytesIO(data))
    if elf.header["e_machine"] != "EM_ARM":
        raise GuestImageError(f"{path}: not an ARM ELF ({elf.header['e_machine']})")
    segs: list[tuple[int, bytes]] = []
    exe: list[tuple[int, int]] = []
    readonly = []
    for seg in elf.iter_segments():
        if seg["p_type"] != "PT_LOAD":
            continue
        if seg["p_filesz"]:
            segs.append((seg["p_vaddr"], seg.data()))
            if not seg["p_flags"] & 2:
                readonly.append((seg["p_vaddr"], seg["p_vaddr"] + seg["p_filesz"]))
        if seg["p_flags"] & 1:
            exe.append((seg["p_vaddr"], seg["p_vaddr"] + seg["p_memsz"]))
    funcs: dict[tuple[int, str], GuestFunc] = {}
    mapping: dict[int, str] = {}
    function_sources: dict[int, set[str | None]] = {}
    local_file = None
    tab = elf.get_section_by_name(".symtab")
    if isinstance(tab, SymbolTableSection):
        for s in tab.iter_symbols():
            if s["st_shndx"] == "SHN_UNDEF":
                continue
            nm = s.name or ""
            typ = s["st_info"]["type"]
            if typ == "STT_FILE":
                local_file = nm or None
            elif typ == "STT_FUNC":
                owner = local_file if s["st_info"]["bind"] == "STB_LOCAL" else None
                function_sources.setdefault(s["st_value"] & ~1, set()).add(owner)
            if nm in ("$t", "$a", "$d") or nm.startswith(("$t.", "$a.", "$d.")):
                mapping[s["st_value"]] = nm[1]
                continue
            if typ == "STT_FUNC" and nm and s["st_size"] > 0:
                st = s["st_value"] & ~1
                funcs.setdefault((st, nm), GuestFunc(nm, st, st + s["st_size"]))
    exception_entries = set()
    thumb_entries = {elf.header["e_entry"] & ~1} if elf.header["e_entry"] & 1 else set()
    text_sections = []
    for sec in elf.iter_sections():
        if sec["sh_flags"] & 6 == 6 and sec["sh_type"] == "SHT_PROGBITS":
            text_sections.append((sec["sh_addr"], sec["sh_addr"] + sec["sh_size"]))
        # Some linkers combine code and writable data in a single RWX segment.
        if sec["sh_flags"] & 2 and not sec["sh_flags"] & 1 and sec["sh_type"] == "SHT_PROGBITS":
            readonly.append((sec["sh_addr"], sec["sh_addr"] + sec["sh_size"]))
        if sec.name in (".isr_vector", ".vectors", ".vector_table"):
            table = sec.data()
            # The first words are initial SP and reset. Entries 2+ are exceptions/IRQs.
            for off in range(4, len(table) - 3, 4):
                entry = int.from_bytes(table[off:off + 4], "little" if elf.little_endian else "big")
                if entry & 1:
                    thumb_entries.add(entry & ~1)
                    if off >= 8:
                        exception_entries.add(entry & ~1)
    return GuestImage(path, sha, tuple(segs), tuple(sorted(exe)),
                      tuple(sorted(funcs.values(), key=lambda f: (f.start, -f.end, f.name))),
                      tuple(sorted(mapping.items())), readonly_ranges=tuple(readonly),
                      exception_entries=frozenset(exception_entries), thumb_entries=frozenset(thumb_entries),
                      text_sections=tuple(sorted(text_sections)), little_endian=elf.little_endian,
                      local_function_sources=tuple((pc, next(iter(owners))) for pc, owners in
                                                   sorted(function_sources.items())
                                                   if len(owners) == 1 and None not in owners))


def load_guest_image(path: str | os.PathLike) -> GuestImage:
    """Load (cached by path + mtime + size) a guest firmware ELF."""
    p = os.path.realpath(os.fspath(path))
    st = os.stat(p)
    return _load(p, st.st_mtime_ns, st.st_size)


def unit_vaddr(unit: Any) -> int | None:
    vs = getattr(unit, "vaddrs", ()) or ()
    return vs[0] if vs else None


def unit_guest_bytes(unit: Any, image: GuestImage | None) -> bytes | None:
    """The unit's guest bytes from ``image``, or None when unavailable or when their
    FNV-1a 64 differs from the record's ``guest_hash`` (RAM-resident, patched, other image)."""
    if image is None:
        return None
    v = unit_vaddr(unit)
    if v is None:
        return None
    b = image.read(v, unit.guest_size)
    if b is None or fnv1a64(b) != unit.guest_hash:
        return None
    return b


def funcs_in(image: GuestImage, lo: int, hi: int) -> Iterable[GuestFunc]:  # pragma: no cover
    return (f for f in image.funcs if f.start < hi and lo < f.end)

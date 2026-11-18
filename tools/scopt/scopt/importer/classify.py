"""Host address classification for the importer (design §2.7 step 6). WP7.

:class:`HostContext` describes the process a capture came from: the code
arena (``code_address``, ``code_size``), the prologue offsets from RUN_INFO,
the MAPS chunk, the RAMBLOCKS chunk and the HELPER table of that ``.scm``.
It answers two questions:

* :meth:`HostContext.code_target` — what an *executed* external target is:
  ``ext.helper`` (the QEMU executable), ``ext.plugin`` (the FastDyn plugin)
  or ``ext.lib`` (any other file-backed executable mapping). A target that
  is in no executable mapping is ``None`` (the importer pins).
* :meth:`HostContext.data_value` — what an absolute host *value* is (pool
  data, RIP-LEA targets, movabs/imm constants): ``abs.guest_ram`` (inside a
  RAMBLOCK), ``abs.heap``, ``abs.plugin_data``, ``abs.qemu_data``,
  ``abs.other``; an executable-mapping address is classified like a code
  target (``ext.*``), an arena address as ``arena.abs``, and a value that is
  in no mapping at all as ``const`` (a plain number such as a packed guest
  constant; it is not listed as a host pointer).

Every classified address also gets an **anchor**, the stable description
CacheRegion v1 uses instead of a raw pointer (§4.5): ``module+0xoff`` for
file mappings (offset from the module's load bias), ``heap+0xoff``,
``ramblock:<idstr>+0xoff`` (``ramblock:@0x<ram_offset>`` when the idstr is
empty), ``anon@0x<start>+0xoff``.

Module roles:

* **qemu**: the mappings whose inode is RUN_INFO ``exe_st_ino``;
* **plugin**: the mappings of ``plugin_path`` if given; otherwise those that
  contain the ``func`` of a HELPER with a NULL name (plugin callbacks have no
  TCGHelperInfo name, plugins/core.c), otherwise a ``libfastdyn*.so`` path;
* **lib**: every other file-backed mapping.

Symbols come from the ELF files at the MAPS paths (pyelftools,
:mod:`scopt.decode.symbols`). The QEMU executable's symbols are used only if
the file's inode, size and mtime still equal RUN_INFO's ``exe_st_*`` (a
rebuilt binary has different symbols); the other modules only if the file
still exists. Symbols are decoration (``module``/``symbol`` in bindings);
they never decide relocatability.
"""

from __future__ import annotations

import bisect
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from ..decode.symbols import ElfImage, MapEntry, Maps, load_elf
from ..formats.enums import MapsTag
from ..formats.scm_v1 import HelperInfo, RamblocksChunk, RunInfo, ScmFile

__all__ = ["HostContext", "AddrClass", "context_for", "helper_symbol_ok", "OWN_STRUCT_IDX"]

OWN_STRUCT_IDX = (0, 1, 3)  # TB_EXIT_IDX0, TB_EXIT_IDX1, TB_EXIT_REQUESTED


@dataclass(frozen=True, slots=True)
class AddrClass:
    """Classification of one absolute host address."""

    cls: str  # ext.helper | ext.plugin | ext.lib | abs.* | arena.abs | prologue.*
    anchor: str
    module: str | None = None
    symbol: str | None = None
    sym_off: int = 0

    def symbol_text(self) -> str | None:
        if self.symbol is None:
            return None
        return self.symbol + (f"+0x{self.sym_off:x}" if self.sym_off else "")


@dataclass(frozen=True, slots=True)
class _RamRange:
    start: int
    end: int
    name: str


@lru_cache(maxsize=32)
def _elf_or_none(path: str, mtime_ns: int, size: int) -> ElfImage | None:
    try:
        return load_elf(path)
    except Exception:  # not an ELF (RAM backing files, ...)
        return None


def _image(path: str, want: tuple[int, int, int] | None = None) -> ElfImage | None:
    """ELF image of ``path`` if it exists (and, with ``want``, has that ino/size/mtime)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if want is not None and (st.st_ino, st.st_size, st.st_mtime_ns) != want:
        return None
    return _elf_or_none(path, st.st_mtime_ns, st.st_size)


@dataclass
class HostContext:
    code_address: int
    code_size: int
    prologue_end: int
    tb_ret_off: int
    epilogue_off: int
    maps: Maps
    helpers: Mapping[int, HelperInfo] = field(default_factory=dict)
    ramblocks: tuple[_RamRange, ...] = ()
    exe_ino: int | None = None
    exe_stat: tuple[int, int, int] | None = None  # (ino, size, mtime_ns)
    plugin_path: str | None = None
    _roles: dict[str, str] = field(default_factory=dict, repr=False)
    _bias: dict[str, int] = field(default_factory=dict, repr=False)
    _images: dict[str, ElfImage | None] = field(default_factory=dict, repr=False)
    _ram_starts: list[int] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.ramblocks = tuple(sorted(self.ramblocks, key=lambda r: r.start))
        self._ram_starts = [r.start for r in self.ramblocks]
        paths = [e.path for e in self.maps if e.path.startswith("/")]
        qemu_paths = {e.path for e in self.maps
                      if self.exe_ino is not None and e.inode == self.exe_ino and e.path}
        plugin_paths: set[str] = set()
        if self.plugin_path:
            plugin_paths = {p for p in paths
                            if os.path.realpath(p) == os.path.realpath(self.plugin_path)}
        if not plugin_paths:
            for h in self.helpers.values():
                if not h.name:
                    e = self.maps.find(h.func)
                    if e is not None and e.path and e.path not in qemu_paths:
                        plugin_paths.add(e.path)
        if not plugin_paths:
            plugin_paths = {p for p in paths if os.path.basename(p).startswith("libfastdyn")}
        for p in dict.fromkeys(paths):
            self._roles[p] = ("qemu" if p in qemu_paths else
                              "plugin" if p in plugin_paths else "lib")
            b = self.maps.load_bias(p)
            if b is not None:
                self._bias[p] = b

    # -- construction ---------------------------------------------------------
    @classmethod
    def from_capture(cls, scm: ScmFile, *, plugin_path: str | None = None) -> "HostContext":
        ri: RunInfo = scm.run_info  # type: ignore[assignment]
        if ri is None:
            raise ValueError("capture has no RUN_INFO")
        m = scm.maps_by_tag(MapsTag.FIRST_COMMIT) or scm.maps_by_tag(MapsTag.EXIT)
        maps = Maps.parse(m.text) if m is not None else Maps()
        rams = _ram_ranges(scm.ramblocks)
        return cls(ri.code_address, ri.code_size, ri.prologue_end, ri.tb_ret_off,
                   ri.epilogue_off, maps, dict(scm.helpers), rams, ri.exe_st_ino,
                   (ri.exe_st_ino, ri.exe_st_size, ri.exe_st_mtim_ns), plugin_path)

    # -- queries ----------------------------------------------------------------
    @property
    def tb_ret_addr(self) -> int:
        return self.code_address + self.tb_ret_off

    @property
    def epilogue_addr(self) -> int:
        return self.code_address + self.epilogue_off

    def in_arena(self, a: int) -> bool:
        return self.code_address <= a < self.code_address + self.code_size

    def role(self, e: MapEntry) -> str:
        if e.path.startswith("/"):
            return self._roles.get(e.path, "lib")
        if e.path == "[heap]":
            return "heap"
        return "anon"

    def module_paths(self, role: str) -> list[str]:
        return sorted(p for p, r in self._roles.items() if r == role)

    def _ram(self, a: int) -> _RamRange | None:
        i = bisect.bisect_right(self._ram_starts, a) - 1
        if i >= 0 and self.ramblocks[i].start <= a < self.ramblocks[i].end:
            return self.ramblocks[i]
        return None

    def _symbols(self, path: str) -> ElfImage | None:
        if path not in self._images:
            img = _image(path)
            if img is not None and self._roles.get(path) == "qemu" and not self._qemu_ok(path, img):
                img = None
            self._images[path] = img
        return self._images[path]

    def _qemu_ok(self, path: str, img: ElfImage) -> bool:
        """Are ``img``'s symbols those of the captured QEMU process?

        True when the file's (ino, size, mtime) still equal RUN_INFO's
        ``exe_st_*``, or when every named HELPER's ``func`` is the start of the
        symbol ``helper_<name>`` (or ``<name>``) at the mapping's load bias, for
        at least one named helper (a rebuild with identical text passes; a
        different binary does not).
        """
        try:
            st = os.stat(path)
            if self.exe_stat is not None and \
                    (st.st_ino, st.st_size, st.st_mtime_ns) == self.exe_stat:
                return True
        except OSError:
            return False
        bias = self._bias.get(path)
        named = [h for h in self.helpers.values() if h.name and self.maps.find(h.func) is not None
                 and self.maps.find(h.func).path == path]
        if bias is None or not named:
            return False
        for h in named:
            hit = img.lookup(h.func - bias)
            if hit is None or hit[1] != 0 or hit[0] not in (h.name_str, f"helper_{h.name_str}"):
                return False
        return True

    def _anon_owner(self, v: int) -> tuple[str, ElfImage, int] | None:
        """``(path, image, bias)`` of the QEMU or plugin module whose PT_LOAD
        (``memsz``, i.e. including .bss) covers the anonymous address ``v``."""
        for path, role in sorted(self._roles.items()):
            if role not in ("qemu", "plugin") or path not in self._bias:
                continue
            img = self._symbols(path) or _image(path)
            if img is None:
                continue
            seg = img.segment_for(v - self._bias[path])
            if seg is not None:
                return path, img, self._bias[path]
        return None

    def _file_class(self, a: int, e: MapEntry, prefix: str) -> AddrClass:
        role = self.role(e)
        mod = os.path.basename(e.path)
        bias = self._bias.get(e.path, e.start - e.offset)
        sym = None
        off = 0
        img = self._symbols(e.path)
        if img is not None:
            hit = img.lookup(a - bias)
            if hit is not None:
                sym, off = hit
        name = {"qemu": "helper", "plugin": "plugin"}.get(role, "lib") if prefix == "ext" else \
            {"qemu": "qemu_data", "plugin": "plugin_data"}.get(role, "other")
        return AddrClass(f"{prefix}.{name}", f"{mod}+0x{a - bias:x}", mod, sym, off)

    def code_target(self, a: int) -> AddrClass | None:
        """Class of an executed target outside the arena (None: not executable/unmapped)."""
        e = self.maps.find(a)
        if e is None or not e.executable:
            return None
        if e.path.startswith("/"):
            return self._file_class(a, e, "ext")
        return AddrClass("ext.lib", f"{e.path or 'anon'}@0x{e.start:x}+0x{a - e.start:x}",
                         e.path or None)

    def data_value(self, v: int, *, tb_offset: int | None = None) -> AddrClass:
        """Binding class of an absolute host value."""
        if self.in_arena(v):
            off = v - self.code_address
            if off == self.tb_ret_off:
                return AddrClass("prologue.tb_ret", f"arena+0x{off:x}")
            if off == self.epilogue_off:
                return AddrClass("prologue.epilogue", f"arena+0x{off:x}")
            return AddrClass("arena.abs", f"arena+0x{off:x}")
        r = self._ram(v)
        if r is not None:
            return AddrClass("abs.guest_ram", f"ramblock:{r.name}+0x{v - r.start:x}")
        e = self.maps.find(v)
        if e is None:  # in no mapping at all: a plain constant, not a host pointer
            return AddrClass("const", f"0x{v:x}")
        if e.path.startswith("/"):
            return self._file_class(v, e, "ext" if e.executable else "abs")
        if e.path == "[heap]":
            return AddrClass("abs.heap", f"heap+0x{v - e.start:x}")
        own = self._anon_owner(v) if not e.path else None
        if own is not None:  # a module's .bss (anonymous part of its last PT_LOAD)
            path, img, bias = own
            role = self._roles[path]
            hit = img.lookup(v - bias)
            mod = os.path.basename(path)
            return AddrClass("abs.qemu_data" if role == "qemu" else "abs.plugin_data",
                             f"{mod}+0x{v - bias:x}", mod, hit[0] if hit else None,
                             hit[1] if hit else 0)
        tag = e.path or "anon"
        return AddrClass("abs.other", f"{tag}@0x{e.start:x}+0x{v - e.start:x}")

    def helper(self, hid: int) -> HelperInfo | None:
        return self.helpers.get(hid)

    def contains(self, a: int) -> bool:
        """For :func:`scopt.decode.x86.extract_refs`: the MAPS ranges."""
        return self.maps.contains(a)

    def describe(self) -> dict[str, Any]:
        return {
            "code_address": f"0x{self.code_address:x}",
            "tb_ret_off": self.tb_ret_off, "epilogue_off": self.epilogue_off,
            "modules": {r: [os.path.basename(p) for p in self.module_paths(r)]
                        for r in ("qemu", "plugin")},
            "maps_entries": len(self.maps),
            "ramblocks": [r.name for r in self.ramblocks],
        }


def _ram_ranges(rb: RamblocksChunk | None) -> tuple[_RamRange, ...]:
    if rb is None:
        return ()
    out = []
    for r in rb.rows:
        if not r.host or not r.used_length:
            continue
        name = r.name or f"@0x{r.ram_offset:x}"
        out.append(_RamRange(r.host, r.host + r.used_length, name))
    return tuple(out)


def context_for(scm: ScmFile, cache: dict[int, HostContext], *,
                plugin_path: str | None = None) -> HostContext:
    """One :class:`HostContext` per capture file (memoized by ``id(scm)``)."""
    k = id(scm)
    if k not in cache:
        cache[k] = HostContext.from_capture(scm, plugin_path=plugin_path)
    return cache[k]


def helper_symbol_ok(name: str | None, symbol: str | None) -> bool | None:
    """Does a HELPER name agree with the ELF symbol at its func? None if not comparable."""
    if not name or not symbol:
        return None
    return symbol in (name, f"helper_{name}")

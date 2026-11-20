"""Bind a captured host function to exact executable bytes."""

from __future__ import annotations

import io
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile

from ..canon import hexaddr, is_sha256_id, sha256_id
from ..decode.symbols import ElfImage, MapEntry, read_elf
from ..formats.scm_v1 import RunInfo
from ..model.native import NativeFrameBody
from ..model.unit import HelperBinding
from .classify import HostContext

_STAT_FIELDS = ("dev", "ino", "size", "mtime_ns")


@dataclass(frozen=True, slots=True)
class NativeCallee:
    image_id: str
    path: str
    symbol: str
    runtime_address: int
    elf_address: int
    image_offset: int
    size: int
    code_hash: str
    build_id: str | None
    role: str
    load_bias: int
    mapping: MapEntry
    isa: str
    abi: str
    qemu_cpuinfo: int
    tcg_typemask: int
    runtime_identity_id: str
    capture_id: str
    code: bytes = field(repr=False)
    frame_chain: ClassVar[tuple[NativeFrameBody, ...]] = ()

    @property
    def code_ranges(self) -> tuple[tuple[int, int], ...]:
        """ELF virtual address intervals. This version accepts one contiguous range."""
        return ((self.elf_address, self.elf_address + self.size),)


@dataclass(frozen=True, slots=True)
class _FramedNativeCallee(NativeCallee):
    # Only a discovered chain extends the prepared image's content identity.
    # Previous manifests and certificates keep their original digest.
    frame_chain: tuple[NativeFrameBody, ...] = ()


class CalleeResolutionError(ValueError):
    def __init__(self, reason: str, detail: str):
        self.reason = reason
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class _NativeImage:
    symbols: ElfImage
    bytes: bytes
    build_id: str | None
    digest: str


def _file_identity(st: os.stat_result) -> tuple[int, int, int, int]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


class NativeCalleeResolver:
    """Read native images once per preparation, matching the bound capture.

    The ABI and ISA identify the executable's function interface. Instruction
    eligibility, CPU features used by the function, and recovery are checked
    separately by the native optimizer.
    """

    def __init__(self, context: HostContext, run_info: RunInfo,
                 runtime: Mapping[str, Any], capture_id: str,
                 images: dict[str, _NativeImage] | None = None):
        self._context = context
        self._run_info = run_info
        self._runtime = runtime
        self._capture_id = capture_id
        self._images = {} if images is None else images
        self._frame_chains = {}

    def _frame_chain(self, image, mapping, bias, address, code):
        from ..validate_sym.frame_chain import discover_chain
        from ..validate_sym.native_virtual import legacy_frame_image

        if legacy_frame_image(image.digest):
            return ()
        key = (mapping.path, bias, address)
        if key in self._frame_chains:
            return self._frame_chains[key]
        maps = self._context.maps

        def text_bytes(va, size):
            seg = image.symbols.segment_for(va)
            m = maps.find(bias + va)
            if (seg is None or seg.flags & 3 != 1 or va + size > seg.vaddr + seg.filesz
                    or m is None or not m.executable or m.perms[1:2] == "w"
                    or (m.path, m.dev, m.inode) != (mapping.path, mapping.dev, mapping.inode)
                    or bias + va + size > m.end):
                return None
            off = seg.offset + va - seg.vaddr
            if (off != m.offset + bias + va - m.start or off + size > len(image.bytes)
                    or any(row.path == m.path and row.perms[1:2] == "w"
                           and max(row.offset, off) < min(row.offset + row.end - row.start, off + size)
                           for row in maps)):
                return None
            return image.bytes[off:off + size]

        def writable_object(va, size):
            seg = image.symbols.segment_for(va)
            m = maps.find(bias + va)
            return (seg is not None and seg.flags & 3 == 2 and va + size <= seg.vaddr + seg.memsz
                    and m is not None and m.perms.startswith("rw-") and bias + va + size <= m.end)

        chain = discover_chain(NativeFrameBody(address, code), image, text_bytes, writable_object)
        self._frame_chains[key] = chain
        return chain

    def _verify_binding(self, mapping: MapEntry) -> tuple[str, str, tuple[int, ...]]:
        ri = self._run_info
        if (self._runtime.get("runtime_signature") != hexaddr(ri.runtime_signature)
                or self._runtime.get("cpuinfo") != hexaddr(ri.cpuinfo)
                or not is_sha256_id(self._runtime.get("runtime_identity_id"))):
            raise CalleeResolutionError("native_image_mismatch",
                                        "runtime identity differs from capture RUN_INFO")
        for role, stat_key in (("qemu", "qemu_exe_stat"), ("plugin", "plugin_stat")):
            recorded = self._runtime.get(stat_key)
            if isinstance(recorded, Mapping) and recorded.get("path") == mapping.path:
                break
        else:
            raise CalleeResolutionError("native_image_unavailable",
                                        "executable mapping is absent from the runtime identity")
        digest = self._runtime.get(f"{role}_sha256")
        identity = tuple(recorded.get(k) for k in _STAT_FIELDS)
        if not is_sha256_id(digest) or any(type(v) is not int or v < 0 for v in identity):
            raise CalleeResolutionError("native_image_mismatch", "incomplete image identity")
        if role == "qemu" and identity != (ri.exe_st_dev, ri.exe_st_ino,
                                           ri.exe_st_size, ri.exe_st_mtim_ns):
            raise CalleeResolutionError("native_image_mismatch", "executable differs from RUN_INFO")
        try:
            dev = tuple(int(part, 16) for part in mapping.dev.split(":"))
            file_dev = os.major(identity[0]), os.minor(identity[0])
        except (OverflowError, ValueError) as exc:
            raise CalleeResolutionError("native_image_mismatch", "invalid image device") from exc
        if mapping.inode != identity[1] or dev != file_dev:
            raise CalleeResolutionError("native_image_mismatch", "image differs from capture MAPS")
        return role, digest, identity

    def _verified_image(self, mapping: MapEntry) -> tuple[str, _NativeImage]:
        role, digest, identity = self._verify_binding(mapping)
        image = self._images.get(mapping.path)
        if image is not None:
            return role, image
        try:
            fd = os.open(mapping.path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as f:
                st = os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode) or _file_identity(st) != identity:
                    raise CalleeResolutionError("native_image_mismatch", "image file was replaced")
                data = f.read()
                if (_file_identity(os.fstat(f.fileno())) != identity
                        or sha256_id(data) != digest):
                    raise CalleeResolutionError("native_image_mismatch", "image digest differs")
            stream = io.BytesIO(data)
            elf = ELFFile(stream)
            if (elf.elfclass != 64 or not elf.little_endian
                    or elf["e_machine"] != "EM_X86_64" or elf["e_type"] not in ("ET_DYN", "ET_EXEC")):
                raise CalleeResolutionError("unsupported_isa", "expected an x86-64 ELF executable")
            for seg in elf.iter_segments():
                if seg["p_type"] == "PT_DYNAMIC" and any(
                        tag.entry.d_tag == "DT_TEXTREL"
                        or (tag.entry.d_tag == "DT_FLAGS" and tag.entry.d_val & 4)
                        for tag in seg.iter_tags()):
                    raise CalleeResolutionError("unsupported_text_relocations",
                                                "image permits runtime text relocations")
            build_ids = {note["n_desc"] for seg in elf.iter_segments() if seg["p_type"] == "PT_NOTE"
                         for note in seg.iter_notes()
                         if note["n_type"] == "NT_GNU_BUILD_ID" and note["n_name"] == "GNU"}
            if len(build_ids) > 1:
                raise CalleeResolutionError("native_image_mismatch", "conflicting ELF build IDs")
            stream.seek(0)
            image = _NativeImage(read_elf(stream, mapping.path), data,
                                 next(iter(build_ids), None), digest)
        except OSError as exc:
            raise CalleeResolutionError("native_image_unavailable",
                                        f"image unavailable: {mapping.path}") from exc
        except ELFError as exc:
            raise CalleeResolutionError("native_image_unavailable",
                                        "image is not a readable ELF") from exc
        self._images[mapping.path] = image
        return role, image

    def resolve(self, binding: HelperBinding) -> NativeCallee:
        recorded = self._context.helpers.get(binding.id)
        address, symbol = binding.func, binding.symbol
        if (recorded is None or address is None or recorded.func != address
                or recorded.tcg_flags != binding.flags or recorded.name_str != binding.name
                or not symbol):
            raise CalleeResolutionError("call_binding_unknown", "missing or mismatched capture binding")
        maps = self._context.maps
        mapping = maps.find(address)
        if mapping is None or not mapping.executable or not mapping.path.startswith("/"):
            raise CalleeResolutionError("native_image_unavailable", "target lacks a file-backed text mapping")
        if mapping.perms[1:2] == "w":
            raise CalleeResolutionError("native_image_mismatch", "target mapping permits executable writes")
        if binding.module is not None and binding.module != os.path.basename(mapping.path):
            raise CalleeResolutionError("native_image_mismatch", "module differs from capture MAPS")
        role, image = self._verified_image(mapping)
        elf, data = image.symbols, image.bytes
        bias = maps.load_bias(mapping.path, elf)
        entry = None if bias is None else elf.bounded_function(symbol, address - bias)
        if entry is None:
            raise CalleeResolutionError("ambiguous_bounds", "target has no unique sized function range")
        segment = elf.segment_for(entry.value)
        if segment is None or segment.flags & 3 != 1:
            raise CalleeResolutionError("ambiguous_bounds", "function is outside executable file ranges")
        file_offset = segment.offset + entry.value - segment.vaddr
        if (file_offset != mapping.offset + address - mapping.start
                or address + entry.size > mapping.end
                or file_offset + entry.size > min(len(data), segment.offset + segment.filesz)):
            raise CalleeResolutionError("ambiguous_bounds", "function crosses its file-backed mapping")
        if any(m.path == mapping.path and m.perms[1:2] == "w"
               and max(m.offset, file_offset) < min(m.offset + m.end - m.start,
                                                   file_offset + entry.size)
               for m in maps):
            raise CalleeResolutionError("native_image_mismatch", "function has a writable file alias")
        code = data[file_offset:file_offset + entry.size]
        chain = self._frame_chain(image, mapping, bias, entry.value, code) if (
            role == "plugin" and recorded.tcg_flags == 0 and recorded.typemask == 400
        ) else ()
        callee_class = _FramedNativeCallee if chain else NativeCallee
        return callee_class(
            image.digest, mapping.path, symbol, address, entry.value, file_offset, entry.size,
            sha256_id(code), image.build_id, role, bias, mapping, "x86-64", "sysv-amd64",
            self._run_info.cpuinfo, recorded.typemask, self._runtime["runtime_identity_id"],
            self._capture_id, code, **({"frame_chain": chain} if chain else {}))
